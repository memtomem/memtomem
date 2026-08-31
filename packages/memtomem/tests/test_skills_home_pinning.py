"""Where a skills worker's user-scope paths land once its caller is gone (#2250).

`generate_all_skills` and `extract_skills_to_canonical` run in
`asyncio.to_thread`, which cannot be cancelled, so a request that times out
returns 503 with its worker still running. #2247 gave those engines an abort
flag, which answers *whether* a late write happens. It cannot answer *where*:
the abort is cooperative, so the write that outruns the last checkpoint is
exactly the one that still resolves a path — and every user-scope path was
resolved from the ambient ``$HOME`` at that moment, not the one the caller
chose.

That is the #2211 shape, found there in the settings engine and fixed with
``pinned_host_homes()``. The resolvers the skills engines go through —
``scope_resolver.canonical_artifact_dir`` and
``_runtime_targets.runtime_fanout_root`` — now read the pin too, so this file
pins both halves:

* the resolvers honour a pin (and, unpinned, still read the live environment,
  which is what keeps the CLI and the detectors unchanged);
* the dispatch sites enter one, so a worker orphaned mid-resolution keeps
  answering with the dispatch home.

The end-to-end test asserts on the paths the worker *resolved*, not on files it
wrote: the abort flag suppresses most late writes, so a write-only assertion
would stay green with the pin removed.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import pytest

from memtomem.context import _host_homes
from memtomem.context import scope_resolver as scope_mod
from memtomem.context import _runtime_targets as targets_mod
from memtomem.context._host_homes import HostHomes, pin_expanduser, pinned_host_homes

from .helpers import set_home


class TestPinExpanduser:
    """The one helper both resolvers absolutise ``~`` through."""

    def test_reads_the_live_home_when_nothing_is_pinned(self, monkeypatch, tmp_path):
        set_home(monkeypatch, tmp_path / "live")
        assert pin_expanduser(Path("~/.claude/skills")) == tmp_path / "live" / ".claude" / "skills"

    def test_pin_wins_over_a_later_env_change(self, monkeypatch, tmp_path):
        set_home(monkeypatch, tmp_path / "at-dispatch")
        with pinned_host_homes():
            set_home(monkeypatch, tmp_path / "later")
            resolved = pin_expanduser(Path("~/.claude/skills"))
        assert resolved == tmp_path / "at-dispatch" / ".claude" / "skills"
        # The token reset restores the live reading for the next caller.
        assert pin_expanduser(Path("~/x")) == tmp_path / "later" / "x"

    def test_a_named_user_anchor_is_not_redirected(self, tmp_path):
        """``~someone`` names an account, not "the caller's home".

        Whatever ``Path.expanduser`` does with an unknown account — return the
        path unchanged, or raise — the pin must not turn it into the caller's
        own home, which would write one user's artifacts under another's name.
        """
        path = Path("~nosuchuser-memtomem/x")
        pinned = HostHomes(home=tmp_path / "pinned", kimi_home=tmp_path / "k")
        try:
            expected = path.expanduser()
        except RuntimeError:
            with pinned_host_homes(pinned):
                with pytest.raises(RuntimeError):
                    pin_expanduser(path)
            return
        with pinned_host_homes(pinned):
            assert pin_expanduser(path) == expected

    @pytest.mark.parametrize("raw", ["/abs/skills", "rel/skills"])
    def test_non_tilde_paths_are_unchanged(self, raw, tmp_path):
        path = Path(raw)
        with pinned_host_homes(HostHomes(home=tmp_path / "pinned", kimi_home=tmp_path / "k")):
            assert pin_expanduser(path) == path.expanduser()


class TestResolversReadThePin:
    """The two call sites a skills worker reaches after its caller is gone."""

    def test_canonical_user_dir_follows_the_pin(self, monkeypatch, tmp_path):
        set_home(monkeypatch, tmp_path / "at-dispatch")
        with pinned_host_homes():
            set_home(monkeypatch, tmp_path / "later")
            resolved = scope_mod.canonical_artifact_dir("skills", "user", None)
        assert (tmp_path / "at-dispatch").resolve() in resolved.parents, resolved

    def test_canonical_user_dir_is_live_without_a_pin(self, monkeypatch, tmp_path):
        set_home(monkeypatch, tmp_path / "live")
        resolved = scope_mod.canonical_artifact_dir("skills", "user", None)
        assert (tmp_path / "live").resolve() in resolved.parents, resolved

    def test_runtime_fanout_root_follows_the_pin(self, monkeypatch, tmp_path):
        set_home(monkeypatch, tmp_path / "at-dispatch")
        with pinned_host_homes():
            set_home(monkeypatch, tmp_path / "later")
            resolved = targets_mod.runtime_fanout_root("skills", "claude", "user", None)
        assert resolved == (tmp_path / "at-dispatch" / ".claude" / "skills").resolve()

    def test_kimi_fanout_root_follows_the_pinned_kimi_home(self, monkeypatch, tmp_path):
        """Kimi reads ``$KIMI_CODE_HOME`` first, so it is snapshotted separately."""
        monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "at-dispatch" / ".kimi-code"))
        set_home(monkeypatch, tmp_path / "at-dispatch")
        with pinned_host_homes():
            monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "later" / ".kimi-code"))
            set_home(monkeypatch, tmp_path / "later")
            resolved = targets_mod.runtime_fanout_root("skills", "kimi", "user", None)
        assert resolved == (tmp_path / "at-dispatch" / ".kimi-code" / "skills").resolve()


SKILL_MD = """---
name: alpha
description: A skill.
---

Body.
"""


def _user_canonical_skill(home: Path) -> None:
    skill = home / ".memtomem" / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")


class TestOrphanedSkillsWorkerKeepsTheDispatchHome:
    """Cancel the caller, move ``$HOME``, release the worker, read what it resolved."""

    async def test_a_cancelled_sync_resolves_the_dispatch_home(self, tmp_path, monkeypatch):
        from memtomem.web.routes import context_skills

        dispatch_home = tmp_path / "dispatch-home"
        later_home = tmp_path / "later-home"
        for home in (dispatch_home, later_home):
            (home / ".claude").mkdir(parents=True)
        _user_canonical_skill(dispatch_home)
        _user_canonical_skill(later_home)
        project_root = tmp_path / "project"
        (project_root / ".memtomem").mkdir(parents=True)

        monkeypatch.setenv("KIMI_CODE_HOME", str(dispatch_home / ".kimi-code"))
        set_home(monkeypatch, dispatch_home)

        entered, release = threading.Event(), threading.Event()
        resolved: list[Path] = []

        real_fanout = targets_mod.runtime_fanout_root

        def _parked_fanout(artifact, runtime, scope, project_root=None):
            # Worker only: the route resolves targets on the loop as well, and
            # blocking there would stall the very timeout under test.
            if scope == "user" and threading.current_thread() is not threading.main_thread():
                entered.set()
                release.wait(timeout=10)
            out = real_fanout(artifact, runtime, scope, project_root)
            if scope == "user" and out is not None:
                resolved.append(out)
            return out

        # Patched where the engine looks the name up, not only at its source.
        monkeypatch.setattr(targets_mod, "runtime_fanout_root", _parked_fanout)
        monkeypatch.setattr("memtomem.context.skills.runtime_fanout_root", _parked_fanout)

        worker_done = threading.Event()
        real_generate = context_skills.generate_all_skills

        def _generate_and_signal(*args, **kwargs):
            try:
                return real_generate(*args, **kwargs)
            finally:
                worker_done.set()

        monkeypatch.setattr(context_skills, "generate_all_skills", _generate_and_signal)

        task = asyncio.create_task(context_skills._sync_skills_core(project_root, "user"))
        try:
            await asyncio.to_thread(entered.wait, 10)
            # What the route's ``asyncio.timeout`` does.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # The originating request is gone and ``monkeypatch`` would restore
            # the real home; a later caller is now running under a different one.
            os.environ["HOME"] = str(later_home)
            os.environ["USERPROFILE"] = str(later_home)
            os.environ["KIMI_CODE_HOME"] = str(later_home / ".kimi-code")

            release.set()
            assert await asyncio.to_thread(worker_done.wait, 20), "orphaned worker never finished"
        finally:
            release.set()
            os.environ["HOME"] = str(dispatch_home)
            os.environ["USERPROFILE"] = str(dispatch_home)
            os.environ["KIMI_CODE_HOME"] = str(dispatch_home / ".kimi-code")

        assert resolved, "the worker never resolved a user-scope fan-out root"
        # Every path resolved AFTER $HOME moved — the pin is the only thing
        # that can keep them in the caller's home. Asserting on files written
        # instead would pass with the pin removed, because the abort flag
        # suppresses the writes.
        assert all(dispatch_home in p.parents for p in resolved), (
            f"the orphaned worker followed $HOME after its caller was cancelled: {resolved}"
        )
        assert not any(later_home in p.parents for p in resolved), (
            f"a user-scope target resolved into the moved home: {resolved}"
        )
        assert not (later_home / ".claude" / "skills" / "alpha").exists(), (
            "the orphaned worker fanned a skill out into a home its caller never chose"
        )

    async def test_the_pin_is_what_holds_it(self, tmp_path, monkeypatch):
        """The same worker with the pin defeated resolves the moved home.

        Without this the test above could not distinguish the pin from the
        abort flag: it asserts a negative that an engine which simply stops
        also satisfies.
        """
        set_home(monkeypatch, tmp_path / "at-dispatch")
        # The pre-#2250 resolver: a plain ``expanduser`` that ignores the pin.
        monkeypatch.setattr(_host_homes, "pin_expanduser", lambda path: path.expanduser())
        monkeypatch.setattr(targets_mod, "pin_expanduser", lambda path: path.expanduser())
        with pinned_host_homes():
            set_home(monkeypatch, tmp_path / "later")
            resolved = targets_mod.runtime_fanout_root("skills", "claude", "user", None)
        assert resolved == (tmp_path / "later" / ".claude" / "skills").resolve(), (
            "the mutation did not reach the resolver — this test is not "
            "distinguishing the pin from anything"
        )
