"""mem_export -> mem_import cross-instance roundtrip (bundle v2).

Question 1 from the mm export/import plan: if we export chunks from instance A
and import into a fresh instance B (simulating two PCs), does B reconstitute A?

Phase 1 established the baseline and flagged two defects (re-import duplicated
rows, collision-merge duplicated rows). Phase 2 lands the v2 bundle schema
(``content_hash`` + ``chunk_id`` per record) and an ``on_conflict`` parameter
on ``import_chunks``; this file now asserts the fixed behaviour.

Covers:
  * cross-PC single roundtrip (content + metadata + top-k preserved,
    chunk.id now preserved too)
  * re-import is idempotent under the default ``on_conflict="skip"``
  * ``on_conflict="update"`` reuses existing UUIDs for hash matches
  * ``on_conflict="duplicate"`` preserves the v1 back-compat behaviour
  * disjoint merge is additive
  * content-collision merge dedupes by hash under ``"skip"``

Run with: ``uv run pytest tests/test_export_import_roundtrip.py -s``
(``-s`` to surface the [BASELINE] print lines, kept for eyeball checks).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components, close_components, create_components
from memtomem.tools.export_import import export_chunks, import_chunks

pytest.importorskip(
    "fastembed",
    reason="fastembed not installed — install with `pip install memtomem[onnx]`",
)


# Synthetic corpus — distinctive content per doc so content_hash and top-k
# overlap are easy to reason about. Mixes English + Korean to verify the
# multilingual path survives the roundtrip.
_CORPUS: dict[str, str] = {
    "caching.md": (
        "# Redis caching\n\n"
        "## Sentinel over Cluster\n"
        "We adopted Redis Sentinel for automated failover without the hash slot\n"
        "management complexity of Cluster mode.\n\n"
        "## volatile-lru eviction\n"
        "Only keys with an explicit TTL are evicted under memory pressure, which\n"
        "protects persistent session metadata.\n"
    ),
    "k8s.md": (
        "# Kubernetes\n\n"
        "## HPA autoscaling\n"
        "Horizontal pod autoscaling reacts to CPU and custom Prometheus metrics.\n\n"
        "## Prometheus monitoring\n"
        "Cluster metrics are scraped by Prometheus and visualized in Grafana.\n"
    ),
    "postgres.md": (
        "# PostgreSQL\n\n"
        "## Connection pooling\n"
        "pgbouncer runs in transaction mode to keep pool churn low for short\n"
        "web requests.\n\n"
        "## Vacuum tuning\n"
        "autovacuum_vacuum_scale_factor is 0.05 on hot tables to keep bloat down.\n"
    ),
    "korean.md": (
        "# 한국어 메모\n\n"
        "## 모니터링 설정\n"
        "쿠버네티스 클러스터 모니터링을 프로메테우스와 그라파나로 구축했다.\n\n"
        "## 캐시 정책\n"
        "세션 캐시는 volatile-lru 정책으로 TTL 만료 키만 제거한다.\n"
    ),
}


async def _make_onnx_components(tmp_path: Path, tag: str) -> tuple[Components, Path]:
    """Build a hermetic onnx-backed component stack with its own DB + mem dir.

    Mirrors ``conftest.onnx_components`` but is parameterised so we can spin up
    two independent instances (PC A, PC B) inside one test.
    """
    db_path = tmp_path / f"{tag}.db"
    mem_dir = tmp_path / f"{tag}_mem"
    mem_dir.mkdir(parents=True, exist_ok=True)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    config.embedding.dimension = 384

    import memtomem.config as _cfg

    _orig = _cfg.load_config_overrides

    def _noop(config: Mem2MemConfig) -> None:
        return None

    _cfg.load_config_overrides = _noop
    try:
        comp = await create_components(config)
    finally:
        _cfg.load_config_overrides = _orig
    return comp, mem_dir


# Second corpus — disjoint content for the "PC_B already has its own chunks"
# merge scenario. Zero content overlap with _CORPUS so hashes must not collide.
_CORPUS_B: dict[str, str] = {
    "security.md": (
        "# Security posture\n\n"
        "## Secret rotation\n"
        "All service account keys rotate every 90 days via the platform issuer.\n\n"
        "## mTLS between services\n"
        "Internal traffic is mutually authenticated with short-lived SPIFFE IDs.\n"
    ),
    "logging.md": (
        "# Structured logging\n\n"
        "## JSON format\n"
        "All services emit JSON logs keyed by trace_id for cross-service correlation.\n"
    ),
}


async def _index_corpus(comp: Components, mem_dir: Path, corpus: dict[str, str] = _CORPUS) -> None:
    for name, content in corpus.items():
        path = mem_dir / name
        path.write_text(content, encoding="utf-8")
        await comp.index_engine.index_file(path)


class TestRoundtripBaseline:
    """One consolidated baseline — prints metrics, fails only on must-holds."""

    async def test_phase1_baseline(self, tmp_path):
        comp_a, mem_a = await _make_onnx_components(tmp_path, "pc_a")
        comp_b, _ = await _make_onnx_components(tmp_path, "pc_b")
        try:
            await _index_corpus(comp_a, mem_a)
            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)

            bundle_path = tmp_path / "bundle.json"
            bundle = await export_chunks(comp_a.storage, output_path=bundle_path)
            stats = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)
            chunks_b = await comp_b.storage.recall_chunks(limit=10_000)

            # --- Metric 1: chunk counts --------------------------------------
            print(
                f"\n[BASELINE] counts | A={len(chunks_a)} B={len(chunks_b)} "
                f"bundle.total={bundle.total_chunks} imported={stats.imported_chunks} "
                f"skipped={stats.skipped_chunks} failed={stats.failed_chunks}"
            )

            # --- Metric 2: content_hash set equality -------------------------
            hashes_a = {c.content_hash for c in chunks_a}
            hashes_b = {c.content_hash for c in chunks_b}
            print(
                f"[BASELINE] content_hash | A={len(hashes_a)} B={len(hashes_b)} "
                f"A∩B={len(hashes_a & hashes_b)} "
                f"A-only={len(hashes_a - hashes_b)} B-only={len(hashes_b - hashes_a)}"
            )

            # --- Metric 3: chunk id preservation -----------------------------
            # v2 bundles carry ``chunk_id``; import preserves it under the
            # default on_conflict="skip".
            ids_a = {c.id for c in chunks_a}
            ids_b = {c.id for c in chunks_b}
            print(
                f"[BASELINE] chunk_id    | A={len(ids_a)} B={len(ids_b)} "
                f"A∩B={len(ids_a & ids_b)} (v2: UUIDs preserved)"
            )

            # --- Metric 4: top-k search overlap ------------------------------
            queries = [
                "redis caching",
                "kubernetes monitoring",
                "postgres vacuum",
                "쿠버네티스 모니터링",
            ]
            for q in queries:
                ra, _ = await comp_a.search_pipeline.search(q, top_k=3)
                rb, _ = await comp_b.search_pipeline.search(q, top_k=3)
                ca = [r.chunk.content for r in ra]
                cb = [r.chunk.content for r in rb]
                overlap = len(set(ca) & set(cb))
                print(
                    f"[BASELINE] top-3 '{q}' | overlap={overlap}/3 "
                    f"A_ranks={[c[:25] for c in ca]} "
                    f"B_ranks={[c[:25] for c in cb]}"
                )

            # --- Metric 5: metadata preservation (pair by content_hash) ------
            by_hash_a = {c.content_hash: c for c in chunks_a}
            by_hash_b = {c.content_hash: c for c in chunks_b}
            meta_mismatches: list[tuple[str, str, object, object]] = []
            for h, ca_c in by_hash_a.items():
                cb_c = by_hash_b.get(h)
                if cb_c is None:
                    continue
                for field in ("tags", "namespace", "heading_hierarchy", "source_file"):
                    va = getattr(ca_c.metadata, field)
                    vb = getattr(cb_c.metadata, field)
                    if va != vb:
                        meta_mismatches.append((field, ca_c.content[:30], va, vb))
            print(f"[BASELINE] metadata mismatches: {len(meta_mismatches)}")
            for f, c, va, vb in meta_mismatches[:5]:
                print(f"  - {f} @ {c!r}: A={va!r} B={vb!r}")

            # --- Hard asserts: must-holds (fail the test if these break) -----
            # These are the minimum we expect to work today. Everything above
            # is diagnostic.
            assert stats.imported_chunks == bundle.total_chunks, (
                "all bundled chunks should import successfully"
            )
            assert stats.failed_chunks == 0, "no chunk should fail to import"
            assert len(chunks_b) == len(chunks_a), (
                f"chunk count drift after roundtrip: A={len(chunks_a)} B={len(chunks_b)}"
            )
            # content_hash is sha256 of NFC-normalised content — if content
            # survives JSON serialisation byte-for-byte, these sets must match.
            assert hashes_a == hashes_b, (
                "content_hash set must match after roundtrip "
                "(content preservation through JSON bundle)"
            )
            # v2: bundle carries chunk_id and import preserves it under the
            # default on_conflict="skip" into a fresh DB (no collisions).
            assert ids_a == ids_b, "chunk.id must be preserved across v2 export/import"
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_reimport_idempotency(self, tmp_path):
        """Importing the same bundle twice with default ``on_conflict="skip"``.

        v2 dedupes by ``content_hash``: the second import is a no-op. Every
        record is reported under ``skipped_duplicates``; row count stays at
        ``len(chunks_a)``.
        """
        comp_a, mem_a = await _make_onnx_components(tmp_path, "pc_a_re")
        comp_b, _ = await _make_onnx_components(tmp_path, "pc_b_re")
        try:
            await _index_corpus(comp_a, mem_a)
            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)

            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            s1 = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)
            b1 = await comp_b.storage.recall_chunks(limit=10_000)
            s2 = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)
            b2 = await comp_b.storage.recall_chunks(limit=10_000)

            hashes_unique_b2 = {c.content_hash for c in b2}
            from collections import Counter

            dup_counter = Counter(c.content_hash for c in b2)
            max_dup = max(dup_counter.values()) if dup_counter else 0

            print(
                f"\n[BASELINE] re-import | A={len(chunks_a)} "
                f"after_1st={len(b1)} after_2nd={len(b2)} "
                f"unique_hashes_B={len(hashes_unique_b2)} max_rows_per_hash={max_dup} "
                f"s1.imported={s1.imported_chunks} s2.imported={s2.imported_chunks} "
                f"s2.skipped_duplicates={s2.skipped_duplicates}"
            )

            assert len(b1) == len(chunks_a), "first import should match source"
            assert s1.imported_chunks == len(chunks_a)
            assert s1.skipped_duplicates == 0, "nothing to dedup against on a fresh DB"
            # v2 skip-by-hash: re-importing the same bundle is a no-op.
            assert len(b2) == len(chunks_a), (
                f"on_conflict='skip' should be idempotent; got {len(b2)} rows "
                f"for {len(chunks_a)} unique contents."
            )
            assert max_dup == 1, "no content_hash should have duplicate rows"
            assert s2.imported_chunks == 0
            assert s2.skipped_duplicates == len(chunks_a)
            assert len(hashes_unique_b2) == len(chunks_a)
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_reimport_on_conflict_update_reuses_uuids(self, tmp_path):
        """``on_conflict="update"``: hash matches reuse existing UUIDs.

        Second import with ``update`` hits every existing hash, so every
        incoming chunk maps onto an existing row's UUID and the upsert takes
        the UPDATE path. Row count stays at ``len(chunks_a)``;
        ``updated_chunks == len(chunks_a)``.
        """
        comp_a, mem_a = await _make_onnx_components(tmp_path, "pc_a_upd")
        comp_b, _ = await _make_onnx_components(tmp_path, "pc_b_upd")
        try:
            await _index_corpus(comp_a, mem_a)
            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)

            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)
            b1 = await comp_b.storage.recall_chunks(limit=10_000)
            ids_after_first = {c.id for c in b1}

            s2 = await import_chunks(
                comp_b.storage,
                comp_b.embedder,
                bundle_path,
                on_conflict="update",
            )
            b2 = await comp_b.storage.recall_chunks(limit=10_000)
            ids_after_second = {c.id for c in b2}

            print(
                f"\n[BASELINE] re-import update | A={len(chunks_a)} "
                f"after_1st={len(b1)} after_2nd={len(b2)} "
                f"s2.updated={s2.updated_chunks} s2.imported={s2.imported_chunks} "
                f"s2.skipped_duplicates={s2.skipped_duplicates}"
            )

            assert len(b2) == len(chunks_a), "update should not grow the row count"
            assert s2.updated_chunks == len(chunks_a)
            assert s2.imported_chunks == 0
            assert s2.skipped_duplicates == 0
            assert ids_after_first == ids_after_second, (
                "update must preserve existing UUIDs (take the UPDATE path, not INSERT)"
            )
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_reimport_on_conflict_duplicate_legacy(self, tmp_path):
        """``on_conflict="duplicate"``: explicit opt-in to the v1 behaviour.

        For callers that need the old semantics (every import adds rows,
        fresh UUIDs, collisions duplicate). Verifies the back-compat knob
        still produces 2x duplication after a re-import.
        """
        comp_a, mem_a = await _make_onnx_components(tmp_path, "pc_a_dup")
        comp_b, _ = await _make_onnx_components(tmp_path, "pc_b_dup")
        try:
            await _index_corpus(comp_a, mem_a)
            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)

            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            await import_chunks(
                comp_b.storage, comp_b.embedder, bundle_path, on_conflict="duplicate"
            )
            await import_chunks(
                comp_b.storage, comp_b.embedder, bundle_path, on_conflict="duplicate"
            )
            b2 = await comp_b.storage.recall_chunks(limit=10_000)
            from collections import Counter

            dup_counter = Counter(c.content_hash for c in b2)

            print(
                f"\n[BASELINE] re-import duplicate | A={len(chunks_a)} "
                f"after_2nd={len(b2)} max_rows_per_hash={max(dup_counter.values())}"
            )

            assert len(b2) == 2 * len(chunks_a), (
                "on_conflict='duplicate' preserves the v1 duplication behaviour"
            )
            assert max(dup_counter.values()) == 2
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_merge_disjoint_content(self, tmp_path):
        """PC_B has its own native chunks, then imports PC_A's disjoint bundle.

        Baseline for question 2 (additive ingestion across PCs). With zero
        content overlap, the resulting DB should contain |A| + |B| chunks and
        the content_hash sets must be disjoint.
        """
        comp_a, mem_a = await _make_onnx_components(tmp_path, "pc_a_m")
        comp_b, mem_b = await _make_onnx_components(tmp_path, "pc_b_m")
        try:
            await _index_corpus(comp_a, mem_a, _CORPUS)
            await _index_corpus(comp_b, mem_b, _CORPUS_B)

            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)
            b_before = await comp_b.storage.recall_chunks(limit=10_000)

            hashes_a = {c.content_hash for c in chunks_a}
            hashes_b_before = {c.content_hash for c in b_before}
            disjoint_precheck = len(hashes_a & hashes_b_before) == 0

            bundle_path = tmp_path / "bundle_disjoint.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)
            stats = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)

            b_after = await comp_b.storage.recall_chunks(limit=10_000)
            hashes_b_after = {c.content_hash for c in b_after}

            print(
                f"\n[BASELINE] merge-disjoint | A={len(chunks_a)} "
                f"B_before={len(b_before)} B_after={len(b_after)} "
                f"imported={stats.imported_chunks} "
                f"pre_hash_overlap={len(hashes_a & hashes_b_before)} "
                f"post_hash_A_covered={len(hashes_a & hashes_b_after)}/{len(hashes_a)}"
            )

            # Spot-check top-k still finds PC_B's native content post-merge
            queries_b = ["secret rotation", "JSON logging"]
            for q in queries_b:
                r, _ = await comp_b.search_pipeline.search(q, top_k=3)
                hit = any(
                    "rotation" in x.chunk.content.lower() or "json" in x.chunk.content.lower()
                    for x in r
                )
                print(f"[BASELINE]   B-native '{q}' | reachable_after_import={hit}")

            assert disjoint_precheck, "corpora should have disjoint content_hash"
            assert len(b_after) == len(b_before) + len(chunks_a), (
                f"additive merge row count wrong: "
                f"{len(b_before)} + {len(chunks_a)} != {len(b_after)}"
            )
            assert hashes_a.issubset(hashes_b_after), (
                "all A-content hashes must be present in B after merge"
            )
            assert hashes_b_before.issubset(hashes_b_after), "B's native hashes must survive merge"
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_merge_with_content_collision(self, tmp_path):
        """PC_B has one doc with content byte-identical to a PC_A doc.

        Under the default ``on_conflict="skip"``, the shared content already
        in B is kept; the colliding incoming chunks from A are skipped.
        After merge: one row per unique hash, B's native non-colliding rows
        survive, and A's non-colliding rows are added.
        """
        comp_a, mem_a = await _make_onnx_components(tmp_path, "pc_a_c")
        comp_b, mem_b = await _make_onnx_components(tmp_path, "pc_b_c")
        try:
            await _index_corpus(comp_a, mem_a, _CORPUS)
            # PC_B has one doc with content identical to PC_A's caching.md
            shared_content = _CORPUS["caching.md"]
            (mem_b / "caching_copy.md").write_text(shared_content, encoding="utf-8")
            await comp_b.index_engine.index_file(mem_b / "caching_copy.md")

            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)
            b_before = await comp_b.storage.recall_chunks(limit=10_000)
            hashes_a = {c.content_hash for c in chunks_a}
            hashes_b_before = {c.content_hash for c in b_before}
            collision_hashes = hashes_a & hashes_b_before

            bundle_path = tmp_path / "bundle_collision.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)
            stats = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)

            b_after = await comp_b.storage.recall_chunks(limit=10_000)
            from collections import Counter

            dup_counter = Counter(c.content_hash for c in b_after)
            duplicated = {h: n for h, n in dup_counter.items() if n > 1}

            print(
                f"\n[BASELINE] merge-collision | A={len(chunks_a)} "
                f"B_before={len(b_before)} B_after={len(b_after)} "
                f"imported={stats.imported_chunks} "
                f"skipped_duplicates={stats.skipped_duplicates} "
                f"collision_hashes={len(collision_hashes)} "
                f"rows_with_dup_hash={len(duplicated)} "
                f"dup_pattern={sorted(duplicated.values())}"
            )

            assert len(collision_hashes) >= 1, "precondition: at least one shared hash"
            # Skip-by-hash: collisions are dropped, non-collision rows are added.
            expected_rows = len(b_before) + len(chunks_a) - len(collision_hashes)
            assert len(b_after) == expected_rows, (
                f"expected {expected_rows} rows after skip-merge, got {len(b_after)}"
            )
            assert stats.skipped_duplicates == len(collision_hashes)
            assert stats.imported_chunks == len(chunks_a) - len(collision_hashes)
            # No content_hash should have more than one row after the merge.
            assert not duplicated, f"skip-merge left duplicate rows: {duplicated}"
        finally:
            await close_components(comp_a)
            await close_components(comp_b)
