"""ADR-0030 PR-C — the explicit Pull apply engine (prepare / commit).

The write half of the Preview/Pull model. These pin the §5 refusal enforced
IN the engine, the capture-once guarantee (the bytes judged are the bytes
written, closing the preview→confirm→apply window), the destination
precondition (``plan_stale``), the audited-once Gate A, and the skills
captured-tree staging with no leak.
"""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from memtomem import privacy
from memtomem.context import skills
from memtomem.context.pull_apply import PullApplyResult, PullPlan, commit_pull, prepare_pull
from memtomem.context.scope_resolver import canonical_artifact_dir

from .helpers import seed_multi_runtime, set_home

_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, str(h))
    return h


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / ".git").mkdir()
    return p


def _skill_body(name: str, marker: str) -> str:
    return f"---\nname: {name}\n---\n{marker}\n"


def _store_skill_dir(proj: Path, name: str, *, scope: str = "project_shared") -> Path:
    return canonical_artifact_dir("skills", scope, proj) / name  # type: ignore[arg-type]


def _store_skill_text(proj: Path, name: str) -> str:
    return (_store_skill_dir(proj, name) / "SKILL.md").read_text(encoding="utf-8")


def _seed_store_agent(proj: Path, name: str, body: str, *, scope: str = "project_shared") -> Path:
    """Dir-layout canonical agent (``<canonical>/<name>/agent.md``)."""
    d = canonical_artifact_dir("agents", scope, proj) / name  # type: ignore[arg-type]
    d.mkdir(parents=True, exist_ok=True)
    dst = d / "agent.md"
    dst.write_text(body, encoding="utf-8")
    return dst


# ── §5 source selection (in-engine) ──────────────────────────────────────────


def test_divergent_no_from_is_source_conflict(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "skills",
        "s",
        {"claude": _skill_body("s", "stale"), "codex": _skill_body("s", "fresh")},
    )
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "source_conflict"
    assert out.distinct_landing_count == 2
    assert {c.runtime for c in out.candidates} >= {"claude", "codex"}
    # Nothing written.
    assert not _store_skill_dir(proj, "s").exists()


def test_from_resolves_divergence(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "skills",
        "s",
        {"claude": _skill_body("s", "stale"), "codex": _skill_body("s", "fresh")},
    )
    plan = prepare_pull(
        "skills", "s", scope="project_shared", project_root=proj, source_runtime="codex"
    )
    assert isinstance(plan, PullPlan)
    assert plan.selected_runtime == "codex"
    res = commit_pull(plan)
    assert res.status == "applied"
    assert "fresh" in _store_skill_text(proj, "s")


def test_identical_candidates_auto_select_and_disclose(home: Path, proj: Path) -> None:
    body = _skill_body("dup", "same")
    seed_multi_runtime(proj, "skills", "dup", {"claude": body, "codex": body})
    plan = prepare_pull("skills", "dup", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    assert plan.selected_runtime == "claude"  # priority-first
    assert "codex" in plan.duplicate_runtimes


def test_single_importable_landing_error_is_source_conflict(home: Path, proj: Path) -> None:
    """R3 Major 3 order: a lone importable landing_error is ambiguous, NOT
    nothing_importable (fail-closed)."""
    # A gemini command with invalid TOML → landing_error (the sole candidate).
    seed_multi_runtime(proj, "commands", "c", {"gemini": "this is not = valid toml ["})
    out = prepare_pull("commands", "c", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "source_conflict"


def test_no_candidates_is_nothing_importable(home: Path, proj: Path) -> None:
    out = prepare_pull("skills", "absent", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "nothing_importable"


def test_from_absent_runtime_is_nothing_importable(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v")})
    out = prepare_pull(
        "skills", "s", scope="project_shared", project_root=proj, source_runtime="gemini"
    )
    assert isinstance(out, PullApplyResult)
    assert out.status == "nothing_importable"


def test_from_landing_error_is_selected_landing_error(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "commands",
        "c",
        {"claude": "# ok\nbody\n", "gemini": "not = valid ["},
    )
    out = prepare_pull(
        "commands", "c", scope="project_shared", project_root=proj, source_runtime="gemini"
    )
    assert isinstance(out, PullApplyResult)
    assert out.status == "selected_landing_error"


# ── capture-once (the crux) ───────────────────────────────────────────────────


def test_commit_writes_captured_not_mutated_runtime(home: Path, proj: Path) -> None:
    """Mutating the runtime source between prepare and commit does not change
    the written bytes — the plan carries the judged bytes."""
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "judged")})
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    # Mutate the runtime source AFTER prepare.
    written["claude"].write_text(_skill_body("s", "MUTATED"), encoding="utf-8")
    res = commit_pull(plan)
    assert res.status == "applied"
    assert "judged" in _store_skill_text(proj, "s")
    assert "MUTATED" not in _store_skill_text(proj, "s")


def test_dry_apply_parity_property(home: Path, proj: Path) -> None:
    """preview.ambiguous ⇔ prepare(no --from) refuses with source_conflict."""
    from memtomem.context.pull_preview import preview_pull

    seed_multi_runtime(
        proj,
        "skills",
        "s",
        {"claude": _skill_body("s", "a"), "codex": _skill_body("s", "b")},
    )
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    is_conflict = isinstance(out, PullApplyResult) and out.status == "source_conflict"
    assert pv.ambiguous == is_conflict


# ── identical no-op (R4 Major 1) ─────────────────────────────────────────────


def test_identical_is_applied_noop(home: Path, proj: Path) -> None:
    body = _skill_body("s", "same")
    seed_multi_runtime(proj, "skills", "s", {"claude": body})
    # Seed the Store with the identical content.
    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "applied"
    assert out.write_outcome == "identical"


# ── overwrite / snapshot (agents) ─────────────────────────────────────────────


def test_agents_overwrite_snapshots_and_writes(home: Path, proj: Path) -> None:
    _seed_store_agent(proj, "a", "STORE A\n")
    seed_multi_runtime(proj, "agents", "a", {"claude": "RUNTIME B\n"})
    plan = prepare_pull("agents", "a", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"
    assert res.write_outcome == "overwritten"
    canonical = canonical_artifact_dir("agents", "project_shared", proj) / "a" / "agent.md"
    assert canonical.read_text(encoding="utf-8") == "RUNTIME B\n"
    # Pre-image snapshot accrued.
    versions = canonical_artifact_dir("agents", "project_shared", proj) / "a" / "versions"
    assert versions.is_dir()
    assert any("STORE A" in p.read_text(encoding="utf-8") for p in versions.glob("*.md"))


def test_agents_store_present_without_overwrite_is_canonical_exists(home: Path, proj: Path) -> None:
    _seed_store_agent(proj, "a", "STORE A\n")
    seed_multi_runtime(proj, "agents", "a", {"claude": "RUNTIME B\n"})
    out = prepare_pull("agents", "a", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "canonical_exists"


def test_skills_store_present_is_overwrite_unsupported(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body("s", "old store"), encoding="utf-8")
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(out, PullApplyResult)
    assert out.status == "skills_overwrite_unsupported"
    # Store intact.
    assert "old store" in _store_skill_text(proj, "s")


# ── plan_stale (R3/R4 Major) ──────────────────────────────────────────────────


def test_plan_stale_skills_created_between_prepare_and_commit(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v")})
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    # Another writer lands the canonical between prepare and commit.
    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body("s", "surprise"), encoding="utf-8")
    res = commit_pull(plan)
    assert res.status == "plan_stale"
    assert "surprise" in _store_skill_text(proj, "s")  # untouched


def test_plan_stale_agents_store_changed(home: Path, proj: Path) -> None:
    _seed_store_agent(proj, "a", "STORE A\n")
    seed_multi_runtime(proj, "agents", "a", {"claude": "RUNTIME B\n"})
    plan = prepare_pull("agents", "a", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    # Store bytes change after prepare (digest precondition, not just existence).
    _seed_store_agent(proj, "a", "STORE C (changed)\n")
    res = commit_pull(plan)
    assert res.status == "plan_stale"
    canonical = canonical_artifact_dir("agents", "project_shared", proj) / "a" / "agent.md"
    assert "STORE C" in canonical.read_text(encoding="utf-8")  # untouched


# ── Gate A: blocked, force, audit-once ────────────────────────────────────────


def test_gate_blocked_project_shared_records_bypass_attempt(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A forced project_shared secret is a hard refusal that MUST still audit
    the bypass attempt with the project_shared code (Codex code-review Major)."""
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", f"tok {_SECRET}")})
    records: list[str] = []
    audits: list[dict] = []
    monkeypatch.setattr(privacy, "record", lambda outcome, tool: records.append(outcome))
    monkeypatch.setattr(privacy, "emit_bypass_audit", lambda **kw: audits.append(kw))
    out = prepare_pull(
        "skills", "s", scope="project_shared", project_root=proj, force_unsafe_import=True
    )
    assert isinstance(out, PullApplyResult)
    assert out.status == "gate_blocked"
    assert out.reason_code == "privacy_blocked_project_shared"
    assert out.force_bypassable is False
    assert records == ["blocked_project_shared"]  # audited exactly once, right outcome
    assert len(audits) == 1 and audits[0]["audit_context"]["blocked_scope"] == "project_shared"
    assert not _store_skill_dir(proj, "s").exists()


def test_gate_blocked_project_shared_not_bypassable(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", f"tok {_SECRET}")})
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "gate_blocked"
    assert out.reason_code == "privacy_blocked"  # unforced block → plain code
    assert out.force_bypassable is False
    assert not _store_skill_dir(proj, "s").exists()


def test_gate_bypassable_tier_needs_force(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj, "skills", "s", {"claude": _skill_body("s", f"tok {_SECRET}")}, scope="user"
    )
    blocked = prepare_pull("skills", "s", scope="user", project_root=proj)
    assert isinstance(blocked, PullApplyResult)
    assert blocked.status == "gate_blocked"
    assert blocked.force_bypassable is True
    # With force → a plan, then applied.
    plan = prepare_pull("skills", "s", scope="user", project_root=proj, force_unsafe_import=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"


def test_audit_records_once_per_pull(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean multi-file skill records ONE aggregate pass, at commit-on-success
    (not one per file, and never at prepare) — Codex code-review Major."""
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "clean")})
    (written["claude"].parent / "extra.txt").write_text("more clean\n", encoding="utf-8")
    records: list[str] = []
    monkeypatch.setattr(privacy, "record", lambda outcome, tool: records.append(outcome))
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    assert records == []  # prepare did not record a proceed
    commit_pull(plan)
    assert records == ["pass"]  # exactly one aggregate record, not one per file


def test_declined_pull_leaves_no_record(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prepare then NOT committing (user declined) records nothing (the batch
    'no pass on rejected' invariant)."""
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "clean")})
    records: list[str] = []
    monkeypatch.setattr(privacy, "record", lambda outcome, tool: records.append(outcome))
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    # Commit never called → no record.
    assert records == []


# ── write_failed + staging cleanup (R4 Major 4 / Minor 1) ─────────────────────


def test_write_failed_on_promote_error_no_staging_leak(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v")})

    def _boom(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOTSUP, "no atomic no-replace rename here")

    monkeypatch.setattr(skills, "_promote_staging", _boom)
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "write_failed"
    # No .staging-* / .old-* leftovers under the canonical parent.
    parent = _store_skill_dir(proj, "s").parent
    if parent.exists():
        leftovers = list(parent.glob(".staging-*")) + list(parent.glob(".old-*"))
        assert leftovers == []


def test_skills_new_pull_writes_tree_no_leftovers(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v")})
    # Add a nested file to the runtime skill so the tree is multi-file.
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v")})
    (written["claude"].parent / "extra.txt").write_text("nested\n", encoding="utf-8")
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"
    d = _store_skill_dir(proj, "s")
    assert (d / "SKILL.md").is_file()
    assert (d / "extra.txt").read_text(encoding="utf-8") == "nested\n"
    parent = d.parent
    assert list(parent.glob(".staging-*")) == []
