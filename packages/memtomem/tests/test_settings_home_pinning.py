"""What a settings worker may do once its caller is gone (#2211, #2218).

``generate_all_settings`` runs in ``asyncio.to_thread``, which cannot be
cancelled, so a request that times out leaves the worker running. Two
guards answer the two questions that raises.

*Where* would a late write land? Every user-scope target is anchored on the
ambient ``$HOME``, and before #2211 the worker resolved its target *when it
got there* — a different home than the caller had. In the suite that showed
up as the #1903 home guard reporting a write to the developer's real
``~/.claude/settings.json`` and blaming whichever unrelated test happened to
be running when it landed.

*Should it happen at all?* No: #2218 made the worker poll an abort flag, so
a sync whose caller has given up leaves its remaining targets untouched
instead of mutating settings behind a response that already said 503. The
pin still matters for the one write that can outrun the flag — a
cancellation landing inside a target's write.

These tests move a stand-in "later" home into place while the worker is
blocked, so nothing here can touch a real home even if a guard regresses.
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


class TestAbandonedWorkerAbortsItsRemainingWrites:
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

    async def test_a_cancelled_sync_does_not_write(self, tmp_path, monkeypatch):
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
        worker_results: list[dict] = []

        def _generate_and_signal(*args, **kwargs):
            try:
                results = real_generate(*args, **kwargs)
                worker_results.append(results)
                return results
            finally:
                worker_done.set()

        monkeypatch.setattr(settings_sync, "generate_all_settings", _generate_and_signal)

        # Which abort check stopped it matters. The worker is past the
        # between-targets one before it parks, so reaching a merge would mean
        # the post-lock check let it through and only the pre-write check
        # caught it — the two are not interchangeable here.
        merges: list[str] = []
        real_merge = settings_mod.ClaudeSettingsGenerator.merge

        def _recording_merge(self, existing, contributions):
            merges.append("claude_settings")
            return real_merge(self, existing, contributions)

        monkeypatch.setattr(settings_mod.ClaudeSettingsGenerator, "merge", _recording_merge)

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
        # The worker resolves its target *after* $HOME moves (it is parked
        # inside ``target_file`` when the move happens), so which home it then
        # locks is the pin's doing, not the abort's. Without that assertion the
        # abort alone would keep both settings files absent and this test would
        # pass with the pin removed (#2211 regression coverage).
        assert (dispatch_home / ".claude" / ".settings.json.lock").exists(), (
            "the worker never locked the target it resolved at dispatch time"
        )
        assert not (later_home / ".claude" / ".settings.json.lock").exists(), (
            "the worker resolved its target from the moved $HOME — the pin is gone"
        )
        assert merges == [], (
            "the abort let the worker reach the merge; the post-lock check "
            "(not just the pre-write one) is what stops it here"
        )
        # #2218: the worker had not written this target when the caller gave
        # up, so it must leave it alone rather than mutate settings behind a
        # request that already failed. The pin (asserted above) covers only
        # the write that outruns the flag.
        assert not written.exists(), (
            "an abandoned sync wrote its target anyway — the caller was told "
            "the sync failed and the settings changed regardless"
        )

        assert worker_results, "the worker never returned a result mapping"
        claude = worker_results[0]["claude_settings"]
        assert claude.status == "aborted", f"expected aborted, got {claude.status}"
        assert "abandoned by its caller" in (claude.reason or "")
