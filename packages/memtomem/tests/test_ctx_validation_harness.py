"""Guard the ADR-0026 §Validation first-run harness against silent rot.

These are pure-Python tests against the *real* diff engine — no browser. They
exist so the seeder in ``fixtures/ctx_validation_states.py`` keeps producing the
six distinct user-test affordances, and in particular so the Store-vs-runtime
direction (the bug class called out in the seeder docstring) can never silently
invert: "Not yet imported" must stay a runtime-only / ``missing canonical`` row,
not a ``missing target`` row.

There is deliberately no committed Playwright test here: how each diff status
renders as a tile verdict/button is already covered by
``tests/web/test_context_gateway_overview.py`` and
``tests/web/test_context_gateway_simple_mode.py``. The risk this module owns is
the *seed* (pure data), so it asserts the engine output the Overview is built
from. The live-UI check is the moderator's manual dry-run, documented in the
ADR-0026 §Validation runbook.
"""

from __future__ import annotations

from pathlib import Path

from fixtures.ctx_validation_states import seed_adr0026_validation_states

from memtomem.context.agents import canonical_agent_name, diff_agents, list_canonical_agents
from memtomem.context.commands import (
    canonical_command_name,
    diff_commands,
    list_canonical_commands,
)
from memtomem.context.mcp_servers import diff_mcp_servers, list_canonical_mcp_servers
from memtomem.context.skills import diff_skills, list_canonical_skills
from memtomem.context.status import summarize_diff_with_canonical

_SCOPE = "project_shared"


def _triples(rows) -> list[tuple[str, str, str]]:
    # DiffRow iterates as the historical 3-tuple; normalise for membership.
    return [tuple(r)[:3] for r in rows]


def test_seed_produces_six_distinct_diff_states(tmp_path: Path) -> None:
    manifest = seed_adr0026_validation_states(tmp_path)
    runtime = manifest["runtime"]

    # (a) out of sync + (f) in sync — both Store-side artifacts the runtime has.
    skills = _triples(diff_skills(tmp_path, scope=_SCOPE))
    assert (f"{runtime}_skills", "code-review", "out of sync") in skills
    assert (f"{runtime}_skills", "commit-helper", "in sync") in skills

    # (b) not yet imported — DIRECTION GUARD. A runtime-only artifact must read
    # as "missing canonical" (pull into the Store / Import), NEVER "missing
    # target" (which would mean the seed wrote the Store side by mistake).
    commands = _triples(diff_commands(tmp_path, scope=_SCOPE))
    assert (f"{runtime}_commands", "summarize", "missing canonical") in commands
    assert all(status != "missing target" for _rt, _name, status in commands), (
        "command seed inverted: a 'missing target' row means the runtime-only "
        "artifact was written to the Store, breaking the Import affordance"
    )

    # (c) empty type — agents seeded with nothing.
    assert _triples(diff_agents(tmp_path, scope=_SCOPE)) == []

    # (d) mcp orphan + (e) parse error — both independently visible. A broken
    # .mcp.json would collapse BOTH into parse_error, so assert orphan survives.
    mcp = _triples(diff_mcp_servers(tmp_path))
    assert ("project_mcp", "orphan-server", "missing canonical") in mcp
    assert ("project_mcp", "broken-server", "parse error") in mcp

    # The manifest the runbook quotes stays honest about what the engine emits.
    for state in manifest["states"].values():
        expected = state.get("diff_status")
        if expected is None:
            continue
        all_rows = skills + commands + mcp
        assert any(row[1] == state.get("name") and row[2] == expected for row in all_rows), (
            f"manifest claims {state} but the engine did not emit it"
        )


def test_seed_overview_summary_verdicts(tmp_path: Path) -> None:
    """The per-tile summaries (exactly what ``/context/overview`` builds) must
    roll up to the intended Simple-mode verdicts."""
    seed_adr0026_validation_states(tmp_path)

    skills = summarize_diff_with_canonical(
        diff_skills(tmp_path, scope=_SCOPE),
        {p.name for p in list_canonical_skills(tmp_path, scope=_SCOPE)},
    )
    # out_of_sync -> _ctxSimpleVerdict needs_sync (Sync). The seed populates
    # EVERY skill runtime, so there must be NO spurious "missing target" rows
    # (a Claude-only seed would emit one per skill per other runtime and drown
    # the intended out-of-sync signal — see the docstring of _skill_runtime_roots).
    assert skills.get("out_of_sync", 0) == 1
    assert skills.get("in_sync", 0) >= 1
    assert skills.get("missing_target", 0) == 0

    commands = summarize_diff_with_canonical(
        diff_commands(tmp_path, scope=_SCOPE),
        {
            canonical_command_name(p, layout)
            for p, layout in list_canonical_commands(tmp_path, scope=_SCOPE)
        },
    )
    # missing_canonical with no needs_sync signal -> not_saved (Import).
    assert commands.get("missing_canonical", 0) == 1
    assert commands.get("out_of_sync", 0) == 0
    assert commands.get("missing_target", 0) == 0

    agents = summarize_diff_with_canonical(
        diff_agents(tmp_path, scope=_SCOPE),
        {
            canonical_agent_name(p, layout)
            for p, layout in list_canonical_agents(tmp_path, scope=_SCOPE)
        },
    )
    assert agents.get("total", -1) == 0  # -> empty-state CTA

    mcp = summarize_diff_with_canonical(
        diff_mcp_servers(tmp_path),
        {p.stem for p in list_canonical_mcp_servers(tmp_path)},
    )
    # parse_error outranks missing_canonical in _ctxSimpleVerdict -> attention;
    # both rows remain individually visible in the default Advanced view.
    assert mcp.get("parse_error", 0) == 1
    assert mcp.get("missing_canonical", 0) == 1


def test_seed_is_idempotent(tmp_path: Path) -> None:
    first = seed_adr0026_validation_states(tmp_path)
    second = seed_adr0026_validation_states(tmp_path)
    assert first == second
    # Re-seeding does not multiply rows.
    assert len(_triples(diff_mcp_servers(tmp_path))) == 2
