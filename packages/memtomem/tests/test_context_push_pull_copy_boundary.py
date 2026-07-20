"""ADR-0030 PR-H2 — the Push/Pull copy sweep's boundary, encoded as tests.

PR-E (#1854) renamed the user-facing vocabulary for the UI, CLI help, and docs
and deliberately froze every machine-readable identifier; PR-H2 finished the job
for engine / route / MCP **output copy**. The risk a rename PR carries is not
that it renames too little — it is that a later sweep renames one string too
many. Three classes must never move together, so they are pinned here:

1. **Human-facing action copy** — moved to Push/Pull (asserted via behavior).
2. **Frozen wire identifiers** — reason codes, surface ids, route paths, CLI
   command strings. Their human text changed *around* them; they did not.
3. **Shared vocabulary** — project enrollment / pause / resume is one mechanism
   for BOTH gateway Push and Hooks Sync, so it stays "sync"; relational drift
   state ("in sync" / "out of sync") is status, not the action, so it stays too.
   Renaming either leaks into a sibling feature — the concrete failure PR-E hit
   (``test_settings_hooks_sync_409``), which is why those two live-fire tests
   remain the primary guard and this file only pins the vocabulary itself.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from memtomem.context import _atomic_reverse, _gate_a, commands, privacy_scan, skills
from memtomem.server.tools import context as mcp_context
from memtomem.web.routes import context_mcp_servers, context_skills, context_sync_all

# Modules whose *output copy* PR-H2 swept.
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


def test_no_stale_sync_import_action_copy_remains() -> None:
    """The specific strings PR-H2 replaced must not reappear."""
    stale = (
        '"Synced:\\n"',
        '"Nothing to sync."',
        "Imported skills:",
        '"Sync skills"',
        '"Sync MCP servers"',
        "already imported from",
        "re-run the import to retry",
        "before re-running sync.",
    )
    for mod in _SWEPT:
        src = _src(mod)
        for s in stale:
            assert s not in src, f"{Path(inspect.getfile(mod)).name} still carries {s!r}"  # type: ignore[arg-type]


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
