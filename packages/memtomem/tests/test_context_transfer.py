"""ADR-0023 — cross-project artifact transfer engine (move|copy).

Engine-level tests for :func:`memtomem.context.transfer.transfer_artifact`
(#1273, campaign #1270 item A-2). The same-root wrapper surface
(``migrate_scope`` byte-compat, CLI, MCP) is pinned by the existing
``test_context_migrate.py`` E4 matrix; this file covers what is NEW in
the engine:

- cross-project move/copy with the two-root fan-out split (discovery at
  the source root, override/render verification at the destination root);
- copy mode (source never consumed) incl. ``--as`` rename and its
  frontmatter ``name:`` rewrite;
- the reject matrix (same store, cross-project user→user, rename in
  move mode), Row-15 collision parity;
- Gate A on staged bytes incl. a secret inside ``versions/vN.md``
  (zero residue at the destination, source-anchored offending file);
- pair-lock ordering across two project roots, EXDEV fallback,
  destination-appeared-during-lock, cross-root partial-move error;
- ``needs_sync`` + exact follow-up sync command.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import shlex
import shutil
from pathlib import Path

import click
import pytest

from memtomem.context._atomic import (
    _file_lock,
    _lock_path_for,
    installed_at_from_dest,
    iter_installed_files,
)
from memtomem.context.dirty import is_asset_dirty
from memtomem.context.lockfile import Lockfile, digests_from_entry, utcnow_iso8601_z
from memtomem.context.migrate import MigratePartialError
from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.transfer import _carry_provenance, _ProvenancePlan, transfer_artifact

_MANIFEST_NAME = {"agents": "agent.md", "commands": "command.md", "skills": "SKILL.md"}
_AGENT_BODY_CLEAN = "---\nname: foo\ndescription: a clean test agent\n---\n\nhello world\n"
_SKILL_BODY_CLEAN = "---\nname: foo\ndescription: a clean test skill\n---\n\nhello\n"
_SECRET_LITERAL = "AKIA1234567890ABCDEF"  # AWS-key shape — caught by privacy.enforce_write_guard


@pytest.fixture
def two_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two project roots + isolated HOME for cross-project transfer tests.

    ``HOME`` / ``USERPROFILE`` both point into the sandbox so the user
    tier (``~/.memtomem``) and user-tier runtime fan-out (``~/.claude``)
    stay hermetic (``feedback_path_home_cross_platform``). Roots are
    pre-resolved so path assertions match the engine's ``.resolve()``d
    values on macOS (``/var`` → ``/private/var``).
    """
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    (proj_a / ".git").mkdir(parents=True)
    (proj_b / ".git").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return {"a": proj_a.resolve(), "b": proj_b.resolve(), "home": home.resolve()}


def _canonical_root(layout: dict[str, Path], kind: str, scope: str, root_key: str) -> Path:
    if scope == "user":
        return layout["home"] / ".memtomem" / kind
    if scope == "project_shared":
        return layout[root_key] / ".memtomem" / kind
    if scope == "project_local":
        return layout[root_key] / ".memtomem" / f"{kind}.local"
    raise ValueError(scope)


def _write_canonical(
    layout: dict[str, Path],
    kind: str,
    scope: str,
    root_key: str,
    name: str,
    body: str,
) -> Path:
    """Write a dir-layout canonical and return the manifest path."""
    artifact_dir = _canonical_root(layout, kind, scope, root_key) / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    manifest = artifact_dir / _MANIFEST_NAME[kind]
    manifest.write_text(body, encoding="utf-8")
    return manifest


def _write_versions(artifact_dir: Path, body: str) -> Path:
    """Seed a minimal ADR-0022 version store inside *artifact_dir*."""
    versions = artifact_dir / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    snapshot = versions / "v1.md"
    snapshot.write_text(body, encoding="utf-8")
    (artifact_dir / "versions.json").write_text(
        '{"versions": {"v1": {}}, "labels": {}}', encoding="utf-8"
    )
    return snapshot


# ── cross-project move ───────────────────────────────────────────────


def test_move_cross_project_shared_to_shared(two_projects):
    """Move A→B (both project_shared): bytes land at B, src + lock entry gone at A.

    ``versions/`` + ``versions.json`` live inside the artifact dir and
    must travel implicitly with the move.
    """
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    _write_versions(src_manifest.parent, _AGENT_BODY_CLEAN)
    Lockfile.at(two_projects["a"]).upsert_entry(
        "agents", "foo", wiki_commit="abc123", installed_at=utcnow_iso8601_z()
    )

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="move",
        apply_=True,
    )

    assert result.transferred is True
    assert result.mode == "move"
    assert result.dst_name == "foo"
    dst_dir = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"
    assert (dst_dir / "agent.md").read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert (dst_dir / "versions" / "v1.md").is_file()
    assert (dst_dir / "versions.json").is_file()
    assert not src_manifest.parent.exists()
    # Source project's lock.json entry dropped (#1123 B4-1, root-qualified).
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") is None
    # Exact follow-up sync command, cd-prefixed into the destination project.
    assert result.needs_sync is True
    expected = f"cd {shlex.quote(str(two_projects['b']))} && mm context sync --scope project_shared"
    assert result.sync_command == expected


def test_move_two_root_fanout_split_override_verifies_at_destination(two_projects):
    """ADR-0023 §4 regression — project_shared→project_shared across two roots.

    Stale fan-out discovery must walk the SOURCE root; override/render
    verification must resolve at the DESTINATION root (the override
    travels inside the artifact dir). The claude override bytes match the
    seeded source runtime file, so a correct two-root split removes it
    with NO ``.bak`` snapshot. A single-root implementation either misses
    the stale file entirely (discovery at dst) or fails override
    resolution and snapshots a spurious ``.bak`` (verification at src).
    """
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    override_bytes = "OVERRIDE CONTENT for claude — not the generator render\n"
    override = src_manifest.parent / "overrides" / "claude.md"
    override.parent.mkdir(parents=True)
    override.write_text(override_bytes, encoding="utf-8")
    stale_target = two_projects["a"] / ".claude" / "agents" / "foo.md"
    stale_target.parent.mkdir(parents=True)
    stale_target.write_text(override_bytes, encoding="utf-8")

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="move",
        apply_=True,
    )

    # POSITIVE: stale source-root fan-out removed, byte-verified via the
    # override now living at the destination root.
    assert not stale_target.exists()
    assert stale_target in result.fanout_cleaned
    # NEGATIVE: in-sync target ⇒ no divergence snapshot anywhere.
    assert result.fanout_backed_up == []
    assert list(stale_target.parent.glob("*.bak")) == []
    # Override travelled with the artifact dir.
    dst_override = (
        _canonical_root(two_projects, "agents", "project_shared", "b")
        / "foo"
        / "overrides"
        / "claude.md"
    )
    assert dst_override.read_text(encoding="utf-8") == override_bytes
    # Destination fan-out is NOT generated by the move.
    assert not (two_projects["b"] / ".claude" / "agents" / "foo.md").exists()


def test_move_dry_run_apply_parity_and_no_mutation(two_projects):
    """Dry-run previews the same fan-out selection apply removes, mutating nothing."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    stale_target = two_projects["a"] / ".claude" / "agents" / "foo.md"
    stale_target.parent.mkdir(parents=True)
    stale_target.write_text("hand-edited divergent body\n", encoding="utf-8")

    kwargs = dict(
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="move",
    )
    preview = transfer_artifact("agents", "foo", apply_=False, **kwargs)

    assert preview.transferred is False
    assert preview.fanout_planned == [stale_target]
    assert preview.needs_sync is True and preview.sync_command is not None
    # No mutation: src intact, dst absent, no staging residue.
    assert src_manifest.is_file()
    dst_root = _canonical_root(two_projects, "agents", "project_shared", "b")
    assert not (dst_root / "foo").exists()
    assert not list(dst_root.glob(".migrate-*")) if dst_root.exists() else True

    applied = transfer_artifact("agents", "foo", apply_=True, **kwargs)
    # Parity: apply acted on exactly the previewed targets (diverged ⇒
    # snapshotted first, then removed).
    assert applied.fanout_cleaned == preview.fanout_planned
    assert applied.fanout_backed_up == [stale_target.with_name("foo.md.bak")]


# ── copy mode ────────────────────────────────────────────────────────


def test_copy_keeps_source_lock_entry_and_fanout(two_projects):
    """Copy A→B: source bytes, lock.json entry, and runtime fan-out all untouched."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    Lockfile.at(two_projects["a"]).upsert_entry(
        "agents", "foo", wiki_commit="abc123", installed_at=utcnow_iso8601_z()
    )
    fanout = two_projects["a"] / ".claude" / "agents" / "foo.md"
    fanout.parent.mkdir(parents=True)
    fanout.write_text(_AGENT_BODY_CLEAN, encoding="utf-8")

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
    )

    assert result.transferred is True
    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "foo" / "agent.md"
    )
    assert dst_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    # Source side fully untouched.
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") is not None
    assert fanout.is_file()
    # Copy never plans/cleans fan-out. The destination gains no lock.json
    # entry here either: this legacy-shaped entry (abbreviated pin, no
    # digests) fails the A-4 carry gates — see the provenance section below
    # for the carried cases.
    assert result.fanout_cleaned == [] and result.fanout_backed_up == []
    assert result.provenance == "not_carried"
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None


def test_copy_as_rename_rewrites_frontmatter_name(two_projects):
    """``--as`` rewrites the staged manifest's ``name:`` so dst sync fans out as bar."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        new_name="bar",
    )

    assert result.dst_name == "bar"
    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "bar" / "agent.md"
    )
    text = dst_manifest.read_text(encoding="utf-8")
    assert text == _AGENT_BODY_CLEAN.replace("name: foo", "name: bar")
    assert result.notes == ()  # no overrides → no caveat
    # Source keeps its original name line.
    src_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "a") / "foo" / "agent.md"
    )
    assert "name: foo" in src_manifest.read_text(encoding="utf-8")


def test_copy_as_rename_without_name_key_is_noop(two_projects):
    """No frontmatter ``name:`` key → bytes copied verbatim (dir-name fallback)."""
    body = "---\ndescription: nameless agent\n---\n\nbody\n"
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", body)

    transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        new_name="bar",
    )

    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "bar" / "agent.md"
    )
    assert dst_manifest.read_text(encoding="utf-8") == body


def test_copy_as_rename_bom_prefixed_manifest(two_projects):
    """Codex review fold — a leading BOM must not defeat the rename rewrite.

    The canonical parsers strip one UTF-8 BOM before matching frontmatter
    (``agents._parse_canonical_agent_text``, #1229), so a BOM-prefixed
    copy that silently skipped the rewrite would promote under ``bar/``
    while still PARSING as ``foo`` — the exact destination collision the
    rewrite exists to close. The BOM itself is preserved verbatim.
    """
    from memtomem.context.agents import parse_canonical_agent

    _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", "\ufeff" + _AGENT_BODY_CLEAN
    )

    transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        new_name="bar",
    )

    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "bar" / "agent.md"
    )
    raw = dst_manifest.read_bytes().decode("utf-8")
    assert raw.startswith("\ufeff---")  # BOM preserved
    assert "name: bar" in raw and "name: foo" not in raw
    # The real parser — the collision vector — sees the new name.
    assert parse_canonical_agent(dst_manifest, layout="dir").name == "bar"


def test_copy_as_rename_crlf_manifest_preserves_endings(two_projects):
    """CRLF manifests rename too, with every line ending preserved verbatim.

    Same parser-tolerance family as the BOM case (#1229): the parser
    normalizes CRLF before matching, so detection must tolerate it — but
    the rewrite must not normalize the file as a side effect (read/write
    go through bytes, no universal-newline translation).
    """
    from memtomem.context.agents import parse_canonical_agent

    crlf_body = _AGENT_BODY_CLEAN.replace("\n", "\r\n")
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    # Seed via bytes, not write_text: text-mode writing translates "\n" to
    # os.linesep, which on Windows turns the intended "\r\n" into "\r\r\n"
    # (caught by CI — the engine handled even that, but the assertions pin
    # exact CRLF endings).
    src_manifest.write_bytes(crlf_body.encode("utf-8"))
    assert src_manifest.read_bytes().decode("utf-8") == crlf_body

    transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        new_name="bar",
    )

    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "bar" / "agent.md"
    )
    raw = dst_manifest.read_bytes().decode("utf-8")
    assert "name: bar\r\n" in raw and "name: foo" not in raw
    # Untouched lines keep their CRLF endings — no silent normalization.
    assert "description: a clean test agent\r\n" in raw
    assert "\n" not in raw.replace("\r\n", "")  # every newline is CRLF
    assert parse_canonical_agent(dst_manifest, layout="dir").name == "bar"


def test_copy_as_rename_multiple_name_keys_refuses(two_projects):
    """Degenerate frontmatter (two ``name:`` keys) refuses loudly, zero residue."""
    body = "---\nname: foo\nname: stale\ndescription: d\n---\n\nbody\n"
    src_manifest = _write_canonical(two_projects, "agents", "project_shared", "a", "foo", body)

    with pytest.raises(click.ClickException, match="2 'name:' lines"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="copy",
            apply_=True,
            new_name="bar",
        )

    assert src_manifest.read_text(encoding="utf-8") == body
    dst_root = _canonical_root(two_projects, "agents", "project_shared", "b")
    assert not (dst_root / "bar").exists()
    assert not list(dst_root.glob(".migrate-*"))


def test_copy_as_rename_with_overrides_emits_note(two_projects):
    """Overrides travel verbatim on rename; the result carries a review note."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    override = src_manifest.parent / "overrides" / "claude.md"
    override.parent.mkdir(parents=True)
    override_bytes = "---\nname: foo\n---\n\noverride body\n"
    override.write_text(override_bytes, encoding="utf-8")

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        new_name="bar",
    )

    assert len(result.notes) == 1 and "overrides" in result.notes[0]
    dst_override = (
        _canonical_root(two_projects, "agents", "project_shared", "b")
        / "bar"
        / "overrides"
        / "claude.md"
    )
    # NOT rewritten — verbatim-by-contract.
    assert dst_override.read_text(encoding="utf-8") == override_bytes


def test_copy_as_rename_dry_run_previews_overrides_note(two_projects):
    """The plan the user confirms must carry the same overrides caveat the
    apply prints — notes used to be computed on the apply path only, so the
    dry-run stayed silent about exactly the review work the user was about
    to sign up for (T2-6)."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    override = src_manifest.parent / "overrides" / "claude.md"
    override.parent.mkdir(parents=True)
    override.write_text("---\nname: foo\n---\n\noverride body\n", encoding="utf-8")

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=False,
        new_name="bar",
    )

    assert result.transferred is False
    assert len(result.notes) == 1 and "overrides" in result.notes[0]
    # Dry-run wrote nothing.
    assert not (_canonical_root(two_projects, "agents", "project_shared", "b") / "bar").exists()


def test_copy_as_rename_dry_run_without_overrides_has_no_note(two_projects):
    """Apply-parity in the other direction: no overrides → the preview must
    not invent a caveat."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=False,
        new_name="bar",
    )

    assert result.notes == ()


def test_partial_move_message_project_local_omits_noop_sync_hint():
    """A ``project_local`` destination has no runtime fan-out (ADR-0011 §3),
    so the partial-move remediation must not tell the user to run the
    NO_FANOUT no-op ``mm context sync --scope project_local`` — while the
    real warning (stale-source fan-out) stays intact. Covers both the
    same-root (migrate wording) and cross-root (transfer wording) branches."""
    from memtomem.context.transfer import _partial_move_message

    # Build the expected clause from the SAME Path object — the message embeds
    # str(src_path), which is backslash-joined on Windows, so a literal POSIX
    # string here fails on windows-latest only (the #1325/#838 trap class).
    src = Path("/src/agents/foo")
    for cross_root in (False, True):
        msg = _partial_move_message(
            "agents",
            "foo",
            src,
            Path("/dst/agents.local/foo"),
            "project_shared",
            "project_local",
            18,
            cross_root,
            None,
        )
        assert "mm context sync --scope project_local" not in msg, msg
        assert "then run" not in msg, msg
        assert f"Remove {src} manually." in msg
        assert "do NOT run `mm context sync --scope project_shared`" in msg

    # Fan-out-bearing destinations keep the follow-up instruction verbatim.
    kept = _partial_move_message(
        "agents",
        "foo",
        Path("/src/agents/foo"),
        Path("/dst/agents/foo"),
        "project_shared",
        "user",
        18,
        True,
        "mm context sync --scope user",
    )
    assert "then run `mm context sync --scope user`" in kept


def test_copy_flat_layout_cross_project_as_rename(two_projects):
    """Flat-layout canonical copies as a flat file; rename rewrites its frontmatter."""
    src_root = _canonical_root(two_projects, "agents", "project_shared", "a")
    src_root.mkdir(parents=True)
    flat = src_root / "foo.md"
    flat.write_text(_AGENT_BODY_CLEAN, encoding="utf-8")

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        new_name="bar",
    )

    assert result.layout == "flat"
    dst_flat = _canonical_root(two_projects, "agents", "project_shared", "b") / "bar.md"
    assert dst_flat.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN.replace(
        "name: foo", "name: bar"
    )
    assert flat.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN


# ── reject matrix + collision ────────────────────────────────────────


def test_rename_rejected_in_move_mode(two_projects):
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    with pytest.raises(click.ClickException, match="copy mode only"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="move",
            apply_=True,
            new_name="bar",
        )


def test_reject_same_root_same_scope_copy(two_projects):
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    with pytest.raises(click.ClickException, match="same canonical store"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["a"],
            to_scope="project_shared",
            mode="copy",
            apply_=True,
        )


def test_reject_same_root_same_scope_move_keeps_noop_literal(two_projects):
    """Same-root move keeps migrate_scope's historical no-op literal (byte-compat)."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    with pytest.raises(click.ClickException) as exc_info:
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["a"],
            to_scope="project_shared",
            mode="move",
            apply_=True,
        )
    assert exc_info.value.message == "agents/foo is already at scope='project_shared' (no-op)."


def test_reject_cross_project_user_to_user(two_projects):
    """User tier is global — cross-project user→user resolves to one store."""
    _write_canonical(two_projects, "agents", "user", "a", "foo", _AGENT_BODY_CLEAN)
    with pytest.raises(click.ClickException, match="user tier is global"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="user",
            dst_project_root=two_projects["b"],
            to_scope="user",
            mode="copy",
            apply_=True,
        )


def test_project_scope_requires_root(two_projects):
    _write_canonical(two_projects, "agents", "user", "a", "foo", _AGENT_BODY_CLEAN)
    with pytest.raises(click.ClickException, match="requires dst_project_root"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=None,
            from_scope="user",
            dst_project_root=None,
            to_scope="project_shared",
            mode="copy",
            apply_=True,
        )


def test_collision_at_destination_hard_fails(two_projects):
    """Row-15 parity: existing destination always refuses, no force valve."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    occupant = _write_canonical(
        two_projects, "agents", "project_shared", "b", "foo", "---\nname: foo\n---\n\nmine\n"
    )

    with pytest.raises(click.ClickException, match="destination already exists"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="copy",
            apply_=True,
        )

    # Occupant untouched, source untouched.
    assert "mine" in occupant.read_text(encoding="utf-8")
    assert src_manifest.is_file()


# ── Gate A on staged bytes ───────────────────────────────────────────


def test_gate_a_versions_secret_copy_blocks_zero_residue(two_projects):
    """A secret in a frozen ``versions/v1.md`` blocks a shared landing (copy).

    The error names the SOURCE-anchored offending file (the staging path
    is transient), and the destination keeps zero residue — no artifact
    dir, no staging leftovers.
    """
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    secret_body = f"---\nname: foo\n---\n\napi_key={_SECRET_LITERAL}\n"
    snapshot = _write_versions(src_manifest.parent, secret_body)

    with pytest.raises(PrivacyBlockedError) as exc_info:
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="copy",
            apply_=True,
        )

    message = str(exc_info.value)
    assert str(snapshot) in message, message  # source-anchored, not the staging path
    assert ".migrate-" not in message
    # Zero residue at the destination; source intact.
    dst_root = _canonical_root(two_projects, "agents", "project_shared", "b")
    assert not (dst_root / "foo").exists()
    assert not list(dst_root.glob(".migrate-*"))
    assert snapshot.read_text(encoding="utf-8") == secret_body


def test_gate_a_versions_secret_move_rolls_back(two_projects):
    """Move variant: Gate A block re-renames staging back to the source."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    secret_body = f"---\nname: foo\n---\n\napi_key={_SECRET_LITERAL}\n"
    snapshot = _write_versions(src_manifest.parent, secret_body)

    with pytest.raises(PrivacyBlockedError):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="move",
            apply_=True,
        )

    # Source restored in full (manifest + version snapshot).
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert snapshot.read_text(encoding="utf-8") == secret_body
    dst_root = _canonical_root(two_projects, "agents", "project_shared", "b")
    assert not (dst_root / "foo").exists()
    assert not list(dst_root.glob(".migrate-*"))


def test_gate_a_not_run_for_project_local_destination(two_projects):
    """Secret-bearing artifact may land in project_local (no scan, no fan-out)."""
    src_manifest = _write_canonical(
        two_projects,
        "agents",
        "project_shared",
        "a",
        "foo",
        f"---\nname: foo\n---\n\napi_key={_SECRET_LITERAL}\n",
    )

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_local",
        mode="copy",
        apply_=True,
    )

    assert result.transferred is True
    assert result.needs_sync is False and result.sync_command is None
    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_local", "b") / "foo" / "agent.md"
    )
    assert _SECRET_LITERAL in dst_manifest.read_text(encoding="utf-8")
    assert src_manifest.is_file()


# ── locking / races / EXDEV ──────────────────────────────────────────


def test_pair_lock_held_across_both_roots_sorted(two_projects, monkeypatch):
    """Both projects' sidecar locks are taken, in global sorted(str) order."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)

    acquired: list[Path] = []
    real = _file_lock

    def logging_lock(lock_path: Path, *, timeout: float | None = None):
        acquired.append(lock_path)
        return real(lock_path, timeout=timeout)

    # _acquire_pair_lock lives in (and reads) the migrate module namespace.
    monkeypatch.setattr("memtomem.context.migrate._file_lock", logging_lock)

    transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
    )

    src_lock = _lock_path_for(two_projects["a"] / ".memtomem" / "agents" / "foo")
    dst_lock = _lock_path_for(two_projects["b"] / ".memtomem" / "agents" / "foo")
    assert acquired == sorted([src_lock, dst_lock], key=str)


def test_destination_appeared_during_lock(two_projects, monkeypatch):
    """Racer creating dst between preview and lock acquire → refuse, no residue."""
    import memtomem.context.transfer as transfer_mod

    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    dst_dir = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"
    real_pair_lock = transfer_mod._acquire_pair_lock

    @contextlib.contextmanager
    def racing_pair_lock(path_a: Path, path_b: Path, *, timeout: float | None = None):
        with real_pair_lock(path_a, path_b, timeout=timeout):
            dst_dir.mkdir(parents=True)
            (dst_dir / "agent.md").write_text("---\nname: foo\n---\n\nracer\n", encoding="utf-8")
            yield

    monkeypatch.setattr(transfer_mod, "_acquire_pair_lock", racing_pair_lock)

    with pytest.raises(click.ClickException, match="appeared during lock acquire"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="move",
            apply_=True,
        )

    # Source untouched (the refusal fired before staging); racer's dst intact.
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert "racer" in (dst_dir / "agent.md").read_text(encoding="utf-8")
    assert not list(dst_dir.parent.glob(".migrate-*"))


def _racing_scan_creating_dst(transfer_mod, dst_dir: Path):
    """A ``scan_artifact_tree`` wrapper that creates *dst_dir* as a side effect.

    The scan runs between the in-lock ``dst_path.exists()`` check and
    ``_promote_move`` — so this reproduces the TOCTOU where an external writer
    (one not holding our sidecar lock) creates the destination in that window
    (#1385 finding 3). Delegates to the real scan so a clean staging tree still
    reports no privacy block.
    """
    real_scan = transfer_mod.scan_artifact_tree

    def racing_scan(staging, **kwargs):
        result = real_scan(staging, **kwargs)
        if not dst_dir.exists():
            dst_dir.mkdir(parents=True)
            (dst_dir / "agent.md").write_text("---\nname: foo\n---\n\nracer\n", encoding="utf-8")
        return result

    return racing_scan


@pytest.mark.parametrize("mode", ["copy", "move"])
def test_destination_appeared_during_promote(two_projects, monkeypatch, mode):
    """dst created in the window between the in-lock check and the promote →
    typed ``TransferCollisionError`` (web 409 / clean CLI ``ClickException``),
    NOT a bare ``FileExistsError`` that ``_classify_exception`` maps to a 500 /
    the CLI dumps as a traceback. #1385 finding 3 — pins BOTH promote call
    sites (copy at transfer.py:911, move at :950)."""
    import memtomem.context.transfer as transfer_mod
    from memtomem.context.transfer import TransferCollisionError

    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    dst_dir = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"
    monkeypatch.setattr(
        transfer_mod, "scan_artifact_tree", _racing_scan_creating_dst(transfer_mod, dst_dir)
    )

    with pytest.raises(TransferCollisionError, match="appeared during promote"):
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode=mode,
            apply_=True,
        )

    # Rollback ran (it lives in the outer ``except BaseException``): the racer's
    # dst is intact, the source bytes are recovered, and no staging residue.
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert "racer" in (dst_dir / "agent.md").read_text(encoding="utf-8")
    assert not list(dst_dir.parent.glob(".migrate-*"))


def test_exdev_fallback_cross_project_move(two_projects, monkeypatch):
    """EXDEV on the staging rename falls back to copy; move still completes."""
    import os as os_mod

    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    real_rename = os_mod.rename

    def exdev_rename(src, dst):
        if ".migrate-" in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real_rename(src, dst)

    # _stage_move reads ``os`` from the migrate module namespace; the
    # module object is shared, so patch the attribute it actually calls.
    monkeypatch.setattr("memtomem.context.migrate.os.rename", exdev_rename)

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="move",
        apply_=True,
    )

    assert result.transferred is True
    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "foo" / "agent.md"
    )
    assert dst_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    # EXDEV cleanup removed the stale source copy.
    assert not src_manifest.parent.exists()


def test_exdev_src_cleanup_failure_cross_root_partial_error(two_projects, monkeypatch):
    """Cross-root partial move raises with root-qualified remediation wording."""
    import os as os_mod

    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    src_dir = src_manifest.parent
    real_rename = os_mod.rename
    real_rmtree = shutil.rmtree

    def exdev_rename(src, dst):
        if ".migrate-" in str(dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link", str(src))
        return real_rename(src, dst)

    def failing_rmtree(path, *args, **kwargs):
        if Path(path) == src_dir:
            raise OSError(13, "Permission denied", str(path))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("memtomem.context.migrate.os.rename", exdev_rename)
    monkeypatch.setattr("memtomem.context.transfer.shutil.rmtree", failing_rmtree)

    with pytest.raises(MigratePartialError) as exc_info:
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="move",
            apply_=True,
        )

    message = exc_info.value.message
    assert message.startswith("Transfer agents/foo:")
    assert "in the source project" in message
    expected_cmd = (
        f"cd {shlex.quote(str(two_projects['b']))} && mm context sync --scope project_shared"
    )
    assert expected_cmd in message
    # Both canonicals on disk, as the error states.
    assert src_manifest.is_file()
    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "foo" / "agent.md"
    )
    assert dst_manifest.is_file()


# ── skills (dir tree with extra assets) ──────────────────────────────


def test_move_skill_cross_project_with_assets(two_projects):
    """Skill dir trees (manifest + extra assets) move whole across projects."""
    manifest = _write_canonical(
        two_projects, "skills", "project_shared", "a", "foo", _SKILL_BODY_CLEAN
    )
    helper = manifest.parent / "scripts" / "run.sh"
    helper.parent.mkdir(parents=True)
    helper.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

    result = transfer_artifact(
        "skills",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="move",
        apply_=True,
    )

    assert result.transferred is True
    dst_dir = _canonical_root(two_projects, "skills", "project_shared", "b") / "foo"
    assert (dst_dir / "SKILL.md").read_text(encoding="utf-8") == _SKILL_BODY_CLEAN
    assert (dst_dir / "scripts" / "run.sh").is_file()
    assert not manifest.parent.exists()


def test_copy_into_user_tier_sync_command(two_projects):
    """User-tier destination: sync command is project-independent (no cd prefix)."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)

    result = transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=None,
        to_scope="user",
        mode="copy",
        apply_=True,
    )

    assert result.needs_sync is True
    assert result.sync_command == "mm context sync --scope user"
    dst_manifest = _canonical_root(two_projects, "agents", "user", "a") / "foo" / "agent.md"
    assert dst_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN


# ── install-provenance carry-over (A-4 #1275) ────────────────────────

_FULL_PIN = "deadbeef" * 5  # 40-char lowercase hex — the ADR-0008 stored-pin shape
_OTHER_PIN = "cafebabe" * 5


def _wiki_install_entry(root: Path, kind: str, name: str, *, pin: str = _FULL_PIN) -> None:
    """Write a wiki-install-shaped lock.json entry over the on-disk tree.

    Mirrors what install/update write (#1247 id 15): per-file SHA-256
    digests hashed from the canonical bytes, ``files`` == digest keys,
    ``installed_at`` captured from the dest tree so the
    ``digests_installed_at`` pairing validates and ``is_asset_dirty``
    takes the digest branch.
    """
    dest = root / ".memtomem" / kind / name
    digests = {
        f.relative_to(dest).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in iter_installed_files(dest)
    }
    Lockfile.at(root).upsert_entry(
        kind,
        name,
        wiki_commit=pin,
        installed_at=installed_at_from_dest(dest),
        files=sorted(digests),
        files_commit=pin,
        digests=digests,
    )


def _shared_to_shared(mode: str, two_projects, *, apply_: bool = True, new_name: str | None = None):
    return transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode=mode,
        apply_=apply_,
        new_name=new_name,
    )


def test_provenance_carried_on_clean_move(two_projects):
    """Clean wiki install, shared→shared move: entry lands at B, leaves A.

    The destination entry must be status-serviceable: carried pin, valid
    digest pairing (``digests_from_entry`` non-None), and a clean
    ``is_asset_dirty`` classification — the inputs that make ``mm context
    status`` render ok/behind instead of untracked. ``versions/`` travels
    inside the artifact dir and is part of the digest surface.
    """
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    _write_versions(src_manifest.parent, _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "agents", "foo")
    src_entry = Lockfile.at(two_projects["a"]).read_entry("agents", "foo")
    assert src_entry is not None

    result = _shared_to_shared("move", two_projects)

    assert result.transferred is True
    assert result.provenance == "carried"
    assert result.provenance_reason is None and result.provenance_reason_code is None
    dst_entry = Lockfile.at(two_projects["b"]).read_entry("agents", "foo")
    assert dst_entry is not None
    assert dst_entry["wiki_commit"] == _FULL_PIN
    assert dst_entry["files"] == src_entry["files"]
    assert dst_entry["digests"] == src_entry["digests"]
    # Paired keys recomputed from the promoted tree, and the pairing
    # validates there (digest branch active at the destination).
    assert digests_from_entry(dst_entry) is not None
    assert is_asset_dirty(two_projects["b"], "agents", "foo").reason == "clean"
    # Source entry removed AFTER the destination carry (move semantics).
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") is None


def test_provenance_carried_on_clean_copy_keeps_source_entry(two_projects):
    """Clean wiki install, shared→shared copy: entries at BOTH ends, source verbatim."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "agents", "foo")
    src_entry_before = Lockfile.at(two_projects["a"]).read_entry("agents", "foo")

    result = _shared_to_shared("copy", two_projects)

    assert result.provenance == "carried"
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") == src_entry_before
    dst_entry = Lockfile.at(two_projects["b"]).read_entry("agents", "foo")
    assert dst_entry is not None and dst_entry["wiki_commit"] == _FULL_PIN
    assert is_asset_dirty(two_projects["b"], "agents", "foo").reason == "clean"


def test_provenance_dirty_source_move_lands_untracked(two_projects):
    """Dirty source: bytes still move, no destination entry, source entry dropped."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    _wiki_install_entry(two_projects["a"], "agents", "foo")
    src_manifest.write_text(_AGENT_BODY_CLEAN + "\nlocal edit\n", encoding="utf-8")

    result = _shared_to_shared("move", two_projects)

    assert result.transferred is True  # dirty blocks the carry, not the move
    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "source_dirty"
    assert "local edits" in result.provenance_reason
    dst_dir = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"
    assert (dst_dir / "agent.md").read_text(encoding="utf-8").endswith("local edit\n")
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None
    # Move still drops the (now-dangling) source entry — #1123 B4-1.
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") is None


def test_provenance_dirty_source_copy_keeps_source_entry(two_projects):
    """Dirty source on copy: no destination entry, source entry untouched."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    _wiki_install_entry(two_projects["a"], "agents", "foo")
    (src_manifest.parent / "extra.md").write_text("unrecorded addition\n", encoding="utf-8")

    result = _shared_to_shared("copy", two_projects)

    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "source_dirty"
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") is not None


def test_provenance_not_applicable_without_entry_and_other_tier_pairs(two_projects):
    """No lock.json entry → quiet not_applicable; non-shared→shared pairs likewise."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)

    result = _shared_to_shared("copy", two_projects)
    assert result.provenance == "not_applicable"
    assert result.provenance_reason is None and result.provenance_reason_code is None

    # shared→project_local with a CLEAN wiki entry: the lockfile only
    # tracks project_shared installs, so the pair stays not_applicable
    # (and the move-out entry drop keeps today's behavior).
    _write_canonical(two_projects, "commands", "project_shared", "a", "bar", _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "commands", "bar")
    result = transfer_artifact(
        "commands",
        "bar",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_local",
        mode="move",
        apply_=True,
    )
    assert result.provenance == "not_applicable"
    assert Lockfile.at(two_projects["a"]).read_entry("commands", "bar") is None


def test_provenance_legacy_entry_without_digests_not_carried(two_projects):
    """Pre-digest entry (mtime evidence only) must not carry — no minting digests."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    Lockfile.at(two_projects["a"]).upsert_entry(
        "agents",
        "foo",
        wiki_commit=_FULL_PIN,
        installed_at=installed_at_from_dest(src_manifest.parent),
    )

    result = _shared_to_shared("copy", two_projects)

    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "source_no_digests"
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None


def test_provenance_abbreviated_pin_not_carried(two_projects):
    """A non-40-hex wiki_commit violates the ADR-0008 stored-pin contract."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "agents", "foo", pin="abc123")

    result = _shared_to_shared("copy", two_projects)

    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "source_invalid_pin"
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None


def test_provenance_renamed_copy_not_carried(two_projects):
    """--as rename: entries are keyed by wiki asset name; carrying would mistarget update."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "agents", "foo")

    result = _shared_to_shared("copy", two_projects, new_name="bar")

    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "renamed_copy"
    dst_lock = Lockfile.at(two_projects["b"])
    assert dst_lock.read_entry("agents", "bar") is None
    assert dst_lock.read_entry("agents", "foo") is None


def test_provenance_corrupt_dest_lockfile_warns_but_move_commits(two_projects, caplog):
    """Corrupt destination lock.json: loud warning, no un-commit, garbage preserved."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "agents", "foo")
    dst_lock_path = two_projects["b"] / ".memtomem" / "lock.json"
    dst_lock_path.parent.mkdir(parents=True, exist_ok=True)
    dst_lock_path.write_bytes(b"{not json")

    with caplog.at_level("WARNING", logger="memtomem.context.transfer"):
        result = _shared_to_shared("move", two_projects)

    assert result.transferred is True
    dst_manifest = (
        _canonical_root(two_projects, "agents", "project_shared", "b") / "foo" / "agent.md"
    )
    assert dst_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "dest_lockfile_error"
    assert any("destination lock.json" in r.message for r in caplog.records)
    # The corrupt file is preserved for the user to fix — never reset
    # (#1247 id 16: a tolerant reset would be persisted).
    assert dst_lock_path.read_bytes() == b"{not json"


def test_provenance_corrupt_source_lockfile_not_carried(two_projects, caplog):
    """Corrupt SOURCE lock.json: transfer proceeds, provenance skip says why."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    src_lock_path = two_projects["a"] / ".memtomem" / "lock.json"
    src_lock_path.parent.mkdir(parents=True, exist_ok=True)
    src_lock_path.write_bytes(b"\x00garbage")

    with caplog.at_level("WARNING", logger="memtomem.context.transfer"):
        result = _shared_to_shared("move", two_projects)

    assert result.transferred is True
    assert result.provenance == "not_carried"
    assert result.provenance_reason_code == "source_lockfile_unreadable"
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None


def test_provenance_dry_run_previews_without_writing(two_projects):
    """Dry-run reports the carry plan and mutates neither lockfile."""
    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    _wiki_install_entry(two_projects["a"], "agents", "foo")

    preview = _shared_to_shared("move", two_projects, apply_=False)

    assert preview.transferred is False
    assert preview.provenance == "carried"  # planned; apply re-verifies
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None
    assert Lockfile.at(two_projects["a"]).read_entry("agents", "foo") is not None

    applied = _shared_to_shared("move", two_projects)
    assert applied.provenance == "carried"  # dry-run/apply parity on the clean path


def test_carry_provenance_digest_mismatch_refuses(two_projects):
    """The post-promote equality gate: foreign bytes at dst → no entry written.

    Unit-level pin of the TOCTOU close — a plan whose digest map does not
    match the on-disk destination tree (as if the bytes changed between
    classification and promote) must refuse rather than bless.
    """
    _write_canonical(two_projects, "agents", "project_shared", "b", "foo", _AGENT_BODY_CLEAN)
    dst_path = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"
    plan = _ProvenancePlan(carry=True, wiki_commit=_FULL_PIN, digests={"agent.md": "0" * 64})

    outcome = _carry_provenance("agents", "foo", dst_path, two_projects["b"], plan)

    assert outcome[0] == "not_carried"
    assert outcome[2] == "dest_bytes_unverified"
    assert Lockfile.at(two_projects["b"]).read_entry("agents", "foo") is None


# ── lock_timeout budget (A-5 #1276) ──────────────────────────────────


def test_lock_timeout_self_aborts_with_nothing_committed(two_projects):
    """A held destination sidecar lock + bounded budget → TimeoutError,
    zero filesystem change (the web route's orphan-thread guard, #1145
    shape). portalocker contends across separate fds in-process, so the
    held lock below blocks the engine exactly like a foreign process."""
    src_manifest = _write_canonical(
        two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN
    )
    dst_dir = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"
    dst_lock = _lock_path_for(dst_dir)

    with _file_lock(dst_lock):
        with pytest.raises(TimeoutError, match="held by another process"):
            transfer_artifact(
                "agents",
                "foo",
                src_project_root=two_projects["a"],
                from_scope="project_shared",
                dst_project_root=two_projects["b"],
                to_scope="project_shared",
                mode="move",
                apply_=True,
                lock_timeout=0.2,
            )

    # Nothing acquired → nothing staged or committed; source untouched.
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY_CLEAN
    assert not dst_dir.exists()
    assert not list(dst_dir.parent.glob(".migrate-*"))


def test_lock_timeout_budget_shared_across_pair(two_projects, monkeypatch):
    """The budget is a whole-call deadline: the second acquisition gets the
    remainder, not a fresh allowance (worst case N, not 2N)."""
    import contextlib as _ctx
    import time as _time

    import memtomem.context.migrate as migrate_mod

    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    seen: list[float | None] = []
    real = _file_lock

    @_ctx.contextmanager
    def slow_lock(lock_path: Path, *, timeout: float | None = None):
        seen.append(timeout)
        _time.sleep(0.1)  # consume budget while "acquiring"
        with real(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(migrate_mod, "_file_lock", slow_lock)

    transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
        lock_timeout=5.0,
    )

    assert len(seen) == 2
    assert all(t is not None for t in seen)
    assert seen[0] <= 5.0
    # The 0.1s spent inside the first acquisition must come off the
    # second's allowance.
    assert seen[1] <= seen[0] - 0.05


def test_lock_timeout_default_none_passes_unbounded(two_projects, monkeypatch):
    """Default ``lock_timeout=None`` keeps the historical blocking waits —
    every CLI/MCP call site is byte-for-byte unaffected."""
    import memtomem.context.migrate as migrate_mod

    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    seen: list[float | None] = []
    real = _file_lock

    def logging_lock(lock_path: Path, *, timeout: float | None = None):
        seen.append(timeout)
        return real(lock_path, timeout=timeout)

    monkeypatch.setattr(migrate_mod, "_file_lock", logging_lock)

    transfer_artifact(
        "agents",
        "foo",
        src_project_root=two_projects["a"],
        from_scope="project_shared",
        dst_project_root=two_projects["b"],
        to_scope="project_shared",
        mode="copy",
        apply_=True,
    )

    assert seen == [None, None]


# ── typed engine exceptions (A-5 #1276) ──────────────────────────────


def test_source_not_found_is_typed_with_pinned_literal(two_projects):
    """``ArtifactNotFoundError`` is a ClickException subclass with the
    byte-identical historical message — CLI/MCP/migrate consumers see no
    change; the web route maps the TYPE to 404. Full-equality pin, not a
    substring search (Codex review: an unanchored match would let most of
    the literal drift)."""
    from memtomem.context.migrate import ArtifactNotFoundError

    with pytest.raises(ArtifactNotFoundError) as excinfo:
        transfer_artifact(
            "agents",
            "ghost",
            src_project_root=two_projects["a"],
            from_scope=None,
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="move",
            apply_=False,
        )
    assert isinstance(excinfo.value, click.ClickException)
    assert excinfo.value.message == (
        "agents/ghost not found in any scope (user / project_shared / project_local)."
    )


def test_collision_is_typed_with_pinned_literal(two_projects):
    """``TransferCollisionError`` carries the historical Row-15 wording —
    pinned by full equality including the remediation sentences."""
    from memtomem.context.transfer import TransferCollisionError

    _write_canonical(two_projects, "agents", "project_shared", "a", "foo", _AGENT_BODY_CLEAN)
    _write_canonical(two_projects, "agents", "project_shared", "b", "foo", _AGENT_BODY_CLEAN)
    dst_path = _canonical_root(two_projects, "agents", "project_shared", "b") / "foo"

    with pytest.raises(TransferCollisionError) as excinfo:
        transfer_artifact(
            "agents",
            "foo",
            src_project_root=two_projects["a"],
            from_scope="project_shared",
            dst_project_root=two_projects["b"],
            to_scope="project_shared",
            mode="move",
            apply_=False,
        )
    assert isinstance(excinfo.value, click.ClickException)
    assert excinfo.value.message == (
        f"destination already exists: {dst_path}. "
        "Resolve manually or remove the existing entry first. "
        "--force does not overwrite scope-tier targets in PR-E4 "
        "(replace verb is a follow-up)."
    )
