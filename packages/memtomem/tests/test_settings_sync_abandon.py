"""``generate_all_settings`` stops writing once its caller has given up (#2218).

The engine-level half of the fix. The end-to-end shape — a web route whose
``asyncio.timeout`` fires while the worker thread runs on — lives in
``test_settings_home_pinning.py``; these drive the abort flag directly so each
check placement is pinned on its own, without a thread or a cancelled task.

Every case writes into ``tmp_path`` with an explicit ``project_shared`` scope,
so nothing here resolves a host home.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.context import settings as settings_mod
from memtomem.context.settings import abandon_sync_on_exit, generate_all_settings

CANONICAL = '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}]}}'  # noqa: E501


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".memtomem").mkdir(parents=True)
    (root / ".memtomem" / "settings.json").write_text(CANONICAL)
    # The Claude generator is available iff its target dir exists; project
    # scope keeps every path under the project root.
    (root / ".claude").mkdir()
    return root


def _written(root: Path) -> Path:
    return root / ".claude" / "settings.json"


def test_a_sync_abandoned_before_it_starts_writes_nothing(tmp_path):
    root = _project(tmp_path)

    with abandon_sync_on_exit() as abandoned:
        abandoned.set()
        results = generate_all_settings(root, scope="project_shared")

    assert results, "the loop should still report a row per registered runtime"
    assert all(r.status == "aborted" for r in results.values()), results
    assert "abandoned by its caller" in (results["claude_settings"].reason or "")
    assert not _written(root).exists()
    # The pre-availability placement matters: an abort must not leave the lock
    # sidecar behind either, since acquiring a lock creates one that is never
    # removed.
    assert not list((root / ".claude").iterdir()), "an abandoned sync touched the target dir"


def test_abandonment_during_the_merge_suppresses_that_target_s_write(tmp_path, monkeypatch):
    """The pre-write check, not just the between-targets one.

    A caller can give up after the lock is held and the merge has run, which
    is past both earlier checks — the write is still the mutation the user was
    told did not happen.
    """
    root = _project(tmp_path)
    real_merge = settings_mod.ClaudeSettingsGenerator.merge

    with abandon_sync_on_exit() as abandoned:

        def _merge_then_abandon(self, existing, contributions):
            merged = real_merge(self, existing, contributions)
            abandoned.set()
            return merged

        monkeypatch.setattr(settings_mod.ClaudeSettingsGenerator, "merge", _merge_then_abandon)
        results = generate_all_settings(root, scope="project_shared")

    assert results["claude_settings"].status == "aborted", results["claude_settings"]
    assert not _written(root).exists(), "the merge completed and the write landed anyway"


def test_a_target_written_before_the_abandonment_keeps_its_write(tmp_path, monkeypatch):
    """Cooperative, not transactional: the fix suppresses, it does not roll back.

    The 503 says as much, so a target that had already been written when the
    caller gave up must still be reported ``ok`` rather than quietly reverted.
    """
    root = _project(tmp_path)
    real_write = settings_mod._write_settings_target

    with abandon_sync_on_exit() as abandoned:

        def _write_then_abandon(name, path, data):
            real_write(name, path, data)
            abandoned.set()

        monkeypatch.setattr(settings_mod, "_write_settings_target", _write_then_abandon)
        results = generate_all_settings(root, scope="project_shared")

    assert results["claude_settings"].status == "ok", results["claude_settings"]
    assert _written(root).exists()
    # Whatever ran after it stopped, so no second target may report ``ok``.
    later = [name for name, r in results.items() if name != "claude_settings" and r.status == "ok"]
    assert not later, f"targets kept writing after the caller gave up: {later}"


def test_a_sync_nobody_abandoned_writes_normally(tmp_path):
    """The default: CLI and detector callers never enter the scope at all."""
    root = _project(tmp_path)

    results = generate_all_settings(root, scope="project_shared")

    assert results["claude_settings"].status == "ok", results["claude_settings"]
    assert _written(root).exists()


def test_leaving_the_scope_normally_does_not_abandon_the_next_sync(tmp_path):
    """The ``finally`` set is a no-op for work that already finished.

    A healthy request exits the scope the same way a cancelled one does, so
    the set must not leak into anything that runs afterwards.
    """
    root = _project(tmp_path)

    with abandon_sync_on_exit():
        first = generate_all_settings(root, scope="project_shared")
    second = generate_all_settings(root, scope="project_shared")

    assert first["claude_settings"].status == "ok", first["claude_settings"]
    assert second["claude_settings"].status in {"ok", "in sync"}, second["claude_settings"]
    assert settings_mod._sync_is_abandoned() is False
