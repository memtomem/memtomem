"""MCP parity pin: ``mem_context_generate`` / ``mem_context_diff`` /
``mem_context_sync`` surface cross-tier duplicate-hook warnings on the
``settings`` include path (#1123 B5-3).

The CLI emits these via ``_print_duplicate_tier_warnings`` (ADR-0010 §4) inside
the real generate / diff / sync workflow. The MCP settings branches dropped
them, so an MCP caller never learned that a memtomem-managed hook was
duplicated in a non-active tier. Each test asserts the MCP output contains the
exact warning line the MCP surface emits — ``format_warning`` over the
path-redacted duplicate record (#1550/#1556) — so a future revert that forgets
to thread the warnings fails CI immediately.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from memtomem.context import error_redact
from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_doctor import detect_duplicate_tiers, format_warning
from memtomem.server.tools.context import (
    _redact_reason,
    mem_context_diff,
    mem_context_generate,
    mem_context_sync,
)

from .helpers import set_home


def _bundled_hook() -> dict:
    """A canonical-shape memtomem-managed hook record."""
    return {
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": "mm session start", "timeout": 5000}],
            }
        ]
    }


def _write_settings(path: Path, hooks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def dup_project(tmp_path, monkeypatch):
    """Project whose user tier duplicates a canonical memtomem-managed hook.

    ``.memtomem/settings.json`` holds the canonical signatures; the same hook
    is planted in the user tier (``~/.claude/settings.json``). With the active
    settings scope = ``project_shared``, the user tier is a non-active tier
    holding a canonical-matched hook → reported as a duplicate.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".claude").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    # Freeze the display-redaction home to the pytest tmp root so the
    # ``$HOME`` → ``~`` collapse in ``redact_message`` fires deterministically
    # on every platform. Windows CI hits it naturally (the pytest tmp dir
    # lives under the real, import-frozen ``_HOME``) while POSIX tmp dirs sit
    # outside it — so asserting the raw ``format_warning`` string here used to
    # pass on POSIX and fail on Windows once #1556 redacted the emission.
    monkeypatch.setattr(error_redact, "_HOME", str(tmp_path))

    _write_settings(project / CANONICAL_SETTINGS_FILE, _bundled_hook())
    _write_settings(home / ".claude" / "settings.json", _bundled_hook())
    monkeypatch.chdir(project)

    # Sanity: the fixture actually produced a duplicate to surface.
    dups = detect_duplicate_tiers(project, active_scope="project_shared")
    assert dups, "fixture did not create a cross-tier duplicate"
    # Mirror the MCP emission exactly: the tier path is redacted BEFORE
    # formatting (path-only, so the 200-char ``redact_message`` cap cannot
    # truncate the migrate hint — see ``_settings_dup_tier_warnings``).
    redacted = replace(dups[0], path=Path(_redact_reason(str(dups[0].path), project)))
    expected = format_warning(redacted, active_scope="project_shared")
    assert str(tmp_path) not in expected, "home collapse did not fire — pin would be vacuous"
    return project, expected


@pytest.mark.anyio
async def test_generate_surfaces_cross_tier_dup_warning(dup_project):
    _project, expected = dup_project
    out = await mem_context_generate(include="settings", scope="project_shared")
    assert expected in out


@pytest.mark.anyio
async def test_diff_surfaces_cross_tier_dup_warning(dup_project):
    _project, expected = dup_project
    out = await mem_context_diff(include="settings", scope="project_shared")
    assert expected in out


@pytest.mark.anyio
async def test_sync_surfaces_cross_tier_dup_warning(dup_project):
    _project, expected = dup_project
    out = await mem_context_sync(include="settings", scope="project_shared")
    assert expected in out


@pytest.mark.anyio
async def test_no_dup_warning_when_only_active_tier(tmp_path, monkeypatch):
    """Negative pin: a hook only in the active (canonical) tier is not a
    duplicate, so no warning is emitted."""
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".claude").mkdir()
    set_home(monkeypatch, tmp_path / "home")
    _write_settings(project / CANONICAL_SETTINGS_FILE, _bundled_hook())
    monkeypatch.chdir(project)

    out = await mem_context_diff(include="settings", scope="project_shared")
    assert "duplicat" not in out.lower()
