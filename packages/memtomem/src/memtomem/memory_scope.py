"""ADR-0011 memory scope → canonical directory resolver.

Single source of truth for the user / project_shared / project_local →
canonical memory directory mapping. Used by:

- CLI ``mm mem add`` (``cli/memory.py``)
- MCP ``mem_add`` / ``mem_batch_add`` (``server/tools/memory_crud.py``)
- ``mem_consolidate_apply`` summary writes (``server/tools/consolidation.py``)
- ``mm context memory-migrate`` (``cli/context_cmd.py``)

Keeping the helper here (instead of inside ``cli/memory.py`` or
``server/tools/memory_crud.py``) avoids cross-importing CLI deps from
server code (and vice versa) when the same resolution is needed on
both surfaces.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from memtomem.config import TargetScope
from memtomem.errors import ConfigError


logger = logging.getLogger(__name__)


DEFAULT_USER_MEMORY_DIR = Path("~/.memtomem/memories")

EMPTY_MEMORY_DIRS_ERROR = (
    "indexing.memory_dirs is empty — no memory directories configured. "
    "Run 'mm init' first or add a directory to ~/.memtomem/config.json."
)


class MemoryScopeError(ValueError):
    """Raised when scope → directory resolution cannot proceed.

    Surface-specific wrappers (``click.ClickException`` for the CLI,
    plain string error messages for MCP tool returns) catch and rewrap
    so each layer surfaces user-facing errors in its native vocabulary.
    """


def require_user_base(memory_dirs: Sequence[Path | str]) -> Path:
    """Return the user-tier base directory (``memory_dirs[0]``), expanded and resolved.

    An empty ``indexing.memory_dirs`` is a valid "index nothing" state
    (#1768): read surfaces degrade gracefully, but any write that needs
    the user-tier base must refuse with an error that names the config
    field instead of crashing with ``IndexError``.

    Raises:
        ConfigError: When ``memory_dirs`` is empty.
    """
    if not memory_dirs:
        raise ConfigError(EMPTY_MEMORY_DIRS_ERROR)
    return Path(memory_dirs[0]).expanduser().resolve()


def resolve_memory_scope_dir(
    scope: TargetScope,
    project_root: Path | None,
    user_base: Path = DEFAULT_USER_MEMORY_DIR,
) -> Path:
    """Resolve an ADR-0011 memory scope to its canonical directory.

    Args:
        scope: One of ``user`` / ``project_shared`` / ``project_local``.
        project_root: Required when ``scope`` is a project tier; the
            project root (the grandparent of the
            ``.memtomem/memories[.local]`` entry registered in
            ``IndexingConfig.project_memory_dirs``). Pass ``None`` for
            ``user`` scope.
        user_base: Override for the user-tier base directory. Defaults
            to ``~/.memtomem/memories`` — the historical hardcoded path
            that ``mm mem add`` used pre-ADR-0011.

    Returns:
        The resolved, expanded canonical directory ``Path`` for the
        given scope. The directory may not exist yet; callers create it
        before writing.

    Raises:
        MemoryScopeError: When ``scope`` is a project tier but
            ``project_root`` is ``None``, or when ``scope`` is unknown.
    """
    if scope == "user":
        return user_base.expanduser().resolve()
    if project_root is None:
        raise MemoryScopeError(
            f"scope='{scope}' requires a registered project context "
            "(no project_memory_dirs entry covers the current cwd)."
        )
    if scope == "project_shared":
        return (project_root / ".memtomem" / "memories").resolve()
    if scope == "project_local":
        return (project_root / ".memtomem" / "memories.local").resolve()
    raise MemoryScopeError(f"unsupported memory scope: {scope!r}")


_FS_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# Digest length and readable-slug budget for per-namespace day files. 16 hex
# chars is far below birthday risk for the number of namespaces one store
# holds, and the slug cap keeps the whole component well inside the 255-byte
# limit every supported filesystem enforces.
_NS_DIGEST_LEN = 16
_NS_SLUG_MAX_BYTES = 60

# Bounded re-target attempts when the in-lock namespace disagrees with the
# pre-lock one the day file's name was built from (issue #2005). Each attempt
# costs one lock acquisition and nothing durable, but an unbounded loop would
# spin forever against a session that keeps flipping.
NS_RETARGET_ATTEMPTS = 3

NS_RETARGET_EXHAUSTED_ERROR = (
    "the active session's namespace kept changing while acquiring the write "
    "lock; nothing was written. Retry, or pass an explicit namespace to pin "
    "the target."
)


def day_file_name(namespace: str | None, default_namespace: str, *, date_str: str) -> str:
    """Return the default append target's file name for ``namespace`` (issue #2005).

    ``index_file`` applies a namespace to *every* chunk of the file it
    re-chunks, so two entries with different namespaces sharing one file
    lose the earlier namespace as soon as chunk merging joins them. A day
    file is the one target every write of the day shares, so the fix is to
    give each namespace its own day file: merging cannot cross files.

    The default namespace keeps the historical ``{date}.md`` name — both
    ``None`` (the engine's "untagged" carve-out, see
    ``IndexingEngine._resolve_namespace``) and an explicit value equal to
    ``default_namespace`` map to it, because both resolve to the same
    stored namespace.

    Every non-default namespace gets a digest of its exact bytes appended,
    and the readable part is only a hint. Nothing weaker is safe, because
    the whole guarantee is "one file, one namespace":

    - The readable part is lossy. Namespaces may contain characters that
      are illegal in file names (``agent-runtime:planner`` is the common
      case; ``:`` is invalid on Windows), so anything outside
      ``[A-Za-z0-9._-]`` becomes ``-``. Without the digest, ``a:b`` and a
      literal namespace ``a-b`` would share a file.
    - The readable part is not case-distinguishing on the default macOS
      and Windows filesystems, where ``Foo`` and ``foo`` name the same
      file. The digest is over the exact string, so case-only differences
      still land in different files.
    - Filename components are capped (255 bytes almost everywhere), so the
      readable part is truncated on a UTF-8 boundary. Truncation is
      another way two namespaces collide, which the digest again settles.
    """
    if namespace is None or namespace == default_namespace:
        return f"{date_str}.md"
    digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:_NS_DIGEST_LEN]
    slug = _FS_UNSAFE_RE.sub("-", namespace).strip("-")
    slug = slug.encode("utf-8")[:_NS_SLUG_MAX_BYTES].decode("utf-8", "ignore").strip("-")
    return f"{date_str}--{slug}-{digest}.md" if slug else f"{date_str}--{digest}.md"


def namespace_mix_error(
    target: Path,
    existing: Iterable[str],
    resolved: str,
    *,
    override_hint: str,
) -> str | None:
    """Refusal message when a write would mix namespaces in one file (issue #2005).

    Returns ``None`` when the write is safe: the file holds no chunks yet,
    or every stored namespace already equals ``resolved``. Otherwise the
    append would let re-chunking merge this entry with the existing ones
    and silently restamp *their* namespace to ``resolved``.

    ``resolved`` must be the namespace ``index_file`` will actually apply
    — callers map the engine's ``None`` carve-out to
    ``namespace.default_namespace`` first, so the comparison is against
    what is stored rather than what was passed in.

    ``override_hint`` is the surface's own spelling of the escape hatch
    (``--allow-namespace-mix`` for the CLI, ``allow_namespace_mix=true``
    for MCP/web) so the remediation names a command the reader can run.
    """
    others = sorted({ns for ns in existing if ns != resolved})
    if not others:
        return None
    listed = ", ".join(f"'{ns}'" for ns in others)
    return (
        f"{target} already holds namespace(s) {listed}; this write resolves to "
        f"namespace '{resolved}'. Appending would re-chunk the file and can "
        f"silently move the existing '{others[0]}' content into '{resolved}' "
        "(issue #2005).\n"
        f"Either write under the namespace the file already uses, omit the "
        "explicit file so the write lands in that namespace's own day file, or "
        f"pass {override_hint} to mix namespaces anyway."
    )


async def namespace_mix_refusal(
    *,
    index_engine: Any,
    storage: Any,
    default_namespace: str,
    target: Path,
    effective_ns: str | None,
    override_hint: str,
) -> str | None:
    """Surface-neutral wrapper around :func:`namespace_mix_error` (issue #2005).

    Asks the engine what it will actually stamp
    (``effective_namespace_for``) rather than re-deriving it, with the
    ``None`` carve-out mapped to ``default_namespace``. Re-deriving is how
    the guard and the indexer drift: resolving through the rules alone
    would refuse a write to a file whose stored namespace the indexer
    would have *preserved*, which is a refusal with no hazard behind it.

    From there it decides from the file *and* the stored rows:

    - Missing or empty file: nothing to protect. Any rows are stale (a
      deleted file whose chunks have not been reaped), so they are
      ignored rather than refusing a write against content that no
      longer exists.
    - Content and rows: compare, and refuse on disagreement.
    - Content but no rows: allowed. Unindexed text has no stored
      namespace to lose, so indexing it alongside this write assigns one
      for the first time rather than moving it — a weaker event than the
      one this guard exists to stop. Refusing here would also break the
      idempotency contract, since "appended, then indexing failed" is
      exactly this state and its keyed retry must report the pending
      claim rather than a namespace error.

    A namespace query that *fails* refuses. There the guard cannot tell a
    safe write from an unsafe one, and the failure mode it protects
    against is silent data movement — for which "proceed and hope" is the
    wrong default. The override releases the write either way.
    """
    try:
        has_content = target.is_file() and target.stat().st_size > 0
    except OSError:
        has_content = False
    if not has_content:
        # Checked before anything else: a brand-new day file is the common
        # case, and it needs neither a namespace resolution nor a query.
        return None
    try:
        resolved = await index_engine.effective_namespace_for(target, effective_ns)
    except Exception:
        logger.warning("namespace mix guard could not resolve %s", target, exc_info=True)
        return (
            f"could not determine which namespace {target} already holds "
            "(the chunk store did not answer). Appending could silently move "
            "its existing entries into another namespace (issue #2005). "
            f"Retry, or pass {override_hint} to append anyway."
        )
    stored = resolved or default_namespace
    try:
        existing = await storage.namespaces_for_source(target)
    except Exception:
        logger.warning("namespace mix guard could not read %s", target, exc_info=True)
        return (
            f"could not determine which namespace {target} already holds "
            "(the chunk store did not answer). Appending could silently move "
            f"its existing entries into '{stored}' (issue #2005). Retry, or "
            f"pass {override_hint} to append anyway."
        )
    return namespace_mix_error(target, existing, stored, override_hint=override_hint)


def project_tier_registration_error(target_dir: Path, scope: TargetScope) -> str:
    """Standard error message for an unregistered project-tier write.

    ADR-0011: project-tier writes are only safe when the target tier
    directory is present in ``IndexingConfig.project_memory_dirs``.
    Without registration the read/search boundary and the indexing
    watcher both miss the write — rows persist with
    ``scope='project_shared'`` / ``project_local'`` but stay invisible
    to default search/recall. The hint is centralised here so every
    write surface (MCP ``mem_add`` / ``mem_batch_add``, CLI
    ``mm mem add``, ``mm context memory-migrate``) emits the same
    setup instruction.
    """
    return (
        f"Target tier {target_dir} is not registered in "
        "IndexingConfig.project_memory_dirs. Writing without registration "
        "would persist a row with the requested scope but the read "
        "surface and indexing watcher would not see it.\n"
        f"Run `mm mem init --scope={scope}` in a terminal from the project "
        f'root, then retry with scope="{scope}". (Registration is CLI-only '
        "by design; alternatively edit ~/.memtomem/config.json and add "
        f"{target_dir} to indexing.project_memory_dirs.)"
    )


def is_project_tier_registered(target_dir: Path, project_memory_dirs) -> bool:
    """``True`` iff ``target_dir`` is in the resolved registered set.

    Both sides expand ``~`` and resolve symlinks so the comparison is
    canonical. Empty / ``None`` registries return ``False`` for any
    project-tier path.
    """
    if not project_memory_dirs:
        return False
    target = target_dir.expanduser().resolve()
    registered = {Path(d).expanduser().resolve() for d in project_memory_dirs}
    return target in registered
