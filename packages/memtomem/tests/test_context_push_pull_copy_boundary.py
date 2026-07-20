"""ADR-0030 PR-H2 — the Push/Pull copy sweep's boundary, encoded as tests.

PR-E (#1854) renamed the user-facing vocabulary for the UI, CLI help, and docs
and deliberately froze every machine-readable identifier; PR-H2 finished the job
for engine / route / MCP **output copy**. The risk a rename PR carries is not
that it renames too little — it is that a later sweep renames one string too
many. Three classes must never move together, so they are pinned here:

1. **Human-facing action copy** — moved to Push/Pull.
2. **Frozen wire identifiers** — reason codes, surface ids, route paths, CLI
   command strings. Their human text changed *around* them; they did not.
3. **Shared vocabulary** — project enrollment / pause / resume is one mechanism
   for BOTH gateway Push and Hooks Sync, so it stays "sync"; relational drift
   state ("in sync" / "out of sync") is status, not the action, so it stays too.
   Renaming either leaks into a sibling feature — the concrete failure PR-E hit
   (``test_settings_hooks_sync_409``), which is why those two live-fire tests
   remain the primary guard and this file only pins the vocabulary itself.

**Scope, stated honestly.** This guard covers the surface PR-H2 swept; it does
NOT prove the trees are free of Sync/Import copy. Scanning is line-based over a
hand-maintained module tuple, so it cannot see a literal implicitly concatenated
across lines, an unlisted module, or a hyphenated form no pattern names. A known
tail remains — persisted ``snapshot_note`` version-history text, the
``mem_context_init`` MCP tool description, several published OpenAPI
descriptions, and ``reverse-sync`` — tracked in the follow-up issue along with
replacing this scanner with a full ``ast`` walk. Read a green run as "the swept
surface has not regressed", never as "the sweep is complete".

Exemptions are deliberately narrow, because each one is a place a regression
could hide: ``#`` comments (never user-facing), docstrings in non-route modules
(developer documentation — resolved through ``ast`` so the whole span is
exempt, not just the opening line), and §-citations whose section title is
QUOTED (the words are the identifier).
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from memtomem.context import (
    _atomic_reverse,
    _gate_a,
    _validation_seed,
    commands,
    mcp_servers_copy,
    privacy_scan,
    skills,
)
from memtomem.server.tools import context as mcp_context
from memtomem.web.routes import (
    _atomic_kind,
    _errors,
    context_agents,
    context_commands,
    context_mcp_servers,
    context_skills,
    context_sync_all,
)

# Every module whose *output copy* PR-H2 swept. This tuple is the sweep's
# coverage contract: the first pass enumerated only the per-kind skills /
# mcp-servers / sync-all routes and silently missed ``_atomic_kind`` — the
# GENERIC agents+commands route module — leaving half the routes saying
# "Sync"/"Import". A module absent from this tuple is a module the stale-string
# guard below cannot police, so add new output-carrying modules here.
_SWEPT = (
    _atomic_reverse,
    _gate_a,
    commands,
    privacy_scan,
    skills,
    mcp_context,
    context_skills,
    context_mcp_servers,
    context_sync_all,
    _atomic_kind,
    context_agents,
    context_commands,
    _errors,
    mcp_servers_copy,
    _validation_seed,
)


def _src(mod: object) -> str:
    return Path(inspect.getfile(mod)).read_text(encoding="utf-8")  # type: ignore[arg-type]


# ── 1. the action copy moved ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mod", "needle"),
    [
        (mcp_context, '"Pushed:\\n"'),
        (mcp_context, '"Nothing to push."'),
        (mcp_context, "Pulled skills:"),
        (context_skills, '"Push skills"'),
        (context_mcp_servers, '"Push MCP servers"'),
        (context_sync_all, "Push All is a project-tier action"),
        (_atomic_reverse, "already pulled from"),
        (privacy_scan, "before re-running the push."),
    ],
)
def test_action_copy_uses_push_pull(mod: object, needle: str) -> None:
    assert needle in _src(mod)


#: Action-copy shapes that must not survive anywhere in :data:`_SWEPT`.
#: Deliberately PATTERNS, not the exact strings the first pass happened to
#: replace: an exact-string list is only ever as complete as the enumeration
#: that produced it, and that enumeration missed ~20 sites. These catch the
#: *shape* — ``"Sync <kind>"`` / ``"Import <kind>"`` / ``… to import`` — so a
#: newly added route cannot reintroduce the old vocabulary unnoticed.
_STALE_ACTION_COPY = (
    r'"Sync(ed)?[: ]',
    r'f"Sync ',
    r'"Import(ed)? ',
    r'f"Import ',
    r"\bto import\b",
    r"already imported from",
    r"re-run (the import|sync) to retry",
    r"re-run sync\.",
    r"before re-running sync\.",
    r"blocked this (sync|import):",
    r"tier to import into",
    # Case-insensitive shapes the first guard missed because every pattern was
    # anchored on a capitalised word: the timeout messages interpolate the kind
    # (``f"{spec.kind.capitalize()} import timed out …"``), so the literal in
    # source is lowercase.
    r"(?i)\b(sync|import) timed out",
    # OpenAPI descriptions — published as the endpoint's docs.
    r"(?i)preview the import\b",
    r"would-import\b",
    r"(?i)per-type sync phase|five-phase sync\b",
    # UI breadcrumbs naming a button whose label is now Push/Pull.
    r"→ Sync\)",
    # Validation-manifest action labels mirroring the UI.
    r'"action": "(Sync|Import)"',
    r"they sync from the working file",
)


#: A module docstring / function docstring is user-facing ONLY in the route
#: layer, where FastAPI publishes it as the endpoint's OpenAPI description. An
#: engine function's docstring describes the callable to a developer, so
#: ``"""Import one runtime's *.md files…"""`` on ``_atomic_reverse`` is correct
#: prose about a reverse-import mechanism and is NOT swept.
_DOCSTRING_IS_USER_FACING = (
    context_skills,
    context_mcp_servers,
    context_sync_all,
    _atomic_kind,
    context_agents,
    context_commands,
)


def _docstring_line_numbers(mod: object) -> frozenset[int]:
    """Every line occupied by a docstring in *mod*, via ``ast``.

    Matching only lines that *start* with ``\"\"\"`` would exempt a multi-line
    docstring's opening line and then scan its body — so an engine docstring
    whose second line said "to import" would fail an unrelated pattern with a
    confusing message. Walking the AST gives the true span.
    """
    import ast

    src = _src(mod)
    tree = ast.parse(src)
    lines: set[int] = set()
    targets = [tree] + [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node in targets:
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        body = node.body[0]  # the docstring Expr — get_docstring guarantees it
        lines.update(range(body.lineno, (body.end_lineno or body.lineno) + 1))
    return frozenset(lines)


def _is_comment_line(line: str) -> bool:
    """A ``#`` comment is developer text and never reaches a user.

    ``# ``dry_run`` records the would-import target`` correctly describes the
    reverse-import mechanism to the next maintainer; only string literals and
    route docstrings (which FastAPI publishes) are user-facing copy.
    """
    return line.lstrip().startswith("#")


def _is_document_citation(line: str) -> bool:
    """A reference to a QUOTED ADR / issue section title is a proper noun.

    ``ADR-0021 §"Sync orchestration"`` names a section that exists under that
    title; renaming it would break the citation. Same reason the frozen wire ids
    are exempt — an identifier, not prose the user acts on.

    Deliberately narrow: an earlier version exempted the whole line for ANY
    ``ADR-NNNN §`` citation, which would have let a real regression like
    ``"… tier to import into (ADR-0011 §3)"`` through. Only a §-citation whose
    section is QUOTED counts, since that is the case where the words themselves
    are the identifier.
    """
    import re

    return bool(re.search(r"§\s*\"", line))


@pytest.mark.parametrize("pattern", _STALE_ACTION_COPY)
def test_no_stale_sync_import_action_copy_remains(pattern: str) -> None:
    """No swept module may carry Sync/Import *action* copy.

    Shape-matched rather than exact-matched — see :data:`_STALE_ACTION_COPY`.
    Docstrings are policed only in the route layer
    (:data:`_DOCSTRING_IS_USER_FACING`), where they become OpenAPI text.
    """
    import re

    offenders = []
    for mod in _SWEPT:
        policed_docstrings = mod in _DOCSTRING_IS_USER_FACING
        exempt_lines = frozenset() if policed_docstrings else _docstring_line_numbers(mod)
        name = Path(inspect.getfile(mod)).name  # type: ignore[arg-type]
        for i, line in enumerate(_src(mod).splitlines(), 1):
            if _is_comment_line(line) or i in exempt_lines:
                continue
            if _is_document_citation(line):
                continue
            if re.search(pattern, line):
                offenders.append(f"{name}:{i}: {line.strip()[:70]}")
    assert not offenders, "stale action copy:\n" + "\n".join(offenders)


# ── 2. frozen wire identifiers did NOT move ───────────────────────────────────


@pytest.mark.parametrize(
    ("mod", "wire_id"),
    [
        # surface ids — Gate A audit attribution.
        (mcp_context, '"mcp_context_sync"'),
        (context_skills, '"web_context_skills_sync"'),
        # route paths.
        (context_sync_all, '"/context/sync-all"'),
        # the follow-up command the transfer result prints stays runnable.
        (mcp_context, "mm context sync"),
    ],
)
def test_wire_identifiers_are_frozen(mod: object, wire_id: str) -> None:
    """Human text moved around these; the identifiers themselves must not."""
    assert wire_id in _src(mod)


@pytest.mark.parametrize(
    ("module_path", "code"),
    [
        # Skip codes: the human reason moved to "already pulled from …", the
        # code it travels with did not.
        ("memtomem.context._skip_reasons", '"already_imported"'),
        ("memtomem.context._skip_reasons", '"in_sync"'),
        # Enrollment codes live with the projects engine + its routes.
        ("memtomem.context.projects", '"sync_paused"'),
        ("memtomem.context.projects", '"sync_not_enrolled"'),
    ],
)
def test_reason_code_constants_keep_import_sync_spelling(module_path: str, code: str) -> None:
    """The skip-code vocabulary is wire, not copy — renaming it would break
    every consumer that branches on ``reason_code``."""
    import importlib

    src = _src(importlib.import_module(module_path))
    assert code in src, f"frozen reason_code {code} disappeared from {module_path}"


# ── 3. shared + relational vocabulary stays "sync" ────────────────────────────


def test_enrollment_vocabulary_stays_sync() -> None:
    """Enrollment / pause / resume is shared with Hooks Sync (PR-E boundary).

    ``settings-hooks-watchdog.js`` renders the same family, so renaming these to
    "push" leaks into a feature this campaign never touched.
    """
    from memtomem.web.routes import context_projects

    src = _src(context_projects)
    assert "not enrolled for sync" in src
    assert "enrollment paused" in src or "Resume sync" in src
    assert "not enrolled for push" not in src


def test_relational_drift_state_stays_sync() -> None:
    """ "in sync" / "out of sync" is status vocabulary (ADR-0030 §4), not the
    action verb — it describes a relation, so Push/Pull does not apply."""
    src = _src(_atomic_reverse)
    assert "in sync" in src
    assert "in push" not in src and "out of push" not in src
