"""ADR-0030 PR-C — the explicit Pull apply engine (prepare / commit).

The write half of the Preview/Pull model. These pin the §5 refusal enforced
IN the engine, the capture-once guarantee (the bytes judged are the bytes
written, closing the preview→confirm→apply window), the destination
precondition (``plan_stale``), the audited-once Gate A, and the skills
captured-tree staging with no leak.
"""

from __future__ import annotations

import errno
import shutil
import sys
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


def test_skills_store_present_without_overwrite_is_canonical_exists(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body("s", "old store"), encoding="utf-8")
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "canonical_exists"


def test_skills_overwrite_snapshots_the_preimage_and_swaps(home: Path, proj: Path) -> None:
    """ADR-0030 §10 / PR-G4b: a skills overwrite-Pull snapshots the current
    payload tree into ``versions/v1/`` and swaps the runtime copy in, preserving
    the Store-owned ``overrides/`` (which fan-out strips, so "absent in source"
    must NOT be read as "delete").
    """
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body("s", "old store"), encoding="utf-8")
    # A Store-owned override the runtime copy does not carry — must survive.
    (d / "overrides").mkdir()
    (d / "overrides" / "vendor.md").write_text("VENDOR EDIT\n", encoding="utf-8")

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"
    assert res.write_outcome == "overwritten"

    # New payload landed.
    assert "new" in _store_skill_text(proj, "s")
    # Pre-image snapshotted as v1 (tree layout), containing the OLD bytes.
    v1_skill = d / "versions" / "v1" / "SKILL.md"
    assert v1_skill.is_file()
    assert "old store" in v1_skill.read_text(encoding="utf-8")
    assert (d / "versions.json").is_file()
    # Store-owned override preserved byte-identical.
    assert (d / "overrides" / "vendor.md").read_text(encoding="utf-8") == "VENDOR EDIT\n"
    # No swap residue left behind.
    assert not any(p.name.startswith((".old-", ".staging-", ".swap-")) for p in d.parent.iterdir())


def test_skills_overwrite_strips_runtime_side_version_store(home: Path, proj: Path) -> None:
    """§6 ingress strip: a runtime skill that happens to carry ``versions/`` /
    ``versions.json`` must not seed the Store's own metadata namespace — the
    swapped-in payload is the NARROW surface, and the Store's own snapshot is
    the only ``versions/`` that survives.
    """
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    runtime_skill_dir = written["claude"].parent
    # Contrive a runtime-side version store (fan-out normally strips these).
    (runtime_skill_dir / "versions").mkdir()
    (runtime_skill_dir / "versions" / "vX.md").write_text("RUNTIME HISTORY\n", encoding="utf-8")
    (runtime_skill_dir / "versions.json").write_text("{}\n", encoding="utf-8")

    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body("s", "old store"), encoding="utf-8")

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"
    # The runtime's versions/vX.md never landed; only the Store's own v1 exists.
    assert not (d / "versions" / "vX.md").exists()
    assert (d / "versions" / "v1").is_dir()
    assert "RUNTIME HISTORY" not in (d / "versions.json").read_text(encoding="utf-8")


def _seed_overwrite_case(proj: Path, *, store_body: str = "old store") -> Path:
    """Seed a runtime skill + a store skill so an overwrite-Pull is prepared."""
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    d = _store_skill_dir(proj, "s")
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body("s", store_body), encoding="utf-8")
    return d


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink type gate")
def test_skills_overwrite_refuses_symlinked_versions_json(home: Path, proj: Path) -> None:
    """§2.3 type gate: a symlinked ``versions.json`` is refused (target_conflict)
    BEFORE any snapshot — it would otherwise block inside ``load_manifest`` with
    both locks held, or read the manifest through a link outside the root."""
    d = _seed_overwrite_case(proj)
    outside = proj / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (d / "versions.json").symlink_to(outside)

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "target_conflict"
    # No snapshot was taken and the store payload is untouched.
    assert not (d / "versions").exists()
    assert "old store" in _store_skill_text(proj, "s")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink type gate")
def test_skills_overwrite_refuses_nested_symlink_in_overrides(home: Path, proj: Path) -> None:
    """§2.3 strict preflight: a symlink nested inside a genuine ``overrides/``
    is refused rather than silently skipped (which would delete the override on
    the swap). Refused BEFORE the snapshot."""
    d = _seed_overwrite_case(proj)
    (d / "overrides").mkdir()
    outside = proj / "secret.md"
    outside.write_text("SECRET\n", encoding="utf-8")
    (d / "overrides" / "link.md").symlink_to(outside)

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "target_conflict"
    assert not (d / "versions").exists()
    assert "old store" in _store_skill_text(proj, "s")


@pytest.mark.skipif(sys.platform == "win32", reason="colon is illegal in a Windows filename")
def test_skills_overwrite_colon_payload_name_is_snapshot_failed(home: Path, proj: Path) -> None:
    """Step-5 translation: a Store payload file whose name contains ``:`` is
    legal on POSIX but rejected as non-portable by the write primitive — the
    resulting ``ValueError`` must fail closed as ``snapshot_failed``, never a
    raw traceback and never ``write_failed`` (nothing was written)."""
    d = _seed_overwrite_case(proj)
    (d / "wei:rd.md").write_text("colon payload\n", encoding="utf-8")

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "snapshot_failed"
    assert "old store" in _store_skill_text(proj, "s")  # untouched


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink type gate")
def test_skills_overwrite_refuses_symlinked_payload_file(home: Path, proj: Path) -> None:
    """Codex Blocker 1: a symlink in the PAYLOAD (not just overrides/versions)
    must be refused before the snapshot. ``iter_skill_payload_files`` silently
    drops symlinks, so without the whole-tree preflight the link would be absent
    from v1 AND deleted by the swap — a silent data loss reported as applied."""
    d = _seed_overwrite_case(proj)
    (d / "scripts").mkdir()
    outside = proj / "target.py"
    outside.write_text("print('x')\n", encoding="utf-8")
    (d / "scripts" / "helper.py").symlink_to(outside)  # nested payload symlink

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "target_conflict"
    # No snapshot, store payload (incl. the symlink) untouched.
    assert not (d / "versions").exists()
    assert (d / "scripts" / "helper.py").is_symlink()
    assert "old store" in _store_skill_text(proj, "s")


def test_skills_pull_non_portable_runtime_filename_is_landing_error(home: Path, proj: Path) -> None:
    """Codex Major 2: a runtime copy carrying a non-portable payload path (a
    ``:`` segment) is refused in PREPARE as a landing_error — never a raw
    ValueError out of commit after a snapshot. A sole importable landing_error is
    fail-closed ambiguous → source_conflict, and the candidate row carries the
    landing_error so the reason is visible."""
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    (written["claude"].parent / "wei:rd.md").write_text("x\n", encoding="utf-8")
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(out, PullApplyResult)
    assert out.status == "source_conflict"
    assert any(c.content_status == "landing_error" for c in out.candidates)


def test_skills_overwrite_non_portable_runtime_filename_takes_no_snapshot(
    home: Path, proj: Path
) -> None:
    """Major 2, overwrite arm: the same non-portable runtime path refuses in
    prepare BEFORE any snapshot — the Store version store is never created and
    the payload is untouched (parity with the new-pull arm; no raw ValueError)."""
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "new")})
    (written["claude"].parent / "wei:rd.md").write_text("x\n", encoding="utf-8")
    d = _seed_overwrite_case(proj)
    out = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(out, PullApplyResult)
    assert out.status == "source_conflict"
    assert not (d / "versions").exists()
    assert "old store" in _store_skill_text(proj, "s")


def test_skills_overwrite_survives_hardlink_fallback(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex Blocker 2 (integration): when os.link fails cross-device, the
    version-store carry (versions/ + versions.json) falls back to a DURABLE
    copy — the overwrite succeeds, the snapshot survives, and each fallback copy
    is fsynced (else a power loss after the swap deletes ``old`` loses history)."""
    from memtomem.context import _atomic as _atomic_mod

    d = _seed_overwrite_case(proj)

    def _no_link(*_a: object, **_k: object) -> None:
        raise OSError(errno.EXDEV, "cross-device")

    monkeypatch.setattr(_atomic_mod.os, "link", _no_link)
    fsynced: list[str] = []
    real_full = _atomic_mod._full_fsync_file

    def _spy(path: Path) -> None:
        fsynced.append(path.name)
        real_full(path)

    monkeypatch.setattr(_atomic_mod, "_full_fsync_file", _spy)
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"
    assert res.write_outcome == "overwritten"
    assert "new" in _store_skill_text(proj, "s")
    # The snapshot was carried into the swapped-in tree via the copy fallback.
    assert (d / "versions" / "v1" / "SKILL.md").read_text(encoding="utf-8").count("old store") == 1
    assert (d / "versions.json").is_file()
    # The fallback copies (versions.json + the v1 tree files) were durably fsynced.
    assert "versions.json" in fsynced


def test_skills_overwrite_dst_vanishes_mid_preflight_is_plan_stale(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex Major (round 3): if ``dst`` disappears after the initial lstat but
    during ``validate_tree_strict``, the FileNotFoundError must map to plan_stale
    (the Store changed), not snapshot_failed — pinned by injecting the race into
    the strict walk."""
    import memtomem.context.pull_apply as pa

    d = _seed_overwrite_case(proj)
    real_validate = pa.validate_tree_strict

    def _vanish(root: Path) -> None:
        # Simulate a non-gateway deletion racing the read-only preflight.
        shutil.rmtree(d)
        real_validate(root)  # now raises FileNotFoundError on the missing tree

    monkeypatch.setattr(pa, "validate_tree_strict", _vanish)
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "plan_stale"


def test_skills_overwrite_does_not_route_through_versioning_op_locked(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deadlock regression: ``_commit_skills`` already holds C0, so the snapshot
    must call ``create_tree_version`` DIRECTLY. Routing through
    ``versioning_op_locked`` would re-acquire the non-reentrant C0 and
    self-deadlock — pin it by making that helper explode and asserting the
    overwrite still succeeds."""
    from memtomem.context import _canonical_txn

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("versioning_op_locked must not be called under C0")

    monkeypatch.setattr(_canonical_txn, "versioning_op_locked", _boom)
    d = _seed_overwrite_case(proj)
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "applied"
    assert (d / "versions" / "v1").is_dir()


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


def test_selected_payload_scanned_once(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The apply path scans the selected payload ONCE (no _collect classify +
    _evaluate_gate double-scan) — code-review Major."""
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "a")})
    (written["claude"].parent / "extra.txt").write_text("b\n", encoding="utf-8")
    calls = {"n": 0}
    real = privacy.enforce_write_guard

    def _counting(*a: object, **k: object):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(privacy, "enforce_write_guard", _counting)
    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    # Two files, scanned once each in _evaluate_gate — NOT 4 (would be a
    # _collect classify pass + an _evaluate_gate pass).
    assert calls["n"] == 2


def test_commit_records_under_prepare_surface(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deferred pass/bypass counter records under the surface prepare used,
    not commit's default — code-review Major."""
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "clean")})
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(privacy, "record", lambda outcome, tool: recorded.append((outcome, tool)))
    plan = prepare_pull(
        "skills", "s", scope="project_shared", project_root=proj, surface="mcp_context_pull"
    )
    assert isinstance(plan, PullPlan)
    commit_pull(plan)
    assert recorded == [("pass", "mcp_context_pull")]


def test_flat_layout_overwrite_is_snapshot_requires_dir_layout(home: Path, proj: Path) -> None:
    """A flat-layout Store + --overwrite refuses with snapshot_requires_dir_layout
    (the actionable 'run mm context migrate'), not plan_stale."""
    # Flat canonical: <canonical>/<name>.md (no dir layout).
    base = canonical_artifact_dir("agents", "project_shared", proj)
    base.mkdir(parents=True, exist_ok=True)
    (base / "a.md").write_text("---\nname: a\n---\nFLAT STORE\n", encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": "---\nname: a\n---\nRUNTIME B\n"})
    plan = prepare_pull("agents", "a", scope="project_shared", project_root=proj, overwrite=True)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)
    assert res.status == "snapshot_requires_dir_layout"
    assert "FLAT STORE" in (base / "a.md").read_text(encoding="utf-8")  # untouched
    assert "RUNTIME B" not in (base / "a.md").read_text(encoding="utf-8")


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


def test_post_promote_reap_failure_still_applies_and_records(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GC failure after the install must not be reported as a failed write.

    The promote's post-commit reap runs inside ``_commit_skills``'s ``try``, so
    an ``OSError`` escaping it would return ``write_failed`` for a skill that IS
    installed and skip ``_record_gate_success`` — the privacy counter silently
    losing a write that happened. ``skills`` pins the swallow at the unit; this
    pins the surface the split-brain would actually appear on (code review).

    ``fired`` is what keeps the pin honest. Both outcome assertions hold
    vacuously if the collector never runs, so dropping ``reap_move_aside=True``
    from the call below — the regression this test exists for — would leave it
    green (verified by mutation, code review).
    """
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v")})
    records: list[str] = []
    monkeypatch.setattr(privacy, "record", lambda outcome, tool: records.append(outcome))

    orig_scan = skills._iter_own_internal_dirs
    fired: list[Path] = []

    def scan(dst: Path, **kwargs: object):  # type: ignore[no-untyped-def]
        # Only the post-promote collector asks for ("old",); the pre-write
        # prelude takes the default and must keep working, or this would pin a
        # failure before the commit rather than after it.
        if kwargs.get("kinds") == ("old",):
            fired.append(dst)
            raise OSError(errno.EIO, "Input/output error", str(dst))
        return orig_scan(dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(skills, "_iter_own_internal_dirs", scan)

    plan = prepare_pull("skills", "s", scope="project_shared", project_root=proj)
    assert isinstance(plan, PullPlan)
    res = commit_pull(plan)

    assert fired, "the post-promote collector never ran — the assertions below are vacuous"
    assert res.status == "applied"
    assert "v" in _store_skill_text(proj, "s")
    assert records == ["pass"], "the gate success went unrecorded for a write that happened"


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
