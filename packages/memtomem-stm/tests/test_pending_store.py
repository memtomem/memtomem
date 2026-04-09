"""Tests for PendingStore backends and SelectiveCompressor integration."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


from memtomem_stm.proxy.compression import PendingSelection, SelectiveCompressor
from memtomem_stm.proxy.pending_store import InMemoryPendingStore, SQLitePendingStore


def _make_selection(chunks: dict[str, str] | None = None) -> PendingSelection:
    return PendingSelection(
        chunks=chunks or {"sec1": "content1", "sec2": "content2"},
        format="markdown",
        created_at=time.monotonic(),
        total_chars=100,
    )


# ── InMemoryPendingStore ────────────────────────────────────────────────


class TestInMemoryPendingStore:
    def test_put_and_get(self):
        store = InMemoryPendingStore()
        sel = _make_selection()
        store.put("k1", sel)
        assert store.get("k1") is sel
        assert store.get("missing") is None

    def test_touch_updates_created_at(self):
        store = InMemoryPendingStore()
        sel = _make_selection()
        original_time = sel.created_at
        store.put("k1", sel)
        time.sleep(0.01)
        store.touch("k1")
        assert store.get("k1").created_at > original_time

    def test_evict_expired(self):
        store = InMemoryPendingStore()
        sel = _make_selection()
        sel.created_at = time.monotonic() - 100  # expired
        store.put("old", sel)
        store.put("new", _make_selection())
        store.evict_expired(ttl=50)
        assert store.get("old") is None
        assert store.get("new") is not None

    def test_evict_oldest(self):
        store = InMemoryPendingStore()
        for i in range(5):
            store.put(f"k{i}", _make_selection())
        assert len(store) == 5
        store.evict_oldest(max_size=3)
        assert len(store) == 3
        # k0 and k1 should be evicted (oldest)
        assert store.get("k0") is None
        assert store.get("k1") is None
        assert store.get("k4") is not None

    def test_len(self):
        store = InMemoryPendingStore()
        assert len(store) == 0
        store.put("k1", _make_selection())
        assert len(store) == 1
        store.delete("k1")
        assert len(store) == 0


# ── SQLitePendingStore ──────────────────────────────────────────────────


class TestSQLitePendingStore:
    def _make_store(self, tmp_path: Path) -> SQLitePendingStore:
        store = SQLitePendingStore(tmp_path / "pending.db")
        store.initialize()
        return store

    def test_put_and_get(self, tmp_path):
        store = self._make_store(tmp_path)
        sel = _make_selection({"a": "alpha", "b": "beta"})
        store.put("k1", sel)
        result = store.get("k1")
        assert result is not None
        assert result.chunks == {"a": "alpha", "b": "beta"}
        assert result.format == "markdown"
        assert result.total_chars == 100
        assert store.get("missing") is None
        store.close()

    def test_touch(self, tmp_path):
        store = self._make_store(tmp_path)
        store.put("k1", _make_selection())
        original = store.get("k1").created_at
        time.sleep(0.05)
        store.touch("k1")
        updated = store.get("k1").created_at
        assert updated > original
        store.close()

    def test_evict_expired(self, tmp_path):
        store = self._make_store(tmp_path)
        sel = _make_selection()
        store.put("k1", sel)
        # Manually set old timestamp
        store._get_db().execute(
            "UPDATE pending_selections SET created_at = ? WHERE key = ?",
            (time.time() - 1000, "k1"),
        )
        store._get_db().commit()
        store.put("k2", _make_selection())
        store.evict_expired(ttl=500)
        assert store.get("k1") is None
        assert store.get("k2") is not None
        store.close()

    def test_evict_oldest(self, tmp_path):
        store = self._make_store(tmp_path)
        for i in range(5):
            store.put(f"k{i}", _make_selection())
            time.sleep(0.01)  # ensure different timestamps
        assert len(store) == 5
        store.evict_oldest(max_size=2)
        assert len(store) == 2
        store.close()

    def test_persistence_across_reopen(self, tmp_path):
        """Data survives close + reopen."""
        db_path = tmp_path / "persist.db"
        store1 = SQLitePendingStore(db_path)
        store1.initialize()
        store1.put("k1", _make_selection({"x": "data"}))
        store1.close()

        store2 = SQLitePendingStore(db_path)
        store2.initialize()
        result = store2.get("k1")
        assert result is not None
        assert result.chunks == {"x": "data"}
        store2.close()

    def test_concurrent_access(self, tmp_path):
        """Multiple threads can put/get without errors."""
        store = self._make_store(tmp_path)
        errors: list[Exception] = []

        def writer(tid: int):
            try:
                for i in range(20):
                    store.put(f"t{tid}_k{i}", _make_selection())
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(store) == 60  # 3 threads × 20 entries
        store.close()


# ── SelectiveCompressor + Store integration ─────────────────────────────


class TestSelectiveCompressorWithStore:
    def test_inmemory_default(self):
        """Default compressor uses InMemoryPendingStore."""
        comp = SelectiveCompressor()
        assert isinstance(comp._store, InMemoryPendingStore)

    def test_inmemory_compress_and_select(self):
        """Basic compress → select cycle with InMemory."""
        comp = SelectiveCompressor()
        text = "# Title\n\n## A\n" + "Content A " * 50 + "\n\n## B\n" + "Content B " * 50 + "\n"
        result = comp.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]
        selected = comp.select(key, ["A"])
        assert "Content A" in selected

    def test_sqlite_compress_and_select(self, tmp_path):
        """Compress → select cycle with SQLite store."""
        store = SQLitePendingStore(tmp_path / "sel.db")
        store.initialize()
        comp = SelectiveCompressor(store=store)
        text = "# Doc\n\n## X\n" + "Data X " * 50 + "\n\n## Y\n" + "Data Y " * 50 + "\n"
        result = comp.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]
        selected = comp.select(key, ["X"])
        assert "Data X" in selected
        store.close()

    def test_multi_instance_shared_sqlite(self, tmp_path):
        """Two compressors sharing one SQLite DB can cross-select."""
        db_path = tmp_path / "shared.db"

        store1 = SQLitePendingStore(db_path)
        store1.initialize()
        comp1 = SelectiveCompressor(store=store1)

        store2 = SQLitePendingStore(db_path)
        store2.initialize()
        comp2 = SelectiveCompressor(store=store2)

        # comp1 creates a TOC
        text = "# Title\n\n## Section1\n" + "Hello world " * 50 + "\n\n## Section2\n" + "Goodbye " * 50 + "\n"
        result = comp1.compress(text, max_chars=50)
        toc = json.loads(result)
        key = toc["selection_key"]

        # comp2 can retrieve from the same DB
        selected = comp2.select(key, ["Section1"])
        assert "Hello world" in selected

        store1.close()
        store2.close()

    def test_config_memory_store(self):
        """pending_store='memory' creates InMemoryPendingStore."""
        from memtomem_stm.proxy.config import SelectiveConfig

        cfg = SelectiveConfig(pending_store="memory")
        assert cfg.pending_store == "memory"

    def test_config_sqlite_store(self, tmp_path):
        """pending_store='sqlite' creates SQLitePendingStore via manager helper."""
        from memtomem_stm.proxy.config import SelectiveConfig

        cfg = SelectiveConfig(
            pending_store="sqlite",
            pending_store_path=tmp_path / "test.db",
        )
        assert cfg.pending_store == "sqlite"

        # Simulate what ProxyManager._create_selective does
        store = SQLitePendingStore(cfg.pending_store_path)
        store.initialize()
        comp = SelectiveCompressor(
            max_pending=cfg.max_pending,
            pending_ttl_seconds=cfg.pending_ttl_seconds,
            store=store,
        )
        text = "# T\n\n## A\n" + "Content " * 50 + "\n\n## B\n" + "More " * 50 + "\n"
        result = comp.compress(text, max_chars=50)
        assert "selection_key" in result
        store.close()
