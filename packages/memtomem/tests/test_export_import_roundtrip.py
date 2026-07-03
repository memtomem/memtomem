"""Cross-instance export -> import roundtrip tests.

Originally Phase 1 baselines; upgraded in Phase 2 to pin the new
``on_conflict`` semantics. ``test_phase1_baseline`` now hard-asserts
metadata and top-k fidelity; ``test_reimport_idempotency`` and
``test_merge_with_content_collision`` assert the Phase 2 (default
``on_conflict="skip"``) behaviour. The ``test_on_conflict_*`` tests pin
the three modes individually, including explicit back-compat for
``"duplicate"`` (the pre-v2 row-duplication behaviour).

Run with: ``uv run pytest tests/test_export_import_roundtrip.py -s``
(``-s`` to surface the [BASELINE] print lines).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components, close_components, create_components
from memtomem.tools.export_import import export_chunks, import_chunks


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


class _DeterministicEmbedder:
    """Hermetic, content-addressed embedder for the roundtrip suite.

    Maps each text to a stable unit vector derived from a SHA-256 digest of the
    text. No network and no model download: this deliberately replaces the real
    ONNX/fastembed model so the roundtrip tests stay hermetic in the main CI
    matrix, which — unlike the dedicated ``golden-path`` job — does not
    pre-cache the model. Live HuggingFace downloads here made the suite flaky
    under HF ``429 Too Many Requests`` rate-limiting, reddening required checks.

    Determinism is the only property the roundtrip asserts rely on: identical
    content embedded the same way on both instances yields identical vectors, so
    content_hash dedup and top-k result *sets* survive the export/import
    roundtrip. The vector combines a bag-of-words feature hash (the "hashing
    trick" — texts sharing words get correlated vectors, so each query separates
    its intended doc decisively) with a small per-text dense background. The
    background gives docs that share *no* query words a distinct, non-zero
    similarity, mirroring how a real dense model never returns exactly-orthogonal
    vectors; without it zero-overlap distractors tie and the lowest top-k slot is
    ordered by an arbitrary tie-break. The vectors carry no real semantic
    meaning; embedding quality floors live in
    ``test_multilingual_regression.py`` against the real model.
    """

    _WORD_RE = re.compile(r"\w+", re.UNICODE)
    _BACKGROUND_WEIGHT = 0.15

    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return "deterministic-test"

    def _background(self, text: str) -> list[float]:
        """Per-text dense vector in [-1, 1), expanded from a SHA-256 digest."""
        values: list[float] = []
        counter = 0
        while len(values) < self._dimension:
            digest = hashlib.sha256(f"{counter}\x00{text}".encode()).digest()
            for offset in range(0, len(digest), 2):
                if len(values) >= self._dimension:
                    break
                values.append(int.from_bytes(digest[offset : offset + 2], "big") / 32768.0 - 1.0)
            counter += 1
        return values

    def _vector(self, text: str) -> list[float]:
        values = self._background(text)
        for i in range(self._dimension):
            values[i] *= self._BACKGROUND_WEIGHT
        for token in self._WORD_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode()).digest()
            slot = int.from_bytes(digest[:4], "big") % self._dimension
            values[slot] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    async def embed_texts(
        self,
        texts: Sequence[str],
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[list[float]]:
        vectors = [self._vector(t) for t in texts]
        if on_progress is not None:
            on_progress(len(texts), len(texts))
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        return self._vector(query)

    async def close(self) -> None:
        pass


async def _make_components(tmp_path: Path, tag: str) -> tuple[Components, Path]:
    """Build a hermetic component stack with its own DB + mem dir.

    Spins up an independent instance (PC A or PC B) backed by
    ``_DeterministicEmbedder`` instead of the real ONNX model, so two stacks can
    be created inside one test without any network access. ``create_embedder``
    is patched at its single construction point in ``component_factory`` so the
    index engine, search pipeline, and ``Components.embedder`` all share the same
    deterministic embedder.
    """
    db_path = tmp_path / f"{tag}.db"
    mem_dir = tmp_path / f"{tag}_mem"
    mem_dir.mkdir(parents=True, exist_ok=True)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "deterministic-test"
    config.embedding.dimension = 384
    # ``Mem2MemConfig()`` reads ``MEMTOMEM_*`` env at construction (before the
    # load_config_d / load_config_overrides patches below), so a developer/CI
    # env could still flip on the only network-bearing optional components
    # ``create_components`` builds — the reranker (cross-encoder download) and
    # the LLM provider. Force them off so the stack stays hermetic.
    config.rerank.enabled = False
    config.llm.enabled = False

    import memtomem.config as _cfg
    from memtomem.server import component_factory as _factory

    _orig_config_d = _cfg.load_config_d
    _orig_overrides = _cfg.load_config_overrides
    _orig_create_embedder = _factory.create_embedder

    def _noop(config: Mem2MemConfig) -> None:
        return None

    def _deterministic_embedder(_embedding_config: object) -> _DeterministicEmbedder:
        return _DeterministicEmbedder(config.embedding.dimension)

    # Neutralise BOTH config layers ``create_components`` applies — ``config.d``
    # fragments and ``config.json`` overrides — so a developer/CI environment
    # can't flip on a network-bearing feature (reranker cross-encoder, LLM) or
    # repoint the embedding provider and break hermeticity.
    _cfg.load_config_d = _noop
    _cfg.load_config_overrides = _noop
    _factory.create_embedder = _deterministic_embedder
    try:
        comp = await create_components(config)
    finally:
        _cfg.load_config_d = _orig_config_d
        _cfg.load_config_overrides = _orig_overrides
        _factory.create_embedder = _orig_create_embedder
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
        comp_a, mem_a = await _make_components(tmp_path, "pc_a")
        comp_b, _ = await _make_components(tmp_path, "pc_b")
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
            # Current impl assigns fresh UUIDs on import, so we expect 0 overlap.
            # Record the number so Phase 2 can track the fix.
            ids_a = {c.id for c in chunks_a}
            ids_b = {c.id for c in chunks_b}
            print(
                f"[BASELINE] chunk_id    | A={len(ids_a)} B={len(ids_b)} "
                f"A∩B={len(ids_a & ids_b)} (current impl assigns fresh UUIDs)"
            )

            # --- Metric 4: top-k search overlap ------------------------------
            queries = [
                "redis caching",
                "kubernetes monitoring",
                "postgres vacuum",
                "쿠버네티스 모니터링",
            ]
            topk_results: list[tuple[str, list[str], list[str]]] = []
            for q in queries:
                ra, _ = await comp_a.search_pipeline.search(q, top_k=3)
                rb, _ = await comp_b.search_pipeline.search(q, top_k=3)
                ca = [r.chunk.content for r in ra]
                cb = [r.chunk.content for r in rb]
                topk_results.append((q, ca, cb))
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
            # Metadata preserved per content_hash across roundtrip. Pairs were
            # collected above; any diff in tags/namespace/heading_hierarchy/
            # source_file means the bundle is losing metadata on the wire.
            assert not meta_mismatches, (
                f"metadata drift across roundtrip: {len(meta_mismatches)} field(s) "
                f"mismatched. First 3: {meta_mismatches[:3]}"
            )
            # Top-k result sets must match on both sides. Import re-embeds
            # ``retrieval_content`` exactly as the index engine does, so a chunk
            # carries the same vector whether it was indexed natively (A) or
            # imported (B); with the deterministic embedder that makes retrieval a
            # pure function of content and the result sets identical. Compare as
            # sets (order-insensitive) so an in-set tie-break reorder is fine; a
            # set drift = imported chunks are not equivalent to the source for
            # retrieval purposes.
            for q, ca, cb in topk_results:
                assert set(ca) == set(cb), (
                    f"top-3 result set drift across roundtrip for {q!r}: "
                    f"A_only={[c[:40] for c in set(ca) - set(cb)]} "
                    f"B_only={[c[:40] for c in set(cb) - set(ca)]}"
                )
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_reimport_idempotency(self, tmp_path):
        """Re-importing the same bundle under default ``on_conflict="skip"``
        must be a no-op: the second import contributes zero new rows and
        reports every record as ``conflict_skipped``.
        """
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_re")
        comp_b, _ = await _make_components(tmp_path, "pc_b_re")
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
            dup_counter = Counter(c.content_hash for c in b2)
            max_dup = max(dup_counter.values()) if dup_counter else 0

            print(
                f"\n[BASELINE] re-import skip | A={len(chunks_a)} "
                f"after_1st={len(b1)} after_2nd={len(b2)} "
                f"unique_hashes_B={len(hashes_unique_b2)} max_rows_per_hash={max_dup} "
                f"s1.imported={s1.imported_chunks} s1.conflict_skipped={s1.conflict_skipped_chunks} "
                f"s2.imported={s2.imported_chunks} s2.conflict_skipped={s2.conflict_skipped_chunks}"
            )

            assert len(b1) == len(chunks_a), "first import should match source"
            assert s1.imported_chunks == len(chunks_a)
            assert s1.conflict_skipped_chunks == 0, "empty DB → no conflicts on first import"

            # Phase 2: second import is fully idempotent under skip mode.
            assert len(b2) == len(b1), (
                f"re-import must be idempotent under on_conflict='skip': "
                f"expected {len(b1)} rows, got {len(b2)}"
            )
            assert s2.imported_chunks == 0, "no new rows on re-import"
            assert s2.conflict_skipped_chunks == len(chunks_a), (
                "every bundle record should be conflict-skipped on re-import"
            )
            assert max_dup == 1, "no row-level duplication in skip mode"
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_merge_disjoint_content(self, tmp_path):
        """PC_B has its own native chunks, then imports PC_A's disjoint bundle.

        Baseline for question 2 (additive ingestion across PCs). With zero
        content overlap, the resulting DB should contain |A| + |B| chunks and
        the content_hash sets must be disjoint.
        """
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_m")
        comp_b, mem_b = await _make_components(tmp_path, "pc_b_m")
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

        Under default ``on_conflict="skip"``, colliding records are dropped
        and the result has exactly one row per unique content_hash: B's
        native row survives, A's colliding record is not added. Only the
        non-colliding A-records contribute new rows.
        """
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_c")
        comp_b, mem_b = await _make_components(tmp_path, "pc_b_c")
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
            dup_counter = Counter(c.content_hash for c in b_after)
            duplicated = {h: n for h, n in dup_counter.items() if n > 1}

            expected_added = len(chunks_a) - len(collision_hashes)
            print(
                f"\n[BASELINE] merge-collision skip | A={len(chunks_a)} "
                f"B_before={len(b_before)} B_after={len(b_after)} "
                f"imported={stats.imported_chunks} "
                f"conflict_skipped={stats.conflict_skipped_chunks} "
                f"collision_hashes={len(collision_hashes)} "
                f"expected_added={expected_added} "
                f"rows_with_dup_hash={len(duplicated)}"
            )

            assert len(collision_hashes) >= 1, "precondition: at least one shared hash"
            assert len(b_after) == len(b_before) + expected_added, (
                f"skip mode should only append non-colliding rows: "
                f"expected {len(b_before) + expected_added}, got {len(b_after)}"
            )
            assert stats.imported_chunks == expected_added
            assert stats.conflict_skipped_chunks == len(collision_hashes)
            # Each collision hash should now have exactly 1 row (B's original).
            for h in collision_hashes:
                assert dup_counter[h] == 1, (
                    f"expected 1 row for colliding hash {h[:10]} in skip mode, got {dup_counter[h]}"
                )
        finally:
            await close_components(comp_a)
            await close_components(comp_b)


class TestOnConflictModes:
    """Pin each ``on_conflict`` mode individually."""

    async def test_duplicate_mode_dedups_at_storage_layer(self, tmp_path):
        """``on_conflict="duplicate"`` used to force fresh UUIDs without
        any hash check — pre-v2 row-duplication semantics. Since #691
        added ``UNIQUE(namespace, source_file, content_hash, start_line)``
        plus ``INSERT OR IGNORE`` in ``upsert_chunks``, the storage layer
        is authoritative: a second import's attempted inserts are silently
        dropped, and the DB ends up with one row per content_hash even
        when the caller asks for the legacy "duplicate" behaviour. The
        mode is kept so existing callers don't break, but it no longer
        produces row duplication.
        """
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_dup")
        comp_b, _ = await _make_components(tmp_path, "pc_b_dup")
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
            dup_counter = Counter(c.content_hash for c in b2)

            assert len(b2) == len(chunks_a)
            assert max(dup_counter.values()) == 1
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_update_mode_overwrites_metadata_and_preserves_uuid(self, tmp_path):
        """``on_conflict="update"`` keeps the existing UUID and rewrites
        metadata (tags/namespace) from the bundle record."""
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_upd")
        comp_b, _ = await _make_components(tmp_path, "pc_b_upd")
        try:
            await _index_corpus(comp_a, mem_a)
            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            # First import to B under "skip" (new DB, everything inserts).
            await import_chunks(comp_b.storage, comp_b.embedder, bundle_path, on_conflict="skip")
            b_first = await comp_b.storage.recall_chunks(limit=10_000)
            first_ids_by_hash = {c.content_hash: c.id for c in b_first}

            # Now re-import under "update" with a namespace override to prove
            # the metadata path actually runs.
            stats = await import_chunks(
                comp_b.storage,
                comp_b.embedder,
                bundle_path,
                namespace="overridden",
                on_conflict="update",
            )
            b_second = await comp_b.storage.recall_chunks(limit=10_000)
            second_ids_by_hash = {c.content_hash: c.id for c in b_second}

            assert len(b_second) == len(b_first), "update must not add rows"
            assert stats.updated_chunks == len(b_first)
            assert stats.imported_chunks == 0
            assert stats.conflict_skipped_chunks == 0

            # UUID preservation: every row's UUID is stable across the update.
            assert first_ids_by_hash == second_ids_by_hash
            # Metadata rewrite: namespace override took effect.
            assert all(c.metadata.namespace == "overridden" for c in b_second)
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_invalid_on_conflict_raises(self, tmp_path):
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_inv")
        comp_b, _ = await _make_components(tmp_path, "pc_b_inv")
        try:
            await _index_corpus(comp_a, mem_a)
            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            with pytest.raises(ValueError, match="on_conflict"):
                await import_chunks(
                    comp_b.storage,
                    comp_b.embedder,
                    bundle_path,
                    on_conflict="bogus",  # type: ignore[arg-type]
                )
        finally:
            await close_components(comp_a)
            await close_components(comp_b)

    async def test_v1_bundle_still_imports(self, tmp_path):
        """A hand-written v1-shaped bundle (no chunk_id / content_hash fields)
        must still import. Skip-mode dedup keeps working because hashes are
        derived from content by ``Chunk.__post_init__``.
        """
        import json
        from datetime import datetime, timezone

        comp_b, _ = await _make_components(tmp_path, "pc_b_v1")
        try:
            v1_bundle = {
                "version": "1",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "total_chunks": 1,
                "chunks": [
                    {
                        "content": "Only legacy v1 record, no chunk_id or hash.",
                        "source_file": "legacy.md",
                        "heading_hierarchy": ["Legacy"],
                        "chunk_type": "raw_text",
                        "start_line": 1,
                        "end_line": 1,
                        "language": "en",
                        "tags": ["legacy"],
                        "namespace": "default",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                ],
            }
            bundle_path = tmp_path / "v1.json"
            bundle_path.write_text(json.dumps(v1_bundle), encoding="utf-8")

            s1 = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)
            assert s1.imported_chunks == 1
            assert s1.conflict_skipped_chunks == 0

            # Re-import of the v1 bundle under default skip must dedup too.
            s2 = await import_chunks(comp_b.storage, comp_b.embedder, bundle_path)
            assert s2.imported_chunks == 0
            assert s2.conflict_skipped_chunks == 1
        finally:
            await close_components(comp_b)

    async def test_preserve_ids_across_instances(self, tmp_path):
        """With ``preserve_ids=True`` on a v2 bundle, a fresh-DB import
        reuses the bundle's original UUIDs — the "same chunk on two PCs"
        identity guarantee.
        """
        comp_a, mem_a = await _make_components(tmp_path, "pc_a_pres")
        comp_b, _ = await _make_components(tmp_path, "pc_b_pres")
        try:
            await _index_corpus(comp_a, mem_a)
            chunks_a = await comp_a.storage.recall_chunks(limit=10_000)

            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            await import_chunks(
                comp_b.storage,
                comp_b.embedder,
                bundle_path,
                on_conflict="skip",
                preserve_ids=True,
            )
            chunks_b = await comp_b.storage.recall_chunks(limit=10_000)

            ids_a = {c.id for c in chunks_a}
            ids_b = {c.id for c in chunks_b}
            assert ids_a == ids_b, "preserve_ids must yield UUID-equal rows across PCs"
        finally:
            await close_components(comp_a)
            await close_components(comp_b)


class TestImportShortEmbeddingArray:
    """Issue #1563, reproduced outside the index engine: the import path must
    not silently ``zip``-truncate a short embedding array into hash-poisoned
    BM25-only rows. Export/import preserves ``content_hash``, so a truncated
    import would commit chunks that never re-embed on any later pass.
    """

    async def test_short_array_aborts_import_with_no_writes(self, tmp_path):
        comp_a, mem_a = await _make_components(tmp_path, "short_a")
        comp_b, _ = await _make_components(tmp_path, "short_b")
        try:
            await _index_corpus(comp_a, mem_a)
            bundle_path = tmp_path / "bundle.json"
            await export_chunks(comp_a.storage, output_path=bundle_path)

            class _ShortEmbedder(_DeterministicEmbedder):
                """Drops the last vector — the partial-failure shape a flaky
                HTTP endpoint produces, injected straight into the import call.
                """

                async def embed_texts(self, texts, *, on_progress=None):
                    vectors = await super().embed_texts(texts, on_progress=on_progress)
                    return vectors[:-1]

            assert await comp_b.storage.recall_chunks(limit=10_000) == []

            stats = await import_chunks(comp_b.storage, _ShortEmbedder(384), bundle_path)

            assert stats.imported_chunks == 0
            assert stats.failed_chunks > 0
            # The distinguishing pin vs. the bug: the buggy ``zip`` would have
            # committed the non-truncated chunks (BM25-only) here. With the
            # guard, nothing lands and no content_hash is recorded.
            assert await comp_b.storage.recall_chunks(limit=10_000) == [], (
                "no chunks may be committed when import embedding truncates"
            )
        finally:
            await close_components(comp_a)
            await close_components(comp_b)
