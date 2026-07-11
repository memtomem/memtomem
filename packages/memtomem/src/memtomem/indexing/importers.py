"""Importers for Notion and Obsidian exports."""

from __future__ import annotations

import logging
import re
import zipfile
from collections.abc import Callable
from pathlib import Path

from memtomem.context._atomic import atomic_write_text
from memtomem.privacy import enforce_write_guard

logger = logging.getLogger(__name__)


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


async def import_notion(
    export_path: Path,
    output_dir: Path,
    *,
    force_unsafe: bool = False,
    scope: str = "user",
    blocked_paths: list[str] | None = None,
) -> list[Path]:
    """Import a Notion export (ZIP or directory) into markdown files.

    Notion exports come as a ZIP with markdown files + nested folders.
    File names contain UUIDs that we strip for cleaner names.

    Args:
        export_path: Path to Notion export ZIP or extracted directory.
        output_dir: Directory to write cleaned markdown files.

    Returns:
        List of imported file paths.
    """
    imported: list[Path] = []
    sources: list[tuple[Path, str, str]] = []
    if export_path.suffix == ".zip":
        # Validate resource/traversal limits, then read markdown members
        # directly. No temporary plaintext extraction tree is created.
        safe_extract_zip(export_path, Path("/tmp"), member_filter=lambda _: False)
        with zipfile.ZipFile(export_path, "r") as zf:
            infos = sorted(
                (i for i in zf.infolist() if not i.is_dir() and i.filename.lower().endswith(".md")),
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
        sources = [
            (p.relative_to(export_path), p.name, p.read_text(encoding="utf-8", errors="replace"))
            for p in sorted(export_path.rglob("*.md"))
        ]

    for rel, original_name, content in sources:
        # Clean Notion-specific artifacts
        content = _clean_notion_markdown(content)

        # Clean filename (remove Notion UUID suffix)
        clean_name = _clean_notion_filename(Path(original_name).stem) + ".md"

        # Preserve directory structure
        target = output_dir / rel.parent / clean_name

        # Add source metadata
        header = f"---\nimported_from: notion\noriginal_file: {original_name}\n---\n\n"
        final = header + content
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
        atomic_write_text(target, final, mode=0o600)
        imported.append(target)

    logger.info("Imported %d files from Notion export", len(imported))
    return imported


async def import_obsidian(
    vault_path: Path,
    output_dir: Path,
    *,
    force_unsafe: bool = False,
    scope: str = "user",
    blocked_paths: list[str] | None = None,
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

    Returns:
        List of imported file paths.
    """
    imported: list[Path] = []

    for md_file in sorted(vault_path.rglob("*.md")):
        # Skip Obsidian config files
        rel = md_file.relative_to(vault_path)
        if str(rel).startswith(".obsidian"):
            continue

        content = md_file.read_text(encoding="utf-8", errors="replace")

        # Convert Obsidian syntax
        content = _convert_obsidian_syntax(content)

        target = output_dir / rel
        # Add source metadata
        header = f"---\nimported_from: obsidian\noriginal_file: {rel}\n---\n\n"
        final = header + content
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
        atomic_write_text(target, final, mode=0o600)
        imported.append(target)

    logger.info("Imported %d files from Obsidian vault", len(imported))
    return imported


# ── Notion helpers ───────────────────────────────────────────────────────


def _clean_notion_filename(stem: str) -> str:
    """Remove Notion's UUID suffix from filenames. 'Page Name abc123def456' → 'Page Name'."""
    # Notion appends a 32-char hex UUID at the end
    cleaned = re.sub(r"\s+[0-9a-f]{32}$", "", stem)
    return cleaned or stem


def _clean_notion_markdown(content: str) -> str:
    """Clean Notion-specific markdown artifacts."""
    # Remove Notion's property tables at the top
    content = re.sub(r"^(\|[^\n]+\|\n)+\n", "", content)

    # Fix Notion's broken link format: [text](Page%20Name%20uuid.md) → [text](Page Name.md)
    def _fix_link(m):
        text = m.group(1)
        href = m.group(2)
        # URL-decode and strip UUID
        from urllib.parse import unquote

        decoded = unquote(href)
        if decoded.endswith(".md"):
            decoded = _clean_notion_filename(decoded[:-3]) + ".md"
        return f"[{text}]({decoded})"

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
