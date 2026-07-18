"""ADR-0030 PR-B — pull-preview engine (:mod:`memtomem.context.pull_preview`).

Read-only preview of what a Pull would land, per runtime candidate, on two
orthogonal axes (``content_status`` / ``gate_status``) plus the §5 ambiguity
signal. These tests pin the design decisions the Codex gate converged on:

* content_status compares the PAYLOAD surface vs the Store (overrides/versions
  excluded) while §5 landing grouping AND the gate scan use the FULL copier
  surface (so a secret under ``versions/`` is caught and a metadata-only
  divergence is never auto-selected);
* landing vs store error phases are distinct and participate in §5 differently;
* the preview never mutates privacy counters or emits an audit line.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from memtomem import privacy
from memtomem.context import pull_preview
from memtomem.context.pull_preview import preview_pull
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.web.schemas.context import ContextPullPreviewCandidate

from .helpers import seed_multi_runtime, set_home

# AWS example key — assembled at runtime so a literal fake token never trips
# GitHub push protection (feedback_github_push_protection_fake_tokens).
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


def _seed_store_skill(proj: Path, name: str, marker: str, *, scope: str = "project_shared") -> Path:
    """Write a canonical Store skill (``<canonical>/<name>/SKILL.md``)."""
    d = canonical_artifact_dir("skills", scope, proj) / name  # type: ignore[arg-type]
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body(name, marker), encoding="utf-8")
    return d


def _cand(preview: pull_preview.PullPreview, runtime: str) -> pull_preview.PullCandidate:
    return next(c for c in preview.candidates if c.runtime == runtime)


# ── founding bug: divergent runtime candidates force ambiguity ────────────


def test_skills_divergent_candidates_are_ambiguous(home: Path, proj: Path) -> None:
    """claude=stale, codex=fresh, empty Store → both ``new``, two distinct
    landing groups, ``ambiguous`` with no auto_source (the campaign's bug)."""
    seed_multi_runtime(
        proj,
        "skills",
        "shared",
        {"claude": _skill_body("shared", "stale v1"), "codex": _skill_body("shared", "fresh v2")},
    )
    pv = preview_pull("skills", "shared", scope="project_shared", project_root=proj)
    assert pv.store_present is False
    assert _cand(pv, "claude").content_status == "new"
    assert _cand(pv, "codex").content_status == "new"
    assert pv.distinct_landing_count == 2
    assert pv.ambiguous is True
    assert pv.auto_source is None
    # Distinct groups.
    assert _cand(pv, "claude").landing_group != _cand(pv, "codex").landing_group


def test_skills_identical_candidates_auto_select_priority_first(home: Path, proj: Path) -> None:
    """Byte-identical candidates → one group, unambiguous, auto_source is the
    priority-first runtime; duplicates still surface as their own rows."""
    body = _skill_body("dup", "same bytes")
    seed_multi_runtime(proj, "skills", "dup", {"claude": body, "codex": body})
    pv = preview_pull("skills", "dup", scope="project_shared", project_root=proj)
    assert pv.distinct_landing_count == 1
    assert pv.ambiguous is False
    assert pv.auto_source == "claude"  # priority-first of KNOWN_RUNTIMES
    assert _cand(pv, "claude").landing_group == _cand(pv, "codex").landing_group == 0
    assert {c.runtime for c in pv.candidates} == {"claude", "codex"}


# ── content_status vs the Store ──────────────────────────────────────────


def test_skills_identical_vs_store(home: Path, proj: Path) -> None:
    body = _skill_body("s", "v1")
    seed_multi_runtime(proj, "skills", "s", {"claude": body})
    _seed_store_skill(proj, "s", "v1")
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert pv.store_present is True
    assert _cand(pv, "claude").content_status == "identical"


def test_skills_differs_vs_store(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "runtime v2")})
    _seed_store_skill(proj, "s", "store v1")
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert _cand(pv, "claude").content_status == "differs"


def test_skills_store_metadata_does_not_count_as_content(home: Path, proj: Path) -> None:
    """A Store skill carrying overrides/ + versions/ is still ``identical`` to a
    runtime with matching payload — internal metadata is not skill content."""
    body = _skill_body("s", "same payload")
    seed_multi_runtime(proj, "skills", "s", {"claude": body})
    store = _seed_store_skill(proj, "s", "same payload")
    (store / "overrides").mkdir()
    (store / "overrides" / "gemini.md").write_text("baked", encoding="utf-8")
    (store / "versions").mkdir()
    (store / "versions" / "v1.md").write_text("old", encoding="utf-8")
    (store / "versions.json").write_text("{}", encoding="utf-8")
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert _cand(pv, "claude").content_status == "identical"


# ── gemini command: landing is the CONVERTED markdown ────────────────────


def test_gemini_command_landing_is_converted_md(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "commands",
        "greet",
        {"gemini": 'description = "g"\nprompt = "hello from gemini"\n'},
    )
    pv = preview_pull("commands", "greet", scope="project_shared", project_root=proj)
    c = _cand(pv, "gemini")
    assert c.content_status == "new"
    assert c.importable is True
    # The store landing is the converted MD; seed a canonical that equals it.
    from memtomem.context.commands import _gemini_toml_to_canonical

    toml = proj / ".gemini" / "commands" / "greet.toml"
    converted = _gemini_toml_to_canonical(toml)
    canon = canonical_artifact_dir("commands", "project_shared", proj) / "greet"
    canon.mkdir(parents=True)
    (canon / "command.md").write_text(converted, encoding="utf-8")
    pv2 = preview_pull("commands", "greet", scope="project_shared", project_root=proj)
    assert _cand(pv2, "gemini").content_status == "identical"


def test_gemini_command_toml_parse_error_is_landing_error(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "commands", "bad", {"gemini": "this is = = not toml ==="})
    pv = preview_pull("commands", "bad", scope="project_shared", project_root=proj)
    c = _cand(pv, "gemini")
    assert c.content_status == "landing_error"
    assert c.gate_status is None
    assert c.landing_group is None
    assert pv.ambiguous is True  # fail-closed


# ── export-only runtimes are display-only not_importable rows ─────────────


def test_agents_export_only_runtimes_are_not_importable(home: Path, proj: Path) -> None:
    """Codex (.toml) and Kimi (.yaml) agents present on disk show as
    not_importable and never join the distinct-landing count."""
    seed_multi_runtime(
        proj,
        "agents",
        "bot",
        {
            "claude": "---\nname: bot\n---\nclaude body\n",
            "codex": 'name = "bot"\n',
            "kimi": "name: bot\n",
        },
    )
    pv = preview_pull("agents", "bot", scope="project_shared", project_root=proj)
    assert _cand(pv, "codex").content_status == "not_importable"
    assert _cand(pv, "codex").gate_status is None
    assert _cand(pv, "codex").importable is False
    assert _cand(pv, "kimi").content_status == "not_importable"
    # Only the importable claude row counts.
    assert pv.distinct_landing_count == 1
    assert _cand(pv, "codex").landing_group is None


# ── gate_status (side-effect-free) ───────────────────────────────────────


def test_gate_requires_unsafe_at_user_scope(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj, "skills", "s", {"claude": _skill_body("s", f"token {_SECRET}")}, scope="user"
    )
    pv = preview_pull("skills", "s", scope="user", project_root=proj)
    assert _cand(pv, "claude").gate_status == "requires_unsafe_confirmation"


def test_gate_blocked_at_project_shared(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", f"token {_SECRET}")})
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert _cand(pv, "claude").gate_status == "blocked"
    # A blocked candidate still participates in landing grouping.
    assert _cand(pv, "claude").landing_group == 0


def test_preview_does_not_mutate_privacy_counters(
    home: Path, proj: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview scans through record_outcome=False — no counter record, no
    bypass audit line (it is a read, not an ingress)."""
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", f"token {_SECRET}")})
    calls: list[str] = []
    monkeypatch.setattr(privacy, "record", lambda *a, **k: calls.append("record"))
    monkeypatch.setattr(privacy, "emit_bypass_audit", lambda *a, **k: calls.append("audit"))
    preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert calls == []


# ── Blocker regression: gate scans the FULL copier surface ────────────────


def test_gate_scans_secret_under_versions_dir(home: Path, proj: Path) -> None:
    """A secret hiding in a runtime skill's top-level ``versions/`` is EXCLUDED
    from the payload compare but STILL scanned — because a Pull would copy it.
    Proves the gate uses the copier surface, not the payload surface."""
    written = seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "clean")})
    skill_dir = written["claude"].parent
    versions = skill_dir / "versions"
    versions.mkdir()
    (versions / "v1.md").write_text(f"leaked {_SECRET}\n", encoding="utf-8")
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    # Payload is clean, but the gate saw the secret under versions/.
    assert _cand(pv, "claude").gate_status == "blocked"


def test_landing_group_splits_on_metadata_only_divergence(home: Path, proj: Path) -> None:
    """Two candidates with identical payload but different top-level
    ``versions.json`` land different trees → two groups → ambiguous. Proves §5
    grouping is the full copier surface, not the payload."""
    body = _skill_body("s", "same payload")
    written = seed_multi_runtime(proj, "skills", "s", {"claude": body, "codex": body})
    (written["claude"].parent / "versions.json").write_text('{"a":1}', encoding="utf-8")
    (written["codex"].parent / "versions.json").write_text('{"b":2}', encoding="utf-8")
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert pv.distinct_landing_count == 2
    assert pv.ambiguous is True


# ── error phases + fail-closed presence ──────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 does not deny read on Windows")
def test_unreadable_candidate_is_landing_error_and_forces_ambiguity(home: Path, proj: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("root bypasses chmod permissions")
    written = seed_multi_runtime(
        proj,
        "skills",
        "s",
        {"claude": _skill_body("s", "readable"), "codex": _skill_body("s", "hidden")},
    )
    bad = written["codex"].parent  # <root>/.agents/skills/s
    os.chmod(bad, 0o000)
    try:
        pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    finally:
        os.chmod(bad, 0o755)
    codex = _cand(pv, "codex")
    assert codex.content_status == "landing_error"
    assert codex.landing_group is None
    assert pv.ambiguous is True  # fail-closed: unreadable copy might diverge


def test_absent_runtime_gets_no_row(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v1")})
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert {c.runtime for c in pv.candidates} == {"claude"}  # codex/gemini/kimi absent


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 does not deny read on Windows")
def test_store_error_keeps_landing_group(home: Path, proj: Path) -> None:
    """Store copy unreadable but the runtime landing computable → store_error,
    and the candidate STILL participates in distinct-landing (landing was
    computed; only the Store side failed)."""
    if os.getuid() == 0:
        pytest.skip("root bypasses chmod permissions")
    seed_multi_runtime(proj, "skills", "s", {"claude": _skill_body("s", "v1")})
    store = _seed_store_skill(proj, "s", "v1")
    os.chmod(store, 0o000)
    try:
        pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    finally:
        os.chmod(store, 0o755)
    c = _cand(pv, "claude")
    assert c.content_status == "store_error"
    assert c.landing_group == 0  # landing computed → participates
    assert c.reason is not None  # store_error carries the read-error diagnostic


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 does not deny read on Windows")
def test_agent_store_permission_is_store_error_not_500(home: Path, proj: Path) -> None:
    """An unreadable agents canonical dir raises PermissionError inside the
    flat/dir resolver's is_file() probe — the engine must fail closed to a
    store_error row, never let the OSError escape (Codex code review Blocker)."""
    if os.getuid() == 0:
        pytest.skip("root bypasses chmod permissions")
    body = "---\nname: bot\n---\nbody\n"
    seed_multi_runtime(proj, "agents", "bot", {"claude": body})
    agents_root = canonical_artifact_dir("agents", "project_shared", proj)
    (agents_root / "bot").mkdir(parents=True)  # dir layout so resolver stats it
    (agents_root / "bot" / "agent.md").write_text(body, encoding="utf-8")
    os.chmod(agents_root / "bot", 0o000)
    try:
        pv = preview_pull("agents", "bot", scope="project_shared", project_root=proj)
    finally:
        os.chmod(agents_root / "bot", 0o755)
    assert _cand(pv, "claude").content_status == "store_error"


def test_payload_excludes_version_store_metadata(home: Path, proj: Path) -> None:
    """A Store skill with the version manifest, its lock, and a versions/ dir
    is still ``identical`` to a matching runtime payload — the payload iterator
    excludes version-store internal files (Codex code review — locks/temps)."""
    body = _skill_body("s", "payload")
    seed_multi_runtime(proj, "skills", "s", {"claude": body})
    store = _seed_store_skill(proj, "s", "payload")
    (store / "versions.json").write_text('{"versions": {}}', encoding="utf-8")
    (store / ".versions.json.lock").write_text("", encoding="utf-8")
    (store / ".versions.json.4321.tmp").write_text("crash leftover", encoding="utf-8")
    (store / "versions").mkdir()
    (store / "versions" / "v1.md").write_text("old snapshot", encoding="utf-8")
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert _cand(pv, "claude").content_status == "identical"


# ── §7 vendor-override warning (raw-vs-raw) ──────────────────────────────


def test_skill_override_warning_on_manifest_match(home: Path, proj: Path) -> None:
    """Runtime SKILL.md byte-equals overrides/<vendor>.md → warn (pulling would
    bake the override into the base canonical)."""
    body = _skill_body("s", "override body")
    seed_multi_runtime(proj, "skills", "s", {"claude": body})
    store = _seed_store_skill(proj, "s", "different store")
    ov = store / "overrides"
    ov.mkdir()
    (ov / "claude.md").write_text(body, encoding="utf-8")  # == runtime SKILL.md
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert _cand(pv, "claude").override_warning is True


def test_skill_override_ignores_aux_file_match(home: Path, proj: Path) -> None:
    """Only the top-level SKILL.md participates in the override compare; an aux
    file equal to the override must NOT warn."""
    body = _skill_body("s", "manifest")
    written = seed_multi_runtime(proj, "skills", "s", {"claude": body})
    (written["claude"].parent / "helper.md").write_text("aux payload", encoding="utf-8")
    store = _seed_store_skill(proj, "s", "manifest")
    ov = store / "overrides"
    ov.mkdir()
    (ov / "claude.md").write_text("aux payload", encoding="utf-8")  # matches aux, not SKILL.md
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    assert _cand(pv, "claude").override_warning is False


def test_gemini_command_override_warning_raw_toml(home: Path, proj: Path) -> None:
    toml = 'description = "g"\nprompt = "hi"\n'
    seed_multi_runtime(proj, "commands", "greet", {"gemini": toml})
    canon = canonical_artifact_dir("commands", "project_shared", proj) / "greet"
    ov = canon / "overrides"
    ov.mkdir(parents=True)
    (ov / "gemini.toml").write_text(toml, encoding="utf-8")  # raw == raw
    pv = preview_pull("commands", "greet", scope="project_shared", project_root=proj)
    assert _cand(pv, "gemini").override_warning is True


# ── flat/dir Store resolution (reuse the ADR-0008 resolver) ──────────────


def test_agent_store_prefers_valid_flat_over_malformed_dir(home: Path, proj: Path) -> None:
    """A valid flat ``<name>.md`` canonical wins over a malformed ``<name>/``
    dir (no agent.md) — we reuse resolve_artifact_under_root, not a naive
    ``root/<name>`` existence shortcut."""
    runtime_body = "---\nname: bot\n---\nbody\n"
    seed_multi_runtime(proj, "agents", "bot", {"claude": runtime_body})
    root = canonical_artifact_dir("agents", "project_shared", proj)
    root.mkdir(parents=True, exist_ok=True)
    (root / "bot.md").write_text(runtime_body, encoding="utf-8")  # valid flat
    (root / "bot").mkdir()  # malformed dir (no agent.md)
    pv = preview_pull("agents", "bot", scope="project_shared", project_root=proj)
    assert pv.store_present is True
    assert _cand(pv, "claude").content_status == "identical"


# ── determinism + schema/engine vocab parity ─────────────────────────────


def test_candidate_order_follows_priority(home: Path, proj: Path) -> None:
    body = _skill_body("s", "x")
    seed_multi_runtime(proj, "skills", "s", {"claude": body, "gemini": body, "codex": body})
    pv = preview_pull("skills", "s", scope="project_shared", project_root=proj)
    order = [c.runtime for c in pv.candidates]
    assert order == ["claude", "gemini", "codex"]  # KNOWN_RUNTIMES order, kimi absent


def test_schema_literals_match_engine_enums() -> None:
    """The wire schema's closed vocabularies must equal the engine Literals so
    the two can't drift (Codex Minor 2)."""
    import typing

    engine_content = set(typing.get_args(pull_preview.ContentStatus))
    engine_gate = set(typing.get_args(pull_preview.GateStatus))
    fields = ContextPullPreviewCandidate.model_fields
    schema_content = set(typing.get_args(fields["content_status"].annotation))
    # gate_status is Optional[Literal[...]] — unwrap the Union to the Literal.
    gate_ann = fields["gate_status"].annotation
    schema_gate = {a for a in _flatten_literal_args(gate_ann) if isinstance(a, str)}
    assert schema_content == engine_content
    assert schema_gate == engine_gate


def _flatten_literal_args(annotation: object) -> set[object]:
    import typing

    out: set[object] = set()
    for arg in typing.get_args(annotation):
        if arg is type(None):
            continue
        sub = typing.get_args(arg)
        if sub:
            out.update(sub)
        else:
            out.add(arg)
    return out
