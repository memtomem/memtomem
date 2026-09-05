"""ADR-0037 — context artifact bundle export/receipt engine.

Each test names the mutation it fails under, because a guard over untrusted
input is only worth what it refuses. Secret fixtures are assembled at runtime:
a literal token in the source tree is refused by GitHub push protection and,
worse, makes this very file unindexable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import click
import pytest

from memtomem.context.bundle import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    _HAS_DIR_FD,
    BundleFormatError,
    BundleIntegrityError,
    BundlePrivacyError,
    BundleSourceError,
    _KNOWN_VERSION_SCHEMAS,
    _SourceReader,
    _collides,
    _read_source_file,
    _structure_digest,
    export_artifact_bundle,
    load_bundle,
    receive_artifact_bundle,
)
from memtomem.context.lockfile import Lockfile
from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.transfer import TransferCollisionError
from memtomem.context.versioning import SCHEMA_VERSION

# Assembled at runtime — never a literal in the tree.
SECRET = "api_key=" + "AKIA" + "TESTKEY" + "1234567890"


def _write_skill(root: Path, name: str = "demo", *, body: str | None = None) -> Path:
    art = root / ".memtomem" / "skills" / name
    (art / "references").mkdir(parents=True)
    (art / "SKILL.md").write_text(body or f"---\nname: {name}\n---\nbody\n", newline="\n")
    (art / "references" / "a.md").write_text("notes\n", newline="\n")
    return art


def _write_agent(root: Path, name: str = "demo", *, body: str | None = None) -> Path:
    art = root / ".memtomem" / "agents" / name
    art.mkdir(parents=True)
    (art / "agent.md").write_text(body or f"---\nname: {name}\n---\nbody\n", newline="\n")
    return art


def _write_versions(art: Path, *, tags: dict[str, str] | None = None, labels: dict | None = None):
    tags = tags or {"v1": "---\nname: demo\n---\nold\n"}
    vdir = art / "versions"
    vdir.mkdir(exist_ok=True)
    for tag, text in tags.items():
        (vdir / f"{tag}.md").write_text(text, newline="\n")
    manifest = {
        "schema_version": 1,
        "versions": {tag: {"created_at": "2026-01-01T00:00:00Z", "note": ""} for tag in tags},
        "labels": labels or {},
    }
    (art / "versions.json").write_text(json.dumps(manifest), newline="\n")


def _export(root: Path, out: Path, *, kind="skills", name="demo", **kw):
    return export_artifact_bundle(
        kind, name, src_project_root=root, from_scope="project_shared", out_path=out, **kw
    )


def _reseal(doc: dict) -> dict:
    """Recompute the structure digest after mutating a bundle document."""
    doc["payload_sha256"] = _structure_digest(
        doc["kind"],
        doc["name"],
        [(f["path"], f["exec"], f["sha256"]) for f in doc["files"]],
        doc["dirs"],
    )
    return doc


def _set_entry(doc: dict, rel: str, data: bytes) -> dict:
    for entry in doc["files"]:
        if entry["path"] == rel:
            entry["content_b64"] = base64.b64encode(data).decode()
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            return _reseal(doc)
    raise AssertionError(f"no entry {rel}")


class TestExport:
    def test_carries_wide_surface_sorted_and_sealed(self, tmp_path: Path) -> None:
        """Versions, overrides and nested files all travel, sorted, under one digest.

        Fails if the walker narrows to the payload-only surface, or drops the
        sort, or seals with the content-only ADR-0030 digest.
        """
        root = tmp_path / "p"
        art = _write_skill(root)
        (art / "overrides").mkdir()
        (art / "overrides" / "claude.md").write_text("vendor\n", newline="\n")
        _write_versions(art)
        out = tmp_path / "b.json"

        result = _export(root, out)

        doc = json.loads(out.read_text())
        paths = [f["path"] for f in doc["files"]]
        assert paths == sorted(paths)
        assert {
            "SKILL.md",
            "references/a.md",
            "overrides/claude.md",
            "versions/v1.md",
            "versions.json",
        } <= set(paths)
        assert doc["versions_included"] is True
        assert result.file_count == len(paths)
        assert doc["payload_sha256"] == _structure_digest(
            "skills",
            "demo",
            [(f["path"], f["exec"], f["sha256"]) for f in doc["files"]],
            doc["dirs"],
        )

    def test_empty_directory_is_carried_not_dropped(self, tmp_path: Path) -> None:
        """Fails if `dirs` is removed: the payload writer only makes file parents."""
        root = tmp_path / "p"
        art = _write_skill(root)
        (art / "fixtures").mkdir()
        out = tmp_path / "b.json"

        _export(root, out)

        assert json.loads(out.read_text())["dirs"] == ["fixtures"]

    def test_flat_source_lands_as_the_dir_manifest(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        store = root / ".memtomem" / "agents"
        store.mkdir(parents=True)
        (store / "demo.md").write_text("---\nname: demo\n---\nbody\n", newline="\n")
        out = tmp_path / "b.json"

        result = _export(root, out, kind="agents")

        assert [f["path"] for f in json.loads(out.read_text())["files"]] == ["agent.md"]
        assert any("flat-layout" in note for note in result.notes)

    def test_skips_copier_reserved_names_at_every_depth(self, tmp_path: Path) -> None:
        """Fails if exclusion is top-level only, or moves after the type check."""
        root = tmp_path / "p"
        art = _write_skill(root)
        (art / "references" / "__pycache__").mkdir()
        (art / "references" / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (art / "SKILL.md.bak").write_text("stale\n", newline="\n")
        out = tmp_path / "b.json"

        result = _export(root, out)

        paths = [f["path"] for f in json.loads(out.read_text())["files"]]
        assert not any("__pycache__" in p or p.endswith(".bak") for p in paths)
        assert any(".bak" in note for note in result.notes)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_symlink_is_refused_loudly_not_skipped(self, tmp_path: Path) -> None:
        """A skipped link is a silent drop the sender never learns about.

        Fails if the walker reverts to `iter_installed_files`, which warns and
        continues.
        """
        root = tmp_path / "p"
        art = _write_skill(root)
        (art / "link.md").symlink_to(art / "SKILL.md")
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="symlink"):
            _export(root, out)
        assert not out.exists()

    def test_reads_each_source_file_exactly_once(self, tmp_path: Path, monkeypatch) -> None:
        """Scanned bytes must be the encoded bytes; a re-read reopens the TOCTOU.

        Fails if export scans the tree from disk and then re-reads for encoding.
        """
        root = tmp_path / "p"
        _write_skill(root)
        out = tmp_path / "b.json"
        opened: list[str] = []
        real_open = os.open

        def counting_open(path, flags, *a, **kw):
            if str(path).endswith(("SKILL.md", "a.md")):
                opened.append(str(path))
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(os, "open", counting_open)
        _export(root, out)

        assert len(opened) == len(set(opened)) == 2

    def test_secret_in_frozen_snapshot_refuses_with_zero_residue(self, tmp_path: Path) -> None:
        """Fails if the scan is dropped, or scoped to the source tier."""
        root = tmp_path / "p"
        art = _write_skill(root)
        _write_versions(art, tags={"v1": f"---\nname: demo\n---\n{SECRET}\n"})
        out = tmp_path / "b.json"

        with pytest.raises(PrivacyBlockedError) as excinfo:
            _export(root, out)

        assert not out.exists()
        assert not list(out.parent.glob(".*tmp"))
        assert "versions/v1.md" in excinfo.value.message
        assert SECRET not in excinfo.value.message

    def test_user_tier_source_is_refused_like_any_other(self, tmp_path: Path, monkeypatch) -> None:
        """The gate does not follow the source tier (ADR-0037 §4)."""
        home = tmp_path / "home"
        art = home / ".memtomem" / "skills" / "demo"
        art.mkdir(parents=True)
        (art / "SKILL.md").write_text(f"---\nname: demo\n---\n{SECRET}\n", newline="\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        out = tmp_path / "b.json"

        with pytest.raises(PrivacyBlockedError):
            export_artifact_bundle(
                "skills", "demo", src_project_root=None, from_scope="user", out_path=out
            )
        assert not out.exists()

    def test_export_exposes_no_privacy_bypass(self) -> None:
        """What enforces §4 is the ABSENCE of a valve, not the scan's scope value.

        The scan's scope decides whether a per-file declaration is honored,
        not whether a hit refuses: with ``force_unsafe`` never set, any
        uncovered hit blocks at every scope. So what enforces "no valve" is that
        neither the engine nor the CLI offers one. Fails the moment someone adds
        ``--force-unsafe`` to export.
        """
        import inspect

        from memtomem.cli.context_cmd import export_cmd

        assert "force_unsafe" not in inspect.signature(export_artifact_bundle).parameters
        option_names = {opt for param in export_cmd.params for opt in getattr(param, "opts", [])}
        assert not any("unsafe" in name for name in option_names)

    def test_export_does_not_scan_at_project_shared_scope(self, tmp_path) -> None:
        """The scope choice is observable now, so it gets a behavioral pin.

        ``project_shared`` refuses a per-file declaration outright, so scanning
        there would make the declaration below inert and refuse every real
        artifact. The two declaration tests that follow are the positive half of
        this witness; this one states the negative directly so the reason is not
        buried in a passing case.
        """
        from memtomem.context import bundle as bundle_mod

        assert bundle_mod._EGRESS_SCAN_SCOPE != "project_shared"

    def test_documented_credential_shapes_export_under_the_manifest_declaration(
        self, tmp_path: Path
    ) -> None:
        """A skill ABOUT API code must be shareable; a skill carrying a key must not.

        Real-data finding: every skill on the author's machine was refused on
        `api_key: str` annotations alone, so the declaration is the difference
        between a usable feature and none. Fails if the declaration stops being
        read from the manifest, or stops covering the artifact's other files.
        """
        root = tmp_path / "p"
        art = _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                "Settings carry an api_key: str field.\n"
            ),
        )
        (art / "scripts").mkdir()
        (art / "scripts" / "conf.py").write_text("secret_key: str = ...\n", newline="\n")
        out = tmp_path / "b.json"

        result = _export(root, out)

        assert result.file_count == 3
        assert out.exists()

    def test_every_exempted_file_is_disclosed_on_both_sides(self, tmp_path: Path) -> None:
        """The declaration is artifact-wide, so what it waived must be visible.

        It lives in the manifest but waives hits in files the author may not
        have re-read, and the waived class includes a plain `password=<value>`.
        Disclosure is what keeps that from being silent. Fails if the list stops
        being recorded, or stops travelling to the receiver.
        """
        root = tmp_path / "p"
        art = _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                "Documents an api_key: str field.\n"
            ),
        )
        (art / "config.env").write_text("password=" + "hunter2" + "\n", newline="\n")
        out = tmp_path / "b.json"

        result = _export(root, out)

        assert result.redaction_exempted == ["SKILL.md", "config.env"]
        doc = json.loads(out.read_text())
        assert doc["redaction_exempted"] == ["SKILL.md", "config.env"]
        # The field is the single carrier; each surface words its own warning,
        # so the same sentence never prints twice on one terminal.
        assert not any("documents-patterns" in note for note in result.notes)
        assert load_bundle(out).redaction_exempted == ["SKILL.md", "config.env"]

    def test_the_declaration_never_waives_a_real_token(self, tmp_path: Path) -> None:
        """The ceiling is unchanged: label rules only, all-or-nothing per file.

        Fails if the declaration is ever widened past `exemption_covers`.
        """
        root = tmp_path / "p"
        _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                f"{SECRET}\ntoken=sk-" + "a" * 24 + "\n"
            ),
        )
        out = tmp_path / "b.json"

        with pytest.raises(PrivacyBlockedError):
            _export(root, out)
        assert not out.exists()

    def test_export_refusal_does_not_claim_a_project_shared_write(self, tmp_path: Path) -> None:
        """The scan does not run at that scope, so the message must not say so.

        Fails if export goes back to borrowing `raise_or_collect`'s
        project_shared wording for a refusal that is about leaving the machine.
        """
        root = tmp_path / "p"
        _write_skill(root, body=f"---\nname: demo\n---\n{SECRET}\n")
        out = tmp_path / "b.json"

        with pytest.raises(PrivacyBlockedError) as excinfo:
            _export(root, out)

        assert "leaves this machine" in excinfo.value.message
        assert "scope='project_shared'" not in excinfo.value.message
        assert "documents-patterns" in excinfo.value.message

    def test_no_versions_drops_both_and_lifts_the_health_rules(self, tmp_path: Path) -> None:
        """--no-versions must be a usable remedy, not advice that cannot be followed.

        Fails if version paths are excluded after the walk instead of before it.
        """
        root = tmp_path / "p"
        art = _write_skill(root)
        _write_versions(art, tags={"v1": f"---\nname: demo\n---\n{SECRET}\n"})
        out = tmp_path / "b.json"

        result = _export(root, out, include_versions=False)

        doc = json.loads(out.read_text())
        paths = {f["path"] for f in doc["files"]}
        assert not any(p.startswith("versions") for p in paths)
        assert doc["versions_included"] is False
        assert result.versions_included is False

    def test_orphan_snapshot_refuses_and_never_says_delete(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        art = _write_skill(root)
        _write_versions(art)
        (art / "versions" / "v2.md").write_text("orphan\n", newline="\n")
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError) as excinfo:
            _export(root, out)

        assert "v2.md" in str(excinfo.value)
        assert "only copy" in str(excinfo.value)
        assert "delete" not in str(excinfo.value).lower()

    def test_dangling_label_refuses(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        art = _write_skill(root)
        _write_versions(art, labels={"production": "v9"})
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="v9"):
            _export(root, out)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
    def test_symlinked_artifact_root_is_refused(self, tmp_path: Path) -> None:
        """A linked root reads a tree the canonical store never held.

        Fails if the walk only type-checks children: the root itself is the
        entry `_detect_source_scope` and the manifest probe both follow.
        """
        outside = tmp_path / "outside" / "demo"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", newline="\n")
        (outside / "elsewhere.md").write_text("not in the store\n", newline="\n")
        store = tmp_path / "p" / ".memtomem" / "skills"
        store.mkdir(parents=True)
        (store / "demo").symlink_to(outside)
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="symlink"):
            _export(tmp_path / "p", out)
        assert not out.exists()

    def test_snapshot_naming_another_artifact_is_refused(self, tmp_path: Path) -> None:
        """A frozen snapshot steers a labeled fan-out through the name inside IT.

        The working-manifest rewrite never touches it, so the check is the only
        thing standing between a bundle and writing someone else's runtime
        target. Fails if the snapshot-name rule is dropped.
        """
        root = tmp_path / "p"
        art = _write_agent(root)
        _write_versions(art, tags={"v1": "---\nname: victim\n---\nold\n"})
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="victim"):
            _export(root, out, kind="agents")

    def test_snapshot_without_a_name_key_is_accepted(self, tmp_path: Path) -> None:
        """It carries no name to inject; the fallback resolves to the artifact.

        Fails if the check starts parsing snapshots by their own path, which
        would resolve every one of them to `v1` and refuse healthy artifacts.
        """
        root = tmp_path / "p"
        art = _write_agent(root)
        _write_versions(art, tags={"v1": "---\ndescription: old\n---\nbody\n"})
        out = tmp_path / "b.json"

        assert _export(root, out, kind="agents").versions_included is True

    def test_tree_snapshot_is_refused_for_a_flat_kind(self, tmp_path: Path) -> None:
        """Agent sync cannot resolve one, so it must not land and fail later."""
        root = tmp_path / "p"
        art = _write_agent(root)
        (art / "versions" / "v1").mkdir(parents=True)
        (art / "versions" / "v1" / "agent.md").write_text(
            "---\nname: demo\n---\nold\n", newline="\n"
        )
        (art / "versions.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "versions": {"v1": {"created_at": "2026-01-01T00:00:00Z", "layout": "tree"}},
                    "labels": {},
                }
            ),
            newline="\n",
        )
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="tree snapshot"):
            _export(root, out, kind="agents")

    def test_unreadable_version_schema_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        art = _write_skill(root)
        _write_versions(art)
        manifest = json.loads((art / "versions.json").read_text())
        manifest["schema_version"] = 999
        (art / "versions.json").write_text(json.dumps(manifest), newline="\n")
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="schema_version"):
            _export(root, out)

    def test_out_path_inside_the_artifact_is_refused(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        art = _write_skill(root)

        with pytest.raises(BundleSourceError, match="inside the artifact"):
            _export(root, art / "self.json")

    def test_existing_out_path_is_never_overwritten(self, tmp_path: Path) -> None:
        root = tmp_path / "p"
        _write_skill(root)
        out = tmp_path / "b.json"
        out.write_text("previous\n", newline="\n")

        with pytest.raises(BundleSourceError, match="already exists"):
            _export(root, out)
        assert out.read_text() == "previous\n"

    def test_wiki_commit_only_travels_when_clean_and_pinned(self, tmp_path: Path) -> None:
        """Fails if the lockfile entry is copied without the carry gate."""
        root = tmp_path / "p"
        _write_skill(root)
        out = tmp_path / "b.json"

        _export(root, out)

        assert json.loads(out.read_text())["source"]["wiki_commit"] is None


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    _write_skill(root)
    out = tmp_path / "b.json"
    _export(root, out)
    return out


@pytest.fixture()
def dst(tmp_path: Path) -> Path:
    root = tmp_path / "dst"
    (root / ".memtomem").mkdir(parents=True)
    return root


def _mutate(bundle: Path, tmp_path: Path, mutate, name: str = "m") -> Path:
    doc = json.loads(bundle.read_text())
    mutate(doc)
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(doc), newline="\n")
    return path


class TestReaderRefusals:
    """Every shape the grammar refuses, pinned as a case (ADR-0037 §2, §7)."""

    def test_tampered_content_fails_the_entry_digest(self, bundle, tmp_path) -> None:
        bad = _mutate(
            bundle,
            tmp_path,
            lambda d: d["files"][0].update(content_b64=base64.b64encode(b"evil").decode()),
        )
        with pytest.raises(BundleIntegrityError, match="digest"):
            load_bundle(bad)

    def test_swapped_identity_fails_the_structure_digest(self, bundle, tmp_path) -> None:
        """kind and name are inside the digest because both steer where bytes land."""
        bad = _mutate(bundle, tmp_path, lambda d: d.update(name="other"))
        with pytest.raises(BundleIntegrityError, match="structure"):
            load_bundle(bad)

    def test_reordered_entries_fail_the_structure_digest(self, bundle, tmp_path) -> None:
        bad = _mutate(bundle, tmp_path, lambda d: d["files"].reverse())
        with pytest.raises(BundleFormatError):
            load_bundle(bad)

    @pytest.mark.parametrize(
        "path",
        [
            "../escape.md",
            "/abs.md",
            "a\\b.md",
            "SKILL.md:stream",
            "CON.md",
            "nul",
            "sub/./x.md",
            "x" * 70 + ".md",
            ".git",
            "notes.bak",
            "é.md",
        ],
    )
    def test_path_outside_the_allowlist_is_refused(self, bundle, tmp_path, path) -> None:
        """One allowlist, no tail of forgotten refusals — and never platform-conditional.

        The bundle is re-sealed after the mutation so a passing digest cannot be
        what refuses it: the assertion below rejects ``BundleIntegrityError``
        explicitly, or this test would go green with the path rules deleted.
        """

        def mutate(doc, p=path):
            doc["files"][0]["path"] = p
            doc["files"].sort(key=lambda e: e["path"])
            _reseal(doc)

        bad = _mutate(bundle, tmp_path, mutate)
        with pytest.raises(BundleFormatError) as excinfo:
            load_bundle(bad)
        assert not isinstance(excinfo.value, BundleIntegrityError)

    @pytest.mark.parametrize("suffix", ["evil.md\n", ".git\n", "trailing.\n"])
    def test_trailing_newline_never_slips_past_the_grammar(self, bundle, tmp_path, suffix) -> None:
        """`match` plus a `$` anchor accepts a trailing newline; `fullmatch` does not.

        Every one of these was ACCEPTED before the anchors were fixed, which
        made the ASCII allowlist, the trailing-dot rule and the forbidden-name
        rule all bypassable with one character. Fails if any regex here goes
        back to `match`.
        """

        def mutate(doc, s=suffix):
            doc["files"][0]["path"] = s
            doc["files"].sort(key=lambda e: e["path"])
            _reseal(doc)

        with pytest.raises(BundleFormatError) as excinfo:
            load_bundle(_mutate(bundle, tmp_path, mutate))
        assert not isinstance(excinfo.value, BundleIntegrityError)

    def test_impossible_timestamp_is_refused(self, bundle, tmp_path) -> None:
        """The shape regex accepts 2026-99-99T99:99:99Z; only a real parse does not."""
        bad = _mutate(bundle, tmp_path, lambda d: d.update(exported_at="2026-99-99T99:99:99Z"))
        with pytest.raises(BundleFormatError, match="real instant"):
            load_bundle(bad)

    def test_case_folded_duplicate_is_refused_on_every_platform(self, bundle, tmp_path) -> None:
        """Asserted on Linux CI too: the rule must not be sys.platform-conditional."""

        def mutate(doc):
            doc["files"].append({**doc["files"][0], "path": "skill.md"})
            doc["files"].sort(key=lambda e: e["path"])
            _reseal(doc)

        with pytest.raises(BundleFormatError, match="case folding"):
            load_bundle(_mutate(bundle, tmp_path, mutate))

    def test_file_prefixing_a_directory_is_refused(self, bundle, tmp_path) -> None:
        bad = _mutate(bundle, tmp_path, lambda d: d["dirs"].append("SKILL.md/inner"))
        with pytest.raises(BundleFormatError):
            load_bundle(bad)

    def test_unknown_per_entry_key_is_refused(self, bundle, tmp_path) -> None:
        """Top-level tolerance is forward compat; an entry is the security unit."""
        bad = _mutate(bundle, tmp_path, lambda d: d["files"][0].update(mode=0o777))
        with pytest.raises(BundleFormatError, match="unknown key"):
            load_bundle(bad)

    def test_unknown_top_level_key_is_tolerated(self, bundle, tmp_path) -> None:
        ok = _mutate(bundle, tmp_path, lambda d: d.update(future_hint="ignored"))
        assert load_bundle(ok).name == "demo"

    def test_duplicate_json_key_is_refused(self, bundle, tmp_path) -> None:
        text = bundle.read_text().replace('"kind"', '"kind": "skills", "kind"', 1)
        bad = tmp_path / "dup.json"
        bad.write_text(text, newline="\n")
        with pytest.raises(BundleFormatError, match="duplicate"):
            load_bundle(bad)

    def test_memory_export_bundle_is_named_and_redirected(self, tmp_path) -> None:
        bad = tmp_path / "mem.json"
        bad.write_text(json.dumps({"version": "2", "total_chunks": 0, "chunks": []}), newline="\n")
        with pytest.raises(BundleFormatError, match="mem_import"):
            load_bundle(bad)

    def test_future_version_is_refused(self, bundle, tmp_path) -> None:
        bad = _mutate(bundle, tmp_path, lambda d: d.update(version=BUNDLE_VERSION + 1))
        with pytest.raises(BundleFormatError, match="not readable"):
            load_bundle(bad)

    def test_wrong_manifest_for_the_kind_is_refused(self, bundle, tmp_path) -> None:
        bad = _mutate(bundle, tmp_path, lambda d: d.update(kind="agents"))
        with pytest.raises(BundleFormatError):
            load_bundle(bad)

    def test_unknown_override_vendor_is_refused(self, bundle, tmp_path) -> None:
        def add(d):
            entry = {**d["files"][0], "path": "overrides/evil.sh"}
            d["files"] = sorted([*d["files"], entry], key=lambda e: e["path"])
            _reseal(d)

        with pytest.raises(BundleFormatError, match="override"):
            load_bundle(_mutate(bundle, tmp_path, add))

    def test_versions_included_must_match_the_payload(self, bundle, tmp_path) -> None:
        bad = _mutate(bundle, tmp_path, lambda d: d.update(versions_included=True))
        with pytest.raises(BundleFormatError, match="versions_included"):
            load_bundle(bad)

    def test_oversize_is_refused_before_any_parse(self, tmp_path, monkeypatch) -> None:
        from memtomem.context import bundle as bundle_mod

        monkeypatch.setattr(bundle_mod, "_MAX_BUNDLE_BYTES", 16)
        fat = tmp_path / "fat.json"
        fat.write_text(json.dumps({"format": BUNDLE_FORMAT, "version": 1}) + " " * 64, newline="\n")
        with pytest.raises(BundleFormatError, match="cap"):
            load_bundle(fat)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
    def test_fifo_input_is_refused_without_blocking(self, tmp_path) -> None:
        """A stat-then-read pair blocks here forever; the fstat check catches it."""
        fifo = tmp_path / "pipe.json"
        os.mkfifo(fifo)
        with pytest.raises(BundleFormatError, match="regular file"):
            load_bundle(fifo)

    def test_provenance_must_be_present_and_null(self, bundle, tmp_path) -> None:
        """Reserved means exactly null, not "anything, ignored" (§3).

        Accepting a marker-shaped value would hand a later reader something this
        reader never validated, and would let a bundle carry a signature under a
        scheme nothing here checks. Fails if the key becomes optional or lax.
        """
        marker = _mutate(
            bundle,
            tmp_path,
            lambda d: d.update(
                provenance={
                    "scheme": "memtomem-bundle-provenance-v1",
                    "algo": "HMAC-SHA256",
                    "signature": "00" * 32,
                }
            ),
            "marker",
        )
        with pytest.raises(BundleFormatError, match="provenance"):
            load_bundle(marker)

        absent = _mutate(bundle, tmp_path, lambda d: d.pop("provenance"), "absent")
        with pytest.raises(BundleFormatError, match="provenance"):
            load_bundle(absent)

        assert load_bundle(bundle).name == "demo"


class TestReceipt:
    def test_round_trip_is_byte_identical_and_lands_untracked(self, tmp_path, dst) -> None:
        """Fails if receipt ever writes a lock.json pin from bundle-claimed data."""
        root = tmp_path / "src"
        art = _write_skill(root)
        (art / "empty").mkdir()
        out = tmp_path / "b.json"
        _export(root, out)

        result = receive_artifact_bundle(
            out, dst_project_root=dst, to_scope="project_local", apply_=True
        )

        landed = dst / ".memtomem" / "skills.local" / "demo"
        assert result.received is True
        assert (landed / "SKILL.md").read_bytes() == (art / "SKILL.md").read_bytes()
        assert (landed / "references" / "a.md").read_bytes() == (
            art / "references" / "a.md"
        ).read_bytes()
        assert (landed / "empty").is_dir()
        assert Lockfile.at(dst).read_entry("skills", "demo") is None

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_executable_bit_round_trips(self, tmp_path, dst) -> None:
        """mm context copy preserves it, so dropping it would strip a runnable script."""
        root = tmp_path / "src"
        art = _write_skill(root)
        (art / "run.sh").write_text("#!/bin/sh\n", newline="\n")
        (art / "run.sh").chmod(0o755)
        out = tmp_path / "b.json"
        _export(root, out)

        receive_artifact_bundle(out, dst_project_root=dst, to_scope="project_local", apply_=True)

        landed = dst / ".memtomem" / "skills.local" / "demo"
        assert landed.joinpath("run.sh").stat().st_mode & 0o111
        assert not landed.joinpath("SKILL.md").stat().st_mode & 0o111

    def test_dry_run_touches_nothing(self, bundle, dst) -> None:
        result = receive_artifact_bundle(
            bundle, dst_project_root=dst, to_scope="project_local", apply_=False
        )
        assert result.received is False
        assert not (dst / ".memtomem" / "skills.local").exists()

    def test_scan_runs_before_any_disk_write(self, tmp_path, dst, monkeypatch) -> None:
        """The staged tree must never exist when the scan refuses.

        Fails the moment the scan moves after `write_tree_payload` — which is
        exactly what the transfer engine does, and what receipt must not.
        """
        from memtomem.context import bundle as bundle_mod

        # Export refuses a secret, so build the bundle from a clean artifact and
        # substitute the secret-bearing manifest afterwards.
        out = tmp_path / "b.json"
        clean = tmp_path / "clean"
        _write_skill(clean)
        _export(clean, out)
        doc = _set_entry(
            json.loads(out.read_text()), "SKILL.md", f"---\nname: demo\n---\n{SECRET}\n".encode()
        )
        tainted = tmp_path / "tainted.json"
        tainted.write_text(json.dumps(doc), newline="\n")

        called: list[object] = []
        monkeypatch.setattr(
            bundle_mod,
            "write_tree_payload",
            lambda *a, **kw: called.append(a),
        )

        with pytest.raises(BundlePrivacyError):
            receive_artifact_bundle(
                tainted, dst_project_root=dst, to_scope="project_local", apply_=True
            )

        assert called == []
        assert not list((dst / ".memtomem").rglob(".staging-*"))

    def test_project_shared_secret_refuses_with_no_valve_and_no_residue(
        self, tmp_path, dst
    ) -> None:
        """force_unsafe must not widen the tier: ADR-0011 §5 has no bypass here."""
        clean = tmp_path / "clean"
        _write_skill(clean)
        out = tmp_path / "b.json"
        _export(clean, out)
        doc = _set_entry(
            json.loads(out.read_text()), "SKILL.md", f"---\nname: demo\n---\n{SECRET}\n".encode()
        )
        tainted = tmp_path / "tainted.json"
        tainted.write_text(json.dumps(doc), newline="\n")

        with pytest.raises(PrivacyBlockedError) as excinfo:
            receive_artifact_bundle(
                tainted,
                dst_project_root=dst,
                to_scope="project_shared",
                apply_=True,
                force_unsafe=True,
            )

        assert SECRET not in excinfo.value.message
        assert ".staging-" not in excinfo.value.message
        assert not (dst / ".memtomem" / "skills" / "demo").exists()
        assert not list((dst / ".memtomem").rglob(".staging-*"))

    def test_user_tier_honours_the_valve_both_ways(self, tmp_path, dst) -> None:
        """Blocked by default (ingress precedent), admitted after review."""
        clean = tmp_path / "clean"
        _write_skill(clean)
        out = tmp_path / "b.json"
        _export(clean, out)
        doc = _set_entry(
            json.loads(out.read_text()), "SKILL.md", f"---\nname: demo\n---\n{SECRET}\n".encode()
        )
        tainted = tmp_path / "tainted.json"
        tainted.write_text(json.dumps(doc), newline="\n")

        with pytest.raises(BundlePrivacyError) as excinfo:
            receive_artifact_bundle(
                tainted, dst_project_root=dst, to_scope="project_local", apply_=True
            )
        # The engine states the condition and carries the reason code; the flag
        # spelling is the CLI's (#1869), pinned in test_cli_context_bundle.py.
        assert "secret-shaped value" in excinfo.value.message
        assert "--force-unsafe" not in excinfo.value.message
        assert excinfo.value.code == "privacy_blocked"

        result = receive_artifact_bundle(
            tainted,
            dst_project_root=dst,
            to_scope="project_local",
            apply_=True,
            force_unsafe=True,
        )
        assert result.received is True

    def test_a_stripped_disclosure_is_refused_not_believed(self, tmp_path, dst) -> None:
        """`redaction_exempted` is outside `payload_sha256`, so it can be stripped.

        Re-deriving it on receipt is the only thing that keeps the receiver from
        being told "nothing was waived" while the waived bytes land anyway.
        Fails if receipt ever renders the wire list instead of its own scan.
        """
        root = tmp_path / "src"
        art = _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                "Documents an api_key: str field.\n"
            ),
        )
        (art / "config.env").write_text("password=" + "hunter2" + "\n", newline="\n")
        out = tmp_path / "b.json"
        _export(root, out)

        doc = json.loads(out.read_text())
        assert doc["redaction_exempted"]
        doc["redaction_exempted"] = []
        stripped = tmp_path / "stripped.json"
        stripped.write_text(json.dumps(doc), newline="\n")

        with pytest.raises(BundleFormatError, match="redaction_exempted"):
            receive_artifact_bundle(
                stripped, dst_project_root=dst, to_scope="project_local", apply_=True
            )
        assert not (dst / ".memtomem" / "skills.local" / "demo").exists()

    def test_declaration_travels_and_is_honored_on_arrival(self, tmp_path, dst) -> None:
        """Export and receipt must agree, or a bundle exports and refuses to land.

        Fails if receipt stops reading the declaration from the manifest bytes.
        """
        root = tmp_path / "src"
        _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                "Settings carry an api_key: str field.\n"
            ),
        )
        out = tmp_path / "b.json"
        _export(root, out)

        result = receive_artifact_bundle(
            out, dst_project_root=dst, to_scope="project_local", apply_=True
        )

        assert result.received is True

    def test_the_declaration_does_not_open_project_shared(self, tmp_path, dst) -> None:
        """ADR-0011 §5's ceiling applies to the declaration exactly as to the flag."""
        root = tmp_path / "src"
        _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                "Settings carry an api_key: str field.\n"
            ),
        )
        out = tmp_path / "b.json"
        _export(root, out)

        with pytest.raises(PrivacyBlockedError):
            receive_artifact_bundle(
                out, dst_project_root=dst, to_scope="project_shared", apply_=True
            )
        assert not (dst / ".memtomem" / "skills" / "demo").exists()

    def test_collision_refuses_on_both_layout_spellings(self, bundle, dst) -> None:
        """A directory landing would otherwise silently shadow a legacy flat file."""
        store = dst / ".memtomem" / "skills.local"
        store.mkdir(parents=True)
        (store / "demo.md").write_text("legacy\n", newline="\n")

        with pytest.raises(TransferCollisionError):
            receive_artifact_bundle(
                bundle, dst_project_root=dst, to_scope="project_local", apply_=True
            )
        assert (store / "demo.md").read_text() == "legacy\n"

    def test_rename_rewrites_only_the_working_manifest(self, tmp_path, dst) -> None:
        root = tmp_path / "src"
        art = _write_skill(root)
        (art / "overrides").mkdir()
        (art / "overrides" / "claude.md").write_text("name: demo\n", newline="\n")
        out = tmp_path / "b.json"
        _export(root, out)

        result = receive_artifact_bundle(
            out, dst_project_root=dst, to_scope="project_local", apply_=True, new_name="demo2"
        )

        landed = dst / ".memtomem" / "skills.local" / "demo2"
        assert "name: demo2" in (landed / "SKILL.md").read_text()
        assert (landed / "overrides" / "claude.md").read_text() == "name: demo\n"
        assert any("overrides" in note for note in result.notes)

    def test_manifest_name_is_normalized_even_without_rename(self, tmp_path, dst) -> None:
        """Fan-out keys on the parsed name, so the landed bytes must agree with the path.

        Fails if the rewrite is applied only under `--as`.
        """
        root = tmp_path / "src"
        store = root / ".memtomem" / "agents"
        store.mkdir(parents=True)
        art = store / "demo"
        art.mkdir()
        (art / "agent.md").write_text("---\nname: victim\n---\nbody\n", newline="\n")
        out = tmp_path / "b.json"
        _export(root, out, kind="agents")

        receive_artifact_bundle(out, dst_project_root=dst, to_scope="project_local", apply_=True)

        landed = dst / ".memtomem" / "agents.local" / "demo" / "agent.md"
        assert "name: demo" in landed.read_text()
        assert "victim" not in landed.read_text()

    def test_renaming_an_agent_bundle_with_history_is_refused(self, tmp_path, dst) -> None:
        """A snapshot's own name would still steer a labeled fan-out."""
        root = tmp_path / "src"
        art = _write_agent(root)
        _write_versions(art)
        out = tmp_path / "b.json"
        _export(root, out, kind="agents")

        with pytest.raises(BundleFormatError, match="no-versions"):
            receive_artifact_bundle(
                out, dst_project_root=dst, to_scope="project_local", apply_=True, new_name="demo2"
            )

    def test_staging_name_is_one_discovery_skips(self, tmp_path, dst, monkeypatch) -> None:
        """A crash must not leave a phantom artifact the sync fan-out picks up."""
        from memtomem.context._names import is_internal_artifact_dir
        from memtomem.context import bundle as bundle_mod

        seen: list[str] = []
        real = bundle_mod._staging_path

        def spy(store, name):
            path = real(store, name)
            seen.append(path.name)
            return path

        monkeypatch.setattr(bundle_mod, "_staging_path", spy)
        root = tmp_path / "src"
        _write_skill(root)
        out = tmp_path / "b.json"
        _export(root, out)
        receive_artifact_bundle(out, dst_project_root=dst, to_scope="project_local", apply_=True)

        assert seen and all(is_internal_artifact_dir(name) for name in seen)

    def test_sync_follow_up_matches_the_destination_tier(self, bundle, dst) -> None:
        local = receive_artifact_bundle(
            bundle, dst_project_root=dst, to_scope="project_local", apply_=False
        )
        assert local.needs_sync is False and local.sync_command is None

        shared = receive_artifact_bundle(
            bundle, dst_project_root=dst, to_scope="project_shared", apply_=False
        )
        assert shared.needs_sync is True
        assert "mm context sync --scope project_shared" in (shared.sync_command or "")

    def test_adopt_hint_only_for_a_same_name_shared_landing(self, bundle, dst) -> None:
        """A renamed import would point adopt at a different wiki asset entirely."""
        plain = receive_artifact_bundle(
            bundle, dst_project_root=dst, to_scope="project_shared", apply_=False
        )
        assert plain.adopt_hint is None  # no wiki_commit on this source


class TestExportReadPath:
    """The descriptor discipline export claims, pinned as behavior."""

    @pytest.mark.skipif(
        not _HAS_DIR_FD,
        reason=(
            "the refusal is descriptor-based, and this platform has no dir_fd — "
            "ADR-0037 §4 states the guarantee as POSIX-strength and degrades the "
            "traversal to paths, where O_NOFOLLOW is 0 and the link is followed"
        ),
    )
    def test_a_directory_swapped_for_a_symlink_after_the_walk_is_refused(self, tmp_path) -> None:
        """The walk vets `sub/` by name; the read must not trust that vetting.

        Fails if the reader goes back to opening `root / rel` by path: only the
        final component carries O_NOFOLLOW there, so an ancestor repointed
        after the walk hands the bundle bytes from outside the artifact.
        """
        root = tmp_path / "art"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a.md").write_text("real\n", newline="\n")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "a.md").write_text("stolen\n", newline="\n")

        with _SourceReader(root) as reader:
            assert reader.read("sub/a.md", budget=1024)[0] == b"real\n"
            (root / "sub").rename(tmp_path / "moved-away")
            (root / "sub").symlink_to(outside)
            with pytest.raises(BundleSourceError, match="symlink"):
                reader.read("sub/a.md", budget=1024)

    def test_reads_are_opened_in_binary_mode(self, tmp_path) -> None:
        """Windows `os.open` defaults to TEXT mode and rewrites CRLF to LF.

        Both readers must set O_BINARY, which is 0 on POSIX. Pinned on the flag
        rather than on bytes because the corruption it prevents is invisible on
        this platform: a CRLF round trip passes here either way, and the digests
        verify on Windows too, since they are computed over the already
        translated bytes. Fails if either open drops the flag.
        """
        import inspect

        from memtomem.context import bundle as bundle_mod

        for fn in (bundle_mod._read_capped, bundle_mod._read_source_file):
            src = inspect.getsource(fn)
            assert 'getattr(os, "O_BINARY", 0)' in src, (
                f"{fn.__name__} opens without O_BINARY; on Windows that reads "
                f"through the CRLF-translating text mode"
            )

    def test_a_crlf_payload_round_trips_byte_identical(self, tmp_path, dst) -> None:
        """The bytes on disk are the bytes in the bundle are the bytes that land."""
        root = tmp_path / "src"
        art = root / ".memtomem" / "skills" / "demo"
        art.mkdir(parents=True)
        raw = b"---\r\nname: demo\r\n---\r\nbody\r\n"
        (art / "SKILL.md").write_bytes(raw)
        out = tmp_path / "b.json"
        _export(root, out)

        receive_artifact_bundle(out, dst_project_root=dst, to_scope="project_local", apply_=True)

        landed = dst / ".memtomem" / "skills.local" / "demo" / "SKILL.md"
        assert landed.read_bytes() == raw

    def test_the_read_stops_at_the_cap_rather_than_loading_the_file(self, tmp_path) -> None:
        """Cap plus one byte is enough to refuse; more is the allocation the cap forbids.

        Fails if the reader drains the descriptor and leaves the size check to
        the caller.
        """
        big = tmp_path / "big.md"
        big.write_bytes(b"x" * 50_000)

        data, _ = _read_source_file(big, "big.md", budget=100)

        assert len(data) == 101

    def test_an_oversize_file_is_refused_by_export(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("memtomem.context.bundle._MAX_ENTRY_DECODED_BYTES", 64)
        root = tmp_path / "src"
        art = _write_skill(root)
        (art / "references" / "a.md").write_bytes(b"y" * 5000)
        out = tmp_path / "b.json"

        with pytest.raises(BundleSourceError, match="payload caps"):
            _export(root, out)
        assert not out.exists()


class TestTypedPathValidation:
    """A path's TYPE is part of what the version and form rules validate."""

    def test_a_directory_spelling_a_file_snapshot_is_refused(self, tmp_path, dst) -> None:
        """`versions/v1.md` as a DIRECTORY satisfied a file-layout record.

        Fails if files and dirs are merged into one list of names again: the
        receiver then gets a manifest whose snapshot resolve_version cannot
        read.
        """
        root = tmp_path / "src"
        art = _write_skill(root)
        _write_versions(art)
        out = tmp_path / "b.json"
        _export(root, out)

        def _swap(doc: dict) -> None:
            doc["files"] = [f for f in doc["files"] if f["path"] != "versions/v1.md"]
            doc["dirs"] = sorted([*doc["dirs"], "versions/v1.md"])
            _reseal(doc)

        bad = _mutate(out, tmp_path, _swap)
        with pytest.raises(BundleSourceError, match="directory"):
            load_bundle(bad)

    def test_an_empty_overrides_directory_round_trips(self, tmp_path, dst) -> None:
        """One path segment is the empty directory, not a malformed override.

        Fails if the override-name check is applied to directory entries.
        """
        root = tmp_path / "src"
        art = _write_skill(root)
        (art / "overrides").mkdir()
        out = tmp_path / "b.json"

        _export(root, out)

        assert "overrides" in json.loads(out.read_text())["dirs"]
        result = receive_artifact_bundle(
            out, dst_project_root=dst, to_scope="project_local", apply_=True
        )
        assert result.received is True
        assert (dst / ".memtomem" / "skills.local" / "demo" / "overrides").is_dir()

    def test_a_directory_standing_where_an_override_file_belongs_is_refused(self, tmp_path) -> None:
        root = tmp_path / "src"
        art = _write_skill(root)
        (art / "overrides").mkdir()
        (art / "overrides" / "claude.md").write_text("o\n", newline="\n")
        out = tmp_path / "b.json"
        _export(root, out)

        def _swap(doc: dict) -> None:
            doc["files"] = [f for f in doc["files"] if f["path"] != "overrides/claude.md"]
            doc["dirs"] = sorted([*doc["dirs"], "overrides/claude.md"])
            _reseal(doc)

        bad = _mutate(out, tmp_path, _swap)
        with pytest.raises(BundleFormatError, match="regular file"):
            load_bundle(bad)

    def test_the_stores_current_schema_version_is_exportable(self, tmp_path) -> None:
        """A literal set here becomes a second, stricter rule on the next bump.

        Pinned by BEHAVIOR at the store's own ceiling rather than by comparing
        the constant to itself: asserting the set equals `range(1, SCHEMA
        _VERSION + 1)` passes for a hardcoded `{1, 2}` too, for as long as the
        ceiling happens to be 2. This fails the moment SCHEMA_VERSION is bumped
        and the bundle set is not, which is the drift — export refusing, with a
        message claiming the store cannot read it, a manifest the store reads.
        """
        root = tmp_path / "src"
        art = _write_skill(root)
        _write_versions(art)
        manifest = json.loads((art / "versions.json").read_text())
        manifest["schema_version"] = SCHEMA_VERSION
        (art / "versions.json").write_text(json.dumps(manifest), newline="\n")
        out = tmp_path / "b.json"

        _export(root, out)

        assert load_bundle(out).versions_included is True
        assert SCHEMA_VERSION in _KNOWN_VERSION_SCHEMAS


class TestGrammarBounds:
    def test_case_variant_implicit_parents_are_refused(self, bundle, tmp_path) -> None:
        """`Docs/a.md` + `docs/b.md` merge into one directory on a folding filesystem.

        Neither parent is itself an entry, so a check over listed paths only
        finds no collision. Fails if implicit parents stop being folded.
        """

        def _add(doc: dict) -> None:
            for rel, text in (("Docs/a.md", b"one\n"), ("docs/b.md", b"two\n")):
                doc["files"].append(
                    {
                        "path": rel,
                        "exec": False,
                        "sha256": hashlib.sha256(text).hexdigest(),
                        "content_b64": base64.b64encode(text).decode(),
                    }
                )
            doc["files"].sort(key=lambda f: f["path"])
            _reseal(doc)

        bad = _mutate(bundle, tmp_path, _add)
        with pytest.raises(BundleFormatError, match="case folding"):
            load_bundle(bad)

    def test_deep_nesting_is_refused_before_the_parser_recurses(self, tmp_path) -> None:
        """json.loads recurses per level, and RecursionError is a RuntimeError.

        No CLI translator lists that, so it escaped as a raw traceback. Fails if
        the depth bound moves back to after the parse.
        """
        deep = tmp_path / "deep.json"
        deep.write_text("[" * 200_000, newline="\n")

        with pytest.raises(BundleFormatError, match="nesting"):
            load_bundle(deep)

    def test_a_non_utf8_manifest_is_refused_at_export(self, tmp_path) -> None:
        """It used to export cleanly and die on the receiver with a codec message.

        Fails if the manifest text check is dropped from the shared form rules,
        which is what makes export and receipt agree here.
        """
        root = tmp_path / "src"
        art = root / ".memtomem" / "skills" / "demo"
        art.mkdir(parents=True)
        (art / "SKILL.md").write_bytes("---\nname: demo\ndesc: caf\xe9\n---\n".encode("latin-1"))
        out = tmp_path / "b.json"

        with pytest.raises(BundleFormatError, match="UTF-8"):
            _export(root, out)
        assert not out.exists()


class TestReceiptContracts:
    def test_force_unsafe_import_keeps_the_declaration_disclosure(self, tmp_path, dst) -> None:
        """The valve decides ADMISSION; it does not answer what was waived.

        Reading coverage off the admission decision made every declared file
        re-derive as waiving nothing under the flag, so receipt refused the
        sender's honest disclosure as malformed — leaving such a bundle
        importable to no tier at all. Fails if the two questions collapse again.
        """
        root = tmp_path / "src"
        _write_skill(
            root,
            body=(
                "---\nname: demo\nredaction: documents-patterns\n---\n"
                "Settings carry an api_key: str field.\n"
            ),
        )
        out = tmp_path / "b.json"
        _export(root, out)
        assert json.loads(out.read_text())["redaction_exempted"] == ["SKILL.md"]

        result = receive_artifact_bundle(
            out,
            dst_project_root=dst,
            to_scope="project_local",
            apply_=True,
            force_unsafe=True,
        )

        assert result.received is True
        assert result.redaction_exempted == ["SKILL.md"]

    def test_as_with_the_artifact_s_own_name_is_not_a_rename(self, tmp_path, dst) -> None:
        """A scripted import always passes --as; the same name is a no-op.

        Fails if the rename consequences key on the flag being present rather
        than on the name differing.
        """
        root = tmp_path / "src"
        art = _write_agent(root)
        _write_versions(art)
        out = tmp_path / "b.json"
        _export(root, out, kind="agents")

        result = receive_artifact_bundle(
            out,
            dst_project_root=dst,
            to_scope="project_local",
            apply_=True,
            new_name="demo",
        )

        assert result.received is True
        assert result.dst_name == "demo"

    def test_an_already_correct_duplicate_name_line_still_imports(self, tmp_path, dst) -> None:
        """The store tolerates duplicate keys; export never inspects frontmatter.

        So such an artifact exported cleanly and then failed every import with a
        rename error for a rename nobody asked for. Fails if the ambiguity
        refusal runs before asking whether anything needs rewriting.
        """
        root = tmp_path / "src"
        _write_skill(root, body="---\nname: demo\nname: demo\n---\nbody\n")
        out = tmp_path / "b.json"
        _export(root, out)

        result = receive_artifact_bundle(
            out, dst_project_root=dst, to_scope="project_local", apply_=True
        )

        assert result.received is True

    def test_a_real_rename_of_an_ambiguous_manifest_is_still_refused(self, tmp_path, dst) -> None:
        """The refusal is about WHICH line to change, and that question is real here."""
        root = tmp_path / "src"
        _write_skill(root, body="---\nname: demo\nname: other\n---\nbody\n")
        out = tmp_path / "b.json"
        _export(root, out)

        with pytest.raises(click.ClickException, match="exactly one"):
            receive_artifact_bundle(
                out,
                dst_project_root=dst,
                to_scope="project_local",
                apply_=True,
                new_name="demo2",
            )

    def test_a_probe_that_cannot_answer_is_not_an_absence(self, tmp_path, monkeypatch) -> None:
        """EACCES while probing the legacy identity is not "nothing is there".

        Treating it as absent lands a directory that silently shadows the flat
        artifact — the exact case the collision check exists to catch.
        """
        store = tmp_path / "store"
        store.mkdir()

        def _denied(self: Path) -> object:
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "lstat", _denied)
        with pytest.raises(PermissionError):
            _collides(store, "demo")

    def test_a_missing_entry_is_still_an_absence(self, tmp_path) -> None:
        assert _collides(tmp_path, "nothing-here") is False

    def test_a_transfer_migrate_leftover_is_never_reaped(self, tmp_path, dst) -> None:
        """Classified is not deletable — reaping `.migrate-*` is data loss (#2304).

        `is_internal_artifact_dir` hides every internal transient, `.migrate-*`
        included, so a reaper keyed on classification alone would collect one.
        But `migrate._stage_move` renames the source into staging on the same
        filesystem, so until the promote that tree is the ONLY copy of the
        artifact. Fails if this reaper goes back to keying on the classified
        set instead of REAPABLE_INTERNAL_ARTIFACT_KINDS.
        """
        from memtomem.context._names import (
            REAPABLE_INTERNAL_ARTIFACT_KINDS,
            is_internal_artifact_dir,
        )

        root = tmp_path / "src"
        _write_agent(root)
        out = tmp_path / "b.json"
        _export(root, out, kind="agents")

        store = dst / ".memtomem" / "agents.local"
        store.mkdir(parents=True)
        migrate_leftover = store / ".migrate-demo-999-abcdef12.tmp"
        migrate_leftover.mkdir()
        (migrate_leftover / "agent.md").write_bytes(b"the only copy\n")

        # The premise: it IS classified, and it is NOT reapable.
        assert is_internal_artifact_dir(migrate_leftover.name)
        assert "migrate" not in REAPABLE_INTERNAL_ARTIFACT_KINDS

        receive_artifact_bundle(out, dst_project_root=dst, to_scope="project_local", apply_=True)

        assert migrate_leftover.is_dir()
        assert (migrate_leftover / "agent.md").read_bytes() == b"the only copy\n"

    def test_a_leftover_staging_tree_is_reaped_and_only_ours(self, tmp_path, dst) -> None:
        """Hidden from discovery is half a contract; something must delete these.

        The skills reaper returns immediately for any other kind, so an agents
        leftover was invisible and immortal. Ownership is by the parsed owner,
        never a `.staging-<name>-*` prefix: that glob would delete `demo-other`'s
        in-flight tree while holding only `demo`'s lock.
        """
        root = tmp_path / "src"
        _write_agent(root)
        out = tmp_path / "b.json"
        _export(root, out, kind="agents")

        store = dst / ".memtomem" / "agents.local"
        store.mkdir(parents=True)
        mine = store / ".staging-demo-999-abcdef.tmp"
        mine.mkdir()
        (mine / "agent.md").write_text("half-written\n", newline="\n")
        neighbour = store / ".staging-demo-other-999-abcdef.tmp"
        neighbour.mkdir()

        receive_artifact_bundle(out, dst_project_root=dst, to_scope="project_local", apply_=True)

        assert not mine.exists()
        assert neighbour.exists()
        assert (store / "demo" / "agent.md").exists()


def test_every_fixture_write_pins_its_line_endings() -> None:
    """No fixture in this file may let the host decide its newlines.

    `Path.write_text` without `newline=` translates `\\n` to `\\r\\n` on Windows,
    so a fixture written that way lands different bytes per platform. This file
    tests a byte-exact transport, and once export reads through a binary
    descriptor those bytes reach the assertions: on Windows the round-trip
    comparison saw CRLF, and a CRLF SKILL.md has no frontmatter as far as the
    markdown chunker — and therefore the redaction declaration — is concerned,
    so six declaration tests failed with a privacy block instead.

    A guard rather than a one-time sweep, because the sweep certifies only the
    calls that existed when it ran. CRLF payloads are still covered, by a test
    that writes those bytes explicitly.
    """
    import ast

    source = Path(__file__).read_text(encoding="utf-8")
    offenders = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
        and not any(kw.arg == "newline" for kw in node.keywords)
    ]
    assert not offenders, (
        f"write_text without newline= at line(s) {offenders} — the host decides "
        f"the bytes, so this file's assertions would test the platform. Pass "
        f'newline="\\n", or write_bytes when the test is about the bytes.'
    )
