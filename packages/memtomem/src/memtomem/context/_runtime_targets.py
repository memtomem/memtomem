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
5. **Codex skills user-tier path is a TBD** — Codex's own docs are
   ambiguous between ``~/.codex/skills`` and ``~/.agents/skills``.
   We mirror the project-scope path (``.agents/skills``) into the
   user tier as a placeholder; revise in PR-E follow-up if the
   Codex CLI's user-scope skill discovery is confirmed elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.config import TargetScope
from memtomem.context.scope_resolver import ArtifactKind


# Sentinel for "no fan-out by design — caller should emit
# NO_PROJECT_FANOUT_FOR_RUNTIME". Distinct from a missing key (which
# is a programming error and raises KeyError).
NO_FANOUT: None = None


# Project-relative tails (joined with project_root at call-time).
_CLAUDE_AGENTS_REL = Path(".claude/agents")
_GEMINI_AGENTS_REL = Path(".gemini/agents")
_CODEX_AGENTS_REL = Path(".codex/agents")

_CLAUDE_SKILLS_REL = Path(".claude/skills")
_GEMINI_SKILLS_REL = Path(".gemini/skills")
_CODEX_SKILLS_REL = Path(".agents/skills")  # NOT .codex/skills — see skills.py:80

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
    # ── skills ───────────────────────────────────────────────────────
    ("skills", "claude", "user"): Path("~/.claude/skills"),
    ("skills", "claude", "project_shared"): _CLAUDE_SKILLS_REL,
    ("skills", "claude", "project_local"): NO_FANOUT,
    ("skills", "gemini", "user"): Path("~/.gemini/skills"),
    ("skills", "gemini", "project_shared"): _GEMINI_SKILLS_REL,
    ("skills", "gemini", "project_local"): NO_FANOUT,
    ("skills", "codex", "user"): Path("~/.agents/skills"),  # TBD per docstring rule 5
    ("skills", "codex", "project_shared"): _CODEX_SKILLS_REL,
    ("skills", "codex", "project_local"): NO_FANOUT,
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
}


# Closed set of runtime keys this module knows about. Callers can use
# this to iterate when they need "every runtime for this artifact".
KNOWN_RUNTIMES: tuple[str, ...] = ("claude", "gemini", "codex")


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
