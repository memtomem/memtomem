"""``mem_decision_record`` through ``mem_do``, against a real store (#3620).

The record's own validation is covered in ``test_decision_record.py``. This
file is about what the action adds around the ``mem_add`` core: the project
check, the guard over decoded strings, the scoping of its idempotency ledger,
and what it does with the core's outcomes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from memtomem import privacy
from memtomem.constants import InvalidNameError
from memtomem.decision_record import parse_body
from memtomem.indexing.engine import PrivacyRejection
from memtomem.memory_scope import decode_day_file_namespace
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.server.tools.memory_crud import _REPLAY_MARKER, mem_add
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.search import _resolve_project_context_root
from memtomem.tools import memory_writer
from memtomem.tools.memory_writer import SourceChangedError, entry_guard_text

from helpers import StubCtx

SURFACE = "mem_decision_record"
ID_1 = "6f0c2a4e-5b1d-4c57-9a3e-2d8f1b7c4e90"
ID_2 = "0b9d7c1a-3e42-4f6b-8a15-c7d2e9f04b63"


def _make_project(base: Path, name: str) -> tuple[Path, Path]:
    root = base / name
    tier = root / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    return root.resolve(), tier.resolve()


@pytest.fixture
def served(bm25_only_components, tmp_path, monkeypatch):
    """A server whose client works in registered project ``proj``."""
    comp, _mem_dir = bm25_only_components
    root, tier = _make_project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    outside = tmp_path / "process"
    outside.mkdir()
    monkeypatch.chdir(outside)
    app = AppContext.from_components(comp)
    app.client_cwd = root
    privacy.reset_for_tests()
    return app, StubCtx(app), root, tier


def _params(**overrides) -> dict:
    params = {
        "decision_id": ID_1,
        "project": "proj",
        "recorded_by": "claude-code",
        "question": "Which axis to build the first sample on",
        "options": ["patterns (Recommended)", "interests", "not yet"],
        "recommended": "patterns (Recommended)",
        "agent_reason": "Options and choice are kept together only in transcripts",
        "chosen": "not yet",
    }
    params.update(overrides)
    return params


async def _record(ctx, **overrides) -> str:
    return await mem_do(action="decision_record", params=_params(**overrides), ctx=ctx)


def _decision_headings(tier: Path) -> list[str]:
    return [
        line
        for path in sorted(tier.rglob("*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("## Decision ")
    ]


def _bodies(tier: Path) -> list[dict]:
    """Every decision record under ``tier``, parsed from the files."""
    records = []
    for path in sorted(tier.rglob("*.md")):
        lines = path.read_text(encoding="utf-8").split("\n")
        starts = [i for i, line in enumerate(lines) if line == "```json"]
        for start in starts:
            end = lines.index("```", start)
            records.append(parse_body("\n".join(lines[start : end + 1])))
    return records


def _ledger_rows(app, tool: str) -> list[tuple[str, str | None]]:
    db = app.storage._get_db()
    return db.execute(
        "SELECT key, result FROM idempotency_ledger WHERE tool = ?", (tool,)
    ).fetchall()


def _ledger_key(root: Path, decision_id: str) -> str:
    return f"{hashlib.sha256(os.fsencode(root)).hexdigest()[:16]}:{decision_id}"


def _nested_json_credential() -> str:
    """A quoted credential inside JSON text that was JSON-encoded twice more.

    Assembled at run time. The write guard resolves two levels of escaping
    and rendering the record adds a third, so only a scan of the decoded
    string sees the quotes the rule needs.
    """
    inner = json.dumps({"pass" + "word": "hunter2" * 3})
    return json.dumps(json.dumps(inner))


class TestRegistration:
    @pytest.mark.asyncio
    async def test_the_action_writes_a_record_that_parses_back(self, served):
        _app, ctx, _root, tier = served
        out = await _record(ctx, user_words="표본은 아직\n논의 계속", refs=["#3620"])
        assert out.startswith("Memory added to "), out
        assert f"- Decision id: {ID_1}" in out
        assert "- Namespace: shared:proj" in out
        assert "- Entry chunks: " in out

        assert _decision_headings(tier) == [f"## Decision {ID_1}"]
        (record,) = _bodies(tier)
        assert record["chosen"] == "not yet" and record["chosen_by"] == "user"
        assert record["recommended"] == "patterns (Recommended)"
        assert record["recommended_by"] == "claude-code"
        assert record["user_words"] == "표본은 아직\n논의 계속"
        assert record["project"] == "proj" and record["refs"] == ["#3620"]
        (path,) = sorted(tier.rglob("*.md"))
        tag_lines = [
            line for line in path.read_text(encoding="utf-8").splitlines() if "> tags:" in line
        ]
        assert tag_lines == [
            '> tags: ["decision-record", "from-claude-code", "decision-user-declined"]'
        ]

    def test_category_and_exposure(self):
        """Derived from what the pruning reads, so no mode has to be booted."""
        from memtomem.server import _CORE_TOOLS, _STANDARD_PACKS
        from memtomem.server.tool_registry import ACTIONS

        info = ACTIONS["decision_record"]
        assert info.category == "decisions"
        assert "decisions" not in _STANDARD_PACKS
        assert "mem_decision_record" not in _CORE_TOOLS

    def test_the_caller_cannot_supply_tags_a_namespace_or_a_valve(self):
        import inspect

        from memtomem.server.tool_registry import ACTIONS

        parameters = set(inspect.signature(ACTIONS["decision_record"].fn).parameters)
        assert not parameters & {"tags", "namespace", "scope", "file", "force_unsafe"}

    @pytest.mark.asyncio
    async def test_a_call_without_decision_id_is_refused_before_any_write(self, served):
        _app, ctx, _root, tier = served
        params = _params()
        del params["decision_id"]
        out = await mem_do(action="decision_record", params=params, ctx=ctx)
        assert out.startswith("Error: invalid parameter for action 'decision_record'"), out
        assert "decision_id" in out
        assert list(tier.rglob("*.md")) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("overrides", "member"),
        [
            ({"options": "ab"}, "options"),
            ({"chosen": 1}, "chosen"),
            ({"refs": {}}, "refs"),
            ({"user_words": "\ud800"}, "user_words"),
            ({"decided_at": "2026-11-26"}, "decided_at"),
        ],
    )
    async def test_a_value_mem_do_lets_through_is_refused_by_the_record(
        self, served, overrides, member
    ):
        _app, ctx, _root, tier = served
        out = await _record(ctx, **overrides)
        assert out.startswith(f"Error: {member} "), out
        assert out.endswith("nothing was written.")
        assert list(tier.rglob("*.md")) == []


class TestGuard:
    POSITIONS = {
        "chosen": lambda s: {"chosen": s},
        "user_words": lambda s: {"user_words": s},
        "agent_reason": lambda s: {"agent_reason": s},
        "options_item": lambda s: {"options": ["patterns (Recommended)", "not yet", s]},
        "refs_item": lambda s: {"refs": ["#1", s]},
        "recommended": lambda s: {"options": [s, "not yet"], "recommended": s},
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("position", sorted(POSITIONS))
    async def test_a_secret_only_the_decoded_string_shows_is_refused(self, served, position):
        from memtomem.decision_record import build_decision_record

        app, ctx, _root, tier = served
        secret = _nested_json_credential()
        overrides = self.POSITIONS[position](secret)

        # The witness has to be one the rendered entry hides, or this test
        # would pass without the decoded strings ever reaching the guard.
        rendered = build_decision_record(**_params(**overrides), max_body_chars=100_000)
        rendered_only = privacy.enforce_write_guard(
            entry_guard_text(rendered.body, rendered.title, rendered.tags),
            surface="probe",
            scope="project_local",
            record_outcome=False,
        )
        assert rendered_only.decision == "pass"

        out = await _record(ctx, **overrides)
        assert out.startswith("Error: the record matches "), out
        assert "nothing was written" in out
        assert "force_unsafe" not in out
        assert "hunter2" not in out
        assert list(tier.rglob("*.md")) == []
        assert privacy.snapshot()["by_tool"][SURFACE]["blocked"] == 1
        assert _ledger_rows(app, SURFACE) == []

    @pytest.mark.asyncio
    async def test_an_ordinary_secret_is_refused_in_the_actions_own_words(self, served):
        _app, ctx, _root, tier = served
        out = await _record(ctx, user_words="token: sk-" + "a" * 30)
        assert out.startswith("Error: the record matches "), out
        assert "force_unsafe" not in out
        assert list(tier.rglob("*.md")) == []
        assert privacy.snapshot()["by_tool"][SURFACE]["blocked"] == 1

    @pytest.mark.asyncio
    async def test_the_first_scan_reads_exactly_what_the_core_reads(self, served, monkeypatch):
        """Same function, same text: the core cannot block what this scan passed."""
        _app, ctx, _root, _tier = served
        seen: list[tuple[str, str]] = []
        real = privacy.enforce_write_guard

        def spy(content, *, surface, **kwargs):
            seen.append((surface, content))
            return real(content, surface=surface, **kwargs)

        monkeypatch.setattr(privacy, "enforce_write_guard", spy)
        out = await _record(ctx, user_words='said "yes"\nthen left')
        assert out.startswith("Memory added to "), out

        own = [content for surface, content in seen if surface == SURFACE]
        core = [content for surface, content in seen if surface == "mem_add"]
        assert len(own) == 2 and len(core) == 1
        assert own[0] == core[0]
        # The second scan holds the strings as they will be read back.
        assert 'said "yes"\nthen left' in own[1]
        assert 'said "yes"\nthen left' not in own[0]

    @pytest.mark.asyncio
    async def test_a_passing_record_is_counted_once_by_the_core(self, served):
        _app, ctx, _root, _tier = served
        assert (await _record(ctx)).startswith("Memory added to ")
        by_tool = privacy.snapshot()["by_tool"]
        assert by_tool["mem_add"]["pass"] == 1
        assert sum(by_tool.get(SURFACE, {}).values()) == 0


class TestProject:
    @pytest.mark.asyncio
    async def test_a_project_that_is_not_the_servers_is_refused(self, served):
        _app, ctx, _root, tier = served
        out = await _record(ctx, project="other-project")
        assert out.startswith("Error: project 'other-project' is not this server's project"), out
        assert "'proj'" in out and "nothing was written" in out
        assert list(tier.rglob("*.md")) == []

    @pytest.mark.asyncio
    async def test_no_project_context_names_both_remedies(self, served, tmp_path):
        app, ctx, _root, tier = served
        app.client_cwd = (tmp_path / "process").resolve()
        out = await _record(ctx)
        assert out.startswith("Error: this server has no project context"), out
        assert "mm mem init --scope project_local" in out
        assert 'mem_do(action="project_bind"' in out
        assert list(tier.rglob("*.md")) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("project", [["proj"], "pr\ud800j", "a*", "", "shared:proj"])
    async def test_a_malformed_project_gets_its_field_error_not_the_mismatch(self, served, project):
        _app, ctx, _root, tier = served
        out = await _record(ctx, project=project)
        assert out.startswith(("Error: project must be ", "Error: project holds ")), out
        out.encode("utf-8")
        assert list(tier.rglob("*.md")) == []

    @pytest.mark.asyncio
    async def test_a_linked_local_tier_is_refused(self, bm25_only_components, tmp_path):
        """A's ``memories.local`` is a link to B's registered tier.

        The core resolves the tier through the link and finds it registered,
        so without the action's check an A-labelled record lands in B.
        """
        comp, _mem_dir = bm25_only_components
        root_b, tier_b = _make_project(tmp_path, "project-b")
        root_a = (tmp_path / "project-a").resolve()
        shared_a = root_a / ".memtomem" / "memories"
        shared_a.mkdir(parents=True)
        try:
            (root_a / ".memtomem" / "memories.local").symlink_to(tier_b, target_is_directory=True)
        except OSError:
            pytest.skip("this platform cannot create a directory link here")
        comp.config.indexing.project_memory_dirs = [shared_a, tier_b]
        app = AppContext.from_components(comp)
        app.client_cwd = root_a
        assert _resolve_project_context_root(app) == root_a

        out = await _record(StubCtx(app), project="project-a")
        assert out.startswith("Error: this project's .memtomem/memories.local"), out
        assert "is a link; nothing was written" in out
        assert list(tier_b.rglob("*.md")) == []

    @pytest.mark.asyncio
    async def test_a_deeper_project_registered_mid_call_does_not_move_the_record(
        self, served, monkeypatch
    ):
        app, ctx, root, tier = served
        nested_root, nested_tier = _make_project(root, "packages")
        real_core = memory_crud._mem_add_core

        async def core_after_registration(**kwargs):
            # Between the action's check and the core's write: a deeper
            # project appears and the client is inside it.
            app.config.indexing.project_memory_dirs = [tier, nested_tier]
            app.client_cwd = nested_root
            assert _resolve_project_context_root(app) == nested_root
            return await real_core(**kwargs)

        monkeypatch.setattr(memory_crud, "_mem_add_core", core_after_registration)
        out = await _record(ctx)
        assert out.startswith("Memory added to "), out
        assert _decision_headings(nested_tier) == []
        assert [r["project"] for r in _bodies(tier)] == ["proj"]
        assert [p.parent for p in tier.rglob("*.md")] == [tier]

    @pytest.mark.asyncio
    async def test_the_built_namespace_goes_through_the_write_gate(self, served, monkeypatch):
        from memtomem import constants

        _app, ctx, _root, tier = served
        order: list[str] = []
        real_validate = constants.validate_writable_namespace
        real_core = memory_crud._mem_add_core

        def validate(value):
            order.append(f"validate:{value}")
            return real_validate(value)

        async def core(**kwargs):
            order.append(f"core:{kwargs['namespace']}")
            return await real_core(**kwargs)

        monkeypatch.setattr(constants, "validate_writable_namespace", validate)
        monkeypatch.setattr(memory_crud, "_mem_add_core", core)
        assert (await _record(ctx)).startswith("Memory added to ")
        assert order == ["validate:shared:proj", "core:shared:proj"]

        def refuse(value):
            raise InvalidNameError(f"invalid namespace {value!r}: refused by the test")

        monkeypatch.setattr(constants, "validate_writable_namespace", refuse)
        out = await _record(ctx, decision_id=ID_2)
        assert out.startswith("Error: "), out
        assert "refused by the test" in out
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("slug", "decodes"),
        [("a" * 53, True), ("a" * 54, False), ("a" * 53 + "-", True)],
    )
    async def test_a_long_slug_is_written_whether_or_not_its_file_name_decodes(
        self, bm25_only_components, tmp_path, slug, decodes
    ):
        """Examples of the ADR-0043 limit, not its definition."""
        comp, _mem_dir = bm25_only_components
        root, tier = _make_project(tmp_path, slug)
        comp.config.indexing.project_memory_dirs = [tier]
        assert comp.config.namespace.default_namespace != f"shared:{slug}"
        app = AppContext.from_components(comp)
        app.client_cwd = root

        out = await _record(StubCtx(app), project=slug)
        assert out.startswith("Memory added to "), out
        assert f"- Namespace: shared:{slug}" in out
        (path,) = sorted(tier.rglob("*.md"))
        declared = decode_day_file_namespace(path.name)
        assert declared == (f"shared:{slug}" if decodes else None)


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_the_ledger_row_is_the_actions_and_keyed_by_root(self, served):
        app, ctx, root, _tier = served
        assert (await _record(ctx)).startswith("Memory added to ")
        ((key, result),) = _ledger_rows(app, SURFACE)
        assert key == _ledger_key(root, ID_1)
        assert result is not None and result.startswith("Memory added to ")
        assert _ledger_rows(app, "mem_add") == []

    @pytest.mark.asyncio
    async def test_an_ordinary_mem_add_key_and_a_decision_key_do_not_replay_each_other(
        self, served
    ):
        _app, ctx, root, tier = served
        key = _ledger_key(root, ID_1)
        added = await mem_add(  # type: ignore[arg-type]
            content="an ordinary note", scope="project_local", idempotency_key=key, ctx=ctx
        )
        assert added.startswith("Memory added to "), added

        out = await _record(ctx)
        assert out.startswith("Memory added to ") and _REPLAY_MARKER not in out, out
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]

        key_2 = _ledger_key(root, ID_2)
        assert (await _record(ctx, decision_id=ID_2)).startswith("Memory added to ")
        again = await mem_add(  # type: ignore[arg-type]
            content="another ordinary note",
            scope="project_local",
            idempotency_key=key_2,
            ctx=ctx,
        )
        assert again.startswith("Memory added to ") and _REPLAY_MARKER not in again, again

    @pytest.mark.asyncio
    async def test_two_roots_with_one_directory_name_both_write(
        self, bm25_only_components, tmp_path
    ):
        comp, _mem_dir = bm25_only_components
        root_1, tier_1 = _make_project(tmp_path / "one", "proj")
        root_2, tier_2 = _make_project(tmp_path / "two", "proj")
        comp.config.indexing.project_memory_dirs = [tier_1, tier_2]
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        app.client_cwd = root_1
        assert (await _record(ctx)).startswith("Memory added to ")
        app.client_cwd = root_2
        second = await _record(ctx)
        assert second.startswith("Memory added to ") and _REPLAY_MARKER not in second, second
        assert _decision_headings(tier_1) == _decision_headings(tier_2) == [f"## Decision {ID_1}"]

    @pytest.mark.asyncio
    async def test_a_repeat_replays_and_writes_nothing(self, served):
        _app, ctx, _root, tier = served
        first = await _record(ctx)
        again = await _record(ctx)
        assert _REPLAY_MARKER in again
        assert again.startswith(first.split("\n\n")[0].split("\n- Decision id")[0])
        assert f"- Decision id: {ID_1}" in again
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]

    @pytest.mark.asyncio
    async def test_a_repeat_with_other_members_keeps_the_first_record_and_says_so(self, served):
        _app, ctx, _root, tier = served
        assert (await _record(ctx)).startswith("Memory added to ")
        changed = await _record(ctx, chosen="interests")
        assert _REPLAY_MARKER in changed
        assert "were not compared with it and were not written" in changed
        assert [r["chosen"] for r in _bodies(tier)] == ["not yet"]

    @pytest.mark.asyncio
    async def test_a_repeat_is_validated_and_guarded_before_it_can_replay(self, served):
        _app, ctx, _root, tier = served
        assert (await _record(ctx)).startswith("Memory added to ")

        oversized = await _record(ctx, user_words="w" * 100_000)
        assert oversized.startswith("Error: the record renders to "), oversized
        guarded = await _record(ctx, user_words=_nested_json_credential())
        assert guarded.startswith("Error: the record matches "), guarded
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", [SourceChangedError("gone"), RuntimeError("disk")])
    async def test_an_append_that_fails_leaves_the_id_free_to_retry(
        self, served, monkeypatch, failure
    ):
        """Both release branches: the claim is under this action's ledger name."""
        app, ctx, _root, tier = served
        real_append = memory_writer.append_entry

        def failing(*args, **kwargs):
            raise failure

        monkeypatch.setattr(memory_writer, "append_entry", failing)
        out = await _record(ctx)
        assert out.startswith("Error"), out
        assert _decision_headings(tier) == []
        assert _ledger_rows(app, SURFACE) == []

        monkeypatch.setattr(memory_writer, "append_entry", real_append)
        retry = await _record(ctx)
        assert retry.startswith("Memory added to ") and _REPLAY_MARKER not in retry, retry
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]

    @pytest.mark.asyncio
    async def test_two_concurrent_calls_with_one_id_write_one_record(self, served):
        _app, ctx, _root, tier = served
        results = await asyncio.gather(_record(ctx), _record(ctx))
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]
        assert sum(("in progress" in r or _REPLAY_MARKER in r) for r in results) == 1

    @pytest.mark.asyncio
    async def test_after_the_ledger_window_the_same_id_writes_again(self, served):
        """Replay is bounded: it is not uniqueness of records."""
        app, ctx, _root, tier = served
        assert (await _record(ctx)).startswith("Memory added to ")
        db = app.storage._get_db()
        db.execute(
            "UPDATE idempotency_ledger SET expires_at = '2000-01-01T00:00:00+00:00' WHERE tool = ?",
            (SURFACE,),
        )
        db.commit()
        again = await _record(ctx)
        assert again.startswith("Memory added to ") and _REPLAY_MARKER not in again, again
        assert _decision_headings(tier) == [f"## Decision {ID_1}"] * 2

    @pytest.mark.asyncio
    async def test_an_index_failure_after_the_append_keeps_the_claim(self, served, monkeypatch):
        app, ctx, _root, tier = served
        real_index = app.index_engine.index_file

        async def failing(*args, **kwargs):
            raise RuntimeError("index boom")

        monkeypatch.setattr(app.index_engine, "index_file", failing)
        out = await _record(ctx)
        assert out.startswith("Error"), out
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]
        ((_key, result),) = _ledger_rows(app, SURFACE)
        assert result is None

        monkeypatch.setattr(app.index_engine, "index_file", real_index)
        retry = await _record(ctx)
        assert "in progress" in retry, retry
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]


class TestCoreOutcomes:
    @pytest.mark.asyncio
    async def test_the_write_records_a_decision_record_provenance_event(self, served):
        from memtomem.server.tools._provenance import PROVENANCE_KIND
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        app, ctx, _root, _tier = served
        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        out = await _record(ctx)
        assert out.startswith("Memory added to "), out
        assert "- Namespace: shared:proj" in out

        events = await app.storage.get_session_events(session_id)
        contents = [e["content"] for e in events if e["content"].startswith(PROVENANCE_KIND)]
        assert len(contents) == 1
        assert contents[0].startswith(f"{PROVENANCE_KIND} decision_record chunks=")
        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_privacy_rejection_after_the_append_is_reported_as_appended(
        self, served, monkeypatch
    ):
        app, ctx, _root, tier = served

        async def rejecting(path, *args, **kwargs):
            raise PrivacyRejection(
                path=Path(path), hit_count=1, scope="project_local", decision="blocked"
            )

        monkeypatch.setattr(app.index_engine, "index_file", rejecting)
        out = await _record(ctx)
        expected = PrivacyRejection(
            path=next(tier.rglob("*.md")), hit_count=1, scope="project_local", decision="blocked"
        ).mutation_detail(SURFACE, outcome="appended", surface="mcp")
        assert out == expected
        assert SURFACE in out or "decision_record" in out
        assert _decision_headings(tier) == [f"## Decision {ID_1}"]

    @pytest.mark.asyncio
    async def test_a_core_refusal_is_returned_unchanged(self, served, monkeypatch):
        _app, ctx, _root, tier = served
        refusal = "Error: a refusal only the core could word; nothing was written."

        async def refusing(**kwargs):
            return (refusal, None)

        monkeypatch.setattr(memory_crud, "_mem_add_core", refusing)
        assert await _record(ctx) == refusal
        assert list(tier.rglob("*.md")) == []

    @pytest.mark.asyncio
    async def test_a_day_file_the_preflight_refuses_takes_no_entry_and_no_claim(self, served):
        app, ctx, _root, tier = served
        assert (await _record(ctx)).startswith("Memory added to ")
        (day_file,) = sorted(tier.rglob("*.md"))
        with day_file.open("a", encoding="utf-8") as handle:
            handle.write("\n## Pasted by hand\n\ntoken: sk-" + "a" * 30 + "\n")
        before = day_file.read_bytes()

        out = await _record(ctx, decision_id=ID_2)
        if len(sorted(tier.rglob("*.md"))) != 1:
            pytest.skip("the UTC day rolled over between the two writes")
        assert "Decision id" not in out, out
        assert "nothing was written" in out, out
        assert day_file.read_bytes() == before
        assert [key for key, _ in _ledger_rows(app, SURFACE)] == [_ledger_key(app.client_cwd, ID_1)]
