"""ADR-0011 PR-E #891 — fail-quiet NO_FANOUT skip parity for runtime-table None.

Each ``generate_all_*`` and ``diff_*`` previously protected its
``target_file`` / ``target_dir`` contract with an explicit
``if x is None: raise RuntimeError(...)`` (PR #890 N1 commit). Issue
#891 inverts that contract: when E3 wires ``scope`` through these
callers, several ``(artifact, runtime, scope)`` tuples will
legitimately resolve to ``None`` per
``_runtime_targets.RUNTIME_FANOUT_TABLE`` — every ``project_local``
entry plus ``(commands, codex, project_*)``. The generators now
treat ``None`` as ``NO_FANOUT`` (skip-and-continue for
``generate_all_*``; silent zero-row for ``diff_*``). The table is
the contract source-of-truth; the generator-level raise was a
redundant guard.

Pin contract (per ``feedback_pin_invert_symmetric_assertion.md`` —
behavior flip = symmetric pin required):

* ``generate_all_*``:
  - **positive marker** — ``result.skipped`` contains a tuple whose
    slot 1 (artifact name) equals the canonical name AND slot 2
    contains the runtime label AND slot 3 equals
    ``skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME``;
  - **negative marker** — ``result.generated`` does NOT contain any
    tuple referencing the runtime that was forced to ``None``.
* ``diff_*``:
  - **silent-skip marker (both sides present)** — returned list has
    zero tuples whose first element equals ``runtime_key`` when
    canonical and runtime are both seeded.
  - **silent-skip marker (canonical only — Codex review fold)** —
    same zero-row result when the runtime side is ABSENT (the
    realistic NO_FANOUT shape). Without the upstream
    ``target_*(project_root, "__probe_891__")`` probe, the diff loop
    leaks ``"missing target"`` rows from canonical-only entries
    before the per-name ``None`` check ever runs.

Mutation validation per ``feedback_pin_test_mutation_validation.md``
(run before commit, not as a regression test):

* **Mutation 1 — skip-emit removal** (primary). Drop the
  ``skipped.append(...)`` line in each ``generate_all_*`` branch but
  keep ``continue``::

      if dst is None:
          continue   # ← skipped.append(...) deleted

  Production runs without crashing; ``result.skipped`` no longer
  contains the expected tuple → positive marker assertion fails for
  the right reason ("skip not emitted").

* **Mutation 2 — skip-code accuracy**. Replace
  ``skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME`` with
  ``skip_codes.UNKNOWN_RUNTIME`` in one ``generate_all_*`` site →
  positive code-typed marker fails.

Note: an earlier draft suggested ``continue → pass`` as a mutation.
That mutation does fail the test, but **by exception**
(``atomic_write_text(None, ...)`` /
``copy_skill(..., None)`` / ``target.read_text()`` on ``None`` →
``AttributeError``/``TypeError``), not by the positive marker
assertion firing. Mutation 1 above is the cleaner substitute.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context.agents import (
    AGENT_GENERATORS,
    diff_agents,
    generate_all_agents,
)
from memtomem.context.commands import (
    COMMAND_GENERATORS,
    diff_commands,
    generate_all_commands,
)
from memtomem.context.skills import (
    SKILL_GENERATORS,
    diff_skills,
    generate_all_skills,
)


def _seed_canonical_agent(
    project_root: Path, name: str = "foo", *, with_runtime: bool = True
) -> None:
    canonical = project_root / ".memtomem" / "agents"
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    # ``with_runtime=True`` covers the "target_* present + forced None"
    # branch; ``with_runtime=False`` covers the realistic NO_FANOUT shape
    # where the runtime side is absent (the upstream probe must short-
    # circuit before "missing target" fires).
    if with_runtime:
        runtime = project_root / ".claude" / "agents"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _seed_canonical_skill(
    project_root: Path, name: str = "foo", *, with_runtime: bool = True
) -> None:
    skill = project_root / ".memtomem" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    if with_runtime:
        runtime_skill = project_root / ".claude" / "skills" / name
        runtime_skill.mkdir(parents=True, exist_ok=True)
        (runtime_skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _seed_canonical_command(
    project_root: Path, name: str = "foo", *, with_runtime: bool = True
) -> None:
    canonical = project_root / ".memtomem" / "commands"
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    if with_runtime:
        runtime = project_root / ".claude" / "commands"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


@pytest.mark.parametrize(
    "kind, seed, runner, runtime_key, registry, attr",
    [
        (
            "agents",
            _seed_canonical_agent,
            generate_all_agents,
            "claude_agents",
            AGENT_GENERATORS,
            "target_file",
        ),
        (
            "skills",
            _seed_canonical_skill,
            generate_all_skills,
            "claude_skills",
            SKILL_GENERATORS,
            "target_dir",
        ),
        (
            "commands",
            _seed_canonical_command,
            generate_all_commands,
            "claude_commands",
            COMMAND_GENERATORS,
            "target_file",
        ),
    ],
)
def test_generate_all_runtime_table_none_emits_skip(
    kind: str,
    seed,
    runner,
    runtime_key: str,
    registry,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#891 — generate_all_<kind> emits NO_PROJECT_FANOUT_FOR_RUNTIME skip
    when the registered generator's ``target_*`` returns ``None``.

    Forces ``None`` by monkeypatching the registered generator's bound
    method, simulating the future scope wiring where the
    ``RUNTIME_FANOUT_TABLE`` legitimately returns ``NO_FANOUT`` for the
    active scope (e.g. ``project_local``).
    """
    seed(tmp_path, name="foo")
    gen = registry[runtime_key]
    monkeypatch.setattr(gen, attr, lambda *args, **kwargs: None)

    result = runner(tmp_path, runtimes=[runtime_key])

    # Positive marker: skip tuple present with correct slots.
    matching_skips = [
        skip
        for skip in result.skipped
        if skip[0] == "foo" and skip[2] == skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME
    ]
    assert len(matching_skips) == 1, (
        f"expected exactly one NO_PROJECT_FANOUT_FOR_RUNTIME skip for 'foo', got {result.skipped!r}"
    )
    # Slot 2 (human-readable reason) must mention the runtime label so CLI/web
    # tooltips have actionable context — see _skip_reasons.py:6-9 contract.
    assert runtime_key in matching_skips[0][1], (
        f"expected runtime label {runtime_key!r} in reason, got {matching_skips[0][1]!r}"
    )

    # Negative marker: nothing was generated for the forced-None runtime.
    generated_runtimes = {entry[0] for entry in result.generated}
    assert runtime_key not in generated_runtimes, (
        f"expected runtime {runtime_key!r} absent from generated, got {result.generated!r}"
    )


@pytest.mark.parametrize(
    "kind, seed, runner, runtime_key, registry, attr",
    [
        (
            "agents",
            _seed_canonical_agent,
            diff_agents,
            "claude_agents",
            AGENT_GENERATORS,
            "target_file",
        ),
        (
            "skills",
            _seed_canonical_skill,
            diff_skills,
            "claude_skills",
            SKILL_GENERATORS,
            "target_dir",
        ),
        (
            "commands",
            _seed_canonical_command,
            diff_commands,
            "claude_commands",
            COMMAND_GENERATORS,
            "target_file",
        ),
    ],
)
def test_diff_runtime_table_none_emits_zero_rows(
    kind: str,
    seed,
    runner,
    runtime_key: str,
    registry,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#891 — diff_<kind> returns zero rows for the forced-None runtime.

    ``diff_*`` has no skip channel (its return type is ``list[tuple[str,
    str, str]]``), so a NO_FANOUT runtime+scope contributes zero rows.
    Other registered runtimes still appear in the result.
    """
    seed(tmp_path, name="foo")
    gen = registry[runtime_key]
    monkeypatch.setattr(gen, attr, lambda *args, **kwargs: None)

    rows = runner(tmp_path)

    # Silent-skip marker: zero rows referencing the forced-None runtime.
    matching_rows = [row for row in rows if row[0] == runtime_key]
    assert matching_rows == [], (
        f"expected zero rows for runtime {runtime_key!r}, got {matching_rows!r}"
    )


@pytest.mark.parametrize(
    "kind, seed, runner, runtime_key, registry, attr",
    [
        (
            "agents",
            _seed_canonical_agent,
            diff_agents,
            "claude_agents",
            AGENT_GENERATORS,
            "target_file",
        ),
        (
            "skills",
            _seed_canonical_skill,
            diff_skills,
            "claude_skills",
            SKILL_GENERATORS,
            "target_dir",
        ),
        (
            "commands",
            _seed_canonical_command,
            diff_commands,
            "claude_commands",
            COMMAND_GENERATORS,
            "target_file",
        ),
    ],
)
def test_diff_runtime_no_fanout_canonical_only_emits_zero_rows(
    kind: str,
    seed,
    runner,
    runtime_key: str,
    registry,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#891 (Codex review fold) — diff_<kind> emits zero rows when the
    runtime side is absent and ``target_*`` returns ``None``.

    This is the realistic NO_FANOUT shape: when ``RUNTIME_FANOUT_TABLE``
    returns ``None`` for a runtime+scope (e.g. ``project_local``), the
    runtime side typically does NOT exist on disk. Without the upstream
    probe, the diff loop reaches the ``"missing target"`` branch (canonical
    in, runtime not) BEFORE the new ``target_*`` None check, so canonical
    entries leak as ``("<runtime>", "<name>", "missing target")`` rows
    instead of being silently skipped.

    Codex review of the initial #891 diff caught this gap. The fix probes
    ``gen.target_*(project_root, "__probe_891__")`` at the top of each
    per-runtime loop and ``continue`` s when ``None``.
    """
    seed(tmp_path, name="foo", with_runtime=False)
    gen = registry[runtime_key]
    monkeypatch.setattr(gen, attr, lambda *args, **kwargs: None)

    rows = runner(tmp_path)

    matching_rows = [row for row in rows if row[0] == runtime_key]
    assert matching_rows == [], (
        f"expected zero rows for forced-None runtime {runtime_key!r} "
        f"with canonical-only seeding, got {matching_rows!r}"
    )
