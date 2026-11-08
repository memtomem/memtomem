"""mem_do(action="hermes_mirror_apply"): the Hermes built-in memory mirror.

Driven through the tool against a real BM25-only store. What is pinned: the
two refusal classes (refused before the commit, nothing stored; accepted but
not rendered, the operation durable and the target pending), the derived file
and its index following the table, the purge fence, and the reply shape the
provider's queue depends on.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools.hermes_mirror import (
    MIRROR_DIR,
    entry_key,
    mem_hermes_mirror_apply,
    render_mirror_markdown,
)
from memtomem.storage.mixins.hermes_mirror import MirrorEntry

OWNER = "0123456789abcdef0123456789abcdef"


def _secret() -> str:
    # Assembled at runtime so the literal never appears in the source.
    return "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


@pytest.fixture
async def mirror(bm25_only_components):
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    async def call(**params):
        params.setdefault("owner", OWNER)
        params.setdefault("target", "memory")
        out = await mem_hermes_mirror_apply(ctx=ctx, **params)  # type: ignore[arg-type]
        return out

    call.comp = comp  # type: ignore[attr-defined]
    call.app = app  # type: ignore[attr-defined]
    call.mem_dir = Path(mem_dir).resolve()  # type: ignore[attr-defined]
    return call


def _file(mirror, target: str = "memory") -> Path:
    return mirror.mem_dir / MIRROR_DIR / OWNER / f"{target}.md"


async def _indexed_text(mirror, target: str = "memory") -> str:
    chunks = await mirror.comp.storage.list_chunks_by_source(_file(mirror, target))
    return "\n".join(c.content for c in chunks)


async def test_add_renders_the_file_and_indexes_it(mirror):
    reply = json.loads(await mirror(op="add", content="User prefers tabs.", ts=10))
    assert reply == {
        "schema_version": 1,
        "op": "add",
        "applied": True,
        "changed": True,
        "accepted_revision": 1,
        "previous": None,
        "content_refused": False,
        "live_entries": 1,
        "rendered": True,
        "rendered_revision": 1,
        "render_error": None,
    }
    text = _file(mirror).read_text(encoding="utf-8")
    assert "User prefers tabs." in text
    assert f"## Entry {entry_key('User prefers tabs.')[:12]}" in text
    assert "User prefers tabs." in await _indexed_text(mirror)


async def test_replace_and_remove_follow_the_native_history(mirror):
    await mirror(op="add", content="Project uses uv.", ts=1)
    await mirror(op="add", content="Deploys on Fridays.", ts=2)
    replaced = json.loads(
        await mirror(
            op="replace",
            previous_content="Deploys on Fridays.",
            content="Deploys on Thursdays.",
            ts=3,
        )
    )
    assert replaced["previous"] == "matched"
    removed = json.loads(await mirror(op="remove", previous_content="Project uses uv.", ts=4))
    assert removed["previous"] == "matched" and removed["live_entries"] == 1
    indexed = await _indexed_text(mirror)
    assert "Deploys on Thursdays." in indexed
    assert "Fridays" not in indexed and "Project uses uv." not in indexed


async def test_an_unknown_previous_entry_is_reported_unmatched(mirror):
    await mirror(op="add", content="Keep A.", ts=1)
    reply = json.loads(await mirror(op="remove", previous_content="Never mirrored.", ts=2))
    assert reply["previous"] == "unmatched"
    assert reply["live_entries"] == 1


async def test_a_retried_operation_changes_nothing(mirror):
    first = json.loads(await mirror(op="add", content="Same entry.", ts=5))
    again = json.loads(await mirror(op="add", content="Same entry.", ts=5))
    assert again["changed"] is False
    assert again["accepted_revision"] == first["accepted_revision"]
    assert again["rendered"] is True


async def test_a_refused_add_stores_nothing(mirror):
    out = await mirror(op="add", content=f"token {_secret()}", ts=1)
    assert out.startswith("Error: content matches")
    assert await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory") is None
    assert not _file(mirror).exists()


async def test_a_refused_replacement_still_removes_the_old_entry(mirror):
    await mirror(op="add", content="Old fact.", ts=1)
    reply = json.loads(
        await mirror(op="replace", previous_content="Old fact.", content=f"new {_secret()}", ts=2)
    )
    assert reply["content_refused"] is True
    assert reply["live_entries"] == 0
    text = _file(mirror).read_text(encoding="utf-8")
    assert "Old fact." not in text and _secret() not in text
    assert "Old fact." not in await _indexed_text(mirror)


async def test_purge_empties_the_file_and_fences_older_operations(mirror):
    await mirror(op="add", content="Purge me.", ts=10)
    purged = json.loads(await mirror(op="purge", ts=20))
    assert purged["applied"] is True and purged["live_entries"] == 0
    assert "Purge me." not in _file(mirror).read_text(encoding="utf-8")
    assert "Purge me." not in await _indexed_text(mirror)
    late = await mirror(op="add", content="Purge me.", ts=10)
    assert late.startswith("Error: purged")
    newer = json.loads(await mirror(op="add", content="After purge.", ts=21))
    assert newer["live_entries"] == 1


async def test_purge_needs_a_ts_from_the_provider_clock(mirror):
    # The fence is compared with the provider's timestamps; a server clock (possibly
    # another host's) could let a queued older write through.
    await mirror(op="add", content="Old.", ts=1)
    out = await mirror(op="purge")
    assert out.startswith("Error: ts is required for op='purge'")
    assert (await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory"))[
        "purged_at"
    ] is None


async def test_reconcile_repairs_a_deleted_or_edited_file(mirror):
    await mirror(op="add", content="Keep me.", ts=1)
    _file(mirror).unlink()
    reply = json.loads(await mirror(op="reconcile"))
    assert reply["rendered"] is True
    assert "Keep me." in _file(mirror).read_text(encoding="utf-8")
    _file(mirror).write_text("# hand edit\n", encoding="utf-8")
    json.loads(await mirror(op="reconcile"))
    assert "Keep me." in _file(mirror).read_text(encoding="utf-8")


async def test_a_failure_after_the_commit_is_reported_as_not_rendered(mirror, monkeypatch):
    import memtomem.tools.memory_mutation as mutation

    async def unreadable(*args, **kwargs):
        raise PermissionError("cannot read the old file")

    monkeypatch.setattr(mutation, "preflight_refusal", unreadable)
    reply = json.loads(await mirror(op="add", content="Durable.", ts=1))
    assert reply["applied"] is True and reply["rendered"] is False
    assert "PermissionError" in reply["render_error"]


async def test_a_cancelled_write_holds_the_lock_until_its_thread_is_done(mirror, monkeypatch):
    import asyncio
    import threading

    import memtomem.context._atomic as atomic

    real_write = atomic.atomic_write_text
    entered = threading.Event()
    release = threading.Event()

    def slow_write(*args, **kwargs):
        entered.set()
        release.wait(10)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(atomic, "atomic_write_text", slow_write)
    task = asyncio.create_task(mirror(op="add", content="Slow.", ts=1))
    await asyncio.get_running_loop().run_in_executor(None, entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.1)
    assert not task.done(), "the cancel released the locks while the write still ran"
    assert mirror.app.get_memory_file_lock(_file(mirror)).locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not mirror.app.get_memory_file_lock(_file(mirror)).locked()


async def test_a_render_failure_keeps_the_operation_and_reconcile_recovers(mirror, monkeypatch):
    engine = mirror.app.index_engine
    real_index_file = engine.index_file

    async def broken(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(engine, "index_file", broken)
    reply = json.loads(await mirror(op="add", content="Durable anyway.", ts=1))
    assert reply["applied"] is True
    assert reply["rendered"] is False and "OSError" in reply["render_error"]
    assert reply["accepted_revision"] == 1 and reply["rendered_revision"] == 0
    state = await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory")
    assert state["render_pending"] is True

    monkeypatch.setattr(engine, "index_file", real_index_file)
    fixed = json.loads(await mirror(op="reconcile"))
    assert fixed["applied"] is False and fixed["rendered"] is True
    assert fixed["rendered_revision"] == 1
    assert "Durable anyway." in await _indexed_text(mirror)


async def test_index_errors_returned_as_stats_count_as_a_failed_render(mirror, monkeypatch):
    engine = mirror.app.index_engine
    real_index_file = engine.index_file

    async def with_errors(*args, **kwargs):
        stats = await real_index_file(*args, **kwargs)
        from dataclasses import replace

        return replace(stats, errors=("embedding failed",))

    monkeypatch.setattr(engine, "index_file", with_errors)
    reply = json.loads(await mirror(op="add", content="Half indexed.", ts=1))
    assert reply["rendered"] is False and reply["render_error"] == "embedding failed"
    state = await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory")
    assert state["render_pending"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
async def test_a_link_on_the_mirror_path_is_refused_before_anything_is_stored(mirror, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (mirror.mem_dir / MIRROR_DIR).mkdir()
    os.symlink(elsewhere, mirror.mem_dir / MIRROR_DIR / OWNER)
    out = await mirror(op="add", content="Should not land.", ts=1)
    assert out.startswith("Error:") and "nothing was written" in out.lower()
    assert await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory") is None
    assert list(elsewhere.iterdir()) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges")
async def test_a_link_that_stays_inside_the_root_is_refused_too(mirror):
    # Contained, user tier, so the scope check passes; the path written would
    # still differ from the path locked, which is what the refusal is for.
    inside = mirror.mem_dir / "other"
    inside.mkdir()
    (mirror.mem_dir / MIRROR_DIR).mkdir()
    os.symlink(inside, mirror.mem_dir / MIRROR_DIR / OWNER)
    out = await mirror(op="add", content="Should not land.", ts=1)
    assert out.startswith("Error: mirror_target_link")
    assert await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory") is None
    assert list(inside.iterdir()) == []


async def test_a_read_only_root_is_refused_before_anything_is_stored(mirror):
    mirror.app.config.indexing.read_only_memory_dirs = [mirror.mem_dir / MIRROR_DIR]
    out = await mirror(op="add", content="Read only.", ts=1)
    assert out.startswith("Error: read_only_target")
    assert await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory") is None


@pytest.mark.parametrize(
    ("params", "needle"),
    [
        ({"owner": "XYZ"}, "owner must be"),
        ({"target": "Memory"}, "target must match"),
        ({"op": "upsert"}, "op must be one of"),
        ({"op": "add", "ts": 1}, "content must be"),
        ({"op": "remove", "ts": 1}, "previous_content must be"),
        ({"op": "add", "content": "x"}, "ts is required"),
        ({"op": "add", "content": "x", "ts": True}, "ts must be"),
        ({"op": "add", "content": "x", "ts": -1}, "ts must be"),
        ({"op": "add", "content": "x", "ts": 1, "deadline_ms": 0}, "deadline_ms must be"),
        ({"op": "add", "content": "x" * (64 * 1024 + 1), "ts": 1}, "longer than"),
    ],
)
async def test_invalid_parameters_are_refused(mirror, params, needle):
    params.setdefault("op", "add")
    out = await mirror(**params)
    assert out.startswith("Error:") and needle in out


async def test_an_expired_deadline_stores_nothing(mirror, monkeypatch):
    import memtomem.server.tools.hermes_mirror as mod

    clock = iter([100.0] + [200.0] * 50)
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    out = await mirror(op="add", content="Too late.", ts=1, deadline_ms=5)
    assert out.startswith("Error (retryable): deadline exceeded")
    assert await mirror.comp.storage.hermes_mirror_target_state(OWNER, "memory") is None


async def test_the_rendered_namespace_matches_a_rebuild(mirror):
    await mirror(op="add", content="Namespace check.", ts=1)
    before = {
        c.metadata.namespace for c in await mirror.comp.storage.list_chunks_by_source(_file(mirror))
    }
    await mirror.app.index_engine.index_file(_file(mirror), force=True)
    after = {
        c.metadata.namespace for c in await mirror.comp.storage.list_chunks_by_source(_file(mirror))
    }
    assert before == after


def test_render_is_deterministic_and_keeps_entries_in_their_own_sections():
    entries = [MirrorEntry("a" * 64, "First\r\nline", 1), MirrorEntry("b" * 64, "Second", 2)]
    once = render_mirror_markdown("memory", entries)
    assert once == render_mirror_markdown("memory", entries)
    assert once.count("\n## Entry ") == 2
    assert "\r" not in once


async def test_a_held_file_is_accepted_but_not_rendered(mirror, monkeypatch):
    import memtomem.tools.memory_mutation as mutation

    async def held(*args, **kwargs):
        return "Error: the file is held (redaction).", b""

    monkeypatch.setattr(mutation, "preflight_refusal", held)
    reply = json.loads(await mirror(op="add", content="Stored, not shown.", ts=1))
    assert reply["applied"] is True and reply["accepted_revision"] == 1
    assert reply["rendered"] is False and "held" in reply["render_error"]
    assert not _file(mirror).exists()


async def test_another_process_holding_the_writer_is_retryable(mirror, monkeypatch):
    import sqlite3
    from contextlib import asynccontextmanager

    from memtomem.errors import StorageError

    @asynccontextmanager
    async def busy(*args, **kwargs):
        try:
            raise sqlite3.OperationalError("database is locked")
        except sqlite3.OperationalError as exc:
            raise StorageError(f"transaction() could not take the write lock: {exc}") from exc
        yield

    monkeypatch.setattr(mirror.comp.storage, "transaction", busy)
    out = await mirror(op="add", content="Later.", ts=1)
    assert out.startswith("Error (retryable): the store's writer is busy")


async def test_other_storage_errors_are_not_called_busy(mirror, monkeypatch):
    from contextlib import asynccontextmanager

    from memtomem.errors import StorageError

    @asynccontextmanager
    async def closing(*args, **kwargs):
        raise StorageError("Database is closing")
        yield

    monkeypatch.setattr(mirror.comp.storage, "transaction", closing)
    out = await mirror(op="add", content="Later.", ts=1)
    assert out.startswith("Error:") and "retryable" not in out


async def test_a_reconcile_whose_budget_ran_out_changes_nothing(mirror, monkeypatch):
    import memtomem.server.tools.hermes_mirror as mod

    await mirror(op="add", content="Keep.", ts=1)
    _file(mirror).unlink()
    clock = iter([100.0] + [200.0] * 50)
    monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
    out = await mirror(op="reconcile", deadline_ms=5)
    assert out.startswith("Error (retryable): deadline exceeded")
    assert not _file(mirror).exists()


async def test_the_reply_revision_is_the_one_this_call_committed(mirror):
    first = json.loads(await mirror(op="add", content="One.", ts=1))
    second = json.loads(await mirror(op="add", content="Two.", ts=2))
    replay = json.loads(await mirror(op="add", content="One.", ts=1))
    assert (first["accepted_revision"], second["accepted_revision"]) == (1, 2)
    assert replay["accepted_revision"] == 2 and replay["changed"] is False
