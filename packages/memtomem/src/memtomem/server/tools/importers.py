"""Tools: mem_import_notion, mem_import_obsidian — migrate notes from other apps."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from memtomem.constants import writable_namespace_arg
from memtomem.errors import ConfigError, EmbeddingPolicyChangedError
from memtomem.memory_scope import require_user_base
from memtomem.server import mcp
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import (
    capture_session_for_untracked_write,
    flag_untracked_write,
)


def _mismatch_refusal(app: object, label: str) -> str | None:
    """Refuse an import before it writes while the index refuses every file.

    In embedding-mismatch mode ``IndexEngine`` indexes nothing, and an import
    writes all its targets before it indexes any. Writing anyway would
    overwrite the previous import's files while their old chunks stay
    searchable beside bytes that no search reads (#3162).
    """
    msg = _check_embedding_mismatch(app)
    return None if msg is None else f"{label} import wrote nothing.\n{msg}"


# The not-indexed list in an import result names this many files at most.
_NOT_INDEXED_SHOWN = 5


@dataclass
class _NotIndexed:
    name: str
    reason: str
    # Only a scope hold says the file's stored rows are hidden: its engine
    # contract is that every stored row is under the held key. Other refusals
    # say nothing reliable about visibility (a repeated read-error hold reports
    # no change while its rows stay hidden), so the result makes no claim.
    hidden: bool = False


@dataclass
class _ImportIndexOutcome:
    total_chunks: int = 0
    engine_blocked: int = 0
    excluded_purged: int = 0
    purged_chunks: int = 0
    relinked: int = 0
    summary_failed: int = 0
    not_indexed: list[_NotIndexed] = field(default_factory=list)


def _display_name(path: Path, output_dir: Path) -> str:
    try:
        return path.relative_to(output_dir).as_posix()
    except ValueError:
        return path.name


async def _index_imported(
    app: AppContext,
    imported: list[Path],
    *,
    output_dir: Path,
    namespace: str,
    source_tag: str,
    tags: list[str] | None,
    force_unsafe: bool,
    written_text: dict[Path, str],
    provenance_session_id: str | None,
) -> _ImportIndexOutcome:
    """Index the files an import wrote, then tag them and flag the session.

    The import guarded each file's exact text; the engine judges it again under
    its own scope, and ``valve_text`` keeps a forced import's valve to those
    bytes. A refused file is reported rather than aborting the import. An
    exclusion installed after a file was written makes the engine skip it;
    ``purge_if_excluded`` then removes the chunks of the bytes this import
    overwrote (#3158). A target replaced by a symlink after its write is held,
    not indexed through the link (``hold_if_relinked``, #3164).

    A file counts as indexed only when the engine says its stored chunks now
    describe what it read (``current_files``). ``errors`` cannot answer that:
    an unreadable file is held with no error, and a file whose chunks committed
    can still carry the error of the AI summary refresh that runs after the
    commit (#3168). Another process restamping the embedding policy makes the
    engine raise ``EmbeddingPolicyChangedError`` for the file being written and
    refuse every later one, so the loop records it and goes on; the files
    indexed before it are still tagged and flagged below.
    """
    from memtomem.indexing.engine import PrivacyRejection

    outcome = _ImportIndexOutcome()
    indexed_files: list[Path] = []
    mutated = False
    for f in imported:
        try:
            stats = await app.index_engine.index_file(
                f,
                namespace=namespace,
                ingress_guarded=True,
                force_unsafe=force_unsafe,
                valve_text=written_text.get(f),
                purge_if_excluded=True,
                hold_if_relinked=True,
            )
        except PrivacyRejection as exc:
            outcome.engine_blocked += 1
            # The engine commits a redaction hold before it re-raises, and
            # ``held`` is True once that hold ran, even over no rows. Counting
            # it as a change can only weaken the session's provenance claim.
            mutated = mutated or bool(exc.held)
            continue
        except EmbeddingPolicyChangedError as exc:
            outcome.not_indexed.append(_NotIndexed(_display_name(f, output_dir), str(exc)))
            continue
        mutated = mutated or stats.mutated
        if stats.relinked_files:
            outcome.relinked += 1
            continue
        if stats.excluded_files:
            outcome.excluded_purged += 1
            outcome.purged_chunks += stats.deleted_chunks
            continue
        if not stats.current_files:
            reason = (
                stats.errors[0].removeprefix(f"{f.name}: ")
                if stats.errors
                else "not indexed (unreadable, missing, or no longer a regular file)"
            )
            outcome.not_indexed.append(
                _NotIndexed(_display_name(f, output_dir), reason, hidden=stats.scope_held)
            )
            continue
        indexed_files.append(f)
        outcome.total_chunks += stats.indexed_chunks
        if stats.errors:
            # On a current file the only error is the post-commit summary refresh.
            outcome.summary_failed += 1

    if tags and outcome.total_chunks > 0:
        for f in indexed_files:
            chunks = await app.storage.list_chunks_by_source(f)
            for c in chunks:
                merged = set(c.metadata.tags) | set(tags) | {source_tag, "imported"}
                if merged != set(c.metadata.tags):
                    c.metadata = c.metadata.__class__(
                        **{
                            **{
                                attr: getattr(c.metadata, attr)
                                for attr in c.metadata.__dataclass_fields__
                            },
                            "tags": tuple(sorted(merged)),
                        }
                    )
            if chunks:
                await app.storage.upsert_chunks(chunks)

    app.search_pipeline.invalidate_cache()

    # A purge or a hold changes the session's chunk set as much as an index does.
    if outcome.total_chunks or outcome.purged_chunks or mutated:
        await flag_untracked_write(app, provenance_session_id)
    return outcome


def _render_import(
    label: str,
    outcome: _ImportIndexOutcome,
    *,
    imported: int,
    guard_blocked: int,
    excluded: int,
    symlinks: int,
    read_only: int,
    source_symlinks: int,
    source_noun: str,
    namespace: str,
    output_dir: Path,
) -> str:
    missed = outcome.not_indexed
    if missed:
        header = (
            f"{label} import incomplete: {len(missed)} of {imported} written file(s) "
            f"were not indexed."
        )
    else:
        header = f"{label} import complete:"
    lines = [
        header,
        f"- Files imported: {imported}",
        f"- Chunks indexed: {outcome.total_chunks}",
    ]
    if missed:
        lines.append(f"- Written but not indexed: {len(missed)}")
        for item in missed[:_NOT_INDEXED_SHOWN]:
            note = " — previous chunks hidden from search" if item.hidden else ""
            lines.append(f"  - {item.name}: {item.reason}{note}")
        if len(missed) > _NOT_INDEXED_SHOWN:
            lines.append(f"  - … and {len(missed) - _NOT_INDEXED_SHOWN} more")
        lines.append(
            "  Chunks from an earlier import of these files were not replaced; "
            "depending on the reason they may still be searchable."
        )
    if outcome.summary_failed:
        lines.append(f"- Indexed, AI summary not refreshed: {outcome.summary_failed}")
    lines += [
        f"- Blocked (redaction): {guard_blocked + outcome.engine_blocked}",
        f"- Skipped (target excluded from indexing): {excluded}",
        f"- Skipped (target is a symlink): {symlinks}",
        f"- Skipped (target under a read-only index root): {read_only}",
        f"- Written but excluded from indexing: {outcome.excluded_purged} "
        f"(previous chunks removed: {outcome.purged_chunks})",
        "- Written but replaced by a symlink (not indexed, previous chunks hidden): "
        f"{outcome.relinked}",
        f"- Skipped (source is a symlink or outside the {source_noun}): {source_symlinks}",
        f"- Namespace: {namespace}",
        f"- Output: {output_dir}",
    ]
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("importers")
async def mem_import_notion(
    path: str,
    namespace: str | None = None,
    tags: list[str] | None = None,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Import a Notion export (ZIP or directory) into memtomem.

    Cleans Notion-specific artifacts (UUID filenames, property tables,
    broken links) and indexes the imported files for search.

    Args:
        path: Path to Notion export ZIP file or extracted directory.
        namespace: Namespace for imported content (default: "notion").
        tags: Tags to apply to all imported chunks.
        force_unsafe: Bypass the redaction guard when an exported page matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line (see ``mem_add_redaction_stats``).
            It never applies to a ``project_shared`` destination — that
            combination is hard-refused, because git history cannot be
            retracted from clones.
    """
    namespace = writable_namespace_arg(namespace)
    from memtomem.indexing.importers import import_notion

    app = await _get_app_initialized(ctx)
    refusal = _mismatch_refusal(app, "Notion")
    if refusal:
        return refusal
    # A bulk import is not session work — its chunks are an ingest, and
    # summarizing them would describe someone else's notes as this
    # session's output. But it does change the session's chunk set, so
    # the session must stop claiming its provenance is the whole story.
    # Captured before the import's awaits so a session that ends midway
    # still gets the flag.
    provenance_session_id = await capture_session_for_untracked_write(app)
    export_path = Path(path).expanduser().resolve()

    if not export_path.exists():
        return f"Error: Path not found: {export_path}"

    try:
        memory_dir = require_user_base(
            app.config.indexing.memory_dirs, app.config.indexing.project_memory_dirs
        )
    except ConfigError as exc:
        return f"Error: {exc}"
    output_dir = memory_dir / "_imported" / "notion"

    from memtomem.config import classify_scope

    scope, _ = classify_scope(output_dir, app.config.indexing.project_memory_dirs)
    blocked_paths: list[str] = []
    excluded_paths: list[str] = []
    symlink_paths: list[str] = []
    read_only_paths: list[str] = []
    source_symlink_paths: list[str] = []
    written_text: dict[Path, str] = {}
    imported = await import_notion(
        export_path,
        output_dir,
        force_unsafe=force_unsafe,
        scope=scope,
        blocked_paths=blocked_paths,
        index_guard=app.index_engine,
        excluded_paths=excluded_paths,
        symlink_paths=symlink_paths,
        read_only_paths=read_only_paths,
        source_symlink_paths=source_symlink_paths,
        written_text=written_text,
    )

    # Refusals first: an import that also hit the redaction guard must still say
    # that some targets were refused for a different reason (#2488).
    if not imported and (
        excluded_paths or symlink_paths or read_only_paths or source_symlink_paths
    ):
        return (
            f"Notion import wrote nothing: {len(excluded_paths)} target(s) excluded from "
            f"indexing, {len(symlink_paths)} target(s) are symbolic links, "
            f"{len(read_only_paths)} target(s) under a read-only index root, "
            f"{len(source_symlink_paths)} source file(s) are symbolic links or outside "
            f"the export, "
            f"{len(blocked_paths)} file(s) blocked by the redaction guard."
        )
    if not imported and blocked_paths:
        return f"Notion import blocked by redaction guard: {len(blocked_paths)} file(s)."
    if not imported:
        return "No markdown files found in the Notion export."

    effective_ns = namespace or "notion"
    outcome = await _index_imported(
        app,
        imported,
        output_dir=output_dir,
        namespace=effective_ns,
        source_tag="notion",
        tags=tags,
        force_unsafe=force_unsafe,
        written_text=written_text,
        provenance_session_id=provenance_session_id,
    )
    return _render_import(
        "Notion",
        outcome,
        imported=len(imported),
        guard_blocked=len(blocked_paths),
        excluded=len(excluded_paths),
        symlinks=len(symlink_paths),
        read_only=len(read_only_paths),
        source_symlinks=len(source_symlink_paths),
        source_noun="export",
        namespace=effective_ns,
        output_dir=output_dir,
    )


@mcp.tool()
@tool_handler
@register("importers")
async def mem_import_obsidian(
    vault_path: str,
    namespace: str | None = None,
    tags: list[str] | None = None,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Import an Obsidian vault into memtomem.

    Converts Obsidian-specific syntax ([[wikilinks]], ![[embeds]],
    callouts) to standard markdown and indexes for search.

    Args:
        vault_path: Path to Obsidian vault root directory.
        namespace: Namespace for imported content (default: "obsidian").
        tags: Tags to apply to all imported chunks.
        force_unsafe: Bypass the redaction guard when a vault note matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line (see ``mem_add_redaction_stats``).
            It never applies to a ``project_shared`` destination — that
            combination is hard-refused, because git history cannot be
            retracted from clones.
    """
    namespace = writable_namespace_arg(namespace)
    from memtomem.indexing.importers import import_obsidian

    app = await _get_app_initialized(ctx)
    refusal = _mismatch_refusal(app, "Obsidian")
    if refusal:
        return refusal
    # A bulk import is not session work — its chunks are an ingest, and
    # summarizing them would describe someone else's notes as this
    # session's output. But it does change the session's chunk set, so
    # the session must stop claiming its provenance is the whole story.
    # Captured before the import's awaits so a session that ends midway
    # still gets the flag.
    provenance_session_id = await capture_session_for_untracked_write(app)
    vault = Path(vault_path).expanduser().resolve()

    if not vault.exists() or not vault.is_dir():
        return f"Error: Obsidian vault not found: {vault}"

    try:
        memory_dir = require_user_base(
            app.config.indexing.memory_dirs, app.config.indexing.project_memory_dirs
        )
    except ConfigError as exc:
        return f"Error: {exc}"
    output_dir = memory_dir / "_imported" / "obsidian"

    from memtomem.config import classify_scope

    scope, _ = classify_scope(output_dir, app.config.indexing.project_memory_dirs)
    blocked_paths: list[str] = []
    excluded_paths: list[str] = []
    symlink_paths: list[str] = []
    read_only_paths: list[str] = []
    source_symlink_paths: list[str] = []
    written_text: dict[Path, str] = {}
    imported = await import_obsidian(
        vault,
        output_dir,
        force_unsafe=force_unsafe,
        scope=scope,
        blocked_paths=blocked_paths,
        index_guard=app.index_engine,
        excluded_paths=excluded_paths,
        symlink_paths=symlink_paths,
        read_only_paths=read_only_paths,
        source_symlink_paths=source_symlink_paths,
        written_text=written_text,
    )

    # Refusals first: an import that also hit the redaction guard must still say
    # that some targets were refused for a different reason (#2488).
    if not imported and (
        excluded_paths or symlink_paths or read_only_paths or source_symlink_paths
    ):
        return (
            f"Obsidian import wrote nothing: {len(excluded_paths)} target(s) excluded from "
            f"indexing, {len(symlink_paths)} target(s) are symbolic links, "
            f"{len(read_only_paths)} target(s) under a read-only index root, "
            f"{len(source_symlink_paths)} source file(s) are symbolic links or outside "
            f"the vault, "
            f"{len(blocked_paths)} file(s) blocked by the redaction guard."
        )
    if not imported and blocked_paths:
        return f"Obsidian import blocked by redaction guard: {len(blocked_paths)} file(s)."
    if not imported:
        return "No markdown files found in the Obsidian vault."

    effective_ns = namespace or "obsidian"
    outcome = await _index_imported(
        app,
        imported,
        output_dir=output_dir,
        namespace=effective_ns,
        source_tag="obsidian",
        tags=tags,
        force_unsafe=force_unsafe,
        written_text=written_text,
        provenance_session_id=provenance_session_id,
    )
    return _render_import(
        "Obsidian",
        outcome,
        imported=len(imported),
        guard_blocked=len(blocked_paths),
        excluded=len(excluded_paths),
        symlinks=len(symlink_paths),
        read_only=len(read_only_paths),
        source_symlinks=len(source_symlink_paths),
        source_noun="vault",
        namespace=effective_ns,
        output_dir=output_dir,
    )
