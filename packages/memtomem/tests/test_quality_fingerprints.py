"""Tests for retrieval profile / corpus / index fingerprints (#1802, PR-3).

The contract these pin: every fingerprinted dependency registers as drift when
it changes, secrets never enter the profile fingerprint, and configuration knobs
that do not affect ranking (cache TTL) do not perturb it.
"""

from __future__ import annotations

import json

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.quality.fingerprints import (
    case_set_fingerprint,
    corpus_fingerprint,
    index_fingerprint,
    profile_fingerprint,
)
from memtomem.secret_masking import is_secret_key

# (content_hash, heading_hierarchy, namespace, scope, created_at, updated_at,
#  valid_from_unix, valid_to_unix, importance_score, tags, source_file, start_line,
#  project_root)
_BASE_CORPUS = [
    (
        "hash-1",
        "[]",
        "default",
        "user",
        "2026-01-01",
        "2026-01-01",
        None,
        None,
        0.0,
        "[]",
        "/n/a.md",
        0,
        None,
    ),
    (
        "hash-2",
        '["H"]',
        "default",
        "project_shared",
        "2026-01-01",
        "2026-01-02",
        None,
        None,
        0.5,
        '["x"]',
        "/n/b.md",
        4,
        "/proj/one",
    ),
]

_EMB = {
    "provider": "onnx",
    "model": "m",
    "dimension": 8,
    "policy_fingerprint": "onnx:v1",
    "max_sequence_tokens": 512,
}
# (content_hash, namespace, source_file, start_line, embedding_blob) — durable identity,
# no rowid/uuid.
_VECTORS = [
    ("hash-1", "default", "/n/a.md", 0, b"\x00\x01\x02\x03"),
    ("hash-2", "default", "/n/b.md", 4, b"\x04\x05\x06\x07"),
]
# (content_hash, namespace, source_file, start_line, fts_content)
_FTS = [
    ("hash-1", "default", "/n/a.md", 0, "alpha body"),
    ("hash-2", "default", "/n/b.md", 4, "beta body"),
]


def _mutate(rows, index, field, value):
    """Return a copy of a row list with one field of one row changed."""
    out = [list(r) for r in rows]
    out[index][field] = value
    return [tuple(r) for r in out]


class TestProfileFingerprint:
    def test_secrets_never_enter_the_dict(self):
        cfg = Mem2MemConfig()
        cfg.embedding.api_key = "SENTINEL-EMB"
        cfg.rerank.api_key = "SENTINEL-RERANK"
        cfg.llm.api_key = "SENTINEL-LLM"
        cfg.query_expansion.strategy = "llm"  # force the LLM branch to run
        _, knobs = profile_fingerprint(cfg)

        blob = json.dumps(knobs)
        assert "SENTINEL" not in blob

        def _walk(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    assert not is_secret_key(str(key)), f"secret key {key!r} leaked into profile"
                    _walk(child)
            elif isinstance(node, list):
                for child in node:
                    _walk(child)

        _walk(knobs)

    def test_cache_ttl_does_not_change_fingerprint(self):
        cfg = Mem2MemConfig()
        base, _ = profile_fingerprint(cfg)
        cfg.search.cache_ttl = cfg.search.cache_ttl + 999
        after, _ = profile_fingerprint(cfg)
        assert base == after

    def test_ranking_knob_changes_fingerprint(self):
        cfg = Mem2MemConfig()
        base, _ = profile_fingerprint(cfg)
        cfg.decay.enabled = not cfg.decay.enabled
        assert profile_fingerprint(cfg)[0] != base

    def test_llm_identity_only_folded_when_expanding_with_llm(self):
        cfg = Mem2MemConfig()
        cfg.query_expansion.enabled = True
        cfg.query_expansion.strategy = "tags"
        base, knobs = profile_fingerprint(cfg)
        assert "llm" not in knobs["query_expansion"]
        cfg.llm.model = "some-model"
        assert profile_fingerprint(cfg)[0] == base  # LLM identity ignored under tags
        cfg.query_expansion.strategy = "llm"
        assert profile_fingerprint(cfg)[0] != base

    def test_disabled_stage_params_do_not_affect_fingerprint(self):
        # A disabled ranking stage must not drift on its dead parameters —
        # otherwise two ranking-identical profiles compare as incompatible.
        cfg = Mem2MemConfig()
        assert cfg.rerank.enabled is False
        base, knobs = profile_fingerprint(cfg)
        assert set(knobs["rerank"]) == {"enabled"}  # no model/provider when off
        cfg.rerank.model = "some/other-model"
        cfg.rerank.oversample = cfg.rerank.oversample + 5
        assert profile_fingerprint(cfg)[0] == base  # dead params ignored
        cfg.rerank.enabled = True
        assert profile_fingerprint(cfg)[0] != base  # enabling is drift

    def test_mmr_inactive_without_dense(self):
        # MMR is skipped when dense retrieval is off, so lambda_param is dead
        # regardless of mmr.enabled — it must not perturb the fingerprint.
        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True
        cfg.search.enable_dense = False
        base, knobs = profile_fingerprint(cfg)
        assert set(knobs["mmr"]) == {"enabled"}  # inactive → no lambda_param
        cfg.mmr.lambda_param = 0.1
        assert profile_fingerprint(cfg)[0] == base
        # With dense on, MMR is live and lambda_param matters again.
        cfg.search.enable_dense = True
        live, _ = profile_fingerprint(cfg)
        cfg.mmr.lambda_param = 0.9
        assert profile_fingerprint(cfg)[0] != live

    def test_disabled_query_expansion_ignores_strategy(self):
        cfg = Mem2MemConfig()
        assert cfg.query_expansion.enabled is False
        base, _ = profile_fingerprint(cfg)
        cfg.query_expansion.strategy = "llm"
        cfg.llm.model = "x"
        assert profile_fingerprint(cfg)[0] == base  # strategy/LLM dead while off


class TestCorpusFingerprint:
    def test_stable_across_reads(self):
        assert corpus_fingerprint(_BASE_CORPUS) == corpus_fingerprint(list(_BASE_CORPUS))

    def test_order_independent(self):
        assert corpus_fingerprint(_BASE_CORPUS) == corpus_fingerprint(list(reversed(_BASE_CORPUS)))

    @pytest.mark.parametrize(
        "field,value",
        [
            (0, "hash-CHANGED"),  # content edit → content_hash
            (1, '["different heading"]'),  # heading changes retrieval text
            (2, "other-ns"),  # namespace
            (3, "project_local"),  # scope
            (5, "2026-09-09"),  # updated_at (decay ages from it)
            (6, 1234567),  # valid_from_unix
            (8, 0.99),  # importance
            (9, '["newtag"]'),  # tags
            (10, "/n/moved.md"),  # source move (same basename would still differ by full path)
            (11, 99),  # start_line
            (12, "/proj/two"),  # project_root (project-tier retrievability)
        ],
    )
    def test_each_dependency_registers_as_drift(self, field, value):
        mutated = _mutate(_BASE_CORPUS, 1, field, value)
        assert corpus_fingerprint(mutated) != corpus_fingerprint(_BASE_CORPUS)

    def test_same_basename_move_still_drifts(self):
        # Two files with the same basename but different directories must not
        # collide — full normalized path feeds the identity hash.
        moved = _mutate(_BASE_CORPUS, 0, 10, "/other/a.md")  # basename a.md unchanged
        assert corpus_fingerprint(moved) != corpus_fingerprint(_BASE_CORPUS)

    def test_multiplicity_preserved(self):
        dup = list(_BASE_CORPUS) + [_BASE_CORPUS[0]]
        assert corpus_fingerprint(dup) != corpus_fingerprint(_BASE_CORPUS)


class TestIndexFingerprint:
    def _base(self, **kw):
        return index_fingerprint(_BASE_CORPUS, _VECTORS, _FTS, _EMB, **kw)

    def test_stable_across_reads(self):
        assert self._base() == self._base()

    def test_vector_blob_edit_drifts(self):
        edited = [("hash-1", "default", "/n/a.md", 0, b"\xff\xff\xff\xff"), _VECTORS[1]]
        assert index_fingerprint(_BASE_CORPUS, edited, _FTS, _EMB) != self._base()

    def test_vector_detaching_from_content_drifts(self):
        # The blobs swap while the content identities stay put — each chunk now
        # points at the other's vector. That is genuine drift.
        swapped = [
            ("hash-1", "default", "/n/a.md", 0, b"\x04\x05\x06\x07"),
            ("hash-2", "default", "/n/b.md", 4, b"\x00\x01\x02\x03"),
        ]
        assert index_fingerprint(_BASE_CORPUS, swapped, _FTS, _EMB) != self._base()

    def test_rekeyed_rebuild_is_stable(self):
        # Same content + same vectors, read back in a different physical order
        # (as a rebuild with new rowids/uuids would produce): NOT drift, because
        # identity is content-based, not rowid/uuid-based.
        reordered = list(reversed(_VECTORS))
        assert index_fingerprint(_BASE_CORPUS, reordered, _FTS, _EMB) == self._base()

    def test_fts_text_edit_drifts(self):
        edited = [("hash-1", "default", "/n/a.md", 0, "ALPHA CHANGED"), _FTS[1]]
        assert index_fingerprint(_BASE_CORPUS, _VECTORS, edited, _EMB) != self._base()

    def test_embedding_identity_drifts(self):
        other = {**_EMB, "model": "different-model"}
        assert index_fingerprint(_BASE_CORPUS, _VECTORS, _FTS, other) != self._base()

    def test_links_only_count_when_provided(self):
        # Endpoints are durable identities: (src content_hash, ns, src, line,
        # tgt content_hash, ns, src, line, link_type, namespace_target).
        links = [
            ("hash-1", "default", "/n/a.md", 0, "hash-2", "default", "/n/b.md", 4, "s", "default")
        ]
        assert self._base(link_rows=links) != self._base()
        assert self._base(link_rows=links) == self._base(link_rows=links)

    def test_link_edit_drifts(self):
        a = self._base(
            link_rows=[
                (
                    "hash-1",
                    "default",
                    "/n/a.md",
                    0,
                    "hash-2",
                    "default",
                    "/n/b.md",
                    4,
                    "s",
                    "default",
                )
            ]
        )
        b = self._base(
            link_rows=[
                (
                    "hash-1",
                    "default",
                    "/n/a.md",
                    0,
                    "hash-3",
                    "default",
                    "/n/c.md",
                    9,
                    "s",
                    "default",
                )
            ]
        )
        assert a != b

    def test_null_source_link_does_not_crash(self):
        # source_id is nullable (ON DELETE SET NULL) → source identity columns
        # arrive as None; the sort must tolerate them.
        self._base(
            link_rows=[(None, None, None, None, "hash-2", "default", "/n/b.md", 4, "s", "default")]
        )

    def test_access_only_counts_when_provided(self):
        access = [("hash-1", "default", "/n/a.md", 0, 5), ("hash-2", "default", "/n/b.md", 4, 0)]
        assert self._base(access_rows=access) != self._base()

    def test_access_bump_drifts_index_but_not_corpus(self):
        low = self._base(access_rows=[("hash-1", "default", "/n/a.md", 0, 1)])
        high = self._base(access_rows=[("hash-1", "default", "/n/a.md", 0, 99)])
        assert low != high
        # access is not part of the corpus fingerprint
        assert corpus_fingerprint(_BASE_CORPUS) == corpus_fingerprint(_BASE_CORPUS)

    def test_access_swap_between_duplicate_content_drifts(self):
        # Two chunks share content_hash but differ by source/line; swapping their
        # access counts must register because boosting ranks per chunk.
        a = self._base(
            access_rows=[("dup", "default", "/n/a.md", 0, 5), ("dup", "default", "/n/b.md", 4, 1)]
        )
        b = self._base(
            access_rows=[("dup", "default", "/n/a.md", 0, 1), ("dup", "default", "/n/b.md", 4, 5)]
        )
        assert a != b


class TestCaseSetFingerprint:
    def _cases(self):
        return [
            {
                "case_id": "cid-1",
                "version": 1,
                "query_text": "q",
                "top_k": 5,
                "filters": {"namespace": None, "scope": None},
                "labels": [
                    {"content_hash": "h1", "judgment": "relevant"},
                    {"content_hash": "h2", "judgment": "not_relevant"},
                ],
            }
        ]

    def test_stable_and_label_order_independent(self):
        base = case_set_fingerprint(self._cases())
        shuffled = self._cases()
        shuffled[0]["labels"].reverse()
        assert case_set_fingerprint(shuffled) == base

    def test_label_edit_changes_fingerprint(self):
        base = case_set_fingerprint(self._cases())
        edited = self._cases()
        edited[0]["labels"][0]["judgment"] = "not_relevant"
        assert case_set_fingerprint(edited) != base

    def test_version_bump_changes_fingerprint(self):
        base = case_set_fingerprint(self._cases())
        bumped = self._cases()
        bumped[0]["version"] = 2
        assert case_set_fingerprint(bumped) != base

    def test_mixed_missing_and_present_case_id_does_not_crash(self):
        # Hand-built dicts can mix None and str case_ids; the None-safe sort must
        # not TypeError (None vs str is unorderable in Python 3).
        cases = [
            {
                "case_id": None,
                "version": 1,
                "query_text": "q",
                "top_k": 1,
                "filters": {},
                "labels": [{"content_hash": "h", "judgment": "relevant"}],
            },
            {
                "case_id": "cid",
                "version": 1,
                "query_text": "q",
                "top_k": 1,
                "filters": {},
                "labels": [{"content_hash": "h", "judgment": "relevant"}],
            },
        ]
        assert isinstance(case_set_fingerprint(cases), str)


class TestStorageReadHelpers:
    """The mixin's read helpers feed the pure functions on a real index."""

    async def test_read_helpers_drive_fingerprints(self, bm25_only_components):
        components, mem_dir = bm25_only_components
        (mem_dir / "note.md").write_text("# Title\n\nAlpha beta gamma content here.\n")
        await components.index_engine.index_path(mem_dir, recursive=True)
        storage = components.storage

        corpus_rows = storage.read_corpus_fingerprint_rows()
        assert corpus_rows  # at least one chunk indexed
        vector_rows = storage.read_vector_fingerprint_rows()  # empty in BM25-only
        fts_rows = storage.read_fts_fingerprint_rows()
        emb = storage.stored_embedding_info

        fp1 = index_fingerprint(corpus_rows, vector_rows, fts_rows, emb)
        fp2 = index_fingerprint(
            storage.read_corpus_fingerprint_rows(),
            storage.read_vector_fingerprint_rows(),
            storage.read_fts_fingerprint_rows(),
            emb,
        )
        assert fp1 == fp2  # deterministic across reads
        assert corpus_fingerprint(corpus_rows)
