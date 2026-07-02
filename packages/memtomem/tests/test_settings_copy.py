"""Tests for settings_copy — cross-project per-hook copy (#1281, A-11).

Engine half: ``plan_hook_copy`` / ``apply_hook_copy``. The CLI and web
surfaces have their own files (``test_cli_settings_copy.py``,
``test_web_settings_copy.py``).
"""

from __future__ import annotations

import json

import pytest

from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.settings import CANONICAL_SETTINGS_FILE, generate_all_settings
from memtomem.context.settings_copy import (
    AmbiguousHookSelectorError,
    HookNotFoundError,
    apply_hook_copy,
    format_copy_summary,
    plan_hook_copy,
)

from .helpers import set_home

# AKIA fixture per feedback_force_unsafe_redaction_valve_only.md — a generic
# placeholder would pass the real scanner and false-negative every assert.
SECRET = "api_key=AKIA1234567890ABCDEF"


def _inner(command: str = "mm session start", *, timeout: int = 5000) -> dict:
    return {"type": "command", "command": command, "timeout": timeout}


def _rule(matcher: str = "Edit|Write", *, inners: list[dict] | None = None) -> dict:
    return {"matcher": matcher, "hooks": list(inners) if inners is not None else [_inner()]}


def _write_doc(path, doc: dict | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(doc, str):
        path.write_text(doc, encoding="utf-8")
    else:
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _read_doc(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def src_project(tmp_path):
    root = tmp_path / "src-proj"
    root.mkdir()
    _write_doc(
        root / CANONICAL_SETTINGS_FILE,
        {"hooks": {"PostToolUse": [_rule("Edit|Write", inners=[_inner("mm session start")])]}},
    )
    return root


@pytest.fixture
def dst_project(tmp_path):
    root = tmp_path / "dst-proj"
    (root / ".memtomem").mkdir(parents=True)
    return root


def _plan(src, dst, *, scope: str = "project_shared", **kwargs):
    return plan_hook_copy(
        src,
        event=kwargs.pop("event", "PostToolUse"),
        matcher=kwargs.pop("matcher", "Edit|Write"),
        dst_project_root=dst,
        dst_scope=scope,
        **kwargs,
    )


# ── Planning / selection ────────────────────────────────────────────


class TestPlanSelection:
    def test_plan_resolves_single_candidate(self, src_project, dst_project):
        plan = _plan(src_project, dst_project)
        assert plan.signature.event == "PostToolUse"
        assert plan.signature.matcher == "Edit|Write"
        assert plan.canonical_inner == _inner("mm session start")
        assert plan.canonical_state == "missing"
        assert plan.target_state == "missing"
        assert plan.label == "PostToolUse:Edit|Write"

    def test_target_rule_is_stamped_canonical_rule_is_not(self, src_project, dst_project):
        plan = _plan(src_project, dst_project)
        assert "statusMessage" not in plan.rule_for_canonical["hooks"][0]
        assert plan.rule_for_target["hooks"][0]["statusMessage"].startswith("memtomem · ")

    def test_no_match_lists_available_labels(self, src_project, dst_project):
        with pytest.raises(HookNotFoundError, match=r"available: PostToolUse:Edit\|Write"):
            _plan(src_project, dst_project, event="SessionStart", matcher="")

    def test_missing_source_canonical_raises(self, tmp_path, dst_project):
        bare = tmp_path / "bare"
        bare.mkdir()
        with pytest.raises(HookNotFoundError, match="no readable canonical settings"):
            _plan(bare, dst_project)

    def test_ambiguous_selector_lists_candidates(self, src_project, dst_project):
        _write_doc(
            src_project / CANONICAL_SETTINGS_FILE,
            {
                "hooks": {
                    "PostToolUse": [
                        _rule("Edit|Write", inners=[_inner("mm session start"), _inner("mm idx")])
                    ]
                }
            },
        )
        with pytest.raises(AmbiguousHookSelectorError, match="mm session start.*mm idx"):
            _plan(src_project, dst_project)

    def test_hook_command_disambiguates(self, src_project, dst_project):
        _write_doc(
            src_project / CANONICAL_SETTINGS_FILE,
            {
                "hooks": {
                    "PostToolUse": [
                        _rule("Edit|Write", inners=[_inner("mm session start"), _inner("mm idx")])
                    ]
                }
            },
        )
        plan = _plan(src_project, dst_project, hook_command="idx")
        assert plan.canonical_inner["command"] == "mm idx"

    def test_hook_command_eliminating_all_lists_candidates(self, src_project, dst_project):
        with pytest.raises(HookNotFoundError, match="matches none.*mm session start"):
            _plan(src_project, dst_project, hook_command="nope")

    def test_matcher_is_normalized_for_matching(self, src_project, dst_project):
        plan = _plan(src_project, dst_project, matcher="  Edit|Write  ")
        assert plan.signature.matcher == "Edit|Write"

    def test_same_project_refused(self, src_project):
        with pytest.raises(ValueError, match="settings-migrate"):
            _plan(src_project, src_project)

    def test_unknown_dst_scope_refused(self, src_project, dst_project):
        with pytest.raises(ValueError, match="unknown destination tier"):
            _plan(src_project, dst_project, scope="prod")

    def test_plan_classifies_existing_states(self, src_project, dst_project):
        # exact at canonical, conflict at tier
        _write_doc(
            dst_project / CANONICAL_SETTINGS_FILE,
            {"hooks": {"PostToolUse": [_rule("Edit|Write", inners=[_inner("mm session start")])]}},
        )
        _write_doc(
            dst_project / ".claude" / "settings.json",
            {"hooks": {"PostToolUse": [_rule("Edit|Write", inners=[_inner("rival cmd")])]}},
        )
        plan = _plan(src_project, dst_project)
        assert plan.canonical_state == "exact"
        assert plan.target_state == "conflict"
        assert "'rival cmd'" in plan.target_reason
        assert plan.has_conflict


# ── Apply ───────────────────────────────────────────────────────────


class TestApplyFresh:
    def test_writes_canonical_verbatim_and_tier_stamped(self, src_project, dst_project):
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written and result.target_written
        assert not result.warnings

        canonical = _read_doc(dst_project / CANONICAL_SETTINGS_FILE)
        [c_rule] = canonical["hooks"]["PostToolUse"]
        assert c_rule == _rule("Edit|Write", inners=[_inner("mm session start")])

        tier = _read_doc(dst_project / ".claude" / "settings.json")
        [t_rule] = tier["hooks"]["PostToolUse"]
        [t_inner] = t_rule["hooks"]
        assert t_inner["command"] == "mm session start"
        assert t_inner["statusMessage"] == "memtomem · PostToolUse"

    def test_missing_dst_canonical_file_is_created(self, src_project, tmp_path):
        dst = tmp_path / "dst-empty"
        dst.mkdir()
        result = apply_hook_copy(_plan(src_project, dst))
        assert result.canonical_written
        assert (dst / CANONICAL_SETTINGS_FILE).is_file()

    def test_preserves_unrelated_dst_content(self, src_project, dst_project):
        _write_doc(
            dst_project / CANONICAL_SETTINGS_FILE,
            {"hooks": {"SessionStart": [_rule("", inners=[_inner("dst own")])]}},
        )
        _write_doc(
            dst_project / ".claude" / "settings.json",
            {"permissions": {"allow": ["Bash"]}, "hooks": {}},
        )
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written and result.target_written
        canonical = _read_doc(dst_project / CANONICAL_SETTINGS_FILE)
        assert "SessionStart" in canonical["hooks"]
        tier = _read_doc(dst_project / ".claude" / "settings.json")
        assert tier["permissions"] == {"allow": ["Bash"]}

    def test_user_tier_destination_home_isolated(self, src_project, dst_project, fake_home):
        result = apply_hook_copy(_plan(src_project, dst_project, scope="user"))
        assert result.canonical_written and result.target_written
        tier = _read_doc(fake_home / ".claude" / "settings.json")
        [t_rule] = tier["hooks"]["PostToolUse"]
        assert t_rule["hooks"][0]["statusMessage"].startswith("memtomem · ")
        # canonical still lands at the destination PROJECT, not under HOME
        assert (dst_project / CANONICAL_SETTINGS_FILE).is_file()

    def test_project_local_tier_path(self, src_project, dst_project):
        result = apply_hook_copy(_plan(src_project, dst_project, scope="project_local"))
        assert result.target_written
        assert (dst_project / ".claude" / "settings.local.json").is_file()

    def test_sync_command_pins_cd_and_scope(self, src_project, dst_project):
        result = apply_hook_copy(_plan(src_project, dst_project, scope="project_local"))
        assert result.needs_sync
        assert str(dst_project) in result.sync_command
        assert result.sync_command.endswith(
            "mm context sync --include=settings --scope project_local"
        )


class TestApplyIdempotent:
    def test_second_run_reports_already_at_target(self, src_project, dst_project):
        apply_hook_copy(_plan(src_project, dst_project))
        canonical_before = (dst_project / CANONICAL_SETTINGS_FILE).read_bytes()
        tier_before = (dst_project / ".claude" / "settings.json").read_bytes()

        second = apply_hook_copy(_plan(src_project, dst_project))
        assert not second.canonical_written and not second.target_written
        assert second.canonical_already and second.target_already
        assert not second.warnings
        assert second.plan.is_noop
        assert (dst_project / CANONICAL_SETTINGS_FILE).read_bytes() == canonical_before
        assert (dst_project / ".claude" / "settings.json").read_bytes() == tier_before
        assert "already" in format_copy_summary(second)

    def test_copy_then_sync_at_destination_is_stable(self, src_project, dst_project, fake_home):
        """The durability claim: the copied rule survives the destination's
        own settings sync (owned-slot replace, no duplicate, no prune)."""
        apply_hook_copy(_plan(src_project, dst_project))
        results = generate_all_settings(dst_project, scope="project_shared")
        assert results["claude_settings"].status == "ok"
        tier = _read_doc(dst_project / ".claude" / "settings.json")
        rules = [r for r in tier["hooks"]["PostToolUse"] if r.get("matcher", "") == "Edit|Write"]
        assert len(rules) == 1
        assert rules[0]["hooks"][0]["command"] == "mm session start"


class TestApplyConflicts:
    def test_canonical_conflict_skips_both_legs(self, src_project, dst_project):
        _write_doc(
            dst_project / CANONICAL_SETTINGS_FILE,
            {"hooks": {"PostToolUse": [_rule("Edit|Write", inners=[_inner("rival cmd")])]}},
        )
        before = (dst_project / CANONICAL_SETTINGS_FILE).read_bytes()
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written and not result.target_written
        assert any("'rival cmd'" in w for w in result.warnings)
        assert (dst_project / CANONICAL_SETTINGS_FILE).read_bytes() == before
        assert not (dst_project / ".claude" / "settings.json").exists()

    def test_tier_conflict_still_writes_canonical(self, src_project, dst_project):
        _write_doc(
            dst_project / ".claude" / "settings.json",
            {"hooks": {"PostToolUse": [_rule("Edit|Write", inners=[_inner("rival cmd")])]}},
        )
        tier_before = (dst_project / ".claude" / "settings.json").read_bytes()
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written and not result.target_written
        assert any("'rival cmd'" in w for w in result.warnings)
        assert any("canonical entry WAS written" in w for w in result.warnings)
        assert (dst_project / ".claude" / "settings.json").read_bytes() == tier_before
        canonical = _read_doc(dst_project / CANONICAL_SETTINGS_FILE)
        assert canonical["hooks"]["PostToolUse"] == [
            _rule("Edit|Write", inners=[_inner("mm session start")])
        ]

    def test_different_timeout_is_a_conflict_not_exact(self, src_project, dst_project):
        _write_doc(
            dst_project / CANONICAL_SETTINGS_FILE,
            {
                "hooks": {
                    "PostToolUse": [
                        _rule("Edit|Write", inners=[_inner("mm session start", timeout=1)])
                    ]
                }
            },
        )
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written
        assert result.warnings

    def test_stamped_tier_rule_classifies_exact(self, src_project, dst_project):
        """Ownership-marker fields are ignored for equality — a previously
        synced (stamped) destination rule is already_at_target, not a
        conflict."""
        _write_doc(
            dst_project / ".claude" / "settings.json",
            {
                "hooks": {
                    "PostToolUse": [
                        _rule(
                            "Edit|Write",
                            inners=[
                                {
                                    **_inner("mm session start"),
                                    "statusMessage": "memtomem · PostToolUse",
                                }
                            ],
                        )
                    ]
                }
            },
        )
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written
        assert result.target_already and not result.target_written
        assert not result.warnings


class TestApplyMalformed:
    def test_malformed_dst_canonical_refuses_both_legs(self, src_project, dst_project):
        _write_doc(dst_project / CANONICAL_SETTINGS_FILE, "{not json")
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written and not result.target_written
        assert any("not valid JSON" in w for w in result.warnings)
        assert not (dst_project / ".claude" / "settings.json").exists()

    def test_malformed_dst_tier_writes_canonical_only(self, src_project, dst_project):
        _write_doc(dst_project / ".claude" / "settings.json", "[1, 2]")
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written and not result.target_written
        assert any("not valid JSON" in w for w in result.warnings)
        assert (dst_project / ".claude" / "settings.json").read_text(encoding="utf-8") == "[1, 2]"

    def test_list_shaped_hooks_in_dst_canonical_refused_not_coerced(self, src_project, dst_project):
        _write_doc(dst_project / CANONICAL_SETTINGS_FILE, {"hooks": []})
        before = (dst_project / CANONICAL_SETTINGS_FILE).read_bytes()
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written
        assert any("not a record" in w for w in result.warnings)
        assert (dst_project / CANONICAL_SETTINGS_FILE).read_bytes() == before


class TestApplyConcurrency:
    def test_canonical_mtime_drift_aborts_everything(self, src_project, dst_project, monkeypatch):
        from memtomem.context import settings_copy as mod

        _write_doc(dst_project / CANONICAL_SETTINGS_FILE, {"hooks": {}})
        real = mod._read_with_mtime

        def stale(path):
            doc, mtime = real(path)
            if path == dst_project / CANONICAL_SETTINGS_FILE:
                return doc, mtime - 1
            return doc, mtime

        monkeypatch.setattr(mod, "_read_with_mtime", stale)
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written and not result.target_written
        assert any("modified by another process" in w for w in result.warnings)

    def test_tier_mtime_drift_keeps_canonical_reports_partial(
        self, src_project, dst_project, monkeypatch
    ):
        from memtomem.context import settings_copy as mod

        _write_doc(dst_project / ".claude" / "settings.json", {"hooks": {}})
        real = mod._read_with_mtime

        def stale(path):
            doc, mtime = real(path)
            if path == dst_project / ".claude" / "settings.json":
                return doc, mtime - 1
            return doc, mtime

        monkeypatch.setattr(mod, "_read_with_mtime", stale)
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written and not result.target_written
        assert any("tier was not" in w for w in result.warnings)

    def test_canonical_delete_race_aborts_and_does_not_recreate(
        self, src_project, dst_project, monkeypatch
    ):
        """A concurrent DELETE of the destination canonical between read and
        write must abort like an edit — not resurrect the deleted file with
        its pre-delete hooks (the settings-migrate #1382 shape)."""
        from memtomem.context import settings_copy as mod

        canonical_path = dst_project / CANONICAL_SETTINGS_FILE
        _write_doc(canonical_path, {"hooks": {}})
        real = mod._read_with_mtime

        def read_then_delete(path):
            doc, mtime = real(path)
            if path == canonical_path:
                canonical_path.unlink()
            return doc, mtime

        monkeypatch.setattr(mod, "_read_with_mtime", read_then_delete)
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written and not result.target_written
        assert any("modified by another process" in w for w in result.warnings)
        assert not canonical_path.exists()
        assert not (dst_project / ".claude" / "settings.json").exists()

    def test_tier_delete_race_keeps_canonical_and_does_not_recreate(
        self, src_project, dst_project, monkeypatch
    ):
        """Same delete race on the tier leg: the canonical write stands, the
        deleted tier file must NOT be recreated."""
        from memtomem.context import settings_copy as mod

        tier_path = dst_project / ".claude" / "settings.json"
        _write_doc(tier_path, {"hooks": {}})
        real = mod._read_with_mtime

        def read_then_delete(path):
            doc, mtime = real(path)
            if path == tier_path:
                tier_path.unlink()
            return doc, mtime

        monkeypatch.setattr(mod, "_read_with_mtime", read_then_delete)
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert result.canonical_written and not result.target_written
        assert any("tier was not" in w for w in result.warnings)
        assert not tier_path.exists()

    def test_lock_budget_exhaustion_writes_nothing(self, src_project, dst_project, monkeypatch):
        import contextlib

        from memtomem.context import settings_copy as mod

        @contextlib.contextmanager
        def always_timeout(lock_path, *, timeout=None):
            raise TimeoutError
            yield  # pragma: no cover

        monkeypatch.setattr(mod, "_file_lock", always_timeout)
        result = apply_hook_copy(_plan(src_project, dst_project))
        assert not result.canonical_written and not result.target_written
        assert any("acquisition budget" in w for w in result.warnings)
        assert not (dst_project / CANONICAL_SETTINGS_FILE).exists()


class TestGateA:
    def test_secret_bearing_hook_blocks_before_any_write(self, tmp_path, dst_project):
        src = tmp_path / "src-secret"
        src.mkdir()
        _write_doc(
            src / CANONICAL_SETTINGS_FILE,
            {"hooks": {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}},
        )
        with pytest.raises(PrivacyBlockedError) as exc_info:
            apply_hook_copy(_plan(src, dst_project, matcher="Edit"))
        assert "git history is forever" in str(exc_info.value)
        assert SECRET not in str(exc_info.value)  # never echo the matched bytes
        assert not (dst_project / CANONICAL_SETTINGS_FILE).exists()
        assert not (dst_project / ".claude" / "settings.json").exists()

    def test_gate_runs_for_private_tier_destinations_too(self, tmp_path, dst_project, fake_home):
        """The destination canonical is git-tracked regardless of tier —
        a user-tier destination must scan (and block) identically."""
        src = tmp_path / "src-secret"
        src.mkdir()
        _write_doc(
            src / CANONICAL_SETTINGS_FILE,
            {"hooks": {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}},
        )
        with pytest.raises(PrivacyBlockedError):
            apply_hook_copy(_plan(src, dst_project, matcher="Edit", scope="user"))
        assert not (fake_home / ".claude" / "settings.json").exists()
