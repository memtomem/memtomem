"""ADR-0030 §10 (PR-G2) — the two skill surfaces and the canonical tree digest.

The load-bearing invariant is the RELATION between the surfaces:

* the WIDE copier surface (:func:`read_skill_tree`) is what a copy moves and
  what Gate A must scan — Store-owned metadata included, because a secret
  under ``overrides/`` would still be copied;
* the NARROW payload surface (:func:`iter_skill_payload_files`) is the skill
  *content* — it drives the snapshot, the tree digest, the Store comparison,
  the fan-out staging surface and the sync diff.

If the two ever collapse, either version history leaks into runtimes / into
the next snapshot (narrow widened) or a secret lands unscanned (wide
narrowed). These tests own that boundary; the digest tests pin the framing as
a wire format, because PR-G3 stores it.
"""

from __future__ import annotations

import os
import stat

import pytest

from memtomem.context._atomic import copy_tree_atomic
from memtomem.context.skill_payload import (
    is_payload_relpath,
    is_payload_top_name,
    iter_skill_payload_files,
    payload_digest,
    read_skill_tree,
)
from memtomem.context.skills import _iter_scannable_skill_files


# ``os.geteuid`` is POSIX-only, and a decorator argument evaluates at COLLECTION
# time — a second ``skipif(os.name == "nt")`` in front of it does not shield it,
# so calling it inline breaks collection on Windows outright.
_CAN_DENY_DIR_READ = hasattr(os, "geteuid") and os.geteuid() != 0


def _make_skill(root):
    """A skill dir carrying payload, Store-owned metadata, and lookalikes."""
    skill = root / "demo"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\nbody\n", encoding="utf-8")
    (skill / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")

    # Store-owned top level (ADR-0022 version store + ADR-0027 overrides).
    (skill / "versions").mkdir()
    (skill / "versions" / "v1.md").write_text("old body\n", encoding="utf-8")
    (skill / "overrides").mkdir()
    (skill / "overrides" / "claude.md").write_text("claude flavor\n", encoding="utf-8")
    (skill / "versions.json").write_text('{"versions": {}}\n', encoding="utf-8")
    (skill / ".versions.json.lock").write_text("", encoding="utf-8")
    (skill / ".versions.json.ab12cd.tmp").write_text("half-written\n", encoding="utf-8")

    # Our own crash leftovers (is_internal_artifact_dir).
    (skill / ".staging-demo-1234-ab12cd.tmp").mkdir()
    (skill / ".staging-demo-1234-ab12cd.tmp" / "SKILL.md").write_text("partial\n", encoding="utf-8")

    # Lookalikes NESTED under a payload dir — ordinary user content.
    (skill / "scripts" / "versions.json").write_text("{}\n", encoding="utf-8")
    (skill / "scripts" / "overrides").mkdir()
    (skill / "scripts" / "overrides" / "note.md").write_text("nested\n", encoding="utf-8")
    return skill


class TestPayloadExclusionSet:
    def test_payload_is_content_only(self, tmp_path):
        skill = _make_skill(tmp_path)
        assert [rel for rel, _ in iter_skill_payload_files(skill)] == [
            "SKILL.md",
            "scripts/overrides/note.md",
            "scripts/run.sh",
            "scripts/versions.json",
        ]

    def test_two_surfaces_differ_by_exactly_the_store_metadata(self, tmp_path):
        """The ADR §10 relation: narrow ⊂ wide, and the difference is
        Store-owned metadata only. Widening the narrow surface leaks version
        history into runtimes and snapshots; narrowing the wide one hides a
        secret from Gate A."""
        skill = _make_skill(tmp_path)
        wide = {rel for rel, _ in read_skill_tree(skill)}
        narrow = {rel for rel, _ in iter_skill_payload_files(skill)}

        assert narrow < wide
        assert wide - narrow == {
            "versions/v1.md",
            "versions.json",
            "overrides/claude.md",
            ".versions.json.lock",
            ".versions.json.ab12cd.tmp",
            ".staging-demo-1234-ab12cd.tmp/SKILL.md",
        }

    def test_wide_surface_equals_the_gate_scan_surface(self, tmp_path):
        """``read_skill_tree`` must stay the exact Gate-A scan surface — the
        privacy scan and the landing capture cannot drift."""
        skill = _make_skill(tmp_path)
        scanned = {p.relative_to(skill).as_posix() for p in _iter_scannable_skill_files(skill)}
        assert scanned == {rel for rel, _ in read_skill_tree(skill)}

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("SKILL.md", True),
            ("scripts", True),
            ("versions", False),
            ("overrides", False),
            ("versions.json", False),
            (".versions.json.lock", False),
            (".versions.json.ab12cd.tmp", False),
            (".staging-demo-1234-ab12cd.tmp", False),
            (".old-demo-1234-ab12cd.tmp", False),
            # Lookalikes that are NOT the manifest sidecar shape.
            ("versions.json.bak", True),
            (".versions.jsonx.tmp", True),
        ],
    )
    def test_top_name_predicate(self, name, expected):
        assert is_payload_top_name(name) is expected

    @pytest.mark.parametrize(
        "rel,expected",
        [
            ("SKILL.md", True),
            ("versions.json", False),
            ("versions/v1.md", False),
            ("overrides/claude.md", False),
            # Only the FIRST segment is Store-owned.
            ("scripts/versions.json", True),
            ("scripts/overrides/note.md", True),
            ("docs/versions/v1.md", True),
            # A sidecar-SHAPED top-level DIRECTORY takes its subtree with it —
            # judging file-shaped names only at depth 0 would make these
            # payload here while the listing-based filters (fan-out, diff)
            # dropped them (Codex review).
            ("versions.json/child.md", False),
            (".versions.json.ab12cd.tmp/child.md", False),
            (".staging-demo-1234-ab12cd.tmp/SKILL.md", False),
        ],
    )
    def test_relpath_predicate(self, rel, expected):
        assert is_payload_relpath(rel) is expected

    @pytest.mark.parametrize(
        "name",
        ["SKILL.md", "scripts", "versions", "overrides", "versions.json", ".versions.json.lock"],
    )
    def test_relpath_and_top_name_predicates_agree(self, name):
        """The listing filter (fan-out, diff) and the relpath filter (digest,
        snapshot, Pull comparison) must judge the same top-level entry the same
        way — at depth 0 AND for anything beneath it."""
        assert is_payload_relpath(name) is is_payload_top_name(name)
        assert is_payload_relpath(f"{name}/nested/file.md") is is_payload_top_name(name)

    def test_copier_skips_are_inherited(self, tmp_path):
        """Both surfaces inherit ``COPY_SKIP_NAMES`` and the symlink refusal
        from the copier iterator — at every depth, not just the root."""
        skill = _make_skill(tmp_path)
        (skill / ".git").mkdir()
        (skill / ".git" / "config").write_text("x\n", encoding="utf-8")
        (skill / "scripts" / "__pycache__").mkdir()
        (skill / "scripts" / "__pycache__" / "c.pyc").write_bytes(b"\x00")
        (skill / "scripts" / "link.sh").symlink_to(skill / "scripts" / "run.sh")

        rels = {rel for rel, _ in iter_skill_payload_files(skill)}
        assert not any(r.startswith(".git/") for r in rels)
        assert "scripts/__pycache__/c.pyc" not in rels
        assert "scripts/link.sh" not in rels

    @pytest.mark.skipif(
        not _CAN_DENY_DIR_READ,
        reason="needs POSIX permission semantics and a non-root uid",
    )
    def test_unreadable_subtree_fails_closed(self, tmp_path):
        """An unreadable subtree must RAISE, never silently shrink the payload
        — a shrunken surface is an unscanned/undigested subtree."""
        skill = _make_skill(tmp_path)
        locked = skill / "locked"
        locked.mkdir()
        (locked / "secret.md").write_text("x\n", encoding="utf-8")
        locked.chmod(0o000)
        try:
            with pytest.raises(OSError):
                iter_skill_payload_files(skill)
        finally:
            locked.chmod(0o700)


class TestPayloadDigest:
    def test_stability_vector(self):
        """Hard-coded: the framing is a WIRE FORMAT (PR-G3 stores it, campaign
        2's CAS reuses it). Changing it invalidates every stored digest, so it
        must never change by accident."""
        assert payload_digest([("SKILL.md", b"body\n"), ("scripts/run.sh", b"echo hi\n")]) == (
            "65e84877b61e396570958612ad4df8eda212b9450186720cc1b55324476aba4e"
        )

    def test_length_prefix_defeats_split_confusion(self):
        """Without length-prefixed framing these two payloads would hash the
        same concatenation."""
        assert payload_digest([("a", b"bc")]) != payload_digest([("ab", b"c")])

    def test_order_independent(self):
        a = [("b.md", b"2"), ("a.md", b"1")]
        assert payload_digest(a) == payload_digest(sorted(a))

    def test_content_change_changes_digest(self):
        assert payload_digest([("a.md", b"1")]) != payload_digest([("a.md", b"2")])

    def test_exec_bit_and_empty_dirs_are_not_tracked(self, tmp_path):
        """The copier normalizes modes to 0o644 and does not preserve empty
        dirs, so a digest that noticed either would be unreproducible after a
        round trip through fan-out."""
        skill = _make_skill(tmp_path)
        before = payload_digest(iter_skill_payload_files(skill))

        script = skill / "scripts" / "run.sh"
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        (skill / "empty-dir").mkdir()

        assert payload_digest(iter_skill_payload_files(skill)) == before


class TestCopierTopLevelPredicate:
    def test_default_is_unchanged(self, tmp_path):
        """No predicate → byte-identical behavior for every existing caller."""
        skill = _make_skill(tmp_path)
        dst = tmp_path / "copy"
        digests = copy_tree_atomic(skill, dst)
        assert (dst / "versions" / "v1.md").is_file()
        assert (dst / "overrides" / "claude.md").is_file()
        assert "versions/v1.md" in digests

    def test_predicate_applies_at_root_only(self, tmp_path):
        """Root-only, exactly like ``skip_top_level`` — a nested
        ``scripts/versions.json`` is user content and must survive."""
        skill = _make_skill(tmp_path)
        dst = tmp_path / "payload-copy"
        digests = copy_tree_atomic(
            skill, dst, skip_top_level_pred=lambda name: not is_payload_top_name(name)
        )

        assert not (dst / "versions").exists()
        assert not (dst / "overrides").exists()
        assert not (dst / "versions.json").exists()
        assert not (dst / ".versions.json.ab12cd.tmp").exists()
        assert (dst / "SKILL.md").is_file()
        assert (dst / "scripts" / "versions.json").is_file()
        assert (dst / "scripts" / "overrides" / "note.md").is_file()
        assert set(digests) == {
            "SKILL.md",
            "scripts/run.sh",
            "scripts/versions.json",
            "scripts/overrides/note.md",
        }

    def test_name_set_and_predicate_compose(self, tmp_path):
        skill = _make_skill(tmp_path)
        dst = tmp_path / "both"
        copy_tree_atomic(
            skill,
            dst,
            skip_top_level=frozenset({"scripts"}),
            skip_top_level_pred=lambda name: not is_payload_top_name(name),
        )
        assert not (dst / "scripts").exists()
        assert not (dst / "versions").exists()
        assert (dst / "SKILL.md").is_file()
