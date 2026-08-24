"""Migration backfill that adopts pre-#2161 consolidation summaries.

Ownership over ``<source>.consolidated.md`` moved from a namespace + tag
inference to the typed ``chunks.origin`` stamp. Summaries written before that
column existed carry no stamp, so a one-shot pass adopts the rows that
reproduce the whole shape ``_make_summary_chunk`` has always written — path
suffix, all three legacy tags, and the ``Consolidated: <name>`` heading derived
from the row's own path — are the target of a ``consolidated_into`` edge, and
sit in a namespace some consolidation policy actually writes summaries under.

The load-bearing cases are the negative ones. Every part of the shape is
metadata a user can type, and even the edge is reachable through ``mem_link``,
so no single conjunct is proof; the namespace is what guarantees this pass can
never reach a row the predicate it replaces would have spared.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import sqlite_vec

from memtomem.models import ORIGIN_CONSOLIDATION_POLICY
from memtomem.storage.sqlite_meta import MetaManager
from memtomem.storage.sqlite_schema import _ORIGIN_BACKFILL_KEY, _backfill_summary_origin


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def _initialize(db: sqlite3.Connection) -> MetaManager:
    from memtomem.storage.sqlite_schema import create_tables

    meta = MetaManager(lambda: db)
    create_tables(db, meta, dimension=0, embedding_provider="none", embedding_model="")
    return meta


def _insert_chunk(
    db: sqlite3.Connection,
    chunk_id: str,
    *,
    source_file: str,
    tags: list[str],
    headings: list[str],
    namespace: str = "archive:summary",
    origin: str | None = None,
    consolidated_into: bool = True,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, heading_hierarchy, "
        "namespace, tags, created_at, updated_at, origin) "
        "VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            chunk_id,
            source_file,
            json.dumps(headings),
            namespace,
            json.dumps(tags),
            now,
            now,
            origin,
        ),
    )
    if consolidated_into:
        # A real summary is the target of edges written from the ids of the
        # chunks it summarised; the seeded source row stands in for one.
        source_id = f"{chunk_id}-src"
        db.execute(
            "INSERT INTO chunks (id, content, content_hash, source_file, namespace, "
            "created_at, updated_at) VALUES (?, '', ?, ?, ?, ?, ?)",
            (
                source_id,
                source_id,
                source_file.replace(".consolidated.md", ""),
                namespace,
                now,
                now,
            ),
        )
        db.execute(
            "INSERT INTO chunk_relations (source_id, target_id, relation_type, created_at) "
            "VALUES (?, ?, 'consolidated_into', ?)",
            (source_id, chunk_id, now),
        )


def _rerun_backfill(db: sqlite3.Connection, meta: MetaManager) -> int:
    """Re-arm and run the one-shot pass.

    ``create_tables`` already recorded it done while the DB was empty, which is
    what a real upgrade does *after* the rows exist — seeding then re-arming is
    how a pre-migration store is reproduced in-memory.
    """
    meta.set_meta(_ORIGIN_BACKFILL_KEY, "")
    return _backfill_summary_origin(db, meta)


def _origin_of(db: sqlite3.Connection, chunk_id: str) -> str | None:
    row = db.execute("SELECT origin FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
    return row[0]


class TestAdoption:
    def test_a_legacy_summary_is_adopted(self) -> None:
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "legacy",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
            )

            assert _rerun_backfill(db, meta) == 1
            assert _origin_of(db, "legacy") == ORIGIN_CONSOLIDATION_POLICY
        finally:
            db.close()

    def test_a_windows_stored_path_is_adopted(self) -> None:
        """A store can travel between platforms, so the separator is not fixed."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "legacy-win",
                source_file=r"C:\Users\me\notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
            )

            assert _rerun_backfill(db, meta) == 1
            assert _origin_of(db, "legacy-win") == ORIGIN_CONSOLIDATION_POLICY
        finally:
            db.close()


class TestConfiguredNamespace:
    """``summary_namespace`` is configurable, and the policy row records it."""

    def _policy(self, db: sqlite3.Connection, config: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO memory_policies (id, name, policy_type, config, created_at, updated_at) "
            "VALUES ('p1', 'consol', 'auto_consolidate', ?, ?, ?)",
            (config, now, now),
        )

    def test_a_summary_under_a_configured_namespace_is_adopted(self) -> None:
        db = _connect()
        try:
            meta = _initialize(db)
            self._policy(db, '{"summary_namespace": "archive:custom"}')
            _insert_chunk(
                db,
                "custom-ns",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
                namespace="archive:custom",
            )

            assert _rerun_backfill(db, meta) == 1
            assert _origin_of(db, "custom-ns") == ORIGIN_CONSOLIDATION_POLICY
        finally:
            db.close()

    def test_a_namespace_no_policy_names_stays_foreign(self) -> None:
        """Fail closed: refusing costs a regeneration, adopting could cost data."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "unknown-ns",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
                namespace="archive:custom",
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "unknown-ns") is None
        finally:
            db.close()

    def test_the_default_matches_the_engines_constant(self) -> None:
        """Storage duplicates the value rather than importing from ``tools``."""
        from memtomem.storage.sqlite_schema import _DEFAULT_SUMMARY_NAMESPACE
        from memtomem.tools.consolidation_engine import DEFAULT_SUMMARY_NAMESPACE

        assert _DEFAULT_SUMMARY_NAMESPACE == DEFAULT_SUMMARY_NAMESPACE


class TestRefusals:
    def test_the_old_predicates_lookalike_stays_foreign(self) -> None:
        """Namespace + all three tags is exactly what must no longer be enough.

        This chunk is the collision from #2161 sitting in a store at upgrade
        time. Adopting it would hand the policy permission to delete a user's
        own note the first time it consolidated that source."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "lookalike",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["My own notes about consolidation"],
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "lookalike") is None
        finally:
            db.close()

    def test_an_exact_shape_lookalike_without_edges_stays_foreign(self) -> None:
        """Every part of the shape is metadata a user can type.

        A real file at ``notes.md.consolidated.md`` carrying the three tags and
        the derived heading — in ``default``, where the old namespace-bearing
        predicate would never have claimed it — must not be adopted. The edge
        rules out the ordinary lookalike, a note that merely reads like a
        summary; it is not itself proof, since ``mem_link`` takes an arbitrary
        ``relation_type`` (the sibling test below pins the namespace conjunct
        that covers that case).

        Mutation check: dropping the edge requirement from the query adopts
        this row and fails the test."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "user-file",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
                namespace="default",
                consolidated_into=False,
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "user-file") is None
        finally:
            db.close()

    def test_an_exact_shape_lookalike_with_a_user_written_edge_stays_foreign(self) -> None:
        """The edge is a conjunct, not the proof — ``mem_link`` can write one.

        ``mem_link(relation_type="consolidated_into")`` is a public tool, so an
        exact-shape chunk in ``default`` could arrive carrying the edge too.
        The namespace conjunct is what keeps this pass from ever reaching a row
        the predicate it replaces would have spared.

        Mutation check: dropping the namespace test adopts this row."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "user-file-linked",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
                namespace="default",
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "user-file-linked") is None
        finally:
            db.close()

    def test_a_summary_whose_edges_never_landed_stays_foreign(self) -> None:
        """The partial write #2158 fixed leaves a summary with no edges.

        Fail-closed is the right side to land on: the source's next
        consolidation refuses instead of deleting, and removing the orphaned
        summary recovers it."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "edgeless",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
                consolidated_into=False,
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "edgeless") is None
        finally:
            db.close()

    def test_a_heading_naming_another_source_stays_foreign(self) -> None:
        """The heading has to be derived from *this* row's path, not any path."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "wrong-source",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: other.md"],
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "wrong-source") is None
        finally:
            db.close()

    def test_an_agent_summary_stays_foreign(self) -> None:
        """``mem_consolidate_apply`` writes two of the three tags."""
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "agent",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary"],
                headings=["Consolidated: notes.md"],
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "agent") is None
        finally:
            db.close()

    def test_a_chunk_outside_the_virtual_path_stays_foreign(self) -> None:
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "ordinary",
                source_file="/tmp/notes.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
            )

            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "ordinary") is None
        finally:
            db.close()


class TestOneShot:
    def test_create_tables_records_the_pass_and_re_runs_are_no_ops(self) -> None:
        db = _connect()
        try:
            meta = _initialize(db)
            assert meta.get_meta(_ORIGIN_BACKFILL_KEY) == "done"

            _insert_chunk(
                db,
                "after-migration",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
            )

            # Already recorded done, so a row that appears afterwards is not
            # adopted — the pass is a one-time transition, not a rule.
            assert _backfill_summary_origin(db, meta) == 0
            assert _origin_of(db, "after-migration") is None
        finally:
            db.close()

    def test_an_existing_stamp_is_left_alone(self) -> None:
        db = _connect()
        try:
            meta = _initialize(db)
            _insert_chunk(
                db,
                "already-stamped",
                source_file="/tmp/notes.md.consolidated.md",
                tags=["consolidated", "summary", "heuristic"],
                headings=["Consolidated: notes.md"],
                origin=ORIGIN_CONSOLIDATION_POLICY,
            )

            # Nothing to do: the row is already owned, so it is not re-counted.
            assert _rerun_backfill(db, meta) == 0
            assert _origin_of(db, "already-stamped") == ORIGIN_CONSOLIDATION_POLICY
        finally:
            db.close()
