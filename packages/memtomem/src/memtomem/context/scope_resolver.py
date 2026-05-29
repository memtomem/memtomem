"""ADR-0011 PR-E canonical-side scope resolver for non-memory artifacts.

Sibling of :mod:`memtomem.memory_scope` covering agents / skills / commands
canonical directories under ``.memtomem/``. The runtime fan-out side
(``~/.claude/agents``, ``<proj>/.gemini/agents``, etc.) lives in
:mod:`memtomem.context._runtime_targets` — these two concepts must NOT be
conflated. ``canonical_artifact_dir`` resolves where the source of truth
file lives; the runtime table resolves where it gets fanned out to.

Used by:

- ``mm context init --scope=...`` (E2)
- ``mm context sync --scope=...`` (E3)
- ``mm context migrate <kind> <name>`` (E4)
- ``context/agents.py``, ``context/skills.py``, ``context/commands.py``
  in place of hardcoded ``CANONICAL_*_ROOT`` constants
- Web routes ``/api/context/{agents,skills,commands}`` per-scope listing

Non-memory artifacts have no SQLite chunks table representation and no
indexing watcher, so this module deliberately omits the
``is_project_tier_registered`` / ``project_tier_registration_error``
helpers that ``memory_scope`` provides — Gate B (``--confirm-project-shared``)
and directory presence are the only gates the artifact write surfaces use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from memtomem.config import TargetScope


ArtifactKind = Literal["agents", "skills", "commands"]

DEFAULT_USER_ARTIFACT_BASE = Path("~/.memtomem")


class ContextScopeError(ValueError):
    """Raised when scope → canonical directory resolution cannot proceed.

    Surface-specific wrappers (``click.ClickException`` for the CLI,
    HTTP 4xx for web routes) catch and rewrap so each layer surfaces
    user-facing errors in its native vocabulary.
    """


_PROJECT_MARKERS: tuple[str, ...] = (".git", "pyproject.toml")
_PROJECT_ROOT_MAX_DEPTH = 10


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (default cwd) to the nearest project root.

    Looks up to :data:`_PROJECT_ROOT_MAX_DEPTH` ancestors for a ``.git`` or
    ``pyproject.toml`` marker and returns the first match; falls back to the
    original ``start`` when no marker is found.

    This is the SINGLE definition of "what is the project root", shared by the
    CLI (``mm context``), the MCP context tools, and the web-app lifespan, so a
    launch from a project subdirectory resolves the same canonical ``.memtomem``
    tree on every surface. Previously the web app pinned the bare ``cwd`` and so
    wrote artifacts to ``<subdir>/.memtomem`` while the CLI/MCP walked up to the
    repo root — silently targeting different canonical trees for one project.

    The returned path is not resolved — callers that need symlink
    canonicalisation should ``.resolve()`` themselves (matches the historical
    ``cli/context_cmd.py`` / ``server/tools/context.py`` behavior).
    """
    origin = Path.cwd() if start is None else start
    p = origin
    for _ in range(_PROJECT_ROOT_MAX_DEPTH):
        if any((p / marker).exists() for marker in _PROJECT_MARKERS):
            return p
        p = p.parent
    return origin


def canonical_artifact_dir(
    artifact: ArtifactKind,
    scope: TargetScope,
    project_root: Path | None,
    user_base: Path = DEFAULT_USER_ARTIFACT_BASE,
) -> Path:
    """Resolve an ADR-0011 (artifact, scope) pair to its canonical directory.

    Args:
        artifact: One of ``agents`` / ``skills`` / ``commands``.
        scope: One of ``user`` / ``project_shared`` / ``project_local``.
        project_root: Required when ``scope`` is a project tier; the
            project root that owns the ``.memtomem/`` subtree. Pass
            ``None`` for ``user`` scope.
        user_base: Override for the user-tier base directory. Defaults to
            ``~/.memtomem`` — the artifact name is appended to form e.g.
            ``~/.memtomem/agents``.

    Returns:
        The expanded canonical directory ``Path``. May not exist yet;
        callers create with ``mkdir(parents=True, exist_ok=True)`` before
        writing.

        - ``user``           → ``user_base / artifact``
        - ``project_shared`` → ``project_root / ".memtomem" / artifact``
        - ``project_local``  → ``project_root / ".memtomem" / f"{artifact}.local"``

    Raises:
        ContextScopeError: When ``scope`` is a project tier but
            ``project_root`` is ``None``, or when ``scope`` is unknown.
    """
    if scope == "user":
        return (user_base / artifact).expanduser().resolve()
    if project_root is None:
        raise ContextScopeError(
            f"scope='{scope}' requires a project context (cwd has no .memtomem ancestor)."
        )
    if scope == "project_shared":
        return (project_root / ".memtomem" / artifact).resolve()
    if scope == "project_local":
        return (project_root / ".memtomem" / f"{artifact}.local").resolve()
    raise ContextScopeError(f"unsupported artifact scope: {scope!r}")


def list_artifact_scopes_present(
    artifact: ArtifactKind,
    project_root: Path,
    user_base: Path = DEFAULT_USER_ARTIFACT_BASE,
) -> list[TargetScope]:
    """Return scopes that have on-disk content for this artifact.

    Used by ``mm context diff --scope all`` and the web routes to know
    which tiers to walk when rendering multi-tier views. Empty
    directories count as "present" — same semantics as ``Path.is_dir()``.
    """
    present: list[TargetScope] = []
    for scope in ("user", "project_shared", "project_local"):
        scope_typed: TargetScope = scope  # type: ignore[assignment]
        try:
            d = canonical_artifact_dir(artifact, scope_typed, project_root, user_base)
        except ContextScopeError:
            continue
        if d.is_dir():
            present.append(scope_typed)
    return present


def project_root_from_artifact_path(path: Path) -> Path | None:
    """Walk parents of ``path`` looking for a ``.memtomem`` ancestor.

    Returns the parent of the first ``.memtomem`` ancestor directory
    (i.e. the project root that owns the canonical subtree), or
    ``None`` if ``path`` does not live under any ``.memtomem`` tree.

    The check is "is this ancestor literally named ``.memtomem``?", NOT
    "does this ancestor contain a ``.memtomem`` child?" — the latter
    would falsely return a project root for any source file under a
    project that happens to contain ``.memtomem`` (e.g. ``src/foo.py``
    in a memtomem-using repo).

    Lifted from the inline helper in ``cli/context_cmd.py:2084-2087``
    (memory-migrate's project-root detection) so non-memory artifact
    paths can use the same walk.

    The returned path is not resolved — callers that need symlink
    canonicalisation should ``.resolve()`` themselves.
    """
    path = path.expanduser()
    for ancestor in (path, *path.parents):
        if ancestor.name == ".memtomem":
            return ancestor.parent
    return None
