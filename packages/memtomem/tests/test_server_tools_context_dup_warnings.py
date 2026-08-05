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

The same surface carries the malformed-matcher axis (#1987), which duplicate
detection cannot see because ``_iter_signatures`` skips non-string matchers.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from memtomem.context import error_redact
from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_doctor import (
    detect_duplicate_tiers,
    find_malformed_matchers,
    format_malformed_warning,
    format_warning,
)
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


@pytest.fixture
def malformed_project(tmp_path, monkeypatch):
    """Project whose user tier holds a hook rule with a non-string matcher.

    The malformed axis (#1987) is invisible to duplicate detection —
    ``_iter_signatures`` skips non-string matchers — so it needs its own MCP
    parity pin, with the same path redaction the duplicate leg gets.
    """
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".claude").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(error_redact, "_HOME", str(tmp_path))

    _write_settings(project / CANONICAL_SETTINGS_FILE, _bundled_hook())
    _write_settings(
        home / ".claude" / "settings.json",
        {
            "SessionStart": [
                {
                    "matcher": ["Bash"],
                    "hooks": [{"type": "command", "command": "mm index", "timeout": 5000}],
                }
            ]
        },
    )
    monkeypatch.chdir(project)

    findings = find_malformed_matchers(project)
    assert findings, "fixture did not create a malformed matcher"
    redacted = replace(findings[0], path=Path(_redact_reason(str(findings[0].path), project)))
    expected = format_malformed_warning(redacted)
    assert str(tmp_path) not in expected, "home collapse did not fire — pin would be vacuous"
    return project, expected


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool",
    [mem_context_generate, mem_context_diff, mem_context_sync],
    ids=["generate", "diff", "sync"],
)
async def test_settings_surfaces_malformed_matcher_warning(malformed_project, tool):
    _project, expected = malformed_project
    out = await tool(include="settings", scope="project_shared")
    assert expected in out


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool",
    [mem_context_generate, mem_context_diff, mem_context_sync],
    ids=["generate", "diff", "sync"],
)
async def test_secret_shaped_event_is_redacted_in_warning(tmp_path, monkeypatch, tool):
    """A secret-shaped hook event never reaches the MCP wire verbatim (#2030).

    ``event`` is a settings dict key, so nothing stops it from carrying secret
    material; the path-only redaction the sibling pin mirrors would ship it
    unchanged to the calling agent's transcript. Expectations are hardcoded
    rather than recomputed through ``format_malformed_warning`` on purpose —
    mirroring the implementation would pass whether or not the scrub fires.
    """
    secret_event = "api_key=AKIA1234567890ABCDEF"
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".claude").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(error_redact, "_HOME", str(tmp_path))

    _write_settings(project / CANONICAL_SETTINGS_FILE, _bundled_hook())
    _write_settings(
        home / ".claude" / "settings.json",
        {
            secret_event: [
                {
                    "matcher": ["Bash"],
                    "hooks": [{"type": "command", "command": "mm index", "timeout": 5000}],
                }
            ]
        },
    )
    monkeypatch.chdir(project)

    out = await tool(include="settings", scope="project_shared")

    assert "non-string matcher (list)" in out, "fixture did not surface a malformed warning"
    assert "AKIA1234567890ABCDEF" not in out
    assert "event '<redacted: secret-shape>' rule #0" in out
    # The remediation suffix survives untruncated — the whole point of scrubbing
    # the value instead of routing the line through ``redact_message``.
    assert "`settings-migrate` to refuse to run." in out


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool",
    [mem_context_generate, mem_context_diff, mem_context_sync],
    ids=["generate", "diff", "sync"],
)
async def test_secret_shaped_canonical_event_is_redacted_in_drop_warning(
    tmp_path, monkeypatch, tool
):
    """Same scrub on the other malformed-warning producer (#2030 review).

    ``_drop_nonstring_matchers`` warns about a malformed rule in the *canonical*
    record, and its warnings reach the same MCP settings output — so redacting
    only the tier-tier warning would still ship a secret-shaped canonical event
    verbatim.
    """
    secret_event = "api_key=AKIA1234567890ABCDEF"
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    (project / ".claude").mkdir()
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(error_redact, "_HOME", str(tmp_path))

    _write_settings(
        project / CANONICAL_SETTINGS_FILE,
        {
            secret_event: [
                {
                    "matcher": ["Bash"],
                    "hooks": [{"type": "command", "command": "mm index", "timeout": 5000}],
                }
            ]
        },
    )
    monkeypatch.chdir(project)

    out = await tool(include="settings", scope="project_shared")

    assert "non-string matcher" in out, "fixture did not surface a drop warning"
    assert "AKIA1234567890ABCDEF" not in out
    assert "Hook rule under '<redacted: secret-shape>'" in out


@pytest.mark.anyio
@pytest.mark.parametrize(
    "tool",
    [mem_context_generate, mem_context_diff, mem_context_sync],
    ids=["generate", "diff", "sync"],
)
async def test_secret_shaped_event_never_reaches_mcp_wire(tmp_path, monkeypatch, tool):
    """Surface sweep: no settings warning ships the raw event (#2030 review).

    The malformed-matcher axis is only one producer. An event key with a
    perfectly *valid* matcher still reaches the per-runtime translators, which
    warn by name when the event has no Codex / Kimi / Gemini equivalent — a
    wider hole than the malformed one, since it needs no typo to trigger. The
    runtime dirs are planted so every translator actually runs; without them
    the targets are skipped as "runtime not installed" and the pin is vacuous.
    """
    secret_event = "api_key=AKIA1234567890ABCDEF"
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    for runtime_dir in (".claude", ".codex", ".gemini", ".kimi"):
        (project / runtime_dir).mkdir()
    set_home(monkeypatch, tmp_path / "home")
    monkeypatch.setattr(error_redact, "_HOME", str(tmp_path))

    _write_settings(
        project / CANONICAL_SETTINGS_FILE,
        {
            secret_event: [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "mm index", "timeout": 5000}],
                }
            ]
        },
    )
    monkeypatch.chdir(project)

    out = await tool(include="settings", scope="project_shared")

    for runtime in ("Codex", "Kimi", "Gemini"):
        assert f"no {runtime} equivalent" in out, f"{runtime} translator did not run"
    assert "AKIA1234567890ABCDEF" not in out
    assert out.count("Hook event '<redacted: secret-shape>'") == 3


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
