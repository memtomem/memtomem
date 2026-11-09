"""Importers for Notion and Obsidian exports."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import yaml

from memtomem.chunking.markdown import _FRONT_MATTER_RE
from memtomem.context._abandon import abandon_sync_on_exit, sync_is_abandoned
from memtomem.context._atomic import atomic_write_text
from memtomem.privacy import enforce_write_guard
from memtomem.source_provenance import WriteTargetGuard, refuse_replace_target

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


# ── Safe ZIP extraction ──────────────────────────────────────────────────
#
# ``zipfile.ZipFile.extractall`` normalizes ``..`` / absolute member names on
# supported runtimes, but enforces no aggregate-size, member-count, or
# compression-ratio bound — so a few-KB crafted archive can expand to
# gigabytes and fill the disk (a decompression bomb). The import path is
# MCP-reachable via ``mem_import_notion``. These caps are generous enough for
# real Notion / Obsidian exports and only trip on pathological archives.
_ZIP_MAX_TOTAL_BYTES = 2 * 1024**3  # 2 GiB aggregate uncompressed
_ZIP_MAX_MEMBER_BYTES = 512 * 1024**2  # 512 MiB single member
_ZIP_MAX_ENTRIES = 100_000  # member count
_ZIP_MAX_RATIO = 200  # aggregate uncompressed:compressed
_ZIP_RATIO_FLOOR_BYTES = 10 * 1024**2  # only ratio-check archives above this


class UnsafeArchiveError(ValueError):
    """An archive exceeds the safe-extraction resource caps, or a member name
    would escape the extraction root."""


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    member_filter: Callable[[str], bool] | None = None,
) -> None:
    """Extract ``zip_path`` into ``dest_dir`` with traversal + resource caps.

    Unlike a bare ``ZipFile.extractall``, this validates archive metadata up
    front and rejects any member whose name resolves outside ``dest_dir`` —
    failing closed on suspicious archives rather than silently normalizing
    them.

    When ``member_filter`` is given, only members whose name satisfies it are
    extracted and counted toward the size / ratio caps; the rest are skipped
    and never written to disk. This lets a caller that only consumes (say)
    markdown ignore arbitrarily large attachments without either extracting
    them (the decompression-bomb vector) or rejecting the whole archive over
    them. Every member is still path-containment checked and counted toward
    the entry cap regardless of the filter.

    Raises:
        UnsafeArchiveError: a member escapes ``dest_dir`` or the extracted set
            exceeds a configured resource cap.
    """
    dest_root = dest_dir.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise UnsafeArchiveError(f"archive has {len(infos)} entries (cap {_ZIP_MAX_ENTRIES})")

        to_extract: list[zipfile.ZipInfo] = []
        total_uncompressed = 0
        total_compressed = 0
        for info in infos:
            # Containment: every member must land strictly under the root.
            # Absolute names and ``..`` chains resolve outside it; an empty or
            # ``.`` member name resolves to the root itself.
            target = (dest_root / info.filename).resolve()
            if target == dest_root:
                if info.is_dir():
                    continue
                raise UnsafeArchiveError(f"member {info.filename!r} has no valid path")
            if dest_root not in target.parents:
                raise UnsafeArchiveError(f"member {info.filename!r} escapes the extraction root")
            if info.is_dir():
                continue
            if member_filter is not None and not member_filter(info.filename):
                continue
            if info.file_size > _ZIP_MAX_MEMBER_BYTES:
                raise UnsafeArchiveError(
                    f"member {info.filename!r} is {info.file_size} bytes "
                    f"(cap {_ZIP_MAX_MEMBER_BYTES})"
                )
            total_uncompressed += info.file_size
            total_compressed += info.compress_size
            to_extract.append(info)

        if total_uncompressed > _ZIP_MAX_TOTAL_BYTES:
            raise UnsafeArchiveError(
                f"archive expands to {total_uncompressed} bytes (cap {_ZIP_MAX_TOTAL_BYTES})"
            )
        if (
            total_compressed > 0
            and total_uncompressed > _ZIP_RATIO_FLOOR_BYTES
            and total_uncompressed / total_compressed > _ZIP_MAX_RATIO
        ):
            ratio = total_uncompressed / total_compressed
            raise UnsafeArchiveError(
                f"archive compression ratio {ratio:.0f}:1 exceeds cap {_ZIP_MAX_RATIO}:1"
            )

        for info in to_extract:
            zf.extract(info, dest_root)


def _refused_target(
    target: Path,
    rel: Path,
    guard: WriteTargetGuard | None,
    excluded_paths: list[str] | None,
    symlink_paths: list[str] | None,
    read_only_paths: list[str] | None,
) -> bool:
    """Record and skip a target the importer must not overwrite (#2488).

    An excluded target would be written and then indexed to zero chunks, and a
    re-import over one already indexed would leave its old chunks searchable. A
    symlinked target is refused because the exclusion check and the atomic
    replace would judge different files (see ``refuse_replace_target``).
    A target under a read-only index root is refused because those bytes belong
    to another tool; it is reported separately rather than folded into
    ``excluded_paths`` because the remedy differs — an exclusion is removed by
    editing a pattern, a read-only root by choosing a different destination.
    """
    if guard is None:
        return False
    refusal = refuse_replace_target(target, guard)
    if refusal == "excluded" and excluded_paths is not None:
        excluded_paths.append(rel.as_posix())
    elif refusal == "symlink" and symlink_paths is not None:
        symlink_paths.append(rel.as_posix())
    elif refusal == "read_only" and read_only_paths is not None:
        read_only_paths.append(rel.as_posix())
    return refusal is not None


def _unsafe_source(source_root: Path, resolved_root: Path, path: Path, rel: Path) -> bool:
    """Whether a walked source file must not be read.

    A file that is a symlink, sits under a symlinked directory, or resolves
    outside the export root would import bytes from somewhere the caller did
    not point at. These are the *sources*; a symlinked *target* is
    ``_refused_target``'s, and the two are reported separately because the
    directory to fix differs.
    """
    if any((source_root / Path(*rel.parts[: i + 1])).is_symlink() for i in range(len(rel.parts))):
        return True
    try:
        path.resolve().relative_to(resolved_root)
    except (OSError, ValueError):
        return True
    return False


async def _run_off_loop(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Run an importer body on a worker thread, and never outlive it (#3124).

    The bodies read archives, walk directories and fsync files; on the loop
    they stall every other request the server is serving. A thread cannot be
    cancelled, so a cancel here signals the worker through
    :func:`abandon_sync_on_exit`, waits for it to stop at its next between-files
    checkpoint, and only then propagates. Returning while the worker still
    wrote would let a retried import race the abandoned one on the same files,
    and let the worker resolve paths after its caller's environment is gone.
    """
    with abandon_sync_on_exit() as abandoned:
        future = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # Set now, not in the context manager's ``finally``: that runs after
            # the drain below, by which time the worker has done everything.
            abandoned.set()
            while not future.done():
                try:
                    await asyncio.wait({future})
                except asyncio.CancelledError:
                    continue
            if not future.cancelled():
                future.exception()  # retrieved; the cancel is what propagates
            raise


def _abandoned(imported: list[Path], label: str) -> list[Path]:
    logger.info("%s import abandoned by its caller after %d file(s)", label, len(imported))
    return imported


def _frozen_guard(index_guard: WriteTargetGuard | None) -> WriteTargetGuard | None:
    """The engine's guard answers frozen on the loop thread, before the hand-off.

    The live engine predicates read whatever config is installed at each call,
    and a concurrent tool call installs new roots between the worker's reads.
    Any other guard is passed through as given.
    """
    from memtomem.indexing.engine import IndexEngine

    if isinstance(index_guard, IndexEngine):
        return index_guard.write_target_snapshot()
    return index_guard


async def import_notion(
    export_path: Path,
    output_dir: Path,
    *,
    force_unsafe: bool = False,
    scope: str = "user",
    blocked_paths: list[str] | None = None,
    index_guard: WriteTargetGuard | None = None,
    excluded_paths: list[str] | None = None,
    symlink_paths: list[str] | None = None,
    read_only_paths: list[str] | None = None,
    source_symlink_paths: list[str] | None = None,
    written_text: dict[Path, str] | None = None,
) -> list[Path]:
    """Import a Notion export on a worker thread; see :func:`_import_notion_sync`.

    ``index_guard`` is frozen here, on the loop, so every target is judged
    against the configuration installed when the import started.
    """
    return await _run_off_loop(
        _import_notion_sync,
        export_path,
        output_dir,
        force_unsafe=force_unsafe,
        scope=scope,
        blocked_paths=blocked_paths,
        index_guard=_frozen_guard(index_guard),
        excluded_paths=excluded_paths,
        symlink_paths=symlink_paths,
        read_only_paths=read_only_paths,
        source_symlink_paths=source_symlink_paths,
        written_text=written_text,
    )


async def import_obsidian(
    vault_path: Path,
    output_dir: Path,
    *,
    force_unsafe: bool = False,
    scope: str = "user",
    blocked_paths: list[str] | None = None,
    index_guard: WriteTargetGuard | None = None,
    excluded_paths: list[str] | None = None,
    symlink_paths: list[str] | None = None,
    read_only_paths: list[str] | None = None,
    source_symlink_paths: list[str] | None = None,
    written_text: dict[Path, str] | None = None,
) -> list[Path]:
    """Import an Obsidian vault on a worker thread; see :func:`_import_obsidian_sync`.

    ``index_guard`` is frozen here, on the loop, so every target is judged
    against the configuration installed when the import started.
    """
    return await _run_off_loop(
        _import_obsidian_sync,
        vault_path,
        output_dir,
        force_unsafe=force_unsafe,
        scope=scope,
        blocked_paths=blocked_paths,
        index_guard=_frozen_guard(index_guard),
        excluded_paths=excluded_paths,
        symlink_paths=symlink_paths,
        read_only_paths=read_only_paths,
        source_symlink_paths=source_symlink_paths,
        written_text=written_text,
    )


def _import_notion_sync(
    export_path: Path,
    output_dir: Path,
    *,
    force_unsafe: bool = False,
    scope: str = "user",
    blocked_paths: list[str] | None = None,
    index_guard: WriteTargetGuard | None = None,
    excluded_paths: list[str] | None = None,
    symlink_paths: list[str] | None = None,
    read_only_paths: list[str] | None = None,
    source_symlink_paths: list[str] | None = None,
    written_text: dict[Path, str] | None = None,
) -> list[Path]:
    """Import a Notion export (ZIP or directory) into markdown files.

    Notion exports come as a ZIP with markdown files + nested folders.
    File names contain UUIDs that we strip for cleaner names.

    Args:
        export_path: Path to Notion export ZIP or extracted directory.
        output_dir: Directory to write cleaned markdown files.
        index_guard: The index engine, asked whether each target may be
            written. When given, a target it excludes is skipped into
            ``excluded_paths``, a symlinked target into ``symlink_paths`` and
            one under a read-only index root into ``read_only_paths``, before
            anything is written for that file.
        source_symlink_paths: Collects source files that are not read
            because they are symlinks, sit under a symlinked directory or
            resolve outside the export root.

    Returns:
        List of imported file paths. A run abandoned by its async caller
        stops between files and returns the files written so far.
    """
    imported: list[Path] = []
    sources: list[tuple[Path, str, str]] = []
    if export_path.suffix == ".zip":
        # Validate resource/traversal limits, then read markdown members
        # directly. No temporary plaintext extraction tree is created.
        with zipfile.ZipFile(export_path, "r") as zf:
            all_infos = zf.infolist()
            if len(all_infos) > _ZIP_MAX_ENTRIES:
                raise UnsafeArchiveError(
                    f"archive has {len(all_infos)} entries (cap {_ZIP_MAX_ENTRIES})"
                )
            for info in all_infos:
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    raise UnsafeArchiveError(f"member {info.filename!r} escapes the archive root")
            infos = sorted(
                (i for i in all_infos if not i.is_dir() and i.filename.lower().endswith(".md")),
                key=lambda i: i.filename,
            )
            total = sum(info.file_size for info in infos)
            total_compressed = sum(info.compress_size for info in infos)
            if total > _ZIP_MAX_TOTAL_BYTES:
                raise UnsafeArchiveError("archive exceeds aggregate size cap")
            if (
                total_compressed > 0
                and total > _ZIP_RATIO_FLOOR_BYTES
                and total / total_compressed > _ZIP_MAX_RATIO
            ):
                raise UnsafeArchiveError("archive compression ratio exceeds cap")
            for info in infos:
                if sync_is_abandoned():
                    return _abandoned(imported, "Notion")
                if info.file_size > _ZIP_MAX_MEMBER_BYTES:
                    raise UnsafeArchiveError(f"member {info.filename!r} exceeds size cap")
                raw = zf.open(info).read(_ZIP_MAX_MEMBER_BYTES + 1)
                sources.append(
                    (
                        Path(info.filename),
                        Path(info.filename).name,
                        raw.decode("utf-8", errors="replace"),
                    )
                )
    else:
        export_root = export_path.resolve()
        for p in sorted(export_path.rglob("*.md")):
            if sync_is_abandoned():
                return _abandoned(imported, "Notion")
            rel = p.relative_to(export_path)
            if _unsafe_source(export_path, export_root, p, rel):
                if source_symlink_paths is not None:
                    source_symlink_paths.append(rel.as_posix())
                continue
            sources.append((rel, p.name, p.read_text(encoding="utf-8", errors="replace")))

    # A cancel that lands during the last read must not go on to map and write.
    if sync_is_abandoned():
        return _abandoned(imported, "Notion")

    # Pass 1: group by folded (rel.parent, clean_stem) and build the output
    # mapping; raises before anything is written if two pages would share a file.
    mapping = _build_notion_output_mapping(sources)

    # Pass 2: Write files under the mapped name
    for rel, original_name, content in sources:
        # Between files only: a write and the bookkeeping after it are one unit.
        if sync_is_abandoned():
            return _abandoned(imported, "Notion")
        canonical_parent = Path(os.path.normpath(rel.parent))
        output_name = mapping[(canonical_parent, original_name)]

        # Clean Notion-specific artifacts and fix links
        content = _clean_notion_markdown(content, rel_parent=canonical_parent, link_mapping=mapping)

        # Preserve directory structure
        parent_dir = output_dir / rel.parent
        target = parent_dir / output_name

        final = _with_import_header(content, "notion", original_name)
        guard = enforce_write_guard(
            final,
            surface="mcp_import_notion",
            force_unsafe=force_unsafe,
            scope=scope,
            audit_context={"filename": original_name},
        )
        if guard.decision.startswith("blocked"):
            if blocked_paths is not None:
                blocked_paths.append(rel.as_posix())
            continue
        if _refused_target(
            target, rel, index_guard, excluded_paths, symlink_paths, read_only_paths
        ):
            continue
        atomic_write_text(target, final, mode=0o600)
        imported.append(target)
        if written_text is not None:
            # The exact text the guard scanned and the file now holds, so an
            # indexer honouring ``force_unsafe`` can bind the valve to it.
            written_text[target] = final

    logger.info("Imported %d files from Notion export", len(imported))
    return imported


def _import_obsidian_sync(
    vault_path: Path,
    output_dir: Path,
    *,
    force_unsafe: bool = False,
    scope: str = "user",
    blocked_paths: list[str] | None = None,
    index_guard: WriteTargetGuard | None = None,
    excluded_paths: list[str] | None = None,
    symlink_paths: list[str] | None = None,
    read_only_paths: list[str] | None = None,
    source_symlink_paths: list[str] | None = None,
    written_text: dict[Path, str] | None = None,
) -> list[Path]:
    """Import an Obsidian vault into memtomem-compatible markdown.

    Converts Obsidian-specific syntax:
    - [[wikilinks]] → [wikilinks](wikilinks.md)
    - ![[embeds]] → [embeds](embeds.md)
    - Callouts (> [!note]) preserved as blockquotes
    - Tags (#tag) preserved

    Args:
        vault_path: Path to Obsidian vault root directory.
        output_dir: Directory to write converted files.
        index_guard: The index engine, asked whether each target may be
            written. When given, a target it excludes is skipped into
            ``excluded_paths``, a symlinked target into ``symlink_paths`` and
            one under a read-only index root into ``read_only_paths``, before
            anything is written for that file.
        source_symlink_paths: Collects source files that are not read
            because they are symlinks, sit under a symlinked directory or
            resolve outside the export root.

    Returns:
        List of imported file paths. A run abandoned by its async caller
        stops between files and returns the files written so far.
    """
    imported: list[Path] = []
    vault_root = vault_path.resolve()

    for md_file in sorted(vault_path.rglob("*.md")):
        # Between files only: a write and the bookkeeping after it are one unit.
        if sync_is_abandoned():
            return _abandoned(imported, "Obsidian")
        # Skip Obsidian config files
        rel = md_file.relative_to(vault_path)
        if str(rel).startswith(".obsidian"):
            continue
        if _unsafe_source(vault_path, vault_root, md_file, rel):
            if source_symlink_paths is not None:
                source_symlink_paths.append(rel.as_posix())
            continue

        content = md_file.read_text(encoding="utf-8", errors="replace")

        # Convert Obsidian syntax
        content = _convert_obsidian_syntax(content)

        # Before the guard and write, so a cancel during this file's read writes nothing.
        if sync_is_abandoned():
            return _abandoned(imported, "Obsidian")
        target = output_dir / rel
        final = _with_import_header(content, "obsidian", str(rel))
        guard = enforce_write_guard(
            final,
            surface="mcp_import_obsidian",
            force_unsafe=force_unsafe,
            scope=scope,
            audit_context={"filename": rel.as_posix()},
        )
        if guard.decision.startswith("blocked"):
            if blocked_paths is not None:
                blocked_paths.append(rel.as_posix())
            continue
        if _refused_target(
            target, rel, index_guard, excluded_paths, symlink_paths, read_only_paths
        ):
            continue
        atomic_write_text(target, final, mode=0o600)
        imported.append(target)
        if written_text is not None:
            # The exact text the guard scanned and the file now holds, so an
            # indexer honouring ``force_unsafe`` can bind the valve to it.
            written_text[target] = final

    logger.info("Imported %d files from Obsidian vault", len(imported))
    return imported


# ── Import provenance header ─────────────────────────────────────────────
#
# Each imported file records where it came from as ``imported_from`` and
# ``original_file`` frontmatter keys. The chunker reads only the frontmatter
# block at the start of a file, so a note that already has one must receive
# the keys *inside* that block; a second block in front of it would turn the
# note's own ``tags`` / ``valid_from`` / ``valid_to`` / ``redaction`` into
# body text (#2716).
#
# The note's block is never re-serialised: a YAML load/dump rewrites its
# quoting and comments and resolves escapes and aliases, which would turn a
# redaction declaration that ``indexing/redaction_exemption.py`` rejects as
# written into one it accepts. The keys are inserted as text and the result
# is checked with the readers' own rules. When the check fails, or the block
# cannot be read as a plain mapping, the note is written unchanged: nothing
# reads the provenance keys, while the note's frontmatter is what indexing
# uses.

_MERGE_TAG = "tag:yaml.org,2002:merge"
_STR_TAG = "tag:yaml.org,2002:str"


def _indexer_view(text: str) -> str:
    """*text* as ``Path.read_text`` will return it once written (universal newlines)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _provenance_lines(fields: dict[str, str]) -> str:
    """Serialise *fields* as one frontmatter line each.

    ``yaml.safe_dump`` quotes a value only when YAML needs it, so ordinary
    names stay plain. A value holding a line break would be dumped as a
    multi-line scalar whose continuation lines the chunker's line-based
    frontmatter scans read as keys of their own, so it is forced into
    double-quoted style, where every break is an escape on a single line.
    Not ``json.dumps``: it writes a character outside the BMP as a surrogate
    pair, which YAML reads back as two separate characters.
    """
    out = []
    for key, value in fields.items():
        if value.splitlines() == [value]:
            out.append(
                yaml.safe_dump(
                    {key: value}, sort_keys=False, allow_unicode=True, width=float("inf")
                )
            )
        else:
            quoted = yaml.safe_dump(value, default_style='"', width=float("inf"))
            out.append(f"{key}: {quoted}")
    return "".join(out)


def _top_level_entries(block: str) -> list[tuple[str, yaml.Node]] | None:
    """Top-level ``(key, value node)`` pairs of a frontmatter *block*, or
    ``None`` if it is not a plain mapping.

    Reads the composed node graph only. Constructing values (``safe_load``)
    runs tag constructors and expands ``<<`` merge chains, both of which can
    be made to fail or blow up by a hostile note, and the Notion import is
    reachable over MCP. Any failure to read the block means the merge cannot
    be checked, so it is treated as "not a plain mapping" whatever its class:
    PyYAML raises bare ``ValueError`` and others besides ``YAMLError`` here.

    A ``<<`` key is refused because it can supply keys this list would not show.
    """
    try:
        node = yaml.compose(block, Loader=yaml.SafeLoader)
    except Exception:
        return None
    if node is None:
        return []
    if not isinstance(node, yaml.MappingNode):
        return None
    entries = []
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or key_node.tag == _MERGE_TAG:
            return None
        entries.append((key_node.value, value_node))
    return entries


def _reads_back(block: str, fields: dict[str, str], note_keys: list[str]) -> bool:
    """Whether *block* is *fields*, as plain strings, followed by *note_keys*."""
    entries = _top_level_entries(block)
    if entries is None or [k for k, _ in entries] != [*fields, *note_keys]:
        return False
    return all(
        isinstance(node, yaml.ScalarNode) and node.tag == _STR_TAG and node.value == fields[key]
        for key, node in entries[: len(fields)]
    )


def _with_import_header(content: str, source: str, original_file: str) -> str:
    """Return *content* with the import provenance keys in its frontmatter."""
    fields = {"imported_from": source, "original_file": original_file}
    match = _FRONT_MATTER_RE.match(_indexer_view(content))
    if match is None:
        lines = _provenance_lines(fields)
        if not _reads_back(lines, fields, []):
            logger.debug("import: provenance keys for %r do not read back; omitted", original_file)
            return content
        return f"---\n{lines}---\n\n{content}"

    block = match.group(1)
    entries = _top_level_entries(block)
    if entries is None:
        logger.debug("import: %r frontmatter is not a plain mapping; kept as is", original_file)
        return content
    keys = [k for k, _ in entries]
    missing = {k: v for k, v in fields.items() if k not in keys}
    if not missing:
        return content

    lines = _provenance_lines(missing)
    opener = 5 if content.startswith("---\r\n") else 4
    merged = content[:opener] + lines + content[opener:]

    # The chunker must see the note's block with our lines in front of it,
    # and YAML our values, then the note's keys.
    merged_match = _FRONT_MATTER_RE.match(_indexer_view(merged))
    if (
        merged_match is None
        or merged_match.group(1) != lines + block
        or not _reads_back(merged_match.group(1), missing, keys)
    ):
        logger.debug("import: provenance keys do not fit %r frontmatter; kept as is", original_file)
        return content
    return merged


# ── Notion helpers ───────────────────────────────────────────────────────

_UUID_RE = re.compile(r"\s+([0-9a-f]{32})$")


def _split_notion_uuid(stem: str) -> tuple[str, str | None]:
    """Split trailing Notion 32-hex UUID suffix if present."""
    m = _UUID_RE.search(stem)
    if m is None:
        return stem, None
    clean = stem[: m.start()]
    if not clean:
        return stem, None
    return clean, m.group(1)


def _clean_notion_filename(stem: str) -> str:
    """Remove Notion's UUID suffix from filenames. 'Page Name abc123def456' → 'Page Name'."""
    clean, _ = _split_notion_uuid(stem)
    return clean


class NotionImportCollisionError(ValueError):
    """Two pages of a Notion export would be written to the same file."""


def _collision_key(name: str) -> str:
    """Fold a path component the way the importer decides two names collide.

    NFC plus casefold, on every platform, so one export gets the same filenames
    on Linux, macOS and Windows. This is a naming policy that covers the case-
    and normalization-insensitive filesystems (APFS, NTFS) for the ordinary
    cases; it is not an exact model of either (NTFS's upcase table, Turkish
    dotless i), and it also merges spellings such as 'Straße' and 'STRASSE'.
    """
    return unicodedata.normalize("NFC", name).casefold()


def _build_notion_output_mapping(
    sources: list[tuple[Path, str, str]],
) -> dict[tuple[Path, str], str]:
    """Build a mapping of (canonical_parent, original_filename) -> output_filename.

    Pages are grouped by their folded directory and folded cleaned title
    (``_collision_key``), so titles that differ only by case or Unicode
    normalization collide like identical ones:

    - A page alone in its group keeps its cleaned name ('Page.md').
    - In a colliding group, every page with a UUID gets
      '<its own stem> (<first 8 hex of UUID>).md', or the full 32 hex when two of
      them share an 8-hex prefix. A page without a UUID keeps its plain name,
      unless another UUID-less page shares the group; then each of those gets
      '<stem> (<first 8 hex of sha256(original filename)>).md'.

    Raises ``NotionImportCollisionError`` before anything is written if two
    sources still get output paths that fold to the same name, and refuses the
    export as a whole rather than resolving it. For example, a page titled
    'X (a1b2c3d4)' next to two 'X' pages, two pages carrying the same UUID (a
    duplicated ZIP member), or folders that differ only by case.
    """
    groups: dict[tuple[str, str], list[tuple[Path, str, str, str | None]]] = defaultdict(list)
    for rel, original_name, _ in sources:
        canonical_parent = Path(os.path.normpath(rel.parent))
        clean_stem, uuid = _split_notion_uuid(Path(original_name).stem)
        group_key = (_collision_key(canonical_parent.as_posix()), _collision_key(clean_stem))
        groups[group_key].append((canonical_parent, original_name, clean_stem, uuid))

    # One entry per source, checked before anything is keyed by Path: equal
    # ``(parent, name)`` keys (a duplicated ZIP member, or parents that a
    # Windows Path compares case-insensitively) must not collapse before the
    # collision check sees both.
    assigned: list[tuple[Path, str, str]] = []
    for members in groups.values():
        if len(members) == 1:
            canonical_parent, orig_name, clean_stem, _ = members[0]
            assigned.append((canonical_parent, orig_name, f"{clean_stem}.md"))
            continue

        prefix_counts = Counter(uuid[:8] for *_, uuid in members if uuid is not None)
        uuidless = sum(1 for *_, uuid in members if uuid is None)
        for canonical_parent, orig_name, clean_stem, uuid in members:
            if uuid is None:
                if uuidless > 1:
                    digest = hashlib.sha256(orig_name.encode("utf-8")).hexdigest()[:8]
                    output_name = f"{clean_stem} ({digest}).md"
                else:
                    output_name = f"{clean_stem}.md"
            elif prefix_counts[uuid[:8]] > 1:
                output_name = f"{clean_stem} ({uuid}).md"
            else:
                output_name = f"{clean_stem} ({uuid[:8]}).md"
            assigned.append((canonical_parent, orig_name, output_name))

    claimed: dict[tuple[str, str], str] = {}
    for canonical_parent, orig_name, output_name in sorted(
        assigned, key=lambda item: (item[0].as_posix(), item[1])
    ):
        source = (canonical_parent / orig_name).as_posix()
        dest_key = (_collision_key(canonical_parent.as_posix()), _collision_key(output_name))
        if dest_key in claimed:
            raise NotionImportCollisionError(
                f"Notion pages {claimed[dest_key]!r} and {source!r} would both be "
                f"written to {(canonical_parent / output_name).as_posix()!r}; "
                "rename one of them in Notion and export again"
            )
        claimed[dest_key] = source

    return {(parent, orig): output for parent, orig, output in assigned}


def _clean_notion_markdown(
    content: str,
    *,
    rel_parent: Path | None = None,
    link_mapping: dict[tuple[Path, str], str] | None = None,
) -> str:
    """Clean Notion-specific markdown artifacts."""
    # Remove Notion's property tables at the top
    content = re.sub(r"^(\|[^\n]+\|\n)+\n", "", content)

    # Fix Notion's broken link format: [text](Page%20Name%20uuid.md) → [text](Page Name.md)
    def _fix_link(m: re.Match[str]) -> str:
        text = m.group(1)
        href = m.group(2)
        # URL-decode and strip UUID
        from urllib.parse import unquote

        decoded = unquote(href)
        fragment = ""
        if "#" in decoded:
            decoded, fragment = decoded.split("#", 1)
            fragment = "#" + fragment
        query = ""
        if "?" in decoded:
            decoded, query = decoded.split("?", 1)
            query = "?" + query

        if decoded.endswith(".md"):
            if "://" in decoded or decoded.startswith(("//", "mailto:")):
                return f"[{text}]({decoded}{query}{fragment})"
            decoded_path = Path(decoded)
            parent = Path(".") if rel_parent is None else rel_parent
            resolved_parent = Path(os.path.normpath(parent / decoded_path.parent))
            target_key = (resolved_parent, decoded_path.name)
            if link_mapping is not None and target_key in link_mapping:
                mapped_name = link_mapping[target_key]
                if str(decoded_path.parent) in ("", "."):
                    decoded = mapped_name
                else:
                    decoded = f"{decoded_path.parent.as_posix()}/{mapped_name}"
            else:
                decoded = _clean_notion_filename(decoded[:-3]) + ".md"
        return f"[{text}]({decoded}{query}{fragment})"

    content = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _fix_link, content)

    # Remove empty toggle blocks
    content = re.sub(r"<details>\s*<summary></summary>\s*</details>", "", content)

    return content.strip()


# ── Obsidian helpers ─────────────────────────────────────────────────────


def _convert_obsidian_syntax(content: str) -> str:
    """Convert Obsidian-specific syntax to standard markdown."""
    # [[wikilink]] → [wikilink](wikilink.md)
    content = re.sub(
        r"!\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]",
        lambda m: f"[{m.group(2) or m.group(1)}]({m.group(1).replace(' ', '%20')}.md)",
        content,
    )
    content = re.sub(
        r"\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]",
        lambda m: f"[{m.group(2) or m.group(1)}]({m.group(1).replace(' ', '%20')}.md)",
        content,
    )

    # Obsidian callouts: > [!note] Title → > **Note**: Title
    content = re.sub(
        r"^(>\s*)\[!(\w+)\]\s*(.*)",
        lambda m: f"{m.group(1)}**{m.group(2).capitalize()}**: {m.group(3)}",
        content,
        flags=re.MULTILINE,
    )

    return content
