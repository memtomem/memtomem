"""A settings write may not follow ``$HOME`` after its caller is gone (#2211).

``generate_all_settings`` runs in ``asyncio.to_thread``, and every user-scope
target is anchored on the ambient ``$HOME``. ``asyncio.to_thread`` cannot be
cancelled, so a request that times out leaves the worker running — and before
the fix that worker resolved its target *when it got there*, which meant a
different home than the caller had. In the suite that showed up as the #1903
home guard reporting a write to the developer's real ``~/.claude/settings.json``
and blaming whichever unrelated test happened to be running when it landed.

These tests move a stand-in "later" home into place while the worker is
blocked, so nothing here can touch a real home even if the pin regresses.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from memtomem.context import settings as settings_mod


CANONICAL = '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}'  # noqa: E501


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".memtomem").mkdir(parents=True)
    (root / ".memtomem" / "settings.json").write_text(CANONICAL)
    return root


class TestHostHomeSnapshot:
    def test_falls_back_to_the_live_home_when_nothing_is_pinned(self, monkeypatch, tmp_path):
        """Synchronous callers (CLI, detectors) must keep reading the env."""
        monkeypatch.setenv("HOME", str(tmp_path / "live"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "live"))
        assert settings_mod.host_home() == Path(str(tmp_path / "live"))

    def test_pin_wins_over_a_later_env_change(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path / "at-dispatch"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "at-dispatch"))
        with settings_mod.pinned_host_homes():
            monkeypatch.setenv("HOME", str(tmp_path / "later"))
            monkeypatch.setenv("USERPROFILE", str(tmp_path / "later"))
            assert settings_mod.host_home() == Path(str(tmp_path / "at-dispatch"))
        # The token reset restores the live reading for the next caller.
        assert settings_mod.host_home() == Path(str(tmp_path / "later"))

    def test_kimi_home_is_snapshotted_separately(self, monkeypatch, tmp_path):
        """``$KIMI_CODE_HOME`` wins over ``$HOME``, so it needs its own capture."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi-at-dispatch"))
        with settings_mod.pinned_host_homes():
            monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "kimi-later"))
            assert settings_mod.host_kimi_home() == Path(str(tmp_path / "kimi-at-dispatch"))

    @pytest.mark.parametrize(
        ("generator", "home_dir"),
        [
            (settings_mod.ClaudeSettingsGenerator, ".claude"),
            (settings_mod.CodexSettingsGenerator, ".codex"),
            (settings_mod.GeminiSettingsGenerator, ".gemini"),
            (settings_mod.KimiSettingsGenerator, ".kimi-code"),
        ],
    )
    def test_target_and_availability_both_follow_the_pin(
        self, monkeypatch, tmp_path, generator, home_dir
    ):
        """The probe decides *whether* to write; it must agree with the target.

        A probe left on the live home could green-light a write whose target
        the pin has already moved somewhere else — so every generator is
        covered here, not just Claude's.
        """
        dispatch_home = tmp_path / "at-dispatch"
        (dispatch_home / home_dir).mkdir(parents=True)
        monkeypatch.setenv("HOME", str(dispatch_home))
        monkeypatch.setenv("USERPROFILE", str(dispatch_home))
        monkeypatch.delenv("KIMI_CODE_HOME", raising=False)

        gen = generator()
        project_root = tmp_path / "project"
        project_root.mkdir()

        with settings_mod.pinned_host_homes():
            monkeypatch.setenv("HOME", str(tmp_path / "later"))
            monkeypatch.setenv("USERPROFILE", str(tmp_path / "later"))
            assert gen.is_available(project_root) is True, "probe drifted off the pinned home"
            target = gen.target_file(project_root, "user")
            assert target is not None
            assert dispatch_home in target.parents, f"{target} escaped the pinned home"


class TestOrphanedWorkerWritesToTheDispatchHome:
    """The end-to-end shape: cancel the caller, move ``$HOME``, release the worker."""

    @staticmethod
    def _blocking_target_file(entered: threading.Event, release: threading.Event):
        real = settings_mod.ClaudeSettingsGenerator.target_file

        def _patched(self, project_root, scope):
            # Only the worker: duplicate detection resolves targets on the
            # calling thread too, and blocking there would stall the loop.
            if threading.current_thread() is threading.main_thread():
                return real(self, project_root, scope)
            entered.set()
            release.wait(timeout=10)
            return real(self, project_root, scope)

        return real, _patched

    async def test_a_cancelled_sync_writes_to_the_home_it_dispatched_with(
        self, tmp_path, monkeypatch
    ):
        from memtomem.web.routes import settings_sync

        dispatch_home = tmp_path / "dispatch-home"
        later_home = tmp_path / "later-home"
        (dispatch_home / ".claude").mkdir(parents=True)
        (later_home / ".claude").mkdir(parents=True)
        project_root = _project(tmp_path)

        entered, release = threading.Event(), threading.Event()
        real, patched = self._blocking_target_file(entered, release)
        monkeypatch.setattr(settings_mod.ClaudeSettingsGenerator, "target_file", patched)

        # The Kimi home comes from ``$KIMI_CODE_HOME`` when set, so redirecting
        # only $HOME would leave a developer's real Kimi config as a live write
        # target — the very drift under test.
        monkeypatch.setenv("KIMI_CODE_HOME", str(dispatch_home / ".kimi-code"))
        monkeypatch.setenv("HOME", str(dispatch_home))
        monkeypatch.setenv("USERPROFILE", str(dispatch_home))

        # Join the worker deterministically instead of sleeping: it is the
        # *whole* call finishing, not the first target's file appearing, that
        # says every generator has chosen its target.
        worker_done = threading.Event()
        real_generate = settings_mod.generate_all_settings

        def _generate_and_signal(*args, **kwargs):
            try:
                return real_generate(*args, **kwargs)
            finally:
                worker_done.set()

        monkeypatch.setattr(settings_sync, "generate_all_settings", _generate_and_signal)

        task = asyncio.create_task(
            settings_sync._sync_settings_core(project_root, "user", allow_host_writes=True)
        )
        try:
            await asyncio.to_thread(entered.wait, 10)
            # This is what the route's ``asyncio.timeout`` does.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The originating test ends here and ``monkeypatch`` restores the
            # real home; a later test is now running under a different one.
            os.environ["HOME"] = str(later_home)
            os.environ["USERPROFILE"] = str(later_home)

            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), "orphaned worker never finished"
            written = dispatch_home / ".claude" / "settings.json"
        finally:
            release.set()
            os.environ["HOME"] = str(dispatch_home)
            os.environ["USERPROFILE"] = str(dispatch_home)

        assert not (later_home / ".claude" / "settings.json").exists(), (
            "the orphaned worker followed $HOME after its caller was cancelled — "
            "this is the write the home guard reports against an innocent test"
        )
        assert written.exists(), "the write should still land, just in the dispatch-time home"
