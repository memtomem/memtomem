"""Policy target namespaces pass the write gate (#2949).

``auto_archive``, ``auto_promote`` and ``auto_consolidate`` move or write chunks
into namespaces their config names. ``mem_policy_add`` refuses a target a search
could not find by that exact name, and the handlers refuse to run a policy
stored with one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from helpers import make_chunk

from memtomem.tools.policy_engine import (
    execute_auto_archive,
    policy_target_errors,
    run_all_enabled,
    run_policy,
)

REFUSED_NAMES = ["a,b", "archive:*", "!system", "agent-runtime:foo:bar", " "]


def _namespace_of(storage, chunk) -> str:
    row = (
        storage._get_db()
        .execute("SELECT namespace FROM chunks WHERE id = ?", [str(chunk.id)])
        .fetchone()
    )
    return row[0]


async def _old_chunks(storage, *chunks):
    await storage.upsert_chunks(list(chunks))
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    db = storage._get_db()
    db.executemany(
        "UPDATE chunks SET created_at = ? WHERE id = ?", [(old, str(c.id)) for c in chunks]
    )
    db.commit()


def _policy(name: str, ptype: str, config: dict) -> dict:
    return {"name": name, "policy_type": ptype, "config": config, "namespace_filter": None}


class TestPolicyTargetErrors:
    @pytest.mark.parametrize("bad", REFUSED_NAMES)
    @pytest.mark.parametrize(
        "ptype,key",
        [
            ("auto_archive", "archive_namespace"),
            ("auto_promote", "target_namespace"),
            ("auto_consolidate", "summary_namespace"),
        ],
    )
    def test_refuses_each_target_key(self, ptype, key, bad):
        (error,) = policy_target_errors(ptype, {key: bad})
        assert error.startswith(f"{key}: ")

    @pytest.mark.parametrize("value", [5, None, ["a"]])
    def test_refuses_a_non_string_target(self, value):
        assert policy_target_errors("auto_promote", {"target_namespace": value})

    def test_defaults_and_ordinary_names_pass(self):
        for ptype in ("auto_archive", "auto_promote", "auto_consolidate"):
            assert policy_target_errors(ptype, {}) == []
        assert policy_target_errors("auto_archive", {"archive_namespace": "cold:2026"}) == []
        assert policy_target_errors("auto_promote", {"target_namespace": "work"}) == []

    def test_a_key_the_type_does_not_read_is_not_judged(self):
        assert policy_target_errors("auto_archive", {"target_namespace": "!system"}) == []
        assert policy_target_errors("auto_expire", {"archive_namespace": "a,b"}) == []
        assert policy_target_errors("auto_tag", {"summary_namespace": "*"}) == []

    def test_template_literal_text_is_judged(self):
        (error,) = policy_target_errors(
            "auto_archive", {"archive_namespace_template": "a,b:{first_tag}"}
        )
        assert "'a,b:{first_tag}'" in error
        assert policy_target_errors("auto_archive", {"archive_namespace_template": "!system"})
        assert (
            policy_target_errors(
                "auto_archive", {"archive_namespace_template": "archive:{first_tag}"}
            )
            == []
        )

    def test_fallback_is_judged_only_when_it_can_be_selected(self):
        bad_fallback = {"archive_namespace": "!system"}
        assert (
            policy_target_errors(
                "auto_archive", {**bad_fallback, "archive_namespace_template": "a:{first_tag}"}
            )
            == []
        )
        # No template and an empty template both select the fallback.
        assert policy_target_errors("auto_archive", bad_fallback)
        assert policy_target_errors(
            "auto_archive", {**bad_fallback, "archive_namespace_template": None}
        )
        assert policy_target_errors(
            "auto_archive", {**bad_fallback, "archive_namespace_template": ""}
        )

    @pytest.mark.parametrize("template", [False, 0, ["a"], 5])
    def test_a_non_string_template_is_refused(self, template):
        (error,) = policy_target_errors("auto_archive", {"archive_namespace_template": template})
        assert "expected a string or null" in error


class TestMemPolicyAdd:
    async def test_refused_target_stores_nothing(self, components):
        from memtomem.server.tools.policy import mem_policy_add

        from tests.test_tools_logic import _fake_ctx

        out = await mem_policy_add(
            name="arch",
            policy_type="auto_archive",
            config=json.dumps({"archive_namespace_template": "a,b:{first_tag}"}),
            ctx=_fake_ctx(components),
        )
        assert out.startswith("Error: archive_namespace_template 'a,b:{first_tag}'")
        assert await components.storage.policy_get("arch") is None

    async def test_valid_target_is_stored(self, components):
        from memtomem.server.tools.policy import mem_policy_add

        from tests.test_tools_logic import _fake_ctx

        out = await mem_policy_add(
            name="promote",
            policy_type="auto_promote",
            config=json.dumps({"target_namespace": "work"}),
            ctx=_fake_ctx(components),
        )
        assert "created" in out
        assert await components.storage.policy_get("promote") is not None

    async def test_explicit_null_template_keeps_flat_mode(self, components):
        from memtomem.server.tools.policy import mem_policy_add

        from tests.test_tools_logic import _fake_ctx

        out = await mem_policy_add(
            name="flat",
            policy_type="auto_archive",
            config=json.dumps({"archive_namespace_template": None}),
            ctx=_fake_ctx(components),
        )
        assert "created" in out


class TestStoredPolicyWithRefusedTarget:
    """A row stored before the gate existed is refused at run time, moving nothing."""

    @pytest.mark.parametrize("dry_run", [True, False])
    async def test_archive_flat(self, storage, dry_run):
        chunk = make_chunk("stale", namespace="default")
        await _old_chunks(storage, chunk)

        result = await run_policy(
            storage,
            _policy("arch", "auto_archive", {"max_age_days": 30, "archive_namespace": "a,b"}),
            dry_run=dry_run,
        )

        assert result.error and result.details.startswith("Error: archive_namespace")
        assert result.affected_count == 0 and result.mutated is False
        assert _namespace_of(storage, chunk) == "default"
        runs = await storage.maintenance_run_latest(limit=5)
        if dry_run:
            assert runs == []
        else:
            (run,) = runs
            assert run["status"] == "error"

    @pytest.mark.parametrize("dry_run", [True, False])
    async def test_promote(self, storage, dry_run):
        chunk = make_chunk("hot", namespace="archive")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute("UPDATE chunks SET access_count = 10 WHERE id = ?", [str(chunk.id)])
        db.commit()

        result = await run_policy(
            storage,
            _policy("promote", "auto_promote", {"target_namespace": "!system"}),
            dry_run=dry_run,
        )

        assert result.error and result.details.startswith("Error: target_namespace")
        assert _namespace_of(storage, chunk) == "archive"
        runs = await storage.maintenance_run_latest(limit=5)
        assert [r["status"] for r in runs] == ([] if dry_run else ["error"])

    @pytest.mark.parametrize("dry_run", [True, False])
    async def test_consolidate(self, storage, dry_run):
        chunks = [
            make_chunk(f"Notes from section {i}.", source="m.md", heading=("M", f"S{i}"))
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)

        result = await run_policy(
            storage,
            _policy("cons", "auto_consolidate", {"summary_namespace": "archive:*"}),
            dry_run=dry_run,
        )

        assert result.error and result.details.startswith("Error: summary_namespace")
        assert result.affected_count == 0
        count = storage._get_db().execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        assert count == 3  # no summary chunk written
        runs = await storage.maintenance_run_latest(limit=5)
        assert [r["status"] for r in runs] == ([] if dry_run else ["error"])

    async def test_bad_fallback_refused_with_empty_template_even_with_no_candidates(self, storage):
        result = await execute_auto_archive(
            storage,
            {"archive_namespace": "!system", "archive_namespace_template": ""},
            namespace=None,
            dry_run=False,
        )
        assert result.error

    async def test_bad_fallback_ignored_when_a_template_is_set(self, storage):
        chunk = make_chunk("stale", tags=("work",))
        await _old_chunks(storage, chunk)

        result = await execute_auto_archive(
            storage,
            {
                "max_age_days": 30,
                "archive_namespace": "!system",
                "archive_namespace_template": "archive:{first_tag}",
            },
            namespace=None,
            dry_run=False,
        )
        assert result.error is None
        assert _namespace_of(storage, chunk) == "archive:work"

    @pytest.mark.parametrize("template", [False, 0, ["a"], 5])
    async def test_non_string_template_is_refused_before_resolution(self, storage, template):
        chunk = make_chunk("stale")
        await _old_chunks(storage, chunk)

        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "archive_namespace_template": template},
            namespace=None,
            dry_run=False,
        )
        assert result.error and "expected a string or null" in result.error
        assert _namespace_of(storage, chunk) == "default"


class TestTemplateTargetsFromTags:
    """A tag can resolve an accepted template to a refused name; that chunk stays."""

    async def test_mixed_run_moves_the_rest_and_reports_an_error(self, storage):
        refused = make_chunk("runtime-tagged", tags=("agent-runtime",))
        ok = make_chunk("work-tagged", tags=("work",))
        await _old_chunks(storage, refused, ok)

        result = await run_policy(
            storage,
            _policy(
                "arch",
                "auto_archive",
                {"max_age_days": 30, "archive_namespace_template": "{first_tag}"},
            ),
            dry_run=False,
        )

        assert _namespace_of(storage, refused) == "default"
        assert _namespace_of(storage, ok) == "work"
        assert result.affected_count == 1
        assert result.mutated is True
        assert result.error and "'agent-runtime'" in result.error
        assert "1 skipped" in result.details
        (run,) = await storage.maintenance_run_latest(kind="auto_archive")
        assert run["status"] == "error"
        assert run["affected_count"] == 1
        assert run["summary"]["refused"] == [{"target": "agent-runtime", "count": 1}]
        assert run["summary"]["moved"] == [{"target": "work", "ids": [str(ok.id)], "omitted": 0}]
        # The refused target was never touched, so it is not a run namespace.
        assert "agent-runtime" not in run["namespaces"]

    async def test_all_refused_run_moves_nothing(self, storage):
        long_tag = "x" * 70  # over the agent-id cap
        chunk = make_chunk("stale", tags=(long_tag,))
        await _old_chunks(storage, chunk)

        result = await run_policy(
            storage,
            _policy(
                "arch",
                "auto_archive",
                {"max_age_days": 30, "archive_namespace_template": "agent-runtime:{first_tag}"},
            ),
            dry_run=False,
        )

        assert _namespace_of(storage, chunk) == "default"
        assert result.affected_count == 0
        assert result.mutated is False
        assert result.error
        assert "moved" not in result.outcome

    async def test_self_move_is_not_a_refusal(self, storage):
        """A chunk already in its resolved target is skipped before the gate.

        The target here is itself refused, so judging it before the self-move
        skip would count a chunk the run never meant to move.
        """
        chunk = make_chunk("in place", tags=("agent-runtime",), namespace="agent-runtime")
        await _old_chunks(storage, chunk)

        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "archive_namespace_template": "{first_tag}"},
            namespace=None,
            dry_run=False,
        )
        assert result.error is None and result.affected_count == 0
        assert "refused" not in result.outcome

    @pytest.mark.parametrize("with_movable_chunk", [True, False])
    async def test_dry_run_counts_only_movable_chunks(self, storage, with_movable_chunk):
        refused = make_chunk("runtime-tagged", tags=("agent-runtime",))
        chunks = [refused]
        if with_movable_chunk:
            chunks.append(make_chunk("work-tagged", tags=("work",)))
        await _old_chunks(storage, *chunks)

        result = await run_policy(
            storage,
            _policy(
                "arch",
                "auto_archive",
                {"max_age_days": 30, "archive_namespace_template": "{first_tag}"},
            ),
            dry_run=True,
        )

        assert result.affected_count == (1 if with_movable_chunk else 0)
        assert result.outcome["refused"] == [{"target": "agent-runtime", "count": 1}]
        assert result.error
        assert all(_namespace_of(storage, c) == "default" for c in chunks)
        assert await storage.maintenance_run_latest(limit=5) == []

    async def test_each_refused_target_is_logged_once_per_run(self, storage, caplog):
        tag_a, tag_b = "a" * 70, "b" * 70  # over the agent-id cap
        chunks = [
            make_chunk("first", tags=(tag_a,)),
            make_chunk("second", tags=(tag_a,)),
            make_chunk("third", tags=(tag_b,)),
        ]
        await _old_chunks(storage, *chunks)
        config = {"max_age_days": 30, "archive_namespace_template": "agent-runtime:{first_tag}"}

        with caplog.at_level("WARNING", logger="memtomem.tools.policy_engine"):
            result = await execute_auto_archive(storage, config, namespace=None, dry_run=False)

        assert result.outcome["refused"] == [
            {"target": f"agent-runtime:{tag_a}", "count": 2},
            {"target": f"agent-runtime:{tag_b}", "count": 1},
        ]
        warnings = [
            r.getMessage() for r in caplog.records if "auto_archive skipped" in r.getMessage()
        ]
        assert len(warnings) == 2
        assert sum(tag_a in w for w in warnings) == 1
        assert sum(tag_b in w for w in warnings) == 1

    async def test_run_all_enabled_continues_and_budgets_only_moved_chunks(self, storage):
        refused = make_chunk("runtime-tagged", tags=("agent-runtime",))
        ok = make_chunk("work-tagged", tags=("work",))
        await _old_chunks(storage, refused, ok)
        await storage.policy_add(
            "arch-template",
            "auto_archive",
            {"max_age_days": 30, "archive_namespace_template": "{first_tag}"},
        )
        await storage.policy_add("promote-bad", "auto_promote", {"target_namespace": "a,b"})
        # Archive runs first, so counting its refused chunk toward the budget
        # (2 of max_actions=2) would stop the batch before the promote policy.
        db = storage._get_db()
        db.execute(
            "UPDATE memory_policies SET created_at = CASE name "
            "WHEN 'arch-template' THEN '2026-01-01T00:00:00' ELSE '2026-01-02T00:00:00' END"
        )
        db.commit()

        results = await run_all_enabled(storage, dry_run=False, max_actions=2)

        by_name = {r.policy_name: r for r in results}
        assert set(by_name) == {"arch-template", "promote-bad"}  # neither stopped the batch
        assert by_name["arch-template"].affected_count == 1
        assert by_name["arch-template"].mutated is True
        assert by_name["promote-bad"].mutated is False
        assert all(r.error for r in results)
        statuses = {
            r["policy_name"]: r["status"] for r in await storage.maintenance_run_latest(limit=10)
        }
        assert statuses == {"arch-template": "error", "promote-bad": "error"}


class TestRunHistoryRendering:
    async def _list(self, components, summary: dict) -> str:
        from memtomem.server.tools.policy import mem_policy_list

        from tests.test_tools_logic import _fake_ctx

        storage = components.storage
        await storage.policy_add("arch", "auto_archive", {})
        run_id = await storage.maintenance_run_start("auto_archive", policy_name="arch")
        await storage.maintenance_run_finish(
            run_id, status="error", affected_count=1, summary=summary
        )
        return await mem_policy_list(runs=1, ctx=_fake_ctx(components))

    async def test_moved_and_refused(self, components):
        out = await self._list(
            components,
            {
                "moved": [{"target": "work", "ids": ["a"], "omitted": 0}],
                "refused": [{"target": "agent-runtime", "count": 2}],
            },
        )
        assert "moved → work: 1, refused → agent-runtime: 2" in out

    async def test_refused_only(self, components):
        out = await self._list(components, {"refused": [{"target": "a,b", "count": 3}]})
        assert "refused → a,b: 3" in out
        assert "moved →" not in out

    async def test_truncated_moved_ids_count_the_omission(self, components):
        out = await self._list(
            components,
            {
                "moved": [
                    {"target": "archive", "ids": ["a", "b"], "omitted": 5},
                    {"target": "cold", "ids": ["c"], "omitted": 0},
                ]
            },
        )
        assert "moved → archive: 7; cold: 1" in out

    async def test_an_applied_archive_run_renders(self, components):
        """The history read crashed on the list-shaped ``moved`` record before."""
        from memtomem.server.tools.policy import mem_policy_list

        from tests.test_tools_logic import _fake_ctx

        storage = components.storage
        chunk = make_chunk("stale")
        await _old_chunks(storage, chunk)
        await storage.policy_add("arch", "auto_archive", {"max_age_days": 30})
        policy = await storage.policy_get("arch")
        await run_policy(storage, policy, dry_run=False)

        out = await mem_policy_list(runs=1, ctx=_fake_ctx(components))
        assert "moved → archive: 1" in out
