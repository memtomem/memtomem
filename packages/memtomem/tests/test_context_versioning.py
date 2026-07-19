"""Tests for context/versioning.py — version snapshots + label pointers (ADR-0022).

Covers the pure-filesystem versioning module (create/promote/resolve, tag
validation, locking/no-overwrite) and its integration with the label-aware
``generate_all_agents`` sync path.
"""

from __future__ import annotations

import json
import threading

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import versioning as v
from memtomem.context.agents import CANONICAL_AGENT_ROOT, generate_all_agents
from memtomem.context.commands import CANONICAL_COMMAND_ROOT, generate_all_commands

# A dir-layout canonical agent whose rendered body carries a distinctive marker
# so we can tell which version's bytes reached the runtime.
_AGENT_TEMPLATE = """---
name: my-agent
description: {desc}
---

BODY MARKER: {marker}
"""


def _make_dir_agent(project_root, name="my-agent", *, marker="A", desc="v"):
    """Create a directory-layout canonical agent and return its artifact dir +
    working file."""
    artifact_dir = project_root / CANONICAL_AGENT_ROOT / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    working = artifact_dir / "agent.md"
    working.write_text(_AGENT_TEMPLATE.format(marker=marker, desc=desc), encoding="utf-8")
    return artifact_dir, working


def _make_flat_agent(project_root, name="flat-agent", *, marker="A"):
    """Create a flat-layout canonical agent (no per-artifact directory)."""
    root = project_root / CANONICAL_AGENT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.md"
    path.write_text(
        _AGENT_TEMPLATE.format(marker=marker, desc="flat").replace("my-agent", name),
        encoding="utf-8",
    )
    return path


_COMMAND_TEMPLATE = "---\ndescription: a command\n---\n\nDo $ARGUMENTS. MARKER: {marker}\n"


def _make_dir_command(project_root, name="my-cmd", *, marker="A"):
    """Create a directory-layout canonical command and return its dir +
    working file."""
    artifact_dir = project_root / CANONICAL_COMMAND_ROOT / name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    working = artifact_dir / "command.md"
    working.write_text(_COMMAND_TEMPLATE.format(marker=marker), encoding="utf-8")
    return artifact_dir, working


# ── Pure versioning module ───────────────────────────────────────────


class TestCreateVersion:
    def test_writes_immutable_file_with_working_bytes(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        rec = v.create_version(artifact_dir, working)
        assert rec.tag == "v1"
        vfile = artifact_dir / "versions" / "v1.md"
        assert vfile.is_file()
        assert vfile.read_bytes() == working.read_bytes()

    def test_increments_tag(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        assert v.create_version(artifact_dir, working).tag == "v1"
        assert v.create_version(artifact_dir, working).tag == "v2"
        assert v.create_version(artifact_dir, working).tag == "v3"
        manifest = v.load_manifest(artifact_dir)
        assert set(manifest.versions) == {"v1", "v2", "v3"}

    def test_snapshots_bytes_at_creation_time(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        v.create_version(artifact_dir, working)
        working.write_text("changed", encoding="utf-8")
        # v1 still holds the original bytes — immutable.
        assert b"BODY MARKER: A" in (artifact_dir / "versions" / "v1.md").read_bytes()

    def test_flat_layout_raises(self, tmp_path):
        # No per-artifact directory exists → versioning impossible.
        missing_dir = tmp_path / CANONICAL_AGENT_ROOT / "flat-agent"
        working = _make_flat_agent(tmp_path)  # creates agents/flat-agent.md, not a dir
        with pytest.raises(v.VersionsDirMissingError):
            v.create_version(missing_dir, working)

    def test_note_persists(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working, note="stable release")
        assert v.load_manifest(artifact_dir).versions["v1"].note == "stable release"

    def test_orphan_version_file_does_not_wedge(self, tmp_path):
        # Crash between the vN.md write and the manifest save leaves an orphan
        # file absent from the manifest. The next create must skip past it
        # (allocate v2), not recompute v1 and wedge forever (Codex review).
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)  # v1.md + manifest{v1}
        # Simulate the crash: manifest forgets v1, but v1.md stays on disk.
        (artifact_dir / "versions.json").write_text(
            json.dumps({"versions": {}, "labels": {}}), encoding="utf-8"
        )
        rec = v.create_version(artifact_dir, working)
        assert rec.tag == "v2"
        assert (artifact_dir / "versions" / "v2.md").is_file()
        assert (artifact_dir / "versions" / "v1.md").is_file()  # orphan preserved


class TestPromoteLabel:
    def test_updates_manifest_pointer(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        v.create_version(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v2")
        manifest = v.load_manifest(artifact_dir)
        assert manifest.labels["production"] == "v2"
        # Persisted to disk.
        raw = json.loads((artifact_dir / "versions.json").read_text())
        assert raw["labels"]["production"] == "v2"

    def test_move_pointer_is_rollback(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        v.create_version(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v2")
        v.promote_label(artifact_dir, "production", "v1")  # rollback
        assert v.load_manifest(artifact_dir).labels["production"] == "v1"

    def test_reserved_label_raises(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        with pytest.raises(v.ReservedLabelError):
            v.promote_label(artifact_dir, "latest", "v1")

    def test_unknown_version_raises(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        with pytest.raises(v.VersionNotFoundError):
            v.promote_label(artifact_dir, "production", "v9")

    def test_invalid_tag_raises(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        with pytest.raises(v.InvalidTagError):
            v.promote_label(artifact_dir, "production", "latest")

    @pytest.mark.parametrize("bad_label", ["v1", "v2", "v10"])
    def test_version_shaped_label_name_rejected(self, tmp_path, bad_label):
        # A label named like a version tag would be permanently shadowed by the
        # same-named version in the sync resolver — reject at write time.
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        with pytest.raises(v.InvalidLabelError):
            v.promote_label(artifact_dir, bad_label, "v1")
        # The shadowing label was never stored.
        assert bad_label not in v.load_manifest(artifact_dir).labels


class TestDeleteLabel:
    def test_removes_pointer(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v1")
        v.delete_label(artifact_dir, "production")
        assert "production" not in v.load_manifest(artifact_dir).labels

    def test_absent_is_noop(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        v.delete_label(artifact_dir, "nope")  # no raise

    def test_reserved_raises(self, tmp_path):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        with pytest.raises(v.ReservedLabelError):
            v.delete_label(artifact_dir, "latest")


class TestResolve:
    def test_resolve_label_returns_version_path(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        v.create_version(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v1")
        resolved = v.resolve_label(artifact_dir, "production")
        assert resolved == artifact_dir / "versions" / "v1.md"
        assert b"BODY MARKER: A" in resolved.read_bytes()

    def test_resolve_label_latest_is_reserved(self, tmp_path):
        # resolve_label deliberately does NOT handle "latest" — the caller
        # branches on it and reads the working file directly.
        artifact_dir, _ = _make_dir_agent(tmp_path)
        with pytest.raises(v.ReservedLabelError):
            v.resolve_label(artifact_dir, "latest")

    def test_resolve_unknown_label_raises(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        with pytest.raises(v.LabelNotFoundError):
            v.resolve_label(artifact_dir, "ghost")

    def test_resolve_dangling_label_raises(self, tmp_path):
        # Label points at a tag whose file was deleted out from under it.
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v1")
        (artifact_dir / "versions" / "v1.md").unlink()
        with pytest.raises(v.VersionNotFoundError):
            v.resolve_label(artifact_dir, "production")

    def test_resolve_version_direct(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        v.create_version(artifact_dir, working)
        assert v.resolve_version(artifact_dir, "v1") == artifact_dir / "versions" / "v1.md"

    def test_resolve_version_bad_tag_raises(self, tmp_path):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        with pytest.raises(v.InvalidTagError):
            v.resolve_version(artifact_dir, "v0")  # v0 invalid
        with pytest.raises(v.InvalidTagError):
            v.resolve_version(artifact_dir, "../etc")  # path-like rejected


class TestManifest:
    def test_load_absent_returns_empty(self, tmp_path):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        manifest = v.load_manifest(artifact_dir)
        assert manifest.versions == {}
        assert manifest.labels == {}

    def test_load_rejects_pathlike_tag(self, tmp_path):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        (artifact_dir / "versions.json").write_text(
            json.dumps({"versions": {"v1/../x": {"created_at": "", "note": ""}}, "labels": {}}),
            encoding="utf-8",
        )
        with pytest.raises(v.InvalidTagError):
            v.load_manifest(artifact_dir)

    def test_load_rejects_reserved_label(self, tmp_path):
        # A hand-edited 'latest' pointer is unmanageable (every mutating API
        # rejects it) — refuse to load it (Codex review).
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        (artifact_dir / "versions.json").write_text(
            json.dumps(
                {"versions": {"v1": {"created_at": "", "note": ""}}, "labels": {"latest": "v1"}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(v.ReservedLabelError):
            v.load_manifest(artifact_dir)

    def test_load_rejects_version_shaped_label(self, tmp_path):
        # A hand-edited version-shaped label is unreachable (shadowed by the
        # same-named version) — refuse to load it (review P2).
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        (artifact_dir / "versions.json").write_text(
            json.dumps(
                {"versions": {"v1": {"created_at": "", "note": ""}}, "labels": {"v1": "v1"}}
            ),
            encoding="utf-8",
        )
        with pytest.raises(v.InvalidLabelError):
            v.load_manifest(artifact_dir)

    @pytest.mark.parametrize("bad", ["[]", '"a string"', "42", '{"versions": []}', '{"labels": 3}'])
    def test_load_rejects_malformed_shape(self, tmp_path, bad):
        # Syntactically valid JSON of the wrong shape must surface a clean
        # VersionError, not an AttributeError traceback (Codex review).
        artifact_dir, _ = _make_dir_agent(tmp_path)
        (artifact_dir / "versions.json").write_text(bad, encoding="utf-8")
        with pytest.raises(v.VersionError):
            v.load_manifest(artifact_dir)

    def test_next_version_tag_pure(self):
        m = v.VersionsManifest()
        assert v.next_version_tag(m) == "v1"
        m.versions["v1"] = v.VersionRecord("v1", "", "")
        m.versions["v3"] = v.VersionRecord("v3", "", "")
        assert v.next_version_tag(m) == "v4"  # max+1, not count


class TestSchemaCompat:
    """Forward-compat prep for tree-layout entries (ADR-0030 §10, PR-G1).

    Readers refuse a NEWER schema loudly; writers round-trip fields they do not
    own. Both directions are pinned so a build that predates the tree layout can
    never silently strip a later writer's ``schema_version`` / per-entry
    ``layout`` during an ordinary ``promote`` / ``delete-label`` cycle.
    """

    def _seed_with_unknown_fields(self, artifact_dir, working):
        """A real v1 plus fields this build does not own, written to disk."""
        v.create_version(artifact_dir, working)
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["future_top_level"] = {"campaign": 2}
        raw["versions"]["v1"]["layout"] = "tree"
        (artifact_dir / "versions.json").write_text(json.dumps(raw), encoding="utf-8")
        return raw

    def test_promote_preserves_unknown_fields(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)

        v.promote_label(artifact_dir, "production", "v1")

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        # The mutation really happened (guards against a no-op passing vacuously)…
        assert raw["labels"] == {"production": "v1"}
        # …and the unknown fields survived it.
        assert raw["future_top_level"] == {"campaign": 2}
        assert raw["versions"]["v1"]["layout"] == "tree"

    def test_delete_label_preserves_unknown_fields(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v1")

        v.delete_label(artifact_dir, "production")

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw["labels"] == {}  # the mutation really happened
        assert raw["future_top_level"] == {"campaign": 2}
        assert raw["versions"]["v1"]["layout"] == "tree"

    def test_create_version_preserves_unknown_fields(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)

        working.write_text(_AGENT_TEMPLATE.format(marker="B", desc="v"), encoding="utf-8")
        v.create_version(artifact_dir, working)

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert "v2" in raw["versions"]  # the mutation really happened
        assert raw["future_top_level"] == {"campaign": 2}
        assert raw["versions"]["v1"]["layout"] == "tree"

    def test_unknown_fields_reach_the_dataclasses(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)

        manifest = v.load_manifest(artifact_dir)

        assert manifest.extra == {"future_top_level": {"campaign": 2}}
        assert manifest.versions["v1"].extra == {"layout": "tree"}
        # Known keys are NOT duplicated into extra.
        assert "versions" not in manifest.extra
        assert "created_at" not in manifest.versions["v1"].extra

    def test_legacy_manifest_gains_schema_version(self, tmp_path):
        """A schema-less manifest round-trips unchanged except the added field."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        raw_before = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw_before.pop("schema_version", None)
        (artifact_dir / "versions.json").write_text(json.dumps(raw_before), encoding="utf-8")

        assert v.load_manifest(artifact_dir).schema_version == v.SCHEMA_VERSION
        v.promote_label(artifact_dir, "production", "v1")

        raw_after = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw_after["schema_version"] == v.SCHEMA_VERSION
        assert raw_after["versions"] == raw_before["versions"]

    def test_refuses_newer_schema_version(self, tmp_path):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        (artifact_dir / "versions.json").write_text(
            json.dumps({"schema_version": v.SCHEMA_VERSION + 1, "versions": {}, "labels": {}}),
            encoding="utf-8",
        )
        with pytest.raises(v.VersionError, match="schema_version"):
            v.load_manifest(artifact_dir)

    @pytest.mark.parametrize(
        "bad",
        [
            0,  # not positive
            -1,
            True,  # isinstance(True, int) is True — must NOT read as 1
            False,
            "1",  # str
            1.0,  # float
            [1],
        ],
        ids=["zero", "negative", "true", "false", "str", "float", "list"],
    )
    def test_refuses_malformed_schema_version(self, tmp_path, bad):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        (artifact_dir / "versions.json").write_text(
            json.dumps({"schema_version": bad, "versions": {}, "labels": {}}),
            encoding="utf-8",
        )
        with pytest.raises(v.VersionError, match="schema_version"):
            v.load_manifest(artifact_dir)


class TestConcurrency:
    def test_concurrent_create_no_overwrite(self, tmp_path):
        """Two threads calling create_version must not both allocate v1 — the
        single per-transaction _file_lock serializes tag allocation."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        barrier = threading.Barrier(2)
        tags: list[str] = []
        errors: list[Exception] = []

        def worker():
            try:
                barrier.wait()
                tags.append(v.create_version(artifact_dir, working).tag)
            except Exception as exc:  # noqa: BLE001 — surface for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        # Distinct tags, both files present, manifest has both.
        assert sorted(tags) == ["v1", "v2"]
        assert (artifact_dir / "versions" / "v1.md").is_file()
        assert (artifact_dir / "versions" / "v2.md").is_file()
        assert set(v.load_manifest(artifact_dir).versions) == {"v1", "v2"}

    def test_lock_timeout_expires_when_sidecar_lock_held(self, tmp_path):
        """``lock_timeout`` bounds the sidecar-lock wait: with the lock held by
        another holder, every mutator raises the builtin ``TimeoutError``
        (the #1145 shape the web routes map to 503) instead of blocking
        forever. Expiry-direction timing: a slow runner only makes the
        expiry later, never a false pass."""
        from memtomem.context._atomic import _file_lock, _lock_path_for

        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)  # so promote has a v1 to point at
        lock = _lock_path_for(v.versions_json_path(artifact_dir))
        with _file_lock(lock):
            with pytest.raises(TimeoutError):
                v.create_version(artifact_dir, working, lock_timeout=0.1)
            with pytest.raises(TimeoutError):
                v.promote_label(artifact_dir, "staging", "v1", lock_timeout=0.1)
            with pytest.raises(TimeoutError):
                v.delete_label(artifact_dir, "staging", lock_timeout=0.1)

    def test_lock_timeout_none_still_blocks_and_succeeds(self, tmp_path):
        """Default ``lock_timeout=None`` keeps the blocking CLI semantics —
        an uncontended call just succeeds."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        record = v.create_version(artifact_dir, working, lock_timeout=None)
        assert record.tag == "v1"


# ── Label-aware sync integration (generate_all_agents) ───────────────


class TestLabelAwareSync:
    def test_label_fans_out_frozen_version(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        v.create_version(artifact_dir, working)  # v1 = A
        v.promote_label(artifact_dir, "production", "v1")
        # Working file moves on to B.
        working.write_text(_AGENT_TEMPLATE.format(marker="B", desc="v"), encoding="utf-8")

        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="production")
        assert result.generated  # something fanned out
        out = (tmp_path / ".claude/agents/my-agent.md").read_text(encoding="utf-8")
        assert "BODY MARKER: A" in out  # frozen v1, not the working B
        assert "BODY MARKER: B" not in out

    def test_label_latest_equals_no_label(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        v.create_version(artifact_dir, working)
        working.write_text(_AGENT_TEMPLATE.format(marker="WORKING", desc="v"), encoding="utf-8")

        generate_all_agents(tmp_path, runtimes=["claude_agents"], label="latest")
        latest_out = (tmp_path / ".claude/agents/my-agent.md").read_text(encoding="utf-8")
        generate_all_agents(tmp_path, runtimes=["claude_agents"])  # no label
        nolabel_out = (tmp_path / ".claude/agents/my-agent.md").read_text(encoding="utf-8")
        assert latest_out == nolabel_out
        assert "BODY MARKER: WORKING" in latest_out  # both use the working file

    def test_version_tag_direct(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path, marker="A")
        v.create_version(artifact_dir, working)  # v1 = A
        working.write_text(_AGENT_TEMPLATE.format(marker="B", desc="v"), encoding="utf-8")

        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="v1")
        assert result.generated
        out = (tmp_path / ".claude/agents/my-agent.md").read_text(encoding="utf-8")
        assert "BODY MARKER: A" in out

    def test_unknown_label_isolated_as_skip(self, tmp_path):
        _make_dir_agent(tmp_path, marker="A")  # no versions/labels created
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="ghost")
        # Per-artifact isolation: skip with LABEL_NOT_FOUND, nothing fanned out,
        # no raise.
        assert not result.generated
        codes = {code for _, _, code in result.skipped}
        assert skip_codes.LABEL_NOT_FOUND in codes
        # Skip row names the artifact (my-agent), not the dir-layout filename
        # (agent.md) — Codex review.
        names = {name for name, _, code in result.skipped if code == skip_codes.LABEL_NOT_FOUND}
        assert names == {"my-agent"}
        assert not (tmp_path / ".claude/agents/my-agent.md").exists()

    def test_flat_layout_with_label_isolated_as_skip(self, tmp_path):
        _make_flat_agent(tmp_path, name="flat-agent", marker="A")
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="production")
        codes = {code for _, _, code in result.skipped}
        assert skip_codes.VERSIONING_REQUIRES_DIR_LAYOUT in codes
        assert not result.generated

    def test_flat_layout_latest_still_works(self, tmp_path):
        # latest / no-label on a flat artifact is unaffected (current behavior).
        _make_flat_agent(tmp_path, name="flat-agent", marker="A")
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="latest")
        assert result.generated
        assert (tmp_path / ".claude/agents/flat-agent.md").is_file()

    def test_malformed_manifest_isolated_as_skip(self, tmp_path):
        # A malformed/tampered versions.json during a labeled sync must not
        # raise a raw traceback — the VersionError family is caught and isolated
        # as a parse-class skip per artifact (Codex review).
        artifact_dir, _ = _make_dir_agent(tmp_path, marker="A")
        (artifact_dir / "versions.json").write_text("[]", encoding="utf-8")  # wrong shape
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="production")
        codes = {code for _, _, code in result.skipped}
        assert skip_codes.PARSE_ERROR in codes
        assert not result.generated


class TestLabelAwareSyncCommands:
    """Commands have distinct parse/render paths from agents — pin that the
    label-aware sync works end to end for them too (Codex review)."""

    def test_label_fans_out_frozen_command_version(self, tmp_path):
        artifact_dir, working = _make_dir_command(tmp_path, marker="A")
        v.create_version(artifact_dir, working)  # v1 = A
        v.promote_label(artifact_dir, "production", "v1")
        working.write_text(_COMMAND_TEMPLATE.format(marker="B"), encoding="utf-8")

        result = generate_all_commands(tmp_path, runtimes=["claude_commands"], label="production")
        assert result.generated
        out = (tmp_path / ".claude/commands/my-cmd.md").read_text(encoding="utf-8")
        assert "MARKER: A" in out and "MARKER: B" not in out

    def test_command_label_latest_equals_no_label(self, tmp_path):
        artifact_dir, working = _make_dir_command(tmp_path, marker="A")
        v.create_version(artifact_dir, working)
        working.write_text(_COMMAND_TEMPLATE.format(marker="WORKING"), encoding="utf-8")
        generate_all_commands(tmp_path, runtimes=["claude_commands"], label="latest")
        latest_out = (tmp_path / ".claude/commands/my-cmd.md").read_text(encoding="utf-8")
        generate_all_commands(tmp_path, runtimes=["claude_commands"])
        nolabel_out = (tmp_path / ".claude/commands/my-cmd.md").read_text(encoding="utf-8")
        assert latest_out == nolabel_out
        assert "MARKER: WORKING" in latest_out
