"""Contract and failure tests for the persistent hybrid Store."""

import asyncio
import json
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict

import pytest

pytest.importorskip("langgraph")
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import GetOp, PutOp

from memtomem.integrations import MemtomemHybridStore


def embed(texts):
    return [[1.0, 0.0] if "apple" in text else [0.0, 1.0] for text in texts]


@pytest.fixture
def store(tmp_path):
    with MemtomemHybridStore(tmp_path / "hybrid.db") as value:
        yield value


def indexed(path, **kwargs):
    return MemtomemHybridStore(
        path,
        index={"embed": embed, "dims": 2, "fields": ["text", "extra"]},
        index_id="deterministic-v1",
        **kwargs,
    )


def test_contract_and_reopen(tmp_path):
    path = tmp_path / "store.db"
    with indexed(path) as store:
        original = {"text": "apple", "nested": {"a": [1, True, None, "한글"]}}
        store.put(("Docs", "one"), "a", original)
        item = store.get(("Docs", "one"), "a")
        assert item.value == original
        store.put(("Docs", "one"), "a", {"text": "pear"})
        updated = store.get(("Docs", "one"), "a")
        assert updated.created_at == item.created_at
        assert updated.updated_at >= item.updated_at
        store.put(("docs",), "a", {"text": "apple"})
        assert len(store.search(("Docs",))) == 1
        assert store.list_namespaces(prefix=("*",), max_depth=1) == [("Docs",), ("docs",)]
        assert store.list_namespaces(suffix=("one",)) == [("Docs", "one")]
    with indexed(path) as store:
        assert store.get(("Docs", "one"), "a").value == {"text": "pear"}
        assert store.search(("Docs",), query="pear")[0].key == "a"
        store.delete(("Docs", "one"), "a")
        assert store.get(("Docs", "one"), "a") is None
        assert not store.search(("Docs",), query="pear")


def test_ranking_dedup_and_diagnostics(tmp_path):
    with indexed(tmp_path / "store.db") as store:
        store.put(("docs",), "a", {"text": "apple", "extra": "apple"})
        store.put(("docs",), "b", {"text": "pear"})
        result = store.search_with_diagnostics((), query="apple")
        assert [item.key for item in result["items"]] == ["a", "b"]
        assert result["items"][0].score == pytest.approx(2 / 61)
        assert result["diagnostics"]["candidate_counts"] == {"bm25": 1, "dense": 2}
        assert result["diagnostics"]["score_scale"] == "weighted_rrf"
        assert store.search((), query="apple", limit=1, offset=1)[0].key == "b"


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid"])
def test_filters_before_candidate_limit(tmp_path, mode):
    with indexed(tmp_path / "store.db", retrieval={"mode": mode}) as store:
        store.batch([PutOp(("noise",), str(i), {"text": "apple", "group": 0}) for i in range(120)])
        store.put(("target",), "rare", {"text": "apple pear", "group": 1})
        results = store.search((), query="apple", filter={"group": 1}, limit=1)
        assert [r.key for r in results] == ["rare"]
        assert store.search(("target",), query="apple", limit=1)[0].key == "rare"


@pytest.mark.parametrize(
    "filters",
    [
        {"nested": {"n": {"$gte": 2}}},
        {"nested.n": {"$in": [2, 3]}},
        {"nested.n": {"$nin": [0]}},
        {"nested.n": {"$gt": 1, "$lt": 3}},
        {"arr": [1, {"name": "x"}]},
        {"nested.n": {"$eq": 2, "$ne": 0, "$lte": 2}},
    ],
)
def test_nested_filters(store, filters):
    store.put(("docs",), "one", {"text": "hello", "nested": {"n": 2}, "arr": [1, {"name": "x"}]})
    assert store.search((), query="hello", filter=filters)[0].key == "one"


def test_unknown_filter_rejected_even_empty(store):
    with pytest.raises(ValueError, match="Unsupported"):
        store.search((), filter={"x": {"$typo": 1}})
    with pytest.raises(ValueError, match="requires a list"):
        store.search((), filter={"x": {"$in": "x"}})


def test_unindexed_and_browse(store):
    store.put(("docs",), "hidden", {"text": "hello"}, index=False)
    assert store.get(("docs",), "hidden")
    assert store.search(())[0].score is None
    assert not store.search((), query="hello")
    store.put(("docs",), "visible", {"text": "hello"})
    assert [r.key for r in store.search(())] == ["visible", "hidden"]
    assert (
        store.search_with_diagnostics((), query="hello")["diagnostics"]["effective_mode"] == "bm25"
    )


def test_ttl_refresh_and_sweep(store):
    store.put(("docs",), "short", {"text": "hello"}, ttl=1)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE items SET expires_at=?", (time.time() + 10,))
    before = store.export_json()["records"][0]["expires_at"]
    store.get(("docs",), "short", refresh_ttl=False)
    assert store.export_json()["records"][0]["expires_at"] == before
    store.search((), query="hello")
    assert store.export_json()["records"][0]["expires_at"] > before
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE items SET expires_at=0")
    assert store.get(("docs",), "short") is None
    assert not store.search((), query="hello")
    assert not store.list_namespaces()
    assert store.sweep_ttl() == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM item_fts").fetchone()[0] == 0


def test_default_ttl_and_refresh_config(tmp_path):
    with MemtomemHybridStore(
        tmp_path / "s.db", ttl={"default_ttl": 2, "refresh_on_read": False}
    ) as store:
        store.put(("a",), "x", {"text": "hi"})
        before = store.export_json()
        store.get(("a",), "x")
        assert store.export_json() == before
        assert before["records"][0]["ttl"] == 2
        store.put(("a",), "y", {}, ttl=None)
        assert next(r for r in store.export_json()["records"] if r["key"] == "y")["ttl"] is None


def test_embedding_failure_preserves_old_record(tmp_path):
    def failing(texts):
        if any("fail" in text for text in texts):
            raise RuntimeError("embedding failed")
        return embed(texts)

    with MemtomemHybridStore(
        tmp_path / "s.db", index={"embed": failing, "dims": 2}, index_id="test"
    ) as store:
        store.put(("a",), "x", {"text": "apple"})
        before = store.export_json()
        with pytest.raises(RuntimeError):
            store.put(("a",), "x", {"text": "fail"})
        assert store.export_json() == before
        with pytest.warns(RuntimeWarning):
            assert (
                store.search_with_diagnostics((), query="fail")["diagnostics"]["fallback_reason"]
                == "query_embedding_failed"
            )
    with MemtomemHybridStore(
        tmp_path / "s.db",
        index={"embed": failing, "dims": 2},
        index_id="test",
        retrieval={"mode": "dense"},
    ) as store:
        with pytest.raises(RuntimeError):
            store.search((), query="fail")


def test_sql_failure_rolls_back_all_representations(tmp_path):
    with indexed(tmp_path / "s.db") as store:
        store.put(("a",), "x", {"text": "apple"})
        before = store.export_json()
        with sqlite3.connect(store.path) as db:
            db.execute(
                "CREATE TRIGGER injected BEFORE INSERT ON vectors BEGIN SELECT RAISE(ABORT,'fault'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            store.put(("a",), "x", {"text": "pear"})
        assert store.export_json() == before
        assert store.search((), query="apple")[0].key == "x"


def test_json_roundtrip_and_atomic_import(tmp_path):
    with indexed(tmp_path / "a.db") as source, indexed(tmp_path / "b.db") as target:
        source.put(("a",), "x", {"text": "apple"}, ttl=10, index=["text"])
        source.put(("a",), "y", {"text": "pear"}, index=False)
        exported = json.loads(json.dumps(source.export_json()))
        assert target.import_json(exported) == 2
        assert target.export_json() == exported
        assert [r.key for r in target.search((), query="apple")] == ["x"]
        exported["records"].insert(0, {**exported["records"][0], "key": "new"})
        with pytest.raises(ValueError, match="already exists"):
            target.import_json(exported)
        assert target.get(("a",), "new") is None


def test_foreign_database_and_config_mismatch(tmp_path):
    path = tmp_path / "foreign.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE core(value TEXT)")
    with pytest.raises(ValueError, match="foreign"):
        MemtomemHybridStore(path)
    with indexed(tmp_path / "s.db"):
        pass
    with pytest.raises(ValueError, match="Incompatible"):
        MemtomemHybridStore(tmp_path / "s.db")
    with pytest.raises(ValueError, match="index_id"):
        MemtomemHybridStore(tmp_path / "new.db", index={"embed": embed, "dims": 2})


@pytest.mark.parametrize("weights", [[0, 0], [-1, 1], [float("nan"), 1], [1]])
def test_invalid_weights(tmp_path, weights):
    with pytest.raises(ValueError):
        MemtomemHybridStore(tmp_path / "s.db", retrieval={"weights": weights})


def test_concurrent_instances(tmp_path):
    with indexed(tmp_path / "s.db") as first, indexed(tmp_path / "s.db") as second:
        with ThreadPoolExecutor(4) as pool:
            list(
                pool.map(
                    lambda i: (first if i % 2 else second).put(
                        ("a",), "same", {"text": "apple", "i": i}
                    ),
                    range(20),
                )
            )
        assert first.export_json()["records"][0]["revision"] == 20
        assert len(second.search((), query="apple")) == 1


@pytest.mark.asyncio
async def test_async_and_actual_langgraph(tmp_path):
    class State(TypedDict):
        answer: str

    async def node(state, *, store):
        await store.aput(("graph",), "key", {"text": "apple"})
        return {"answer": (await store.asearch(("graph",), query="apple"))[0].value["text"]}

    async with indexed(tmp_path / "s.db") as store:
        graph = StateGraph(State)
        graph.add_node("remember", node)
        graph.add_edge(START, "remember")
        graph.add_edge("remember", END)
        assert (await graph.compile(store=store).ainvoke({"answer": ""}))["answer"] == "apple"
        assert (await store.aget(("graph",), "key")).value == {"text": "apple"}
        assert await store.alist_namespaces() == [("graph",)]
        assert (await store.asearch_with_diagnostics((), query="apple"))["items"]
        assert (await store.aexport_json())["records"]
        await store.adelete(("graph",), "key")
        assert not await store.asearch(())
    with pytest.raises(RuntimeError, match="closed"):
        await store.aget(("graph",), "key")


@pytest.mark.asyncio
async def test_cancelled_caller_write_drains_on_close(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def slow(texts):
        entered.set()
        release.wait(5)
        return embed(texts)

    store = MemtomemHybridStore(
        tmp_path / "s.db", index={"embed": slow, "dims": 2}, index_id="slow"
    )
    task = asyncio.create_task(store.aput(("a",), "x", {"text": "apple"}))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await store.aclose()
    with MemtomemHybridStore(
        tmp_path / "s.db", index={"embed": slow, "dims": 2}, index_id="slow"
    ) as reopened:
        assert reopened.get(("a",), "x")


def test_batch_is_per_operation_atomic(store):
    with pytest.raises(ValueError):
        store.batch([PutOp(("a",), "x", {}), PutOp((), "invalid", {})])
    assert store.batch([GetOp(("a",), "x")])[0]


def test_scope_and_privacy_guard(tmp_path, monkeypatch):
    tier = tmp_path / "project" / ".memtomem" / "memories"
    monkeypatch.setenv("MEMTOMEM_INDEXING__PROJECT_MEMORY_DIRS", json.dumps([str(tier)]))
    with pytest.raises(ValueError, match="confirm_project_shared"):
        MemtomemHybridStore(tier / "store.db")
    assert not tier.exists()
    with MemtomemHybridStore(
        tier / "store.db", confirm_project_shared=True, force_unsafe=True
    ) as shared:
        assert shared.scope == "project_shared"
        with pytest.raises(ValueError, match="privacy"):
            shared.put(("a",), "x", {"api_key": "sk-secret-value"})
        assert not shared.search(())
    with MemtomemHybridStore(tmp_path / "private.db", force_unsafe=True) as private:
        private.put(("a",), "x", {"api_key": "sk-secret-value"})
        exported = private.export_json()
    with MemtomemHybridStore(tmp_path / "guarded.db") as guarded:
        with pytest.raises(ValueError, match="privacy"):
            guarded.import_json(exported)
        assert not guarded.search(())


def test_read_snapshot_keeps_ranking_and_value_consistent(tmp_path, monkeypatch):
    from memtomem.storage import hybrid_store

    original = hybrid_store.weighted_rank_scores
    with indexed(tmp_path / "s.db") as reader, indexed(tmp_path / "s.db") as writer:
        reader.put(("a",), "x", {"text": "apple"})

        def change_after_ranking(*args):
            writer.put(("a",), "x", {"text": "pear"})
            return original(*args)

        monkeypatch.setattr(hybrid_store, "weighted_rank_scores", change_after_ranking)
        result = reader.search((), query="apple", refresh_ttl=False)
        assert result[0].value == {"text": "apple"}
        assert reader.get(("a",), "x").value == {"text": "pear"}


def test_interrupted_transaction_reopens_consistently(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "s.db"
    with indexed(path) as store:
        store.put(("a",), "x", {"text": "apple"})
    script = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
connection.execute('DELETE FROM item_fts')
connection.execute('DELETE FROM vectors')
connection.execute('DELETE FROM items')
os._exit(23)
"""
    assert subprocess.run([sys.executable, "-c", script, str(path)]).returncode == 23
    with indexed(path) as reopened:
        assert reopened.search((), query="apple")[0].value == {"text": "apple"}


@pytest.mark.parametrize("vector", [[0, 0], [float("inf"), 0], [float("nan"), 1], [1]])
def test_bad_vectors_never_commit(tmp_path, vector):
    with MemtomemHybridStore(
        tmp_path / "s.db",
        index={"embed": lambda texts: [vector for _ in texts], "dims": 2},
        index_id="invalid",
    ) as store:
        with pytest.raises(ValueError, match="Embedding"):
            store.put(("a",), "x", {"text": "apple"})
        assert not store.search(())


def test_zero_weight_excludes_leg(tmp_path):
    with indexed(tmp_path / "s.db", retrieval={"weights": [1, 0]}) as store:
        store.put(("a",), "apple", {"text": "apple"})
        store.put(("a",), "pear", {"text": "pear"})
        result = store.search_with_diagnostics((), query="apple")
        assert [r.key for r in result["items"]] == ["apple"]
        assert result["diagnostics"]["candidate_counts"]["dense"] == 0


def test_namespace_ties_use_tuple_order_before_limits(tmp_path):
    with indexed(tmp_path / "s.db", retrieval={"mode": "dense"}) as store:
        namespaces = [("a",), ('a"',), ("a ",), ("a", "b"), ("한글",)]
        for ns in namespaces:
            store.put(ns, "same", {"text": "apple"})
        results = store.search((), query="apple", limit=3)
        assert [r.namespace for r in results] == sorted(namespaces)[:3]


def test_selected_fields_and_index_false_replace_vectors(tmp_path):
    with indexed(tmp_path / "s.db") as store:
        store.put(("a",), "x", {"text": "apple", "extra": "pear"}, index=["extra"])
        result = store.search_with_diagnostics((), query="apple")
        assert result["diagnostics"]["candidate_counts"] == {"bm25": 0, "dense": 1}
        store.put(("a",), "x", {"text": "apple"}, index=False)
        assert not store.search((), query="apple")
        with sqlite3.connect(store.path) as db:
            assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file mode (stat.S_IMODE) — Windows ignores POSIX permission bits",
)
def test_database_file_is_owner_only(store):
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_periodic_sweeper_and_idempotent_close(tmp_path):
    with MemtomemHybridStore(tmp_path / "s.db", ttl={"sweep_interval_minutes": 0.0001}) as store:
        store.put(("a",), "x", {}, ttl=1)
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE items SET expires_at=0")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with sqlite3.connect(store.path) as db:
                if db.execute("SELECT count(*) FROM items").fetchone()[0] == 0:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("Configured sweeper did not remove expired record")
    store.close()


def test_sweeper_survives_a_failed_sweep(tmp_path, caplog):
    """One failing tick logs and keeps sweeping; close() does not re-raise it."""
    with MemtomemHybridStore(tmp_path / "s.db", ttl={"sweep_interval_minutes": 0.0001}) as store:
        database = store._database
        original = database.sweep
        failures = threading.Event()

        def sweep_once_failing():
            if not failures.is_set():
                failures.set()
                raise sqlite3.OperationalError("database is locked")
            return original()

        database.sweep = sweep_once_failing
        assert failures.wait(2), "sweeper never ticked"
        store.put(("a",), "x", {}, ttl=1)
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE items SET expires_at=0")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with sqlite3.connect(store.path) as db:
                if db.execute("SELECT count(*) FROM items").fetchone()[0] == 0:
                    break
            time.sleep(0.01)
        else:
            pytest.fail("Sweeper stopped after a failed sweep")
    assert any("TTL sweep failed" in record.message for record in caplog.records)


def test_async_import_and_embedder(tmp_path):
    async def asynchronous(texts):
        await asyncio.sleep(0)
        return embed(texts)

    async def run():
        async with MemtomemHybridStore(
            tmp_path / "s.db", index={"embed": asynchronous, "dims": 2}, index_id="async"
        ) as store:
            await store.aput(("a",), "x", {"text": "apple"})
            exported = await store.aexport_json()
            await store.adelete(("a",), "x")
            assert await store.aimport_json(exported) == 1
            assert (await store.asearch((), query="apple"))[0].key == "x"

    asyncio.run(run())


@pytest.mark.parametrize(
    "change",
    [
        {"index_id": "different"},
        {"index": {"embed": embed, "dims": 3, "fields": ["text", "extra"]}},
        {"index": {"embed": embed, "dims": 2, "fields": ["text"]}},
    ],
)
def test_reopen_rejects_changed_embedding_contract(tmp_path, change):
    with indexed(tmp_path / "s.db"):
        pass
    options = {
        "index": {"embed": embed, "dims": 2, "fields": ["text", "extra"]},
        "index_id": "deterministic-v1",
        **change,
    }
    with pytest.raises(ValueError, match="Incompatible"):
        MemtomemHybridStore(tmp_path / "s.db", **options)


def test_fts_literal_query_and_lazy_module_export(store):
    from memtomem.integrations.langgraph import MemtomemHybridStore as Alias

    assert Alias is MemtomemHybridStore
    store.put(("a",), "x", {"text": 'Use path/to/file and "quoted" values'})
    assert store.search((), query="path/to/file")[0].key == "x"
    assert store.search((), query='"quoted"')[0].key == "x"


# A store whose reentrancy guard misses the callback deadlocks on a *non-daemon*
# executor thread, so an in-process bound (a timed join, a wait_for) still leaves
# the interpreter hanging at exit and CI stalls instead of reporting. These
# scenarios therefore run in a child process, where the bound is enforceable: a
# regression trips subprocess.TimeoutExpired and the test fails. #2365
_REENTRANCY_TIMEOUT_S = 60

_REENTRANCY_PREAMBLE = """
import asyncio, sys, threading
from memtomem.integrations import MemtomemHybridStore

observed = {}

def vectors(texts):
    return [[1.0, 0.0] for _ in texts]

def report(name, value):
    print(f"{name}={value}", flush=True)
"""


def _run_reentrancy_scenario(script, tmp_path):
    """Run one callback-reentrancy scenario in a child process and return stdout."""
    import os
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    completed = subprocess.run(
        [sys.executable, "-c", _REENTRANCY_PREAMBLE + script, str(tmp_path / "s.db")],
        capture_output=True,
        text=True,
        timeout=_REENTRANCY_TIMEOUT_S,
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home)},
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_refusal_ends_with_the_callback_not_with_the_context(tmp_path):
    """A context copied inside the callback is refused there, and freed after.

    Resetting the ContextVar cannot reach a context the callback copied, so a
    task an embedder spawns would stay locked out of the store for good if the
    marker did not carry its own liveness.
    """
    output = _run_reentrancy_scenario(
        """
import contextvars

copied = {}

def embed(texts):
    copied["context"] = contextvars.copy_context()
    try:
        copied["context"].run(store.get, ("a",), "x")
        report("DURING", "allowed")
    except RuntimeError as exc:
        report("DURING", exc)
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")
store.put(("a",), "x", {"text": "apple"})
report("AFTER", copied["context"].run(store.get, ("a",), "x").value)
store.close()
report("CLOSED", "yes")
""",
        tmp_path,
    )
    assert "DURING=Store operations cannot reenter from an embedding callback\n" in output
    assert "AFTER={'text': 'apple'}\n" in output
    assert "CLOSED=yes\n" in output


# A task an async embedder spawns keeps running on the store's loop after the
# callback returns, so the callback marker is correctly gone by then. What is
# still true of that task is where it runs: a synchronous call from there waits
# on a future only that same loop can complete. These three scenarios share the
# shape — spawn a task inside the callback, release it after the put — and
# differ in what it does with the store afterwards.
_DESCENDANT_TASK = """
release, finished, outcome = threading.Event(), threading.Event(), {}

async def later():
    while not release.is_set():
        await asyncio.sleep(0.01)
    outcome["thread"] = threading.current_thread()
    try:
        outcome["result"] = %s
    except RuntimeError as exc:
        outcome["result"] = str(exc)
    finished.set()

async def embed(texts):
    asyncio.get_running_loop().create_task(later())
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")

async def main():
    await store.aput(("a",), "x", {"text": "apple"})
    release.set()
    report("FINISHED", await asyncio.to_thread(finished.wait, 30))
    report("ON_STORE_THREAD", outcome["thread"] is store._thread)
    report("OUTCOME", outcome["result"])
    # A refusal that lands after close() has already marked the store closed
    # would read the same from the task; only the store's own state tells the
    # two apart, so every scenario reports it.
    report("STILL_OPEN", (await store.aget(("a",), "x")) is not None)
    await store.aclose()
    report("CLOSED", "yes")

asyncio.run(main())
"""


def _assert_descendant_scenario(output, outcome):
    assert "FINISHED=True\n" in output
    assert "ON_STORE_THREAD=True\n" in output
    assert f"OUTCOME={outcome}\n" in output
    assert "STILL_OPEN=True\n" in output
    assert "CLOSED=yes\n" in output


def test_sync_call_from_a_spawned_task_is_refused_not_left_to_block(tmp_path):
    """The loop cannot wait on itself, whether or not a callback is in flight."""
    _assert_descendant_scenario(
        _run_reentrancy_scenario(
            _DESCENDANT_TASK % 'store.get(("a",), "x") and "get returned"', tmp_path
        ),
        "Cannot run a synchronous store operation from the store's event loop; await the async API",
    )


def test_closing_from_a_spawned_task_is_refused_not_left_to_block(tmp_path):
    """close() waits on the loop too, so it is refused from the loop as well."""
    _assert_descendant_scenario(
        _run_reentrancy_scenario(_DESCENDANT_TASK % 'store.close() or "close returned"', tmp_path),
        "Cannot close the store from its own event loop; close it from outside the store's own threads",
    )


def test_aclose_from_a_spawned_task_is_refused_not_left_to_block(tmp_path):
    """Shutdown is the one async method the loop cannot ask for.

    ``aclose()`` offloads ``close()`` to the loop's own default executor, so the
    worker joins the store thread while the store thread waits for that same
    executor to drain. Refusing before the offload is what breaks the cycle.
    """
    _assert_descendant_scenario(
        _run_reentrancy_scenario(
            _DESCENDANT_TASK % 'await store.aclose() or "aclose returned"', tmp_path
        ),
        "Cannot close the store from its own event loop; close it from outside the store's own threads",
    )


def test_async_call_from_a_spawned_task_still_reaches_the_store(tmp_path):
    """The refusal is about blocking, not about the thread: awaiting is allowed.

    Without this, moving the loop-thread check back into ``_submit`` would shut
    a legitimate caller out and every other scenario here would stay green.
    """
    _assert_descendant_scenario(
        _run_reentrancy_scenario(
            _DESCENDANT_TASK % '(await store.aget(("a",), "x")).value', tmp_path
        ),
        "{'text': 'apple'}",
    )


def test_sync_embedder_reentry_raises_instead_of_hanging(tmp_path):
    """A sync embedder runs off the store thread, and reentry is still refused."""
    output = _run_reentrancy_scenario(
        """
def embed(texts):
    observed["thread"] = threading.current_thread()
    store.get(("a",), "x")
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")
try:
    store.put(("a",), "x", {"text": "apple"})
    report("OUTCOME", "put returned")
except RuntimeError as exc:
    report("OUTCOME", exc)
report("ON_STORE_THREAD", observed["thread"] is store._thread)
store.close()
report("CLOSED", "yes")
""",
        tmp_path,
    )
    assert "OUTCOME=Store operations cannot reenter from an embedding callback\n" in output
    # The premise the guard has to survive: this callback is on a foreign
    # thread, so a thread-identity check would have waved it through.
    assert "ON_STORE_THREAD=False\n" in output
    assert "CLOSED=yes\n" in output


def test_reentrant_query_embedder_falls_back_to_bm25(tmp_path):
    """The refusal surfaces where the callback made it, not at the outer search.

    A hybrid search treats the reentry error like any other embedding failure,
    so the caller gets BM25 results and a warning rather than the RuntimeError.
    The guide says so; this is what says it is true.
    """
    output = _run_reentrancy_scenario(
        """
import warnings

reentering = {"now": False}

def embed(texts):
    if reentering["now"]:
        store.get(("a",), "x")
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")
store.put(("a",), "x", {"text": "apple"})
reentering["now"] = True
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    result = store.search_with_diagnostics((), query="apple")
report("KEYS", [item.key for item in result["items"]])
report("FALLBACK", result["diagnostics"]["fallback_reason"])
report("WARNED", any("BM25" in str(warning.message) for warning in caught))
store.close()
report("CLOSED", "yes")
""",
        tmp_path,
    )
    assert "KEYS=['x']\n" in output
    assert "FALLBACK=query_embedding_failed\n" in output
    assert "WARNED=True\n" in output
    assert "CLOSED=yes\n" in output


def test_offloaded_close_inside_a_live_callback_is_refused(tmp_path):
    """`to_thread(close)` during the callback carries the marker to the worker.

    This is the hop the guide calls undetected *after* the callback returns. It
    is detected while the callback runs, because the context travels with it,
    and close() is where that refusal lands.
    """
    output = _run_reentrancy_scenario(
        """
async def embed(texts):
    try:
        await asyncio.to_thread(store.close)
        report("OUTCOME", "close returned")
    except RuntimeError as exc:
        report("OUTCOME", exc)
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")

async def main():
    await store.aput(("a",), "x", {"text": "apple"})
    report("STILL_OPEN", (await store.aget(("a",), "x")) is not None)
    await store.aclose()
    report("CLOSED", "yes")

asyncio.run(main())
""",
        tmp_path,
    )
    assert "OUTCOME=Cannot close the store from its embedding callback\n" in output
    assert "STILL_OPEN=True\n" in output
    assert "CLOSED=yes\n" in output


def test_sync_embedder_closing_raises_instead_of_hanging(tmp_path):
    """close() from a sync callback is refused, and leaves the store usable."""
    output = _run_reentrancy_scenario(
        """
def embed(texts):
    observed["thread"] = threading.current_thread()
    store.close()
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")
try:
    store.put(("a",), "x", {"text": "apple"})
    report("OUTCOME", "put returned")
except RuntimeError as exc:
    report("OUTCOME", exc)
report("ON_STORE_THREAD", observed["thread"] is store._thread)
store.put(("a",), "later", {"text": "pear"}, index=False)
report("STILL_OPEN", store.get(("a",), "later") is not None)
store.close()
report("CLOSED", "yes")
""",
        tmp_path,
    )
    assert "OUTCOME=Cannot close the store from its embedding callback\n" in output
    assert "ON_STORE_THREAD=False\n" in output
    assert "STILL_OPEN=True\n" in output
    assert "CLOSED=yes\n" in output


def test_async_embedder_awaiting_aclose_raises_instead_of_hanging(tmp_path):
    """An async embedder runs *on* the store thread, and aclose() is refused there."""
    output = _run_reentrancy_scenario(
        """
offloads = []
to_thread = asyncio.to_thread

async def counting_to_thread(function, *args, **kwargs):
    offloads.append(function)
    return await to_thread(function, *args, **kwargs)

asyncio.to_thread = counting_to_thread

async def embed(texts):
    observed["thread"] = threading.current_thread()
    await store.aclose()
    return vectors(texts)

store = MemtomemHybridStore(sys.argv[1], index={"embed": embed, "dims": 2}, index_id="reentrant")

async def main():
    try:
        await store.aput(("a",), "x", {"text": "apple"})
        report("OUTCOME", "aput returned")
    except RuntimeError as exc:
        report("OUTCOME", exc)
    report("OFFLOADED", bool(offloads))
    report("ON_STORE_THREAD", observed["thread"] is store._thread)
    await store.aput(("a",), "later", {"text": "pear"}, index=False)
    report("STILL_OPEN", await store.aget(("a",), "later") is not None)
    await store.aclose()
    report("CLOSED", "yes")

asyncio.run(main())
""",
        tmp_path,
    )
    assert "OUTCOME=Cannot close the store from its embedding callback\n" in output
    # close() raises the same message, so name which guard refused: aclose()
    # answered in the caller's own frame and never reached asyncio.to_thread.
    assert "OFFLOADED=False\n" in output
    # The other direction of the same premise: here the callback *is* the store
    # thread, which is why one identity check cannot cover both cases.
    assert "ON_STORE_THREAD=True\n" in output
    assert "STILL_OPEN=True\n" in output
    assert "CLOSED=yes\n" in output
