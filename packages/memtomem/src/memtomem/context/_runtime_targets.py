"""ADR-0011 PR-E runtime fan-out resolver — explicit per-(artifact, runtime, scope) table.

Sibling of :mod:`memtomem.context.scope_resolver` (canonical-side). The
canonical resolver answers "where does the source-of-truth file live?";
this module answers "where does it get fanned out to for runtime X?".
The two MUST stay separate — earlier plan revisions conflated them and
got the test expectations wrong.

Design rules:

1. **Every (artifact, runtime, scope) tuple is explicit.** No string
   interpolation, no ``.get(default=...)``. Lookup is via direct
   dict access; missing tuples raise ``KeyError`` (fail-loud per
   ``feedback_defensive_noise.md``).
2. **`None` means "no fan-out by design"** — emit
   ``skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME``. This applies to
   every ``project_local`` entry (ADR §3 — gitignored draft tier
   has no runtime equivalent) and to a few runtime-specific cases
   (e.g. Codex CLI prompts are user-tier only by design).
3. **Project-tier entries store the project-relative tail** as a
   ``Path`` (e.g. ``Path(".claude/agents")``); ``runtime_fanout_root``
   prepends ``project_root`` at call-time. User-tier entries store
   the absolute home-relative path (e.g. ``Path("~/.claude/agents")``).
   Callers receive a fully expanded absolute ``Path``.
4. **Codex commands** are reserved as user-only (per ``commands.py:5``
   docstring) even though no ``CodexCommandsGenerator`` is registered
   yet. Tests assert ``project_*`` entries are ``None``; the user
   entry returns ``~/.codex/prompts`` so a future
   ``CodexCommandsGenerator`` can land without table churn.
5. **Codex skills user-tier path is ``~/.agents/skills``** — verified
   externally against the Agent Skills Open Specification (Anthropic
   2025-12 release, OpenAI Codex adoption documented in-repo at
   ``context/skills.py:12-13``). The user-scope path follows the same
   spec-aligned convention as the project-scope tail
   (``.agents/skills`` — see ``context/detector.py:34-39`` for the
   in-repo anchor). Both paths are vendor-neutral by design so a
   skill installed once is discovered by Claude / Gemini / Codex
   through their respective alias resolution.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem.config import TargetScope
from memtomem.context._names import InvalidNameError, is_internal_artifact_dir, validate_name
from memtomem.context.scope_resolver import ArtifactKind

logger = logging.getLogger(__name__)


# Sentinel for "no fan-out by design — caller should emit
# NO_PROJECT_FANOUT_FOR_RUNTIME". Distinct from a missing key (which
# is a programming error and raises KeyError).
NO_FANOUT: None = None


# Project-relative tails (joined with project_root at call-time).
_CLAUDE_AGENTS_REL = Path(".claude/agents")
_GEMINI_AGENTS_REL = Path(".gemini/agents")
_CODEX_AGENTS_REL = Path(".codex/agents")
_KIMI_AGENTS_REL = Path(".kimi/agents")

_CLAUDE_SKILLS_REL = Path(".claude/skills")
_GEMINI_SKILLS_REL = Path(".gemini/skills")
_CODEX_SKILLS_REL = Path(".agents/skills")  # NOT .codex/skills — see skills.py module docstring
_KIMI_SKILLS_REL = Path(".kimi/skills")

_CLAUDE_COMMANDS_REL = Path(".claude/commands")
_GEMINI_COMMANDS_REL = Path(".gemini/commands")


# Full table — every (artifact, runtime, scope) tuple populated.
# ``None`` = "no fan-out by design" (loud-emit-on-call). Lookup uses
# direct dict access; missing keys raise KeyError (fail-loud).
RUNTIME_FANOUT_TABLE: dict[tuple[ArtifactKind, str, TargetScope], Path | None] = {
    # ── agents ───────────────────────────────────────────────────────
    ("agents", "claude", "user"): Path("~/.claude/agents"),
    ("agents", "claude", "project_shared"): _CLAUDE_AGENTS_REL,
    ("agents", "claude", "project_local"): NO_FANOUT,  # ADR §3
    ("agents", "gemini", "user"): Path("~/.gemini/agents"),
    ("agents", "gemini", "project_shared"): _GEMINI_AGENTS_REL,
    ("agents", "gemini", "project_local"): NO_FANOUT,
    ("agents", "codex", "user"): Path("~/.codex/agents"),  # agents.py:12
    ("agents", "codex", "project_shared"): _CODEX_AGENTS_REL,
    ("agents", "codex", "project_local"): NO_FANOUT,
    ("agents", "kimi", "user"): Path("~/.kimi/agents"),
    ("agents", "kimi", "project_shared"): _KIMI_AGENTS_REL,
    ("agents", "kimi", "project_local"): NO_FANOUT,
    # ── skills ───────────────────────────────────────────────────────
    ("skills", "claude", "user"): Path("~/.claude/skills"),
    ("skills", "claude", "project_shared"): _CLAUDE_SKILLS_REL,
    ("skills", "claude", "project_local"): NO_FANOUT,
    ("skills", "gemini", "user"): Path("~/.gemini/skills"),
    ("skills", "gemini", "project_shared"): _GEMINI_SKILLS_REL,
    ("skills", "gemini", "project_local"): NO_FANOUT,
    ("skills", "codex", "user"): Path(
        "~/.agents/skills"
    ),  # Agent Skills Open Spec — see docstring rule 5
    ("skills", "codex", "project_shared"): _CODEX_SKILLS_REL,
    ("skills", "codex", "project_local"): NO_FANOUT,
    ("skills", "kimi", "user"): Path("~/.kimi/skills"),
    ("skills", "kimi", "project_shared"): _KIMI_SKILLS_REL,
    ("skills", "kimi", "project_local"): NO_FANOUT,
    # ── commands ─────────────────────────────────────────────────────
    ("commands", "claude", "user"): Path("~/.claude/commands"),
    ("commands", "claude", "project_shared"): _CLAUDE_COMMANDS_REL,
    ("commands", "claude", "project_local"): NO_FANOUT,
    ("commands", "gemini", "user"): Path("~/.gemini/commands"),
    ("commands", "gemini", "project_shared"): _GEMINI_COMMANDS_REL,
    ("commands", "gemini", "project_local"): NO_FANOUT,
    # Codex commands: user-only by design (commands.py:5). project_*
    # entries reserved (no CodexCommandsGenerator registered yet but
    # keeping the table shape uniform across runtimes).
    ("commands", "codex", "user"): Path("~/.codex/prompts"),
    ("commands", "codex", "project_shared"): NO_FANOUT,
    ("commands", "codex", "project_local"): NO_FANOUT,
    # Kimi CLI has file-backed skills and agent files, but the linked Kimi
    # docs do not define a project/user custom-command file format.
    ("commands", "kimi", "user"): NO_FANOUT,
    ("commands", "kimi", "project_shared"): NO_FANOUT,
    ("commands", "kimi", "project_local"): NO_FANOUT,
}


# Closed set of runtime keys this module knows about. Callers can use
# this to iterate when they need "every runtime for this artifact".
KNOWN_RUNTIMES: tuple[str, ...] = ("claude", "gemini", "codex", "kimi")


# Per-artifact-kind Pull source vocabulary (ADR-0030 §12). This is the
# FIRST-CLASS answer to "which runtimes can an artifact be imported FROM",
# which is NOT the same as ``KNOWN_RUNTIMES``: the extract engines only
# read Claude and Gemini for agents/commands (codex/kimi are export-only
# renderers — Codex CLI prompts are user-only by design and Kimi has no
# importer branch). Pickers, ``source_runtime`` validation, and the pull
# preview all key off this table so a UI can never offer an un-pullable
# runtime and the engines can never drift from it. The order matters: it
# is the deterministic first-wins priority for the batch import path.
IMPORT_SOURCE_RUNTIMES: dict[ArtifactKind, tuple[str, ...]] = {
    "skills": KNOWN_RUNTIMES,
    "agents": ("claude", "gemini"),
    "commands": ("claude", "gemini"),
}


def resolve_import_runtimes(artifact: ArtifactKind, source_runtime: str | None) -> tuple[str, ...]:
    """Return the runtime scan order for an import, honoring ``source_runtime``.

    ``source_runtime is None`` (the default) returns the full per-kind
    priority tuple — byte-compatible with the pre-ADR-0030 hardcoded
    loops. A non-None value must be pull-eligible for this artifact kind
    (:data:`IMPORT_SOURCE_RUNTIMES`); otherwise raise ``ValueError`` with
    a message that names the export-only case explicitly so the CLI/web
    surfaces can translate it without re-deriving the reason.
    """
    eligible = IMPORT_SOURCE_RUNTIMES[artifact]
    if source_runtime is None:
        return eligible
    if source_runtime in eligible:
        return (source_runtime,)
    if source_runtime in KNOWN_RUNTIMES:
        raise ValueError(
            f"runtime {source_runtime!r} is export-only for {artifact} — it cannot be "
            f"pulled from; choose one of: {', '.join(eligible)}"
        )
    raise ValueError(f"unknown runtime {source_runtime!r}; choose one of: {', '.join(eligible)}")


def runtime_fanout_root(
    artifact: ArtifactKind,
    runtime: str,
    scope: TargetScope,
    project_root: Path | None,
) -> Path | None:
    """Resolve where to fan out an artifact for a given runtime + scope.

    Args:
        artifact: ``agents`` / ``skills`` / ``commands``.
        runtime: One of :data:`KNOWN_RUNTIMES`.
        scope: ``user`` / ``project_shared`` / ``project_local``.
        project_root: Required for ``project_*`` scopes; ignored for ``user``.
            Callers that have a project context should pass it; ``None`` for
            project tiers raises ``ValueError`` (fail-loud — silently
            returning ``None`` would conflate "no project" with "no
            fan-out by design").

    Returns:
        Absolute, expanded ``Path`` to the fan-out root directory, or
        ``None`` when the (artifact, runtime, scope) tuple has no
        fan-out by design (caller emits
        ``skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME``).

    Raises:
        KeyError: When the ``(artifact, runtime, scope)`` tuple is not
            in :data:`RUNTIME_FANOUT_TABLE` — a programming error
            (unknown runtime or unknown artifact). Fail-loud.
        ValueError: When ``scope`` is ``project_shared`` /
            ``project_local`` but ``project_root`` is ``None``.
    """
    # Direct dict access — KeyError fail-loud on unknown tuple.
    entry = RUNTIME_FANOUT_TABLE[(artifact, runtime, scope)]
    if entry is None:
        return None
    if scope == "user":
        return entry.expanduser().resolve()
    # project_shared / project_local — entry is a project-relative tail.
    if project_root is None:
        raise ValueError(
            f"runtime_fanout_root({artifact!r}, {runtime!r}, {scope!r}) "
            "requires project_root for a project-tier scope."
        )
    return (project_root / entry).resolve()


class DiffRow(tuple):
    """One diff result row, optionally carrying a diagnostic ``reason``.

    Iterates and unpacks as the historical 3-tuple ``(runtime, name,
    status)`` — every existing positional consumer (CLI printers, MCP
    tools, web groupers, external callers of the public ``diff_*``
    functions) keeps working unchanged, and equality against a plain
    3-tuple still holds. Consumers that want diagnostics read
    ``row.reason`` (#1229 U7: the bare "parse error" status carried no
    file path or cause anywhere; "invalid name" rows ride the same
    field with the ``validate_name`` message).

    ``reason`` is raw, unsanitized engine text (exception messages embed
    absolute source paths) — the web routes AND the MCP context tools sanitize
    it at their wire boundary (``web/routes/context_gateway.sanitize_diff_reason``
    / ``context.error_redact.redact_engine_reason``), since both surface the
    reason beyond the host; only the CLI prints it for the local operator
    verbatim.
    """

    reason: str | None

    def __new__(cls, runtime: str, name: str, status: str, reason: str | None = None) -> "DiffRow":
        row = super().__new__(cls, (runtime, name, status))
        row.reason = reason
        return row

    @property
    def runtime(self) -> str:
        return self[0]

    @property
    def name(self) -> str:
        return self[1]

    @property
    def status(self) -> str:
        return self[2]

    def __getnewargs__(self) -> tuple[str, str, str, str | None]:
        # Pickle reconstructs via ``__new__(cls, *args)`` — without this the
        # 3-element tuple state reaches a 4-arg constructor and unpickling
        # fails, breaking public diff_* callers that cache rows or pass them
        # through multiprocessing (Codex review).
        return (self[0], self[1], self[2], self.reason)

    def __repr__(self) -> str:  # diagnostics show up in test failures
        return (
            f"DiffRow(runtime={self[0]!r}, name={self[1]!r}, "
            f"status={self[2]!r}, reason={self.reason!r})"
        )


def runtime_artifact_listing(
    artifact: ArtifactKind,
    runtime: str,
    project_root: Path | None,
    scope: TargetScope,
    *,
    file_suffix: str | None = None,
    dir_manifest: str | None = None,
) -> tuple[set[str], list[tuple[str, str]]]:
    """Return ``(valid_names, invalid_entries)`` under the runtime root.

    Replaces the per-module helpers ``_runtime_agent_names`` /
    ``_runtime_command_names`` and the inline runtime-listing in
    ``diff_skills`` (ADR-0011 PR-E3 cleanup item #4 — collapses three
    implementations onto one source of truth so the
    ``RUNTIME_FANOUT_TABLE`` shape is the only place a runtime root is
    spelled out).

    Pass exactly one of:

    * ``file_suffix`` (e.g. ``".md"`` / ``".toml"``) — return the
      ``stem`` of every regular file matching the suffix. Used for
      agents and commands fan-out where each artifact is one file.
    * ``dir_manifest`` (e.g. ``"SKILL.md"``) — return the ``name`` of
      every directory containing the named manifest file. Used for
      skills where each artifact is a directory tree.

    ``invalid_entries`` carries ``(raw_name, reason)`` pairs for entries
    that match the artifact shape but fail :func:`validate_name` — diff
    surfaces them as a dedicated ``"invalid name"`` status row (with the
    validation message as the row reason) instead of silently dropping
    them (#1229: an unmanaged runtime artifact used to be invisible, so
    the dashboard read fully in-sync while extract emitted a visible
    ``INVALID_NAME`` skip for the very same file). The names are raw,
    unsanitized strings — web rendering escapes them, and log emission
    keeps going through the structured ``extra`` dict (never the message).

    Returns ``(set(), [])`` when the table entry is ``NO_FANOUT`` or
    when the resolved root does not yet exist on disk (caller treats
    "no runtime listing" the same as "table says no fan-out").
    """
    if (file_suffix is None) == (dir_manifest is None):
        raise ValueError(
            "runtime_artifact_listing requires exactly one of file_suffix= or dir_manifest=."
        )
    root = runtime_fanout_root(artifact, runtime, scope, project_root)
    if root is None or not root.is_dir():
        return set(), []
    kind = f"{artifact[:-1]} name"
    names: set[str] = set()
    invalid: list[tuple[str, str]] = []
    if file_suffix is not None:
        entries = ((p.stem, p) for p in root.iterdir() if p.is_file() and p.suffix == file_suffix)
    else:
        assert dir_manifest is not None  # mypy narrow
        # Skip our own crash-leftover staging/move-aside trees (silently —
        # they are internal artifacts, not user content): they contain a
        # full SKILL.md mirror and would otherwise surface as phantom
        # "missing canonical" diff rows (#1229). Agents/commands leftovers
        # are ``.tmp`` *files*, already excluded by the suffix match above.
        entries = (
            (p.name, p)
            for p in root.iterdir()
            if p.is_dir() and (p / dir_manifest).is_file() and not is_internal_artifact_dir(p.name)
        )
    for raw_name, path in entries:
        try:
            names.add(validate_name(raw_name, kind=kind))
        except InvalidNameError as exc:
            invalid.append((raw_name, str(exc)))
            logger.warning(
                "Skipping invalid runtime artifact name",
                extra={
                    "artifact": artifact,
                    "runtime": runtime,
                    "scope": scope,
                    "runtime_path": str(path),
                    "artifact_name": raw_name,
                    "reason": str(exc),
                },
            )
    return names, sorted(invalid)


def runtime_artifact_names(
    artifact: ArtifactKind,
    runtime: str,
    project_root: Path | None,
    scope: TargetScope,
    *,
    file_suffix: str | None = None,
    dir_manifest: str | None = None,
) -> set[str]:
    """Valid-names-only view of :func:`runtime_artifact_listing` (back-compat)."""
    names, _invalid = runtime_artifact_listing(
        artifact,
        runtime,
        project_root,
        scope,
        file_suffix=file_suffix,
        dir_manifest=dir_manifest,
    )
    return names
