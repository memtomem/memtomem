"""Source-lock and storage-CAS contracts for namespace mutations (#2016)."""

from __future__ import annotations

import ast
import dataclasses
import multiprocessing
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from helpers import make_chunk
from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.errors import NamespaceMutationBusyError
from memtomem.services import namespace_management
from memtomem.storage.base import NamespaceChunkCandidate, NamespaceRenameResult


def _hold_sidecar_in_child(lock_path: str, ready, release) -> None:
    """Process target: prove the coordinator contends on the OS-level lock."""
    with _file_lock(Path(lock_path)):
        ready.send(True)
        release.recv()


def _candidate(source: Path, namespace: str = "old") -> NamespaceChunkCandidate:
    return NamespaceChunkCandidate(
        chunk_id=uuid4(),
        source_file=source,
        source_file_text=str(source),
        namespace=namespace,
    )


class TestNamespaceCoordinator:
    async def test_retargets_new_sources_then_acquires_the_sorted_lock_set(
        self, tmp_path: Path, monkeypatch
    ):
        source_a = tmp_path / "a.md"
        source_b = tmp_path / "b.md"
        candidate_a = _candidate(source_a)
        candidate_b = _candidate(source_b)
        storage = AsyncMock()
        storage.list_namespace_chunk_candidates = AsyncMock(
            side_effect=[
                [candidate_b],
                [candidate_b, candidate_a],
                [candidate_b, candidate_a],
                [candidate_b, candidate_a],
            ]
        )
        expected = NamespaceRenameResult(2, False, False)
        storage.rename_namespace = AsyncMock(return_value=expected)
        events: list[tuple[str, Path]] = []
        timeouts: list[float] = []

        @asynccontextmanager
        async def fake_lock(lock_path: Path, *, timeout: float):
            events.append(("enter", lock_path))
            timeouts.append(timeout)
            try:
                yield
            finally:
                events.append(("exit", lock_path))

        monkeypatch.setattr(namespace_management, "async_file_lock", fake_lock)

        result = await namespace_management.rename_namespace(storage, "old", "new")

        assert result is expected
        lock_a = _lock_path_for(source_a.resolve())
        lock_b = _lock_path_for(source_b.resolve())
        assert events == [
            ("enter", lock_b),
            ("exit", lock_b),
            ("enter", lock_a),
            ("enter", lock_b),
            ("exit", lock_b),
            ("exit", lock_a),
        ]
        assert all(0 < timeout <= 30 for timeout in timeouts)
        storage.rename_namespace.assert_awaited_once_with(
            "old",
            "new",
            merge=False,
            candidates=[candidate_b, candidate_a],
        )
        assert storage.list_namespace_chunk_candidates.await_count == 3

    async def test_candidate_churn_stops_after_three_retarget_attempts(
        self, tmp_path: Path, monkeypatch
    ):
        candidates = [_candidate(tmp_path / f"{name}.md") for name in "abcd"]
        snapshots = [candidates[:1], candidates[:2], candidates[:3], candidates[:4]]
        storage = AsyncMock()
        storage.list_namespace_chunk_candidates = AsyncMock(side_effect=snapshots)
        storage.rename_namespace = AsyncMock()

        @asynccontextmanager
        async def fake_lock(_lock_path: Path, *, timeout: float):
            assert timeout > 0
            yield

        monkeypatch.setattr(namespace_management, "async_file_lock", fake_lock)

        with pytest.raises(NamespaceMutationBusyError, match="3 attempt"):
            await namespace_management.rename_namespace(storage, "old", "new")

        storage.rename_namespace.assert_not_awaited()
        assert storage.list_namespace_chunk_candidates.await_count == 4

    async def test_missing_source_parent_is_not_recreated_for_a_sidecar(
        self, tmp_path: Path, monkeypatch
    ):
        source = tmp_path / "removed" / "source.md"
        candidate = _candidate(source)
        storage = AsyncMock()
        storage.list_namespace_chunk_candidates = AsyncMock(side_effect=[[candidate], [candidate]])
        expected = NamespaceRenameResult(1, False, False)
        storage.rename_namespace = AsyncMock(return_value=expected)
        lock = AsyncMock()
        monkeypatch.setattr(namespace_management, "async_file_lock", lock)

        result = await namespace_management.rename_namespace(storage, "old", "new")

        assert result is expected
        assert not source.parent.exists()
        lock.assert_not_called()

    async def test_cross_process_source_lock_refuses_without_touching_rows(
        self, storage, tmp_path: Path, monkeypatch
    ):
        source = tmp_path / "source.md"
        source.write_text("source", encoding="utf-8")
        chunk = make_chunk(content="one", namespace="old")
        chunk = dataclasses.replace(
            chunk,
            metadata=dataclasses.replace(chunk.metadata, source_file=source),
        )
        await storage.upsert_chunks([chunk])

        mp = multiprocessing.get_context("spawn")
        ready_parent, ready_child = mp.Pipe(duplex=False)
        release_child, release_parent = mp.Pipe(duplex=False)
        process = mp.Process(
            target=_hold_sidecar_in_child,
            args=(str(_lock_path_for(source.resolve())), ready_child, release_child),
        )
        process.start()
        ready_child.close()
        release_child.close()
        try:
            assert ready_parent.poll(5), "child did not acquire the sidecar"
            assert ready_parent.recv() is True
            monkeypatch.setattr(
                namespace_management,
                "_NAMESPACE_MUTATION_BUDGET_S",
                0.15,
            )

            with pytest.raises(NamespaceMutationBusyError, match="nothing.*changed"):
                await namespace_management.rename_namespace(storage, "old", "new")

            remaining = await storage.get_chunk(chunk.id)
            assert remaining is not None
            assert remaining.metadata.namespace == "old"
        finally:
            if process.is_alive():
                release_parent.send(True)
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            ready_parent.close()
            release_parent.close()
        assert process.exitcode == 0


class TestNamespaceStorageCandidateCAS:
    async def test_rename_preserves_noncanonical_persisted_source_text(self, storage):
        chunk = make_chunk(content="first", namespace="old", source="first.md")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        canonical = db.execute(
            "SELECT source_file FROM chunks WHERE id=?", (str(chunk.id),)
        ).fetchone()[0]
        path = Path(canonical)
        noncanonical = f"{path.parent}//{path.name}"
        assert str(Path(noncanonical)) != noncanonical
        db.execute(
            "UPDATE chunks SET source_file=? WHERE id=?",
            (noncanonical, str(chunk.id)),
        )
        db.commit()

        snapshot = await storage.list_namespace_chunk_candidates(namespace="old")

        assert snapshot[0].source_file_text == noncanonical
        result = await storage.rename_namespace("old", "new", candidates=snapshot)
        assert result.chunks_moved == 1
        assert (await storage.get_chunk(chunk.id)).metadata.namespace == "new"

    async def test_rename_rejects_an_extra_candidate_and_rolls_back_every_table(self, storage):
        first = make_chunk(content="first", namespace="old", source="first.md")
        await storage.upsert_chunks([first])
        await storage.set_namespace_meta("old", description="source metadata")
        await storage.create_session("session", "agent", "old", {})
        snapshot = await storage.list_namespace_chunk_candidates(namespace="old")
        late = make_chunk(content="late", namespace="old", source="late.md")
        await storage.upsert_chunks([late])

        with pytest.raises(NamespaceMutationBusyError, match="nothing was changed"):
            await storage.rename_namespace(
                "old",
                "new",
                candidates=snapshot,
            )

        assert (await storage.get_chunk(first.id)).metadata.namespace == "old"
        assert (await storage.get_chunk(late.id)).metadata.namespace == "old"
        assert await storage.get_namespace_meta("old") is not None
        assert await storage.get_namespace_meta("new") is None
        assert (await storage.get_session("session"))["namespace"] == "old"
        assert storage._get_db().in_transaction is False
        assert (
            storage._get_db()
            .execute(
                "SELECT 1 FROM sqlite_temp_master WHERE name=?",
                ("_ns_candidates",),
            )
            .fetchone()
            is None
        )

    async def test_assign_rejects_a_candidate_whose_namespace_changed(self, storage):
        chunk = make_chunk(content="first", namespace="old", source="first.md")
        await storage.upsert_chunks([chunk])
        snapshot = await storage.list_namespace_chunk_candidates(
            namespace="old",
            exclude_namespace="target",
        )
        db = storage._get_db()
        db.execute("UPDATE chunks SET namespace='other' WHERE id=?", (str(chunk.id),))
        db.commit()

        with pytest.raises(NamespaceMutationBusyError, match="nothing was changed"):
            await storage.assign_namespace(
                "target",
                old_namespace="old",
                candidates=snapshot,
            )

        assert (await storage.get_chunk(chunk.id)).metadata.namespace == "other"
        assert db.in_transaction is False


def test_public_surfaces_do_not_call_raw_namespace_storage_writers() -> None:
    """Architecture guard: raw writers belong to the coordinator and backend."""
    source_root = Path(__file__).parents[1] / "src" / "memtomem"
    allowed = {
        Path("services/namespace_management.py"),
        Path("storage/sqlite_backend.py"),
    }
    offenders: list[str] = []

    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root)
        if relative in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"rename_namespace", "assign_namespace"}:
                continue
            value = node.func.value
            parts: list[str] = []
            while isinstance(value, ast.Attribute):
                parts.append(value.attr)
                value = value.value
            if isinstance(value, ast.Name):
                parts.append(value.id)
            if "storage" in parts:
                offenders.append(f"{relative}:{node.lineno}")

    assert offenders == []
