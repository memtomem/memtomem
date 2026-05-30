"""Regression: `mm init` settings step must surface every
``generate_all_settings`` status, not only ``ok``/``skipped`` (#1123 B7-4).

Before the fix, ``_step_settings`` looped over the per-runtime results and only
handled ``ok`` and ``skipped``; a ``needs_confirmation`` (host-write guard
refused) or any other status was silently dropped, so the user got no signal
that a target was not written.
"""

from __future__ import annotations

from pathlib import Path

import memtomem.cli.init_cmd as init_cmd
from memtomem.context.settings import SettingsSyncResult

from .helpers import set_home


def _drive_step_settings(tmp_path: Path, monkeypatch, results: dict) -> str:
    """Run ``_step_settings`` with the interactive bits stubbed and
    ``generate_all_settings`` replaced by ``results``; return captured stdout."""
    home = tmp_path / "home"
    # The step early-returns unless ~/.claude exists.
    (home / ".claude").mkdir(parents=True)
    set_home(monkeypatch, home)

    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)

    # Stub the interactive helpers (called as bare module names in init_cmd).
    monkeypatch.setattr(init_cmd, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(init_cmd, "nav_confirm", lambda *a, **k: True)

    # generate_all_settings is imported *locally* inside _step_settings, so it
    # must be patched on the source module, not on init_cmd.
    monkeypatch.setattr(
        "memtomem.context.settings.generate_all_settings",
        lambda project_root, scope: results,
    )

    import click

    buf: list[str] = []
    monkeypatch.setattr(click, "echo", lambda *a, **k: buf.append(a[0] if a else ""))
    monkeypatch.setattr(click, "secho", lambda msg="", *a, **k: buf.append(msg))

    state: dict = {"step_index": 0, "total_steps": 1}
    init_cmd._step_settings(state)
    return "\n".join(str(x) for x in buf)


def test_needs_confirmation_is_surfaced(tmp_path, monkeypatch):
    out = _drive_step_settings(
        tmp_path,
        monkeypatch,
        {
            "claude": SettingsSyncResult(
                status="needs_confirmation",
                reason="host write refused",
                target=Path("~/.claude/settings.json"),
            )
        },
    )
    assert "needs confirmation" in out.lower()
    assert "claude" in out


def test_unknown_status_is_not_swallowed(tmp_path, monkeypatch):
    out = _drive_step_settings(
        tmp_path,
        monkeypatch,
        {"claude": SettingsSyncResult(status="error", reason="boom")},
    )
    # The defensive else-branch echoes the raw status + reason.
    assert "error" in out.lower()
    assert "boom" in out


def test_ok_and_skipped_still_reported(tmp_path, monkeypatch):
    # Build the target from ``tmp_path`` and expect the same ``str(target)`` the
    # ok-branch renders (``_step_settings`` does ``f"Merged → {r.target}"``).
    # A hardcoded ``"/tmp/claude.json"`` literal mismatched the backslash
    # separator on Windows (``str(Path("/tmp/claude.json"))`` → ``\tmp\claude.json``),
    # failing the assertion on windows-latest.
    target = tmp_path / "claude.json"
    out = _drive_step_settings(
        tmp_path,
        monkeypatch,
        {
            "claude": SettingsSyncResult(status="ok", target=target),
            "codex": SettingsSyncResult(status="skipped", reason="not installed"),
        },
    )
    # Assert on the loop's own output, not the static intro line ("Hooks are
    # merged into ~/.claude/settings.json additively.") which contains the word
    # "merged" regardless of whether the ``ok`` branch ran — a bare
    # ``"merged" in out.lower()`` would pass even if that branch regressed.
    assert f"Merged → {target}" in out
    assert "skipped codex: not installed" in out
