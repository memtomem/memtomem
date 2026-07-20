"""Tests for context/versioning.py — version snapshots + label pointers (ADR-0022).

Covers the pure-filesystem versioning module (create/promote/resolve, tag
validation, locking/no-overwrite) and its integration with the label-aware
``generate_all_agents`` sync path.
"""

from __future__ import annotations

import json
import shutil
import stat
import sys
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
        """A real v1 plus fields this build does not own, written to disk.

        The unknown per-entry key must be one this build genuinely does not own
        — ``layout`` was the stand-in while it was hypothetical (PR-G1), but
        PR-G3 made it a real field, so using it here would test round-tripping
        of a KNOWN key and quietly stop covering the unknown-key path.
        """
        v.create_version(artifact_dir, working)
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["future_top_level"] = {"campaign": 2}
        raw["versions"]["v1"]["future_entry_field"] = {"cas": "sha256:…"}
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
        assert raw["versions"]["v1"]["future_entry_field"] == {"cas": "sha256:…"}

    def test_delete_label_preserves_unknown_fields(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v1")

        v.delete_label(artifact_dir, "production")

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw["labels"] == {}  # the mutation really happened
        assert raw["future_top_level"] == {"campaign": 2}
        assert raw["versions"]["v1"]["future_entry_field"] == {"cas": "sha256:…"}

    def test_create_version_preserves_unknown_fields(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)

        working.write_text(_AGENT_TEMPLATE.format(marker="B", desc="v"), encoding="utf-8")
        v.create_version(artifact_dir, working)

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert "v2" in raw["versions"]  # the mutation really happened
        assert raw["future_top_level"] == {"campaign": 2}
        assert raw["versions"]["v1"]["future_entry_field"] == {"cas": "sha256:…"}

    def test_unknown_fields_reach_the_dataclasses(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        self._seed_with_unknown_fields(artifact_dir, working)

        manifest = v.load_manifest(artifact_dir)

        assert manifest.extra == {"future_top_level": {"campaign": 2}}
        assert manifest.versions["v1"].extra == {"future_entry_field": {"cas": "sha256:…"}}
        # ``layout`` is an OWNED key now — it must not leak into extra.
        assert "layout" not in manifest.versions["v1"].extra
        # Known keys are NOT duplicated into extra.
        assert "versions" not in manifest.extra
        assert "created_at" not in manifest.versions["v1"].extra

    def test_legacy_manifest_gains_schema_version(self, tmp_path):
        """A schema-less manifest round-trips unchanged except the added field.

        The added value is **1**, not this build's ``SCHEMA_VERSION`` — a
        file-entries-only manifest declares the minimum a reader needs, so an
        older build keeps reading it (see ``_required_schema_version``).
        """
        assert v.SCHEMA_VERSION == 2, "this test's point is that 1 != SCHEMA_VERSION"
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        raw_before = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw_before.pop("schema_version", None)
        (artifact_dir / "versions.json").write_text(json.dumps(raw_before), encoding="utf-8")

        assert v.load_manifest(artifact_dir).schema_version == 1
        v.promote_label(artifact_dir, "production", "v1")

        raw_after = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw_after["schema_version"] == 1
        assert raw_after["versions"] == raw_before["versions"]

    def test_refuses_newer_schema_version(self, tmp_path):
        artifact_dir, _ = _make_dir_agent(tmp_path)
        (artifact_dir / "versions.json").write_text(
            json.dumps({"schema_version": v.SCHEMA_VERSION + 1, "versions": {}, "labels": {}}),
            encoding="utf-8",
        )
        with pytest.raises(v.UnsupportedSchemaVersionError, match="schema_version"):
            v.load_manifest(artifact_dir)

    def test_schema_gate_runs_before_shape_checks(self, tmp_path):
        """Ordering is load-bearing: a newer schema may legitimately change the
        shape of ``versions``/``labels``, so the schema refusal must win over
        the shape validation rather than reporting a confusing shape error."""
        artifact_dir, _ = _make_dir_agent(tmp_path)
        (artifact_dir / "versions.json").write_text(
            json.dumps({"schema_version": v.SCHEMA_VERSION + 1, "versions": [], "labels": 3}),
            encoding="utf-8",
        )
        with pytest.raises(v.UnsupportedSchemaVersionError, match="schema_version"):
            v.load_manifest(artifact_dir)

    def test_newer_schema_fails_writes_closed(self, tmp_path):
        """The invariant that actually prevents corruption: a mutator refuses a
        newer-schema manifest too (every one loads under the lock before saving),
        so an old build cannot rewrite — and thereby strip — a newer file."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["schema_version"] = v.SCHEMA_VERSION + 1
        before = json.dumps(raw)
        (artifact_dir / "versions.json").write_text(before, encoding="utf-8")

        with pytest.raises(v.UnsupportedSchemaVersionError):
            v.promote_label(artifact_dir, "production", "v1")
        with pytest.raises(v.UnsupportedSchemaVersionError):
            v.delete_label(artifact_dir, "production")
        with pytest.raises(v.UnsupportedSchemaVersionError):
            v.create_version(artifact_dir, working)
        # Refused means untouched, not partially rewritten.
        assert (artifact_dir / "versions.json").read_text(encoding="utf-8") == before

    def test_known_keys_win_over_extra(self, tmp_path):
        """``extra`` is attacker-controlled content if a manifest is tampered
        with — it must never shadow a field this build owns."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)

        manifest = v.load_manifest(artifact_dir)
        manifest.extra["versions"] = {"vBOGUS": {}}
        manifest.extra["labels"] = {"evil": "v1"}
        manifest.extra["schema_version"] = 999
        manifest.versions["v1"].extra["created_at"] = "hacked"
        manifest.versions["v1"].extra["note"] = "hacked"
        v._save_manifest(artifact_dir, manifest)

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert list(raw["versions"]) == ["v1"]
        assert raw["labels"] == {}
        # 1, not SCHEMA_VERSION: the manifest holds only file-layout entries,
        # and a tampered ``extra`` must not be able to advertise otherwise.
        assert raw["schema_version"] == 1
        assert raw["versions"]["v1"]["created_at"] != "hacked"
        assert raw["versions"]["v1"]["note"] != "hacked"

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


# ── Tree snapshots (ADR-0030 §10, PR-G3) ─────────────────────────────


def _make_skill(project_root, name="demo", *, extra=None):
    """A canonical skill dir with a SKILL.md plus optional extra files."""
    skill_dir = project_root / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo\n---\n\nBody\n", encoding="utf-8")
    for rel, text in (extra or {}).items():
        path = skill_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return skill_dir


def _read_tree(root):
    """``{posix_relpath: bytes}`` of every file under *root*."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


class TestSchemaMinimality:
    """Writers emit the MINIMUM schema the manifest's content needs, never this
    build's maximum — so a flat agents/commands store stays readable by a build
    that predates tree layout (``_required_schema_version``)."""

    def test_flat_only_manifest_writes_schema_1(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        v.create_version(artifact_dir, working)
        v.promote_label(artifact_dir, "production", "v2")

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == 1
        assert v.SCHEMA_VERSION == 2  # …and the constant really did advance

    def test_flat_entry_carries_no_layout_key(self, tmp_path):
        """File entries stay byte-shape identical to what every pre-G3 build
        wrote — an added default key would churn every manifest on first touch."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working, note="n")

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw["versions"]["v1"] == {
            "created_at": raw["versions"]["v1"]["created_at"],
            "note": "n",
        }

    def test_tree_entry_bumps_manifest_to_schema_2(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

        raw = json.loads((skill_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == 2
        assert raw["versions"]["v1"]["layout"] == "tree"

    def test_mixed_manifest_stays_schema_2_after_flat_create(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_tree_version(artifact_dir, [("SKILL.md", b"x")])
        v.create_version(artifact_dir, working)

        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert raw["schema_version"] == 2
        assert raw["versions"]["v1"]["layout"] == "tree"
        assert "layout" not in raw["versions"]["v2"]

    def test_hand_written_schema_2_without_tree_entries_downgrades(self, tmp_path):
        """Deliberate, not a bug: the manifest holds nothing an older reader
        cannot read, so declaring 2 would lock it out for no reason."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["schema_version"] = 2
        (artifact_dir / "versions.json").write_text(json.dumps(raw), encoding="utf-8")

        v.promote_label(artifact_dir, "production", "v1")

        after = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        assert after["schema_version"] == 1

    @pytest.mark.parametrize("bad", ["sparse", "", 1, None, ["tree"]])
    def test_unknown_layout_value_refused(self, tmp_path, bad):
        """A newer storage shape MUST also bump schema_version (which the gate
        refuses first), so an unknown layout here is corruption — fail closed
        rather than treat a tree-ish entry as a readable file."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["versions"]["v1"]["layout"] = bad
        (artifact_dir / "versions.json").write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(v.VersionError, match="layout"):
            v.load_manifest(artifact_dir)


class TestCreateTreeVersion:
    def test_writes_payload_into_versions_dir(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        payload = [
            ("SKILL.md", b"# demo\n"),
            ("scripts/run.sh", b"echo hi\n"),
            ("references/deep/notes.md", b"notes\n"),
        ]
        rec = v.create_tree_version(skill_dir, payload, note="from pull")

        assert rec.tag == "v1"
        assert rec.layout == "tree"
        assert rec.note == "from pull"
        snapshot = skill_dir / "versions" / "v1"
        assert snapshot.is_dir()
        assert _read_tree(snapshot) == dict(payload)

    def test_snapshot_digest_matches_payload_digest(self, tmp_path):
        """The stored tree must re-digest to the value the caller computed —
        otherwise a later CAS / drift check would see a phantom change."""
        from memtomem.context.skill_payload import payload_digest

        skill_dir = _make_skill(tmp_path)
        payload = [("SKILL.md", b"# demo\n"), ("scripts/run.sh", b"echo\n")]
        v.create_tree_version(skill_dir, payload)

        snapshot = skill_dir / "versions" / "v1"
        assert payload_digest(list(_read_tree(snapshot).items())) == payload_digest(payload)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
    def test_files_land_at_0o644(self, tmp_path):
        """The copier's content mode — and why the §10 digest excludes the exec
        bit (preserving a bit the copier drops makes digests unreproducible)."""
        skill_dir = _make_skill(tmp_path)
        v.create_tree_version(skill_dir, [("scripts/run.sh", b"echo\n")])
        mode = (skill_dir / "versions" / "v1" / "scripts" / "run.sh").stat().st_mode
        assert stat.S_IMODE(mode) == 0o644

    def test_snapshot_is_a_new_inode_not_a_hardlink(self, tmp_path):
        """Never hardlink live payload: an editor (or a pre-swap crash) could
        then mutate history through the shared inode."""
        skill_dir = _make_skill(tmp_path)
        live = skill_dir / "SKILL.md"
        v.create_tree_version(skill_dir, [("SKILL.md", live.read_bytes())])
        snapshot_file = skill_dir / "versions" / "v1" / "SKILL.md"

        assert snapshot_file.stat().st_ino != live.stat().st_ino
        live.write_bytes(b"EDITED\n")
        assert snapshot_file.read_bytes() != b"EDITED\n"

    @pytest.mark.parametrize(
        "payload",
        [
            [("versions/v1/SKILL.md", b"x")],
            [("versions", b"x")],
            [("versions.json", b"{}")],
            [("SKILL.md", b"ok"), ("versions/v1/SKILL.md", b"x")],
        ],
    )
    def test_refuses_version_store_internal_payload(self, tmp_path, payload):
        """A snapshot can never contain the version store — else v2 contains v1,
        every snapshot doubles the store, and fan-out ships history."""
        skill_dir = _make_skill(tmp_path)
        with pytest.raises(v.VersionError, match="version-store internal"):
            v.create_tree_version(skill_dir, payload)
        assert not (skill_dir / "versions").exists()

    def test_refuses_empty_payload(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        with pytest.raises(v.VersionError, match="empty payload"):
            v.create_tree_version(skill_dir, [])
        assert not (skill_dir / "versions").exists()

    @pytest.mark.parametrize(
        "rel", ["../escape.md", "/abs.md", "a//b.md", "", ".", "a/../../b.md", "a\\b.md"]
    )
    def test_refuses_traversal_relpaths(self, tmp_path, rel):
        skill_dir = _make_skill(tmp_path)
        with pytest.raises(ValueError):
            v.create_tree_version(skill_dir, [(rel, b"x")])
        # Nothing written and no staging leftover.
        vdir = skill_dir / "versions"
        assert not vdir.exists() or list(vdir.iterdir()) == []

    def test_refuses_duplicate_relpath(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        with pytest.raises(ValueError, match="duplicate"):
            v.create_tree_version(skill_dir, [("a.md", b"1"), ("a.md", b"2")])

    def test_v2_does_not_contain_v1(self, tmp_path):
        """The recursion hazard §10 exists to close, end to end: snapshot a real
        skill twice through the payload iterator that defines skill content."""
        from memtomem.context.skill_payload import iter_skill_payload_files

        skill_dir = _make_skill(
            tmp_path,
            extra={
                "scripts/run.sh": "echo\n",
                # Store-owned top level — excluded from payload…
                "overrides/claude.md": "override\n",
                # …but a NESTED same-name file is ordinary user content.
                "scripts/versions.json": "{}\n",
            },
        )
        v.create_tree_version(skill_dir, iter_skill_payload_files(skill_dir))
        v.create_tree_version(skill_dir, iter_skill_payload_files(skill_dir))

        v2 = _read_tree(skill_dir / "versions" / "v2")
        assert not any(rel.startswith("versions/") for rel in v2)
        assert "versions.json" not in v2
        assert not any(rel.startswith("overrides/") for rel in v2)
        assert "scripts/versions.json" in v2  # nested content survives
        assert set(v2) == {"SKILL.md", "scripts/run.sh", "scripts/versions.json"}

    def test_flat_layout_raises(self, tmp_path):
        missing = tmp_path / ".memtomem" / "skills" / "ghost"
        with pytest.raises(v.VersionsDirMissingError):
            v.create_tree_version(missing, [("SKILL.md", b"x")])

    def test_orphan_tree_dir_preserved_and_bumps_tag(self, tmp_path):
        """A crash between the snapshot write and the manifest save leaves an
        orphan vN/ — real snapshot bytes whose row we failed to record. It is
        PRESERVED and skipped, never reaped (ADR-0030 §10)."""
        skill_dir = _make_skill(tmp_path)
        orphan = skill_dir / "versions" / "v1"
        orphan.mkdir(parents=True)
        (orphan / "SKILL.md").write_bytes(b"orphan bytes\n")

        rec = v.create_tree_version(skill_dir, [("SKILL.md", b"new\n")])

        assert rec.tag == "v2"
        assert (orphan / "SKILL.md").read_bytes() == b"orphan bytes\n"
        assert set(v.load_manifest(skill_dir).versions) == {"v2"}

    def test_tree_dir_blocks_flat_tag_reuse(self, tmp_path):
        """Cross-layout reconciliation: one tag must never name two snapshots."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)  # v1.md
        (artifact_dir / "versions" / "v2").mkdir()  # orphan tree dir

        assert v.create_version(artifact_dir, working).tag == "v3"

    def test_write_once_refuses_taken_tag(self, tmp_path, monkeypatch):
        skill_dir = _make_skill(tmp_path)
        v.create_tree_version(skill_dir, [("SKILL.md", b"first\n")])
        monkeypatch.setattr(v, "_next_version_tag_reconciled", lambda *_: "v1")

        with pytest.raises(v.InvalidTagError, match="already exists"):
            v.create_tree_version(skill_dir, [("SKILL.md", b"second\n")])

        # The existing snapshot is untouched and the manifest did not grow.
        assert (skill_dir / "versions" / "v1" / "SKILL.md").read_bytes() == b"first\n"
        assert set(v.load_manifest(skill_dir).versions) == {"v1"}

    def test_promote_race_on_destination_fails_closed(self, tmp_path, monkeypatch):
        """If the destination appears between allocation and the rename, the
        exclusive rename must refuse rather than clobber (#1839 contract)."""
        skill_dir = _make_skill(tmp_path)
        real = v.write_tree_payload

        def _racing(dst_dir, payload, **kw):
            real(dst_dir, payload, **kw)
            (skill_dir / "versions" / "v1").mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(v, "write_tree_payload", _racing)
        with pytest.raises(OSError):
            v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

        assert v.load_manifest(skill_dir).versions == {}
        assert not list((skill_dir / "versions").glob(".staging-*"))

    def test_manifest_save_failure_leaves_orphan_not_a_dangling_row(self, tmp_path, monkeypatch):
        """Snapshot lands BEFORE the row, so a crash leaves an orphan (harmless,
        skipped) rather than a manifest row naming nothing."""
        skill_dir = _make_skill(tmp_path)

        def _boom(*_args, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(v, "_save_manifest", _boom)
        with pytest.raises(OSError):
            v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

        assert (skill_dir / "versions" / "v1" / "SKILL.md").read_bytes() == b"x"
        assert v.load_manifest(skill_dir).versions == {}
        monkeypatch.undo()
        assert v.create_tree_version(skill_dir, [("SKILL.md", b"y")]).tag == "v2"

    def test_conforming_staging_leftover_is_reaped(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        stale = skill_dir / "versions" / ".staging-v1-4242-a1b2c3.tmp"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_bytes(b"junk")

        v.create_tree_version(skill_dir, [("SKILL.md", b"x")])
        assert not stale.exists()

    def test_non_conforming_staging_name_is_not_deleted(self, tmp_path):
        """The reaper defers to ``is_internal_artifact_dir``; a name that merely
        looks staging-ish is user data and must survive (#1229 lesson)."""
        skill_dir = _make_skill(tmp_path)
        keep = skill_dir / "versions" / ".staging-v1-notes.tmp"
        keep.mkdir(parents=True)
        (keep / "keep.md").write_bytes(b"mine")

        v.create_tree_version(skill_dir, [("SKILL.md", b"x")])
        assert (keep / "keep.md").read_bytes() == b"mine"

    @pytest.mark.parametrize("mode", ["returns_false", "raises"])
    def test_survives_unavailable_directory_fsync(self, tmp_path, monkeypatch, mode):
        """Windows and some network/tmpfs mounts reject directory fsync; there
        durability degrades to process-crash consistency and the create must
        still succeed."""
        skill_dir = _make_skill(tmp_path)
        seen = []

        def _fake(path):
            seen.append(path)
            if mode == "raises":
                raise OSError("not supported")
            return False

        monkeypatch.setattr(v, "fsync_dir", _fake)
        if mode == "raises":
            with pytest.raises(OSError):
                v.create_tree_version(skill_dir, [("SKILL.md", b"x")])
            return
        rec = v.create_tree_version(skill_dir, [("SKILL.md", b"x")])
        assert rec.tag == "v1"
        assert skill_dir / "versions" in seen and skill_dir in seen


class TestTreeResolution:
    """Single-file APIs refuse a tree entry and vice versa — neither can
    silently serve the other's shape."""

    def _seeded(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        v.create_tree_version(skill_dir, [("SKILL.md", b"x")])
        return skill_dir

    def test_resolve_version_refuses_tree(self, tmp_path):
        skill_dir = self._seeded(tmp_path)
        with pytest.raises(v.TreeVersionError, match="v1"):
            v.resolve_version(skill_dir, "v1")

    def test_resolve_label_propagates_refusal(self, tmp_path):
        skill_dir = self._seeded(tmp_path)
        # The label can't even be created (see below), so point one by hand to
        # prove the READ path refuses too, not just the write path.
        raw = json.loads((skill_dir / "versions.json").read_text(encoding="utf-8"))
        raw["labels"] = {"production": "v1"}
        (skill_dir / "versions.json").write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(v.TreeVersionError):
            v.resolve_label(skill_dir, "production")

    def test_promote_label_refuses_tree_target(self, tmp_path):
        skill_dir = self._seeded(tmp_path)
        with pytest.raises(v.TreeVersionError, match="deferred"):
            v.promote_label(skill_dir, "production", "v1")
        assert v.load_manifest(skill_dir).labels == {}

    def test_label_resolver_refusal_is_isolated_as_a_skip(self, tmp_path):
        """A tree version reached through labeled fan-out must degrade to a
        per-artifact skip, not abort the whole sync."""
        artifact_dir, _ = _make_dir_agent(tmp_path)
        v.create_tree_version(artifact_dir, [("SKILL.md", b"x")])
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["labels"] = {"production": "v1"}
        (artifact_dir / "versions.json").write_text(json.dumps(raw), encoding="utf-8")

        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], label="production")
        assert result.skipped and not result.generated
        assert skip_codes.PARSE_ERROR in {code for _, _, code in result.skipped}

    def test_resolve_version_tree_round_trip(self, tmp_path):
        skill_dir = self._seeded(tmp_path)
        assert v.resolve_version_tree(skill_dir, "v1") == skill_dir / "versions" / "v1"

    def test_resolve_version_tree_refuses_file_entry(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        with pytest.raises(v.TreeVersionError, match="file snapshot"):
            v.resolve_version_tree(artifact_dir, "v1")

    def test_resolve_version_tree_missing_dir(self, tmp_path):
        skill_dir = self._seeded(tmp_path)
        shutil.rmtree(skill_dir / "versions" / "v1")
        with pytest.raises(v.VersionNotFoundError, match="missing"):
            v.resolve_version_tree(skill_dir, "v1")


class TestTreeConcurrency:
    def test_concurrent_tree_creates_allocate_distinct_tags(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        barrier = threading.Barrier(2)
        tags: list[str] = []
        errors: list[Exception] = []

        def worker(marker):
            try:
                barrier.wait()
                tags.append(v.create_tree_version(skill_dir, [("SKILL.md", marker)]).tag)
            except Exception as exc:  # noqa: BLE001 — surface for assertion
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(m,)) for m in (b"a\n", b"b\n")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert sorted(tags) == ["v1", "v2"]
        assert (skill_dir / "versions" / "v1" / "SKILL.md").is_file()
        assert (skill_dir / "versions" / "v2" / "SKILL.md").is_file()
        assert set(v.load_manifest(skill_dir).versions) == {"v1", "v2"}
        assert not list((skill_dir / "versions").glob(".staging-*"))

    def test_flat_and_tree_creates_do_not_collide(self, tmp_path):
        artifact_dir, working = _make_dir_agent(tmp_path)
        barrier = threading.Barrier(2)
        tags: list[str] = []
        errors: list[Exception] = []

        def flat():
            try:
                barrier.wait()
                tags.append(v.create_version(artifact_dir, working).tag)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def tree():
            try:
                barrier.wait()
                tags.append(v.create_tree_version(artifact_dir, [("SKILL.md", b"x")]).tag)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=fn) for fn in (flat, tree)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, errors
        assert sorted(tags) == ["v1", "v2"]
        entries = sorted(p.name for p in (artifact_dir / "versions").iterdir())
        assert entries in (["v1", "v2.md"], ["v1.md", "v2"])

    def test_lock_timeout_expires_when_sidecar_held(self, tmp_path):
        from memtomem.context._atomic import _file_lock, _lock_path_for

        skill_dir = _make_skill(tmp_path)
        lock = _lock_path_for(v.versions_json_path(skill_dir))
        with _file_lock(lock):
            with pytest.raises(TimeoutError):
                v.create_tree_version(skill_dir, [("SKILL.md", b"x")], lock_timeout=0.1)
        assert not (skill_dir / "versions").exists() or not list((skill_dir / "versions").iterdir())


class TestCodexReviewRegressions:
    """Findings from the PR-G3 review gate — each reproduced before it was fixed."""

    def test_case_variant_on_a_case_sensitive_fs_is_allowed(self, tmp_path):
        """The guard tests ALIASING, not spelling. On ext4 a user's ``Versions/``
        is a distinct directory and must stay legal content — banning it by name
        would break Linux to fix a macOS/Windows bug.

        Skipped where the filesystem really is case-insensitive, since there the
        premise cannot hold.
        """
        skill_dir = _make_skill(tmp_path)
        probe = skill_dir / "CaseProbe"
        probe.mkdir()
        if (skill_dir / "caseprobe").exists():
            pytest.skip("filesystem is case-insensitive")
        probe.rmdir()

        (skill_dir / "Versions").mkdir()
        (skill_dir / "Versions" / "note.md").write_text("user content\n", encoding="utf-8")

        assert v.create_tree_version(skill_dir, [("SKILL.md", b"x")]).tag == "v1"

    def test_case_colliding_versions_dir_is_refused(self, tmp_path):
        """On a case-INSENSITIVE filesystem a pre-existing ``Versions/`` IS the
        store, but the payload iterator's exclusion set is case-sensitive and
        reads it as skill content. The two disagree, the snapshot lands inside
        a directory the next payload read includes, and v2 ends up containing
        v1 — the exact recursion hazard §10 exists to prevent. Refuse loudly.
        """
        skill_dir = _make_skill(tmp_path)
        (skill_dir / "Versions").mkdir()
        if not (skill_dir / "versions").exists():
            pytest.skip("filesystem is case-sensitive — no aliasing to detect")
        (skill_dir / "Versions" / "note.md").write_text("user content\n", encoding="utf-8")

        with pytest.raises(v.VersionError, match="case-insensitive"):
            v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

    def test_case_colliding_manifest_is_refused(self, tmp_path):
        skill_dir = _make_skill(tmp_path)
        (skill_dir / "Versions.JSON").write_text("{}", encoding="utf-8")
        if not (skill_dir / "versions.json").exists():
            pytest.skip("filesystem is case-sensitive — no aliasing to detect")

        with pytest.raises(v.VersionError, match="case-insensitive"):
            v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

    def test_exact_case_reserved_names_are_not_refused(self, tmp_path):
        """The guard targets ALIASES, not the store's own files — a second
        create must not trip over the ``versions/`` the first one made."""
        skill_dir = _make_skill(tmp_path)
        assert v.create_tree_version(skill_dir, [("SKILL.md", b"a")]).tag == "v1"
        assert v.create_tree_version(skill_dir, [("SKILL.md", b"b")]).tag == "v2"

    def test_declared_schema_below_entry_requirement_is_refused(self, tmp_path):
        """``schema_version: 1`` carrying a tree entry is self-contradictory —
        no build that wrote it could have meant it, and accepting it would let
        a hand-edited file smuggle tree state past the version gate."""
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)
        raw = json.loads((artifact_dir / "versions.json").read_text(encoding="utf-8"))
        raw["versions"]["v1"]["layout"] = "tree"  # schema_version stays 1
        (artifact_dir / "versions.json").write_text(json.dumps(raw), encoding="utf-8")

        with pytest.raises(v.VersionError, match="require"):
            v.load_manifest(artifact_dir)

    def test_versions_dir_entry_is_durable_before_promotion(self, tmp_path, monkeypatch):
        """The artifact dir must be fsynced when ``versions/`` is first created,
        BEFORE anything is promoted into it — otherwise a power cut could leave
        a saved manifest row naming a directory whose entry never landed."""
        skill_dir = _make_skill(tmp_path)
        order: list[str] = []
        real_rename = v.rename_no_replace
        monkeypatch.setattr(v, "fsync_dir", lambda p: order.append(f"fsync:{p.name}") or True)
        monkeypatch.setattr(
            v,
            "rename_no_replace",
            lambda s, d: order.append("promote") or real_rename(s, d),
        )

        v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

        assert order.index(f"fsync:{skill_dir.name}") < order.index("promote")

    def test_barrier_fires_even_when_a_flat_create_made_versions_dir(self, tmp_path, monkeypatch):
        """ "It already existed" is NOT proof the entry was ever fsynced.

        ``create_version`` creates ``versions/`` as a side effect of
        ``atomic_write_bytes``' parent mkdir and never syncs the parent, so a
        flat-then-tree sequence would skip the barrier on exactly the shape
        that needs it. The barrier is therefore unconditional.
        """
        artifact_dir, working = _make_dir_agent(tmp_path)
        v.create_version(artifact_dir, working)  # makes versions/ with no parent fsync
        assert (artifact_dir / "versions").is_dir()

        order: list[str] = []
        real_rename = v.rename_no_replace
        monkeypatch.setattr(v, "fsync_dir", lambda p: order.append(f"fsync:{p.name}") or True)
        monkeypatch.setattr(
            v,
            "rename_no_replace",
            lambda s, d: order.append("promote") or real_rename(s, d),
        )
        v.create_tree_version(artifact_dir, [("SKILL.md", b"x")])

        assert order.index(f"fsync:{artifact_dir.name}") < order.index("promote")

    def test_alias_created_after_the_first_check_is_still_refused(self, tmp_path, monkeypatch):
        """The sidecar lock serializes memtomem's own writers, but nothing stops
        an out-of-band ``mkdir Versions/`` landing between the check and the
        ``mkdir(exist_ok=True)`` that would then silently adopt it."""
        skill_dir = _make_skill(tmp_path)
        probe = skill_dir / "CaseProbe"
        probe.mkdir()
        insensitive = (skill_dir / "caseprobe").exists()
        probe.rmdir()
        if not insensitive:
            pytest.skip("filesystem is case-sensitive — no aliasing to detect")

        calls: list[int] = []
        real = v._refuse_case_colliding_store

        def _racing(artifact_dir):
            real(artifact_dir)
            calls.append(1)
            if len(calls) == 1:  # slip the alias in right after the FIRST check
                (skill_dir / "Versions").mkdir(exist_ok=True)

        monkeypatch.setattr(v, "_refuse_case_colliding_store", _racing)
        with pytest.raises(v.VersionError, match="case-insensitive"):
            v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

    def test_manifest_alias_appearing_mid_transaction_fails_closed(self, tmp_path, monkeypatch):
        """Both collision checks run before the manifest exists, so a
        ``Versions.JSON`` created later still claims the directory entry on a
        case-insensitive filesystem — ``os.replace`` writes our bytes under the
        FOREIGN spelling, which the case-sensitive payload iterator then reads
        as skill content and fans out.

        Must fail loudly, and must leave the promoted ``vN/`` behind as the
        deliberately preserved orphan so a retry allocates cleanly.
        """
        skill_dir = _make_skill(tmp_path)
        probe = skill_dir / "CaseProbe"
        probe.mkdir()
        insensitive = (skill_dir / "caseprobe").exists()
        probe.rmdir()
        if not insensitive:
            pytest.skip("filesystem is case-sensitive — no aliasing to detect")

        real_save = v._save_manifest

        def _racing(artifact_dir, manifest):
            # Slip the alias in just before our own manifest write lands.
            (skill_dir / "Versions.JSON").write_text("{}", encoding="utf-8")
            real_save(artifact_dir, manifest)

        monkeypatch.setattr(v, "_save_manifest", _racing)
        with pytest.raises(v.VersionError, match="canonical name"):
            v.create_tree_version(skill_dir, [("SKILL.md", b"x")])

        monkeypatch.undo()
        # The snapshot survives as an orphan, and the retry (after the user
        # renames the stray file) allocates the NEXT tag rather than wedging.
        assert (skill_dir / "versions" / "v1" / "SKILL.md").read_bytes() == b"x"
        (skill_dir / "Versions.JSON").unlink()
        assert v.create_tree_version(skill_dir, [("SKILL.md", b"y")]).tag == "v2"
