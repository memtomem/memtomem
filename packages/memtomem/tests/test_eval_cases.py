"""Storage tests for durable evaluation cases (#1802, Quality Lab PR-3).

Promotion copies a labeled search run into a self-contained case atomically,
refuses runs it cannot faithfully replay, and survives the history prune it is
deliberately decoupled from. Export/import round-trips through content_hash
identity, leaving chunk_id unresolved for the importing index.
"""

from __future__ import annotations

import json

import pytest

from memtomem.config import StorageConfig
from memtomem.errors import EvalCaseError
from memtomem.storage.sqlite_backend import SqliteBackend

RUN_A = "11111111-1111-4111-8111-111111111111"
RUN_B = "22222222-2222-4222-8222-222222222222"

FP = {"profile": "profile-fp", "corpus": "corpus-fp", "index": "index-fp"}

_OPEN_FILTERS = {
    "namespace": None,
    "scope": None,
    "has_source_filter": False,
    "has_tag_filter": False,
    "has_metadata_filter": False,
    "has_as_of": False,
}


async def _seed_labeled_run(
    storage,
    run_id: str = RUN_A,
    *,
    chunks: list[tuple[str, str]] | None = None,
    feedback: list[tuple[str, str]] | None = None,
    filters: dict | None = None,
    top_k: int | None = None,
) -> str:
    """Seed a run whose snapshot carries content_hash, then attach feedback.

    ``chunks`` is ``[(chunk_id, content_hash), ...]``; ``feedback`` is
    ``[(chunk_id, judgment), ...]``.
    """
    chunks = chunks if chunks is not None else [("c1", "hash-1"), ("c2", "hash-2")]
    feedback = feedback if feedback is not None else [("c1", "relevant"), ("c2", "not_relevant")]
    snapshot = [
        {
            "chunk_id": cid,
            "rank": i + 1,
            "score": 0.9 - i * 0.1,
            "source_name": "note.md",
            "content_hash": chash,
        }
        for i, (cid, chash) in enumerate(chunks)
    ]
    observation = {
        "origin": "mcp",
        "top_k": top_k if top_k is not None else len(chunks),
        "filters": {**_OPEN_FILTERS, **(filters or {})},
    }
    await storage.save_search_observation(
        "quality query",
        [0.1, 0.2],
        [cid for cid, _ in chunks],
        [0.9] * len(chunks),
        run_id=run_id,
        observation=observation,
        result_snapshot=snapshot,
    )
    for cid, judgment in feedback:
        await storage.save_search_feedback(run_id, cid, judgment)
    return run_id


@pytest.fixture
async def other_backend(tmp_path):
    cfg = StorageConfig()
    cfg.sqlite_path = tmp_path / "import-target.db"
    backend = SqliteBackend(cfg, dimension=8)
    await backend.initialize()
    yield backend
    await backend.close()


class TestPromotion:
    async def test_copies_case_and_labels(self, storage):
        await _seed_labeled_run(storage)
        case = await storage.promote_search_run(RUN_A, name="baseline", fingerprints=FP)

        assert case["name"] == "baseline"
        assert case["query_text"] == "quality query"
        assert case["top_k"] == 2
        assert case["source_run_id"] == RUN_A
        assert case["version"] == 1
        assert case["status"] == "active"
        assert case["promoted_fingerprints"] == FP
        assert case["filters"] == {"namespace": None, "scope": None}
        assert len(case["promotion_snapshot"]) == 2
        judgments = {lab["content_hash"]: lab["judgment"] for lab in case["labels"]}
        assert judgments == {"hash-1": "relevant", "hash-2": "not_relevant"}
        assert {lab["chunk_id"] for lab in case["labels"]} == {"c1", "c2"}

    async def test_refuses_unknown_run(self, storage):
        with pytest.raises(EvalCaseError, match="not found"):
            await storage.promote_search_run(RUN_A, fingerprints=FP)

    async def test_refuses_run_without_feedback(self, storage):
        await _seed_labeled_run(storage, feedback=[])
        with pytest.raises(EvalCaseError, match="no feedback"):
            await storage.promote_search_run(RUN_A, fingerprints=FP)

    async def test_requires_all_fingerprints(self, storage):
        await _seed_labeled_run(storage)
        with pytest.raises(EvalCaseError, match="fingerprints must include"):
            await storage.promote_search_run(RUN_A, fingerprints={"profile": "p"})

    async def test_name_collision_is_atomic(self, storage):
        await _seed_labeled_run(storage, RUN_A)
        await _seed_labeled_run(
            storage, RUN_B, chunks=[("d1", "hash-9")], feedback=[("d1", "relevant")]
        )
        await storage.promote_search_run(RUN_A, name="dup", fingerprints=FP)
        with pytest.raises(EvalCaseError, match="already exists"):
            await storage.promote_search_run(RUN_B, name="dup", fingerprints=FP)
        # The failed promotion left nothing behind.
        cases = await storage.list_eval_cases()
        assert len(cases) == 1
        assert cases[0]["source_run_id"] == RUN_A

    @pytest.mark.parametrize(
        "flag",
        ["has_source_filter", "has_tag_filter", "has_metadata_filter", "has_as_of"],
    )
    async def test_refuses_unreplayable_filters(self, storage, flag):
        await _seed_labeled_run(storage, filters={flag: True})
        with pytest.raises(EvalCaseError, match="unreplayable"):
            await storage.promote_search_run(RUN_A, fingerprints=FP)
        # ...unless the caller explicitly opts in.
        case = await storage.promote_search_run(
            RUN_A, fingerprints=FP, allow_unreplayable_filters=True
        )
        assert case["case_id"]

    @pytest.mark.parametrize(
        "scope",
        [
            "project_shared",
            "project_local",
            "project_*",
            ["user", "project_shared"],
            "PROJECT_*",  # SQLite LIKE is case-insensitive → still reaches project tiers
            "project%*",  # translated '%' is a wildcard under LIKE
            "*",  # matches everything, project tiers included
        ],
    )
    async def test_refuses_project_scope(self, storage, scope):
        await _seed_labeled_run(storage, filters={"scope": scope})
        with pytest.raises(EvalCaseError, match="project-scoped"):
            await storage.promote_search_run(RUN_A, fingerprints=FP)

    @pytest.mark.parametrize("scope", [None, "user", "user*", "USER*", "unknown_tier"])
    async def test_allows_user_and_non_project_scope(self, storage, scope):
        # None/user/user-only glob (any case)/unknown token never reach a project
        # tier, so project_context_root is irrelevant and the run is promotable.
        await _seed_labeled_run(storage, filters={"scope": scope})
        case = await storage.promote_search_run(RUN_A, fingerprints=FP)
        assert case["filters"]["scope"] == scope

    async def test_duplicate_hash_agreeing_collapses(self, storage):
        await _seed_labeled_run(
            storage,
            chunks=[("c1", "same"), ("c2", "same")],
            feedback=[("c1", "relevant"), ("c2", "relevant")],
        )
        case = await storage.promote_search_run(RUN_A, fingerprints=FP)
        assert len(case["labels"]) == 1
        assert case["labels"][0]["judgment"] == "relevant"

    async def test_duplicate_hash_conflict_raises(self, storage):
        await _seed_labeled_run(
            storage,
            chunks=[("c1", "same"), ("c2", "same")],
            feedback=[("c1", "relevant"), ("c2", "not_relevant")],
        )
        with pytest.raises(EvalCaseError, match="conflicting judgments"):
            await storage.promote_search_run(RUN_A, fingerprints=FP)

    async def test_refuses_inside_transaction_block(self, storage):
        await _seed_labeled_run(storage)
        with pytest.raises(EvalCaseError, match="transaction block"):
            async with storage.transaction():
                await storage.promote_search_run(RUN_A, fingerprints=FP)

    async def test_non_numeric_observation_top_k_raises(self, storage):
        # A corrupt observation with a non-numeric top_k must raise EvalCaseError,
        # not a raw ValueError/TypeError from int().
        await _seed_labeled_run(storage, filters={}, top_k=2)
        db = storage._get_db()
        row = db.execute(
            "SELECT observation_json FROM query_history WHERE run_id = ?", (RUN_A,)
        ).fetchone()
        obs = json.loads(row[0])
        obs["top_k"] = "lots"
        db.execute(
            "UPDATE query_history SET observation_json = ? WHERE run_id = ?",
            (json.dumps(obs), RUN_A),
        )
        db.commit()
        with pytest.raises(EvalCaseError, match="non-numeric top_k"):
            await storage.promote_search_run(RUN_A, fingerprints=FP)

    async def test_case_survives_history_prune(self, storage):
        await _seed_labeled_run(storage)
        case = await storage.promote_search_run(RUN_A, fingerprints=FP)
        db = storage._get_db()
        db.execute("UPDATE query_history SET created_at = '2020-01-01T00:00:00+00:00'")
        db.commit()
        storage._prune_old_history()

        assert db.execute("SELECT COUNT(*) FROM query_history").fetchone()[0] == 0
        survived = await storage.get_eval_case(case["case_id"])
        assert survived["source_run_id"] == RUN_A  # dangling provenance, not an FK
        assert len(survived["labels"]) == 2


class TestRetrieval:
    async def test_get_by_name_and_id(self, storage):
        await _seed_labeled_run(storage)
        case = await storage.promote_search_run(RUN_A, name="byname", fingerprints=FP)
        by_id = await storage.get_eval_case(case["case_id"])
        by_name = await storage.get_eval_case("byname")
        assert by_id["case_id"] == by_name["case_id"]

    async def test_get_missing_raises(self, storage):
        with pytest.raises(EvalCaseError, match="not found"):
            await storage.get_eval_case("nope")

    async def test_set_status_bumps_updated_at(self, storage):
        await _seed_labeled_run(storage)
        case = await storage.promote_search_run(RUN_A, fingerprints=FP)
        updated = await storage.set_eval_case_status(case["case_id"], "archived")
        assert updated["status"] == "archived"
        assert updated["updated_at"] >= case["updated_at"]

    async def test_set_status_rejects_bad_value(self, storage):
        await _seed_labeled_run(storage)
        case = await storage.promote_search_run(RUN_A, fingerprints=FP)
        with pytest.raises(EvalCaseError, match="status must be"):
            await storage.set_eval_case_status(case["case_id"], "bogus")

    async def test_list_filters_by_status(self, storage):
        await _seed_labeled_run(storage, RUN_A)
        await _seed_labeled_run(
            storage, RUN_B, chunks=[("d1", "h9")], feedback=[("d1", "relevant")]
        )
        a = await storage.promote_search_run(RUN_A, name="a", fingerprints=FP)
        await storage.promote_search_run(RUN_B, name="b", fingerprints=FP)
        await storage.set_eval_case_status(a["case_id"], "archived")

        assert {c["name"] for c in await storage.list_eval_cases()} == {"a", "b"}
        assert {c["name"] for c in await storage.list_eval_cases(status="active")} == {"b"}
        assert {c["name"] for c in await storage.list_eval_cases(status="archived")} == {"a"}

    async def test_case_id_lookup_wins_over_name_collision(self, storage):
        # Pathological but possible: case B's name equals case A's case_id. A
        # lookup by that string must resolve to A (id match wins), not B.
        await _seed_labeled_run(storage, RUN_A)
        await _seed_labeled_run(
            storage, RUN_B, chunks=[("d1", "h9")], feedback=[("d1", "relevant")]
        )
        a = await storage.promote_search_run(RUN_A, fingerprints=FP)
        await storage.promote_search_run(RUN_B, name=a["case_id"], fingerprints=FP)

        resolved = await storage.get_eval_case(a["case_id"])
        assert resolved["source_run_id"] == RUN_A  # id-match wins over the name collision
        # set_status resolves the same way.
        await storage.set_eval_case_status(a["case_id"], "archived")
        assert (await storage.get_eval_case(a["case_id"]))["status"] == "archived"


class TestResetAll:
    async def test_reset_all_clears_eval_tables(self, storage):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, fingerprints=FP)
        deleted = await storage.reset_all()
        assert deleted.get("eval_cases") == 1
        assert deleted.get("eval_case_labels") == 2
        assert await storage.list_eval_cases() == []


class TestExportImport:
    async def test_round_trip_leaves_chunk_id_null(self, storage, other_backend):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="shared", fingerprints=FP)
        payload = await storage.export_eval_cases()

        result = await other_backend.import_eval_cases(payload)
        assert result["imported"] == 1
        imported = await other_backend.get_eval_case("shared")
        assert imported["query_text"] == "quality query"
        assert {lab["content_hash"]: lab["judgment"] for lab in imported["labels"]} == {
            "hash-1": "relevant",
            "hash-2": "not_relevant",
        }
        assert all(lab["chunk_id"] is None for lab in imported["labels"])

    async def test_export_omits_chunk_id(self, storage):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="e", fingerprints=FP)
        payload = await storage.export_eval_cases()
        assert payload["kind"] == "eval_case_set"
        assert payload["schema_version"] == 1
        for lab in payload["cases"][0]["labels"]:
            assert set(lab) == {"content_hash", "judgment"}

    async def test_import_rejects_bad_schema_version(self, other_backend):
        with pytest.raises(EvalCaseError, match="schema_version"):
            await other_backend.import_eval_cases(
                {"schema_version": 2, "kind": "eval_case_set", "cases": []}
            )

    async def test_import_rejects_bad_kind(self, other_backend):
        with pytest.raises(EvalCaseError, match="kind"):
            await other_backend.import_eval_cases(
                {"schema_version": 1, "kind": "junk", "cases": []}
            )

    async def test_import_rejects_bad_judgment(self, other_backend):
        payload = {
            "schema_version": 1,
            "kind": "eval_case_set",
            "cases": [
                {
                    "name": "x",
                    "query_text": "q",
                    "top_k": 1,
                    "labels": [{"content_hash": "h", "judgment": "maybe"}],
                }
            ],
        }
        with pytest.raises(EvalCaseError, match="judgment must be"):
            await other_backend.import_eval_cases(payload)

    @pytest.mark.parametrize(
        "overrides,match",
        [
            ({"top_k": 0}, "top_k"),
            ({"top_k": -3}, "top_k"),
            ({"top_k": "lots"}, "top_k"),
            ({"status": "weird"}, "status"),
            ({"filters": "notadict"}, "filters"),
            ({"promoted_fingerprints": "nope"}, "promoted_fingerprints"),
            ({"name": 123}, "name"),
            # Nested non-scalars must raise EvalCaseError, never a raw
            # TypeError (unhashable in a frozenset test) or SQLite binding error.
            ({"status": []}, "status"),
            ({"labels": [{"content_hash": "h", "judgment": []}]}, "judgment"),
            ({"labels": [{"content_hash": 5, "judgment": "relevant"}]}, "content_hash"),
            ({"query_text": ["not", "a", "string"]}, "query_text"),
            ({"promoted_fingerprints": {"profile": ["x"]}}, "fingerprint"),
            ({"source_run_id": ["nested"]}, "source_run_id"),
            (
                {
                    "labels": [
                        {"content_hash": "dup", "judgment": "relevant"},
                        {"content_hash": "dup", "judgment": "not_relevant"},
                    ]
                },
                "duplicate label",
            ),
            # A present-but-null fingerprint value must raise EvalCaseError, not a
            # raw NOT NULL IntegrityError at insert (.get default only fills an
            # absent key).
            ({"promoted_fingerprints": {"profile": None}}, "fingerprint"),
            ({"labels": []}, "at least one label"),
        ],
    )
    async def test_import_rejects_malformed_domain_fields(self, other_backend, overrides, match):
        case = {
            "name": "c",
            "query_text": "q",
            "top_k": 1,
            "labels": [{"content_hash": "h", "judgment": "relevant"}],
        }
        case.update(overrides)
        payload = {"schema_version": 1, "kind": "eval_case_set", "cases": [case]}
        with pytest.raises(EvalCaseError, match=match):
            await other_backend.import_eval_cases(payload)
        assert await other_backend.list_eval_cases() == []  # nothing persisted

    async def test_import_name_collision_without_replace(self, storage, other_backend):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="dup", fingerprints=FP)
        payload = await storage.export_eval_cases()
        await other_backend.import_eval_cases(payload)
        with pytest.raises(EvalCaseError, match="already exists"):
            await other_backend.import_eval_cases(payload)

    async def test_import_preserves_case_id_and_version(self, storage, other_backend):
        # Export→import is identity-preserving: the imported case keeps the
        # source case_id (so case_set_fingerprint matches across machines) and
        # the source version, rather than minting fresh ones.
        await _seed_labeled_run(storage)
        source = await storage.promote_search_run(RUN_A, name="dup", fingerprints=FP)
        payload = await storage.export_eval_cases()
        await other_backend.import_eval_cases(payload)

        imported = await other_backend.get_eval_case("dup")
        assert imported["case_id"] == source["case_id"]
        assert imported["version"] == source["version"] == 1

    async def test_import_replace_preserves_payload_version(self, storage, other_backend):
        # Replace overwrites the same case_id in place and adopts the payload's
        # version (import preserves version; it does not auto-bump).
        await _seed_labeled_run(storage)
        source = await storage.promote_search_run(RUN_A, name="dup", fingerprints=FP)
        payload = await storage.export_eval_cases()
        await other_backend.import_eval_cases(payload)
        payload["cases"][0]["version"] = 7
        await other_backend.import_eval_cases(payload, replace=True)

        replaced = await other_backend.get_eval_case("dup")
        assert replaced["case_id"] == source["case_id"]  # id retained in place
        assert replaced["version"] == 7

    async def test_import_name_owned_by_different_case_conflicts(self, storage, other_backend):
        # A name already owned by a *different* case_id is a hard conflict — even
        # with replace, we never silently delete the unrelated case.
        await _seed_labeled_run(storage, RUN_A)
        await _seed_labeled_run(
            storage, RUN_B, chunks=[("d1", "h9")], feedback=[("d1", "relevant")]
        )
        a = await storage.promote_search_run(RUN_A, name="shared", fingerprints=FP)
        payload_a = await storage.export_eval_cases([a["case_id"]])
        await other_backend.import_eval_cases(payload_a)
        # A second, different case that wants the same name "shared".
        b = await storage.promote_search_run(RUN_B, fingerprints=FP)
        payload_b = await storage.export_eval_cases([b["case_id"]])
        payload_b["cases"][0]["name"] = "shared"
        with pytest.raises(EvalCaseError, match="different case"):
            await other_backend.import_eval_cases(payload_b, replace=True)

    async def test_export_unknown_id_raises(self, storage):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="real", fingerprints=FP)
        with pytest.raises(EvalCaseError, match="unknown eval case ids"):
            await storage.export_eval_cases(["does-not-exist"])

    async def test_import_duplicate_case_id_in_payload_rejected(self, storage, other_backend):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="c", fingerprints=FP)
        case = (await storage.export_eval_cases())["cases"][0]
        # Two cases in one envelope sharing a case_id would silently last-wins
        # under replace; reject it up front.
        second = {**case, "name": "c2"}
        payload = {"schema_version": 1, "kind": "eval_case_set", "cases": [case, second]}
        with pytest.raises(EvalCaseError, match="duplicate case_id"):
            await other_backend.import_eval_cases(payload, replace=True)
        assert await other_backend.list_eval_cases() == []  # rolled back / nothing written

    async def test_failed_batch_rolls_back_a_replacement(self, storage, other_backend):
        # A later malformed case must not leave an earlier replace half-applied:
        # the whole batch is one transaction.
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="keep", fingerprints=FP)
        good = (await storage.export_eval_cases())["cases"][0]
        assert (
            await other_backend.import_eval_cases(
                {
                    "schema_version": 1,
                    "kind": "eval_case_set",
                    "cases": [good],
                }
            )
        )["imported"] == 1

        # Batch: case 1 would REPLACE "keep"; case 2 is malformed and raises.
        bad = {
            "name": "new",
            "query_text": "q",
            "top_k": 1,
            "labels": [{"content_hash": "h", "judgment": "maybe"}],
        }
        with pytest.raises(EvalCaseError, match="judgment must be"):
            await other_backend.import_eval_cases(
                {"schema_version": 1, "kind": "eval_case_set", "cases": [good, bad]},
                replace=True,
            )
        # "keep" is untouched (still v1, replacement rolled back) and "new" absent.
        assert (await other_backend.get_eval_case("keep"))["version"] == 1
        assert {c["name"] for c in await other_backend.list_eval_cases()} == {"keep"}

    async def test_import_refuses_inside_transaction_block(self, storage, other_backend):
        await _seed_labeled_run(storage)
        await storage.promote_search_run(RUN_A, name="x", fingerprints=FP)
        payload = await storage.export_eval_cases()
        with pytest.raises(EvalCaseError, match="transaction block"):
            async with other_backend.transaction():
                await other_backend.import_eval_cases(payload)
