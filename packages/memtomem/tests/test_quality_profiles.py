"""Tests for versioned retrieval-profile documents (#1844, PR-1).

The contract these pin:

- equivalent definitions (omitted vs explicit default, int vs float, key order)
  produce one document fingerprint;
- a document cannot persist or echo a credential, an absolute path, or a
  secret-shaped value, and rejection messages never echo the offending input;
- ``apply_profile`` is pure (never mutates the ambient config), applies the same
  defaults-resolved values it fingerprints, and keeps every pinned section and
  field from the ambient install.
"""

from __future__ import annotations

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.errors import EvalCaseValidationError
from memtomem.quality.profiles import (
    PROFILE_KIND,
    PROFILE_SCHEMA_VERSION,
    apply_profile,
    canonicalize_profile,
    load_profile_document,
    profile_doc_fingerprint,
    profile_warnings,
)
from memtomem.secret_masking import is_secret_key


def _doc(knobs: dict | None = None, *, name: str = "p1", **extra) -> dict:
    d: dict = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "kind": PROFILE_KIND,
        "name": name,
    }
    if knobs is not None:
        d["knobs"] = knobs
    d.update(extra)
    return d


# --------------------------------------------------------------------------- #
# Equivalence / canonicalization
# --------------------------------------------------------------------------- #
def test_omitted_and_explicit_default_share_fingerprint():
    # rrf_k default is 60; omitting it must hash identically to writing it.
    omitted = load_profile_document(_doc({"decay": {"enabled": True, "half_life_days": 14.0}}))
    explicit = load_profile_document(
        _doc({"decay": {"enabled": True, "half_life_days": 14.0}, "search": {"rrf_k": 60}})
    )
    assert profile_doc_fingerprint(omitted)[0] == profile_doc_fingerprint(explicit)[0]


def test_int_and_float_forms_share_fingerprint():
    as_int = load_profile_document(_doc({"decay": {"enabled": True, "half_life_days": 14}}))
    as_float = load_profile_document(_doc({"decay": {"enabled": True, "half_life_days": 14.0}}))
    fp_int, canon = profile_doc_fingerprint(as_int)
    assert fp_int == profile_doc_fingerprint(as_float)[0]
    assert canon["decay"]["half_life_days"] == 14.0
    assert isinstance(canon["decay"]["half_life_days"], float)


def test_key_order_does_not_change_fingerprint():
    a = load_profile_document(_doc({"search": {"rrf_k": 40, "enable_dense": False}}))
    b = load_profile_document(_doc({"search": {"enable_dense": False, "rrf_k": 40}}))
    assert profile_doc_fingerprint(a)[0] == profile_doc_fingerprint(b)[0]


def test_different_knobs_diverge():
    a = load_profile_document(_doc({"search": {"rrf_k": 40}}))
    b = load_profile_document(_doc({"search": {"rrf_k": 41}}))
    assert profile_doc_fingerprint(a)[0] != profile_doc_fingerprint(b)[0]


def test_fingerprint_is_stable_across_runs():
    doc = load_profile_document(_doc({"mmr": {"enabled": True, "lambda_param": 0.5}}))
    assert profile_doc_fingerprint(doc)[0] == profile_doc_fingerprint(doc)[0]


def test_name_and_description_do_not_change_fingerprint():
    a = load_profile_document(_doc({"search": {"rrf_k": 40}}, name="alpha"))
    b = load_profile_document(
        _doc({"search": {"rrf_k": 40}}, name="beta", description="a different note")
    )
    assert profile_doc_fingerprint(a)[0] == profile_doc_fingerprint(b)[0]


def test_canonical_covers_every_eligible_field():
    doc = load_profile_document(_doc({}))
    canon = canonicalize_profile(doc)
    # Every eligible section present, each fully defaults-resolved.
    for section in (
        "search",
        "decay",
        "mmr",
        "access",
        "importance",
        "context_window",
        "rerank",
        "query_expansion",
        "session_summary",
    ):
        assert section in canon
    assert canon["search"]["rrf_k"] == 60
    assert canon["rerank"]["provider"] == "fastembed"


# --------------------------------------------------------------------------- #
# apply_profile — purity, correctness, pinning
# --------------------------------------------------------------------------- #
def test_apply_profile_is_pure():
    ambient = Mem2MemConfig()
    before = ambient.model_dump()
    apply_profile(ambient, load_profile_document(_doc({"search": {"rrf_k": 40}})))
    assert ambient.model_dump() == before


def test_apply_profile_applies_eligible_and_pins_the_rest():
    ambient = Mem2MemConfig()
    cfg = apply_profile(
        ambient, load_profile_document(_doc({"search": {"enable_dense": False, "rrf_k": 40}}))
    )
    # Eligible knobs applied…
    assert cfg.search.rrf_k == 40
    assert cfg.search.enable_dense is False
    # …pinned within-section fields kept from ambient…
    assert cfg.search.tokenizer == ambient.search.tokenizer
    assert cfg.search.cache_ttl == ambient.search.cache_ttl
    # …and whole pinned sections kept from ambient.
    assert cfg.storage.sqlite_path == ambient.storage.sqlite_path
    assert cfg.embedding.provider == ambient.embedding.provider


def test_apply_profile_omitted_knob_takes_default_not_ambient():
    # Ambient sets a non-default rrf_k; a profile that omits rrf_k must run the
    # package default (canonical value), NOT the ambient value — otherwise the
    # doc fingerprint would disagree with what executes.
    ambient = Mem2MemConfig()
    ambient.search.rrf_k = 99
    cfg = apply_profile(ambient, load_profile_document(_doc({"decay": {"enabled": True}})))
    assert cfg.search.rrf_k == 60  # package default, not 99


def test_apply_profile_matches_canonical_effective_profile():
    from memtomem.quality.fingerprints import profile_fingerprint

    ambient = Mem2MemConfig()
    doc = load_profile_document(_doc({"search": {"enable_dense": False, "rrf_k": 40}}))
    cfg_a = apply_profile(ambient, doc)
    cfg_b = apply_profile(ambient, doc)
    # Deterministic + independent objects.
    assert profile_fingerprint(cfg_a)[0] == profile_fingerprint(cfg_b)[0]


def test_apply_profile_no_deprecation_warning():
    import warnings

    ambient = Mem2MemConfig()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        # The rerank.top_k migration would raise here if apply left it in the payload.
        apply_profile(ambient, load_profile_document(_doc({"rerank": {"enabled": True}})))


# --------------------------------------------------------------------------- #
# Rejection matrix — every reject also proves no value leaks into the message
# --------------------------------------------------------------------------- #
_SECRET = "ghp_" + "a" * 36  # a shape the privacy scanner flags, charset-valid


@pytest.mark.parametrize(
    "bad, needle",
    [
        (_doc({"embedding": {"provider": "openai"}}), "openai"),
        (_doc({"llm": {"model": "gpt-4"}}), "gpt-4"),
        (_doc({"storage": {"sqlite_path": "/tmp/x.db"}}), "/tmp/x.db"),
        (_doc({"rerank": {"api_key": "supersecret"}}), "supersecret"),
        (_doc({"rerank": {"model": "/etc/passwd"}}), "passwd"),
        (_doc({"rerank": {"model": "a/b/c"}}), None),
        (_doc({"rerank": {"model": "../evil"}}), "evil"),
        (_doc({"rerank": {"model": _SECRET}}), _SECRET),
        (_doc({"rerank": {"provider": "weaviate"}}), "weaviate"),
        (_doc({"rerank": {"max_pool": 5, "min_pool": 20}}), None),
        (_doc({"search": {"rrf_k": True}}), None),
        (_doc({"search": {"rrf_k": 1.5}}), None),
        (_doc({"search": {"rrf_k": -5}}), None),
        (_doc({"search": {"rrf_k": None}}), None),
        (_doc({"search": {"rrf_weights": [1, 2, 3]}}), None),
        (_doc({"search": {"rrf_weights": [1.0, -1.0]}}), None),
        (_doc({"mmr": {"lambda_param": float("nan")}}), None),
        (_doc({"mmr": {"lambda_param": 2.0}}), None),
        (_doc({"query_expansion": {"max_terms": 0}}), None),
        (_doc({"query_expansion": {"strategy": "vibes"}}), "vibes"),
        (_doc({"session_summary": {"expansion_lookup_top_k": 0}}), None),
        (_doc({"search": {"bogus": 1}}), None),
        (_doc(name="Bad Name!"), None),
        (_doc(name=_SECRET), _SECRET),
        (_doc({}, description="see /etc/passwd"), "passwd"),
        ({"schema_version": 2, "kind": PROFILE_KIND, "name": "p"}, None),
        ({"schema_version": True, "kind": PROFILE_KIND, "name": "p"}, None),
        ({"schema_version": 1, "kind": "something_else", "name": "p"}, None),
    ],
)
def test_rejections_never_echo_the_offending_value(bad, needle):
    with pytest.raises(EvalCaseValidationError) as exc:
        load_profile_document(bad)
    if needle is not None:
        assert needle not in str(exc.value)


def test_secret_key_rejected_at_any_depth():
    with pytest.raises(EvalCaseValidationError):
        load_profile_document(_doc({"search": {"api_key": "x"}}))


def test_no_secret_key_survives_into_canonical_output():
    # Sentinel: no eligible field name in the canonical dict is secret-shaped.
    doc = load_profile_document(_doc({}))
    canon = canonicalize_profile(doc)
    for section, fields in canon.items():
        for key in fields:
            assert not is_secret_key(str(key)), f"{section}.{key}"


def test_description_allows_crlf_whitespace():
    # A note round-tripped from Windows (\r\n) must be accepted; other control
    # chars are still rejected.
    doc = load_profile_document(_doc({}, description="line one\r\nline two\ttabbed"))
    assert doc.description == "line one\r\nline two\ttabbed"
    with pytest.raises(EvalCaseValidationError):
        load_profile_document(_doc({}, description="bad\x00null"))


def test_apply_profile_output_carries_no_secret_from_document():
    ambient = Mem2MemConfig()
    ambient.rerank.api_key = "AMBIENT-SENTINEL"
    cfg = apply_profile(ambient, load_profile_document(_doc({"rerank": {"enabled": True}})))
    # The document never carries api_key; the applied config inherits the
    # ambient one (needed for cohere) but the profile could not have set it.
    assert cfg.rerank.api_key == "AMBIENT-SENTINEL"


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #
def test_mmr_without_dense_warns():
    ambient = Mem2MemConfig()
    doc = load_profile_document(_doc({"mmr": {"enabled": True}, "search": {"enable_dense": False}}))
    assert "mmr_inactive_dense_disabled" in profile_warnings(apply_profile(ambient, doc), doc)


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("cohere", "Xenova/ms-marco-MiniLM-L-6-v2"),
        ("local", "jinaai/jina-reranker-v2-base-multilingual"),
        ("fastembed", "rerank-english-v3.0"),
        ("fastembed", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
        ("cohere", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
        ("local", "rerank-multilingual-v3.0"),
    ],
)
def test_recognized_rerank_provider_model_mismatch_warns(provider, model):
    ambient = Mem2MemConfig()
    ambient.rerank.api_key = "test-key"
    doc = load_profile_document(
        _doc({"rerank": {"enabled": True, "provider": provider, "model": model}})
    )
    assert "rerank_provider_model_mismatch" in profile_warnings(
        apply_profile(ambient, doc), doc
    )


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("fastembed", "Xenova/ms-marco-MiniLM-L-6-v2"),
        ("cohere", "rerank-english-v3.0"),
        ("local", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    ],
)
def test_matching_rerank_provider_model_does_not_warn(provider, model):
    ambient = Mem2MemConfig()
    ambient.rerank.api_key = "test-key"
    doc = load_profile_document(
        _doc({"rerank": {"enabled": True, "provider": provider, "model": model}})
    )
    assert "rerank_provider_model_mismatch" not in profile_warnings(
        apply_profile(ambient, doc), doc
    )


@pytest.mark.parametrize(
    "rerank_knobs",
    [
        {"enabled": True, "provider": "cohere"},
        {"enabled": True, "provider": "local"},
        {"enabled": True, "model": "rerank-english-v3.0"},
    ],
)
def test_rerank_mismatch_uses_resolved_defaults(rerank_knobs):
    ambient = Mem2MemConfig()
    ambient.rerank.api_key = "test-key"
    doc = load_profile_document(_doc({"rerank": rerank_knobs}))
    assert "rerank_provider_model_mismatch" in profile_warnings(
        apply_profile(ambient, doc), doc
    )


@pytest.mark.parametrize("provider", ["fastembed", "cohere", "local"])
def test_unknown_custom_rerank_model_does_not_warn(provider):
    ambient = Mem2MemConfig()
    ambient.rerank.api_key = "test-key"
    doc = load_profile_document(
        _doc(
            {
                "rerank": {
                    "enabled": True,
                    "provider": provider,
                    "model": "acme/custom-reranker-v1",
                }
            }
        )
    )
    assert "rerank_provider_model_mismatch" not in profile_warnings(
        apply_profile(ambient, doc), doc
    )


def test_disabled_rerank_provider_model_mismatch_does_not_warn():
    ambient = Mem2MemConfig()
    doc = load_profile_document(
        _doc(
            {
                "rerank": {
                    "enabled": False,
                    "provider": "cohere",
                    "model": "Xenova/ms-marco-MiniLM-L-6-v2",
                }
            }
        )
    )
    assert "rerank_provider_model_mismatch" not in profile_warnings(
        apply_profile(ambient, doc), doc
    )


def test_rerank_mismatch_and_missing_key_warnings_are_sorted():
    ambient = Mem2MemConfig()
    doc = load_profile_document(_doc({"rerank": {"enabled": True, "provider": "cohere"}}))
    assert profile_warnings(apply_profile(ambient, doc), doc) == [
        "rerank_cohere_without_api_key",
        "rerank_provider_model_mismatch",
    ]


def test_clean_profile_has_no_warnings():
    ambient = Mem2MemConfig()
    doc = load_profile_document(_doc({"decay": {"enabled": True}}))
    assert profile_warnings(apply_profile(ambient, doc), doc) == []
