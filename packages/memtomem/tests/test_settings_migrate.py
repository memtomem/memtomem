"""Tests for settings_migrate — move memtomem-managed hooks between tiers (#872).

Covers two surfaces:

* Pure planner / applier unit tests (``settings_migrate.plan_migration``
  and ``apply_migration``).
* CLI ``mm context settings-migrate`` subcommand — dry-run preview,
  ``--apply`` mutation, idempotent re-run, partial-overlap, host-write
  prompt, ``--json`` schema.
"""

from __future__ import annotations

import json
import logging

import pytest
from click.testing import CliRunner

from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.settings import CANONICAL_SETTINGS_FILE, MalformedHookMatcherError
from memtomem.context.settings_migrate import (
    MigrateMove,
    MigratePlan,
    MigrateResult,
    apply_migration,
    format_plan_summary,
    plan_migration,
)
from memtomem.context.settings_doctor import HookSignature
from .helpers import consent_lines, set_home


def _make_move(
    *,
    matcher: str = "Edit|Write",
    command: str = "mm session start",
    already: bool = False,
    conflict: bool = False,
) -> MigrateMove:
    """Build a synthetic MigrateMove for direct unit tests."""
    sig = HookSignature(event="PostToolUse", matcher=matcher, command_shape=command)
    return MigrateMove(
        signature=sig,
        rule_to_write_at_target={
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command, "timeout": 5000}],
        },
        already_at_target=already,
        conflict_at_target=conflict,
        conflict_reason="synthetic" if conflict else "",
    )


# ── Helpers ────────────────────────────────────────────────────────


def _inner(command: str = "mm session start", *, type_: str = "command") -> dict:
    """One inner hook entry."""
    return {"type": type_, "command": command, "timeout": 5000}


def _rule(matcher: object = "Edit|Write", *, inners: list[dict] | None = None) -> dict:
    """One hook rule with one or more inner hooks."""
    return {
        "matcher": matcher,
        "hooks": list(inners) if inners is not None else [_inner()],
    }


def _settings_doc(hooks: dict) -> dict:
    return {"hooks": hooks}


def _write_settings(path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


def _write_canonical(project_root, hooks: dict) -> None:
    _write_settings(project_root / CANONICAL_SETTINGS_FILE, _settings_doc(hooks))


def _bundled_hook() -> dict:
    return {"PostToolUse": [_rule("Edit|Write", inners=[_inner("mm session start")])]}


def _stamped_bundled_hook() -> dict:
    return {
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


def _read_settings(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude").mkdir()
    return root


# ── Planner unit tests ─────────────────────────────────────────────


class TestPlanMigration:
    def test_same_scope_rejected(self, project_root, fake_home):
        with pytest.raises(ValueError, match="must differ"):
            plan_migration(project_root, source_scope="user", target_scope="user")

    def test_no_canonical_means_empty_plan(self, project_root, fake_home):
        # Source has the bundled hook but canonical is missing → planner
        # has nothing to match against → empty plan.
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan.moves == ()
        assert plan.is_noop is True

    def test_no_source_means_empty_plan(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        # Source tier file does not exist.
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan.moves == ()

    def test_simple_user_to_project_local(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert len(plan.moves) == 1
        move = plan.moves[0]
        assert move.signature.event == "PostToolUse"
        assert move.signature.matcher == "Edit|Write"
        assert move.signature.command_shape == "mm session start"
        assert move.already_at_target is False
        assert move.conflict_at_target is False
        # Rule to write at target uses the canonical inner shape.
        assert move.rule_to_write_at_target["matcher"] == "Edit|Write"
        assert move.rule_to_write_at_target["hooks"][0]["command"] == "mm session start"

    def test_omitted_matcher_remains_valid_match_all(self, project_root, fake_home):
        rule = {"hooks": [_inner("mm index")]}
        hooks = {"SessionStart": [rule]}
        _write_canonical(project_root, hooks)
        _write_settings(
            fake_home / ".claude" / "settings.json",
            _settings_doc(hooks),
        )

        plan = plan_migration(
            project_root,
            source_scope="user",
            target_scope="project_local",
        )

        assert len(plan.moves) == 1
        assert plan.moves[0].signature.matcher == ""

    def test_partial_overlap_already_at_target(self, project_root, fake_home):
        """Target already has the same canonical-signature inner hook —
        the plan still includes the move (so source gets cleaned) but
        marks ``already_at_target`` so target write is a no-op."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert len(plan.moves) == 1
        assert plan.moves[0].already_at_target is True

    def test_non_canonical_source_entries_ignored(self, project_root, fake_home):
        """A user-authored hook that is NOT in canonical does not move."""
        _write_canonical(project_root, _bundled_hook())
        # User has the bundled hook plus a hand-authored Bash hook.
        user_doc = _settings_doc(
            {
                "PostToolUse": [
                    _rule("Edit|Write", inners=[_inner("mm session start")]),
                    _rule("Bash", inners=[_inner("echo something")]),
                ]
            }
        )
        _write_settings(fake_home / ".claude" / "settings.json", user_doc)
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        # Only the bundled hook signature is moved; Bash one stays.
        assert len(plan.moves) == 1
        assert plan.moves[0].signature.matcher == "Edit|Write"

    def test_whitespace_variant_in_source_still_moves(self, project_root, fake_home):
        """Source has a whitespace-variant of the canonical command —
        signature normalization still picks it up."""
        _write_canonical(project_root, _bundled_hook())
        variant = {"PostToolUse": [_rule("Edit|Write", inners=[_inner("mm   session   start  ")])]}
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(variant))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert len(plan.moves) == 1
        # The rule written to target uses the canonical (clean) command.
        assert plan.moves[0].rule_to_write_at_target["hooks"][0]["command"] == "mm session start"

    def test_target_same_matcher_different_inner_is_conflict(self, project_root, fake_home):
        """Codex review (PR #876): target has the canonical matcher but
        carries a *different* inner hook (user-authored). Without this
        check the planner would set ``conflict_at_target=False``,
        ``apply`` would append the canonical rule alongside the user's,
        and Claude Code would fire both same-matcher rules — the kind of
        silent double-execution ADR-0010 §4 was written to prevent.
        """
        _write_canonical(project_root, _bundled_hook())
        # Source has the canonical entry.
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        # Target has a user-authored hook under the same matcher.
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [_rule("Edit|Write", inners=[_inner("user-script")])]}),
        )
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert len(plan.moves) == 1
        move = plan.moves[0]
        assert move.conflict_at_target is True
        assert move.already_at_target is False
        assert "Resolve manually" in move.conflict_reason
        # ``is_noop`` is True (applicable_moves is empty) — the apply
        # path must still exit 1 to surface the unresolved drift.
        assert plan.is_noop is True
        assert plan.applicable_moves == ()

    def test_target_near_match_with_drifted_timeout_is_conflict(self, project_root, fake_home):
        """Codex review (PR #876): target has the same matcher AND the
        same command, but a different ``timeout`` (or any other inner
        key drift). The signature-only check would mark this as
        ``already_at_target=True`` and ``apply`` would clean source
        without writing target — leaving target permanently drifted
        from ``.memtomem/settings.json``. The planner must classify
        this as a conflict instead.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        # Target has the canonical command but a different timeout.
        drifted_inner = {"type": "command", "command": "mm session start", "timeout": 99999}
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [{"matcher": "Edit|Write", "hooks": [drifted_inner]}]}),
        )
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert len(plan.moves) == 1
        move = plan.moves[0]
        assert move.conflict_at_target is True
        assert move.already_at_target is False

    def test_target_byte_equal_canonical_inner_is_already_at_target(self, project_root, fake_home):
        """Sanity: when target carries an inner byte-equal to canonical,
        the planner classifies it as ``already_at_target`` (not
        conflict) — apply skips target write, source clean-up still
        runs."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan.moves[0].already_at_target is True
        assert plan.moves[0].conflict_at_target is False

    def test_target_stamped_canonical_inner_is_already_at_target(self, project_root, fake_home):
        """ADR-0019 marker fields are cosmetic for migrate's target check."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_stamped_bundled_hook()),
        )
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert len(plan.moves) == 1
        assert plan.moves[0].already_at_target is True
        assert plan.moves[0].conflict_at_target is False


@pytest.mark.parametrize(
    "malformed",
    [None, 7, ["Bash"], {"tool": "Bash"}],
    ids=["null", "int", "list", "dict"],
)
@pytest.mark.parametrize("location", ["canonical", "source", "target"])
def test_malformed_matcher_in_any_related_file_rejects_whole_plan(
    project_root, fake_home, malformed, location
):
    source_path = fake_home / ".claude" / "settings.json"
    target_path = project_root / ".claude" / "settings.local.json"
    healthy = _bundled_hook()
    _write_canonical(project_root, healthy)
    _write_settings(source_path, _settings_doc(healthy))
    _write_settings(target_path, _settings_doc({}))
    malformed_hooks = {
        **healthy,
        "SessionStart": [_rule(malformed, inners=[_inner("broken")])],
    }
    if location == "canonical":
        _write_canonical(project_root, malformed_hooks)
    elif location == "source":
        _write_settings(source_path, _settings_doc(malformed_hooks))
    else:
        _write_settings(target_path, _settings_doc(malformed_hooks))
    paths = [project_root / CANONICAL_SETTINGS_FILE, source_path, target_path]
    before = {path: path.read_bytes() for path in paths}

    with pytest.raises(MalformedHookMatcherError) as exc_info:
        plan_migration(project_root, source_scope="user", target_scope="project_local")

    message = str(exc_info.value)
    assert "event 'SessionStart'" in message
    assert "rule index 0" in message
    assert type(malformed).__name__ in message
    # The user has to hand-edit a file, so name it — not just the leg.
    offender = {
        "canonical": project_root / CANONICAL_SETTINGS_FILE,
        "source": source_path,
        "target": target_path,
    }[location]
    assert str(offender) in message
    assert {path: path.read_bytes() for path in paths} == before


# ── Apply unit tests ───────────────────────────────────────────────


class TestApplyMigration:
    def test_round_trip_user_to_project_local(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        result = apply_migration(plan, surface="test_settings_migrate")
        assert result.target_written is True
        assert result.source_written is True

        # User tier no longer carries the canonical-signature entry.
        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        # Either the event was pruned entirely or the rules list is empty.
        assert user_doc.get("hooks", {}).get("PostToolUse", []) == [] or (
            "PostToolUse" not in user_doc.get("hooks", {})
        )

        # Project_local now carries the canonical rule.
        target_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        post = target_doc["hooks"]["PostToolUse"]
        assert any(
            rule.get("matcher") == "Edit|Write"
            and any(inner.get("command") == "mm session start" for inner in rule.get("hooks", []))
            for rule in post
        )
        handler = post[0]["hooks"][0]
        assert handler["statusMessage"] == "memtomem · PostToolUse"

    def test_idempotent_rerun(self, project_root, fake_home):
        """After a successful apply, a second plan/apply is a no-op."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan1 = plan_migration(project_root, source_scope="user", target_scope="project_local")
        apply_migration(plan1, surface="test_settings_migrate")

        plan2 = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan2.is_noop is True
        result2 = apply_migration(plan2, surface="test_settings_migrate")
        assert result2.target_written is False
        assert result2.source_written is False

    def test_partial_overlap_idempotent(self, project_root, fake_home):
        """Target already has the entry; apply only cleans source."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        result = apply_migration(plan, surface="test_settings_migrate")
        # Target already had the entry → no rewrite needed.
        assert result.target_written is False
        # Source was cleaned.
        assert result.source_written is True

    def test_source_with_mixed_inner_hooks_preserves_user_entry(self, project_root, fake_home):
        """User has the bundled hook AND a user-authored inner hook
        sharing the same matcher. Migration moves only the bundled one;
        the user-authored entry stays in source verbatim."""
        _write_canonical(project_root, _bundled_hook())
        # Source has Edit|Write rule with TWO inners — bundled + user.
        mixed = {
            "PostToolUse": [
                _rule(
                    "Edit|Write",
                    inners=[_inner("mm session start"), _inner("user-script")],
                )
            ]
        }
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(mixed))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        apply_migration(plan, surface="test_settings_migrate")

        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        post = user_doc["hooks"]["PostToolUse"]
        # The user-script inner is still there; the bundled one was removed.
        assert len(post) == 1
        commands = [inner.get("command") for inner in post[0].get("hooks", [])]
        assert "user-script" in commands
        assert "mm session start" not in commands

    def test_conflict_leaves_both_source_and_target_untouched(self, project_root, fake_home):
        """Codex review (PR #876): when ``conflict_at_target=True`` for
        every move, ``apply_migration`` must not write either tier.
        Otherwise we either silently double-fire (target append) or
        silently drift target away from canonical (source clean-up
        without target update)."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        # Target has a user-authored hook under the same matcher.
        target_doc = _settings_doc(
            {"PostToolUse": [_rule("Edit|Write", inners=[_inner("user-script")])]}
        )
        _write_settings(project_root / ".claude" / "settings.local.json", target_doc)

        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        result = apply_migration(plan, surface="test_settings_migrate")
        assert result.target_written is False
        assert result.source_written is False

        # Source still has the canonical entry.
        source_after = _read_settings(fake_home / ".claude" / "settings.json")
        post = source_after["hooks"]["PostToolUse"]
        assert any(
            inner.get("command") == "mm session start"
            for rule in post
            for inner in rule.get("hooks", [])
        )
        # Target still carries only the user-authored entry.
        target_after = _read_settings(project_root / ".claude" / "settings.local.json")
        target_post = target_after["hooks"]["PostToolUse"]
        assert len(target_post) == 1
        target_cmds = [inner.get("command") for inner in target_post[0].get("hooks", [])]
        assert target_cmds == ["user-script"]

    def test_other_top_level_keys_preserved(self, project_root, fake_home):
        """Source's non-hooks top-level keys (e.g. ``permissions``)
        survive the migration verbatim."""
        _write_canonical(project_root, _bundled_hook())
        source_doc = {
            "hooks": _bundled_hook(),
            "permissions": {"allow": ["Bash(ls *)"]},
            "model": "claude-3-5-sonnet",
        }
        _write_settings(fake_home / ".claude" / "settings.json", source_doc)
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        apply_migration(plan, surface="test_settings_migrate")

        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        assert user_doc["permissions"] == {"allow": ["Bash(ls *)"]}
        assert user_doc["model"] == "claude-3-5-sonnet"

    @pytest.mark.parametrize(
        "malformed",
        [None, 7, ["Bash"], {"tool": "Bash"}],
        ids=["null", "int", "list", "dict"],
    )
    @pytest.mark.parametrize("location", ["source", "target"])
    def test_apply_time_matcher_drift_refuses_all_writes(
        self, project_root, fake_home, malformed, location
    ):
        _write_canonical(project_root, _bundled_hook())
        source_path = fake_home / ".claude" / "settings.json"
        _write_settings(source_path, _settings_doc(_bundled_hook()))
        plan = plan_migration(
            project_root,
            source_scope="user",
            target_scope="project_local",
        )
        path = plan.source_path if location == "source" else plan.target_path
        _write_settings(
            path,
            _settings_doc({"SessionStart": [_rule(malformed, inners=[_inner("broken")])]}),
        )
        before = path.read_bytes()

        result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert any("Malformed hook matcher" in warning for warning in result.warnings)
        assert path.read_bytes() == before
        if location == "source":
            assert not plan.target_path.exists()


# ── CLI subcommand tests ───────────────────────────────────────────


class TestSettingsMigrateCli:
    def test_dry_run_no_changes(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(settings_migrate_cmd, ["--from=user", "--to=project_local"])
        assert result.exit_code == 0, result.output
        assert "Will migrate" in result.output
        assert "Run with --apply" in result.output
        # Disk is untouched.
        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        assert "PostToolUse" in user_doc["hooks"]

    def test_apply_round_trip_user_to_project_local(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        # --yes skips the host-write confirmation (user → project_local
        # touches ~/.claude/, which is outside the project root).
        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert (project_root / ".claude" / "settings.local.json").is_file()
        target_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert "PostToolUse" in target_doc["hooks"]

    def test_apply_idempotent_rerun(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        runner = CliRunner()
        first = runner.invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply", "--yes"],
        )
        assert first.exit_code == 0, first.output
        second = runner.invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply", "--yes"],
        )
        assert second.exit_code == 0, second.output
        # Re-run plan finds nothing to migrate.
        assert "nothing to migrate" in second.output

    def test_host_write_prompt_declined_aborts(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        # No --yes → prompt fires; declining ('n') aborts.
        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply"],
            input="n\n",
        )
        assert result.exit_code == 1
        assert "Aborted" in result.output
        # Source untouched.
        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        assert "PostToolUse" in user_doc["hooks"]

    def test_json_dry_run_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "ok"
        assert payload["applied"] is False
        assert payload["from"] == "user"
        assert payload["to"] == "project_local"
        assert len(payload["moves"]) == 1
        move = payload["moves"][0]
        assert move["event"] == "PostToolUse"
        assert move["already_at_target"] is False

    def test_json_apply_writes_disk(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply", "--yes", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["applied"] is True
        assert payload["target_written"] is True
        assert payload["source_written"] is True

    def test_same_scope_errors(self, project_root, fake_home, monkeypatch):
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(settings_migrate_cmd, ["--from=user", "--to=user"])
        assert result.exit_code == 1
        assert "must differ" in result.output

    def test_json_malformed_matcher_returns_error(self, project_root, fake_home, monkeypatch):
        _write_canonical(
            project_root,
            {"SessionStart": [_rule(["Bash"], inners=[_inner("broken")])]},
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--json"],
        )

        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "event 'SessionStart'" in payload["error"]
        assert not (project_root / ".claude" / "settings.local.json").exists()

    def test_apply_with_target_conflict_exits_one(self, project_root, fake_home, monkeypatch):
        """Codex review (PR #876): all-conflict apply must exit 1
        (previously the ``is_noop`` early-return swallowed it)."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [_rule("Edit|Write", inners=[_inner("user-script")])]}),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply", "--yes"],
        )
        assert result.exit_code == 1, result.output
        assert "conflict" in result.output.lower()

    def test_json_needs_confirmation_payload(self, project_root, fake_home, monkeypatch):
        """JSON ``--apply`` without ``--yes`` must refuse host writes
        explicitly: the prompt would never reach a JSON caller."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=user", "--to=project_local", "--apply", "--json"],
        )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "needs_confirmation"
        assert payload["applied"] is False
        # Source is user-tier → outside project root → must appear in
        # host_writes. Compare against the actual source path so the
        # check is separator-agnostic (Windows uses ``\`` not ``/``).
        expected_source = str(fake_home / ".claude" / "settings.json")
        assert expected_source in payload["host_writes"]
        assert "--yes" in payload["hint"]
        # Disk untouched.
        assert not (project_root / ".claude" / "settings.local.json").is_file()

    def test_project_to_project_asks_gate_b_not_the_host_prompt(
        self, project_root, fake_home, monkeypatch
    ):
        """project_shared → project_local stays in-tree, so the host-write
        prompt does not fire — but Gate B does, on the source leg (#2348).

        Both legs are inside the project root, which is what made this
        direction look ungated: the one prompt the command used to have
        keys on leaving the project. The tier this repository tracks is
        being stripped, so the question that gets asked names the source
        file, and the host-write wording must be absent from the output.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            project_root / ".claude" / "settings.json",
            _settings_doc(_bundled_hook()),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        result = CliRunner().invoke(
            settings_migrate_cmd,
            ["--from=project_shared", "--to=project_local", "--apply"],
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert (project_root / ".claude" / "settings.local.json").is_file()
        assert "outside this project" not in result.output
        assert "remove 1 hook entry from the project_shared tier" in result.output
        assert str(project_root / ".claude" / "settings.json") in result.output


# ── ADR-0011 §5 gates (#2348) ──────────────────────────────────────


SECRET = "api_key=AKIA1234567890ABCDEF"


class TestSettingsMigrateGateB:
    """``--to``/``--from project_shared`` takes the ADR-0011 §5 gates (#2348).

    The command already had a prompt, which is why it read as covered: the
    host-write check, keyed on a leg lying *outside* the project root. A
    ``project_shared`` path is ``<root>/.claude/settings.json`` by
    construction, so that prompt can never fire for the one tier that needs
    a gate. These pin the second gate and its consent record.

    ``consent_lines`` asserts on the whole list, not on membership: a
    refused, dry-run or untracked-tier run must record *nothing*.
    """

    def _shared_target_setup(self, project_root, monkeypatch):
        """Source ``project_local`` → target ``project_shared``. Both legs
        are in-tree, so the host-write prompt is out of the picture."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        return settings_migrate_cmd

    def test_apply_without_confirmation_refuses_and_writes_nothing(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """The issue's reproduction, inverted: no stdin, nothing written."""
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply"],
                input="",
            )
        assert result.exit_code != 0, result.output
        assert not (project_root / ".claude" / "settings.json").exists()
        assert consent_lines(caplog) == []
        # The source is left carrying the entries it started with.
        source_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert "PostToolUse" in source_doc["hooks"]

    def test_yes_alone_does_not_satisfy_gate_b(self, project_root, fake_home, monkeypatch, caplog):
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply", "--yes"],
            )
        assert result.exit_code != 0
        assert "--confirm-project-shared" in result.output
        assert "--yes alone is not sufficient" in result.output
        assert not (project_root / ".claude" / "settings.json").exists()
        assert consent_lines(caplog) == []

    def test_interactive_decline_aborts(self, project_root, fake_home, monkeypatch, caplog):
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply"],
                input="n\n",
            )
        assert result.exit_code != 0
        assert not (project_root / ".claude" / "settings.json").exists()
        assert consent_lines(caplog) == []

    def test_interactive_accept_writes_and_records_prompt_consent(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply"],
                input="y\n",
            )
        assert result.exit_code == 0, result.output
        target_doc = _read_settings(project_root / ".claude" / "settings.json")
        assert "PostToolUse" in target_doc["hooks"]
        lines = consent_lines(caplog)
        assert len(lines) == 1
        assert "project_shared.confirmed_via=cli_context_settings_migrate" in lines[0]
        assert "mechanism=prompt" in lines[0]
        assert "action=move" in lines[0]
        assert "from_scope='project_local'" in lines[0]
        assert "to_scope='project_shared'" in lines[0]
        assert "shared_leg='target'" in lines[0]

    def test_flag_writes_and_records_flag_consent(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                [
                    "--from=project_local",
                    "--to=project_shared",
                    "--apply",
                    "--confirm-project-shared",
                ],
            )
        assert result.exit_code == 0, result.output
        assert (project_root / ".claude" / "settings.json").is_file()
        lines = consent_lines(caplog)
        assert len(lines) == 1
        assert "mechanism=flag" in lines[0]

    def test_shared_source_removal_is_gated_and_recorded(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """The reverse direction is a change to the tracked tier too (#2348).

        The issue filed only the ``--to`` half, on the reasoning that
        ``project_shared → project_local`` writes the gitignored file. It
        also *strips* the tracked one, and ADR-0011 §5 treats removing bytes
        the project committed as the same class of change as adding them.
        The consent line names the source leg.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            project_root / ".claude" / "settings.json",
            _settings_doc(_bundled_hook()),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            refused = CliRunner().invoke(
                settings_migrate_cmd,
                ["--from=project_shared", "--to=project_local", "--apply", "--yes"],
            )
        assert refused.exit_code != 0
        assert "--from project_shared requires --confirm-project-shared" in refused.output
        assert consent_lines(caplog) == []
        # Untouched: the tracked tier still carries the entry.
        assert "PostToolUse" in _read_settings(project_root / ".claude" / "settings.json")["hooks"]

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            accepted = CliRunner().invoke(
                settings_migrate_cmd,
                [
                    "--from=project_shared",
                    "--to=project_local",
                    "--apply",
                    "--confirm-project-shared",
                ],
            )
        assert accepted.exit_code == 0, accepted.output
        assert _read_settings(project_root / ".claude" / "settings.json")["hooks"] == {}
        lines = consent_lines(caplog)
        assert len(lines) == 1
        assert "shared_leg='source'" in lines[0]
        assert "from_scope='project_shared'" in lines[0]

    def test_json_apply_without_flag_needs_confirmation(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply", "--json"],
            )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "needs_confirmation"
        assert payload["applied"] is False
        assert payload["project_shared_leg"] == "target"
        assert payload["project_shared_path"] == str(project_root / ".claude" / "settings.json")
        assert "--confirm-project-shared" in payload["hint"]
        assert not (project_root / ".claude" / "settings.json").exists()
        assert consent_lines(caplog) == []

    def test_dry_run_names_the_flag_and_records_nothing(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(cmd, ["--from=project_local", "--to=project_shared"])
        assert result.exit_code == 0, result.output
        assert "Run with --apply --confirm-project-shared to execute." in result.output
        assert consent_lines(caplog) == []

    def test_untracked_target_keeps_the_plain_footer(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """A ``project_local`` target names no flag and records no consent."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(settings_migrate_cmd, ["--from=user", "--to=project_local"])
        assert result.exit_code == 0, result.output
        assert "Run with --apply to execute." in result.output
        assert consent_lines(caplog) == []

    def test_noop_rerun_asks_nothing(self, project_root, fake_home, monkeypatch, caplog):
        """Nothing pending never prompts (#1263) — including the second run."""
        cmd = self._shared_target_setup(project_root, monkeypatch)
        first = CliRunner().invoke(
            cmd,
            [
                "--from=project_local",
                "--to=project_shared",
                "--apply",
                "--confirm-project-shared",
            ],
        )
        assert first.exit_code == 0, first.output

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            second = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply"],
                input="",
            )
        assert second.exit_code == 0, second.output
        assert consent_lines(caplog) == []

    def test_json_refusal_stays_json_even_with_yes(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """``--json --yes`` is still answered in JSON, with ``--yes`` named.

        This command already hands its JSON callers a structured error on the
        plan-time failure, so a bare stderr line here would be the one refusal
        a script could not parse. The ``error`` key is what separates "you
        passed the wrong flag" from "you passed no flag" — both are
        ``needs_confirmation``, and only one of them mentions ``--yes``.
        """
        cmd = self._shared_target_setup(project_root, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                cmd,
                ["--from=project_local", "--to=project_shared", "--apply", "--json", "--yes"],
            )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "needs_confirmation"
        assert payload["applied"] is False
        assert "--yes alone is not sufficient" in payload["error"]
        assert not (project_root / ".claude" / "settings.json").exists()
        assert consent_lines(caplog) == []

    def test_only_already_at_target_moves_still_ask(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """A plan whose every move is ``already_at_target`` is still gated.

        Nothing here is written to the shared target *as the plan sees it* —
        the entries are all there already — so narrowing Gate B to the moves
        the planner marked as writes looks harmless and passes every other
        case in this class. It is not harmless: apply re-classifies against
        the live tier under its lock, so a target that lost the entry in
        between turns this into a write into the tracked file. The source is
        cleaned either way, which is itself a change the plan commits to.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        _write_settings(
            project_root / ".claude" / "settings.json",
            _settings_doc(_stamped_bundled_hook()),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )
        assert [m.already_at_target for m in plan.applicable_moves] == [True]

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                settings_migrate_cmd,
                ["--from=project_local", "--to=project_shared", "--apply", "--yes"],
            )
        assert result.exit_code != 0
        assert "--confirm-project-shared" in result.output
        assert consent_lines(caplog) == []
        # The source keeps its entry: the refusal precedes the clean-up.
        source_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert "PostToolUse" in source_doc["hooks"]

    def test_all_conflict_plan_asks_nothing(self, project_root, fake_home, monkeypatch, caplog):
        """An all-conflict plan writes nothing, so it must not ask.

        The absence is asserted by making the prompt itself fail, not by
        reading the outcome: an EOF on an unanswered prompt also exits 1, and
        the word "conflict" is already in the plan preview above it, so a gate
        that wrongly fired here would look exactly like a gate that correctly
        stayed quiet.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        _write_settings(
            project_root / ".claude" / "settings.json",
            _settings_doc({"PostToolUse": [_rule("Edit|Write", inners=[_inner("user-script")])]}),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli import context_cmd as ctx

        def _no_prompt(*args, **kwargs):
            raise AssertionError("a plan that writes nothing must not prompt")

        monkeypatch.setattr(ctx.click, "confirm", _no_prompt)

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                ctx.settings_migrate_cmd,
                ["--from=project_local", "--to=project_shared", "--apply"],
                input="",
                catch_exceptions=False,
            )
        assert result.exit_code == 1, result.output
        assert "conflict" in result.output.lower()
        assert consent_lines(caplog) == []

    def test_host_prompt_still_runs_after_consent_is_recorded(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """Two gates, two questions — and the consent survives a later refusal.

        ``--from user --to project_shared`` trips both: the source is outside
        the project root, the target is the tracked tier. ``--yes`` answers
        only the host prompt and ``--confirm-project-shared`` only Gate B.
        Declining the host prompt still leaves the Gate B consent recorded,
        because the line reports consent *given*, not the write landing
        (ADR-0011 §5, #2306).
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            declined = CliRunner().invoke(
                settings_migrate_cmd,
                [
                    "--from=user",
                    "--to=project_shared",
                    "--apply",
                    "--confirm-project-shared",
                ],
                input="n\n",
            )
        assert declined.exit_code == 1, declined.output
        assert "Aborted" in declined.output
        assert not (project_root / ".claude" / "settings.json").exists()
        assert len(consent_lines(caplog)) == 1

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            accepted = CliRunner().invoke(
                settings_migrate_cmd,
                [
                    "--from=user",
                    "--to=project_shared",
                    "--apply",
                    "--confirm-project-shared",
                    "--yes",
                ],
            )
        assert accepted.exit_code == 0, accepted.output
        assert (project_root / ".claude" / "settings.json").is_file()
        assert len(consent_lines(caplog)) == 1


class TestSettingsMigrateGateA:
    """The privacy scan on a ``project_shared`` target (#2348)."""

    def test_secret_bearing_rule_blocks_before_any_write(self, project_root):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]},
        )
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )
        assert plan.applicable_moves

        with pytest.raises(PrivacyBlockedError) as exc_info:
            apply_migration(plan, surface="test_settings_migrate")

        assert SECRET not in str(exc_info.value)  # never echo the matched bytes
        assert not (project_root / ".claude" / "settings.json").exists()
        # Source untouched — the block precedes both writes.
        source_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert "PostToolUse" in source_doc["hooks"]

    def test_untracked_target_is_not_scanned(self, project_root):
        """The same secret migrating to ``project_local`` is allowed through.

        Gate A here is conditional on the target tier, unlike the copy
        sibling's unconditional scan: migrate writes no canonical, so an
        untracked target exposes nothing, and refusing it with no force valve
        would block the very move that gets a secret out of a shared tier.
        """
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]},
        )
        _write_settings(
            project_root / ".claude" / "settings.json",
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        plan = plan_migration(
            project_root, source_scope="project_shared", target_scope="project_local"
        )
        result = apply_migration(plan, surface="test_settings_migrate")
        assert result.target_written is True
        assert result.warnings == []

    def test_every_applicable_rule_under_one_event_is_scanned(self, project_root):
        """Several moves can share an event; all of their rules are scanned.

        A payload keyed by event that kept only the last rule would scan the
        clean entry and write the secret-bearing one. The secret is placed
        first and a clean rule second, so that shape goes green while the
        block is what is correct.
        """
        _write_canonical(
            project_root,
            {
                "PostToolUse": [
                    _rule("Edit", inners=[_inner(f"echo {SECRET}")]),
                    _rule("Write", inners=[_inner("mm index")]),
                ]
            },
        )
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(
                {
                    "PostToolUse": [
                        _rule("Edit", inners=[_inner(f"echo {SECRET}")]),
                        _rule("Write", inners=[_inner("mm index")]),
                    ]
                }
            ),
        )
        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )
        assert len(plan.applicable_moves) == 2

        with pytest.raises(PrivacyBlockedError):
            apply_migration(plan, surface="test_settings_migrate")
        assert not (project_root / ".claude" / "settings.json").exists()

    def test_the_scanned_bytes_are_the_rules_that_would_be_written(self, project_root, monkeypatch):
        """Pin *what* reaches the scanner, not just that something did.

        Three wrong implementations pass every blocking assertion in this
        class, because they all still scan a payload containing the secret:
        scanning the source tier's bytes instead of the canonical rules that
        get written, keeping only the first rule under an event instead of
        the last, and scanning a conflicted move that will never be written.
        The spy separates them — the payload must be the stamped canonical
        output for exactly the applicable moves.
        """
        canonical = {
            "PostToolUse": [
                _rule("Edit", inners=[_inner("mm session start")]),
                _rule("Write", inners=[_inner("mm index")]),
            ],
            "SessionStart": [_rule("startup", inners=[_inner("mm status")])],
        }
        _write_canonical(project_root, canonical)
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(canonical),
        )
        # A conflict on the SessionStart move: the target holds a different
        # inner under the same (event, matcher), so it is never written.
        _write_settings(
            project_root / ".claude" / "settings.json",
            _settings_doc({"SessionStart": [_rule("startup", inners=[_inner("user-script")])]}),
        )
        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )
        assert len(plan.moves) == 3
        assert len(plan.applicable_moves) == 2

        seen: dict[str, object] = {}

        def _spy(text, **kwargs):
            seen["text"] = text
            seen.update(kwargs)
            return _real_scan(text, **kwargs)

        from memtomem.context import settings_migrate as engine

        _real_scan = engine.scan_text_content
        monkeypatch.setattr(engine, "scan_text_content", _spy)
        result = apply_migration(plan, surface="test_settings_migrate")
        assert result.target_written is True

        scanned = json.loads(str(seen["text"]))
        # Both applicable rules under the one shared event, in write order.
        assert [r["matcher"] for r in scanned["PostToolUse"]] == ["Edit", "Write"]
        # The conflicted move is absent: it is never written, so scanning it
        # would refuse a migration over bytes that stay where they are.
        assert "SessionStart" not in scanned
        # The stamped canonical rule, not the source tier's copy of it.
        assert scanned["PostToolUse"][0]["hooks"][0]["statusMessage"] == "memtomem · PostToolUse"
        assert seen["scope"] == "project_shared"
        assert seen["source_path"] == project_root / CANONICAL_SETTINGS_FILE

    def test_a_blocked_scan_takes_no_lock(self, project_root, monkeypatch):
        """Gate A precedes the pair-lock, and the source is left byte-identical.

        Moving the scan inside the lock would keep every other assertion in
        this class green: the refusal still lands before either write. What
        changes is that a refused migration starts creating lock sidecars and
        contending with a concurrent settings write for a run that was never
        going to proceed.
        """
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]},
        )
        source = project_root / ".claude" / "settings.local.json"
        _write_settings(
            source,
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        before = source.read_bytes()
        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )

        from memtomem.context import settings_migrate as engine

        def _no_lock(*args, **kwargs):
            raise AssertionError("Gate A must refuse before any lock is taken")

        monkeypatch.setattr(engine, "_file_lock", _no_lock)
        with pytest.raises(PrivacyBlockedError):
            apply_migration(plan, surface="test_settings_migrate")
        assert source.read_bytes() == before

    def test_an_abandoned_caller_is_not_scanned(self, project_root, monkeypatch):
        """The entry abandonment check precedes Gate A.

        ``test_settings_migrate_abandon.py`` cannot see this: its plans target
        an untracked tier, where the scan is a no-op either way. A caller that
        is already gone should leave no audit record of a scan it never had a
        reason to run.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )
        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )
        assert plan.applicable_moves

        from memtomem.context import settings_migrate as engine

        def _no_scan(*args, **kwargs):
            raise AssertionError("an abandoned apply must not scan")

        monkeypatch.setattr(engine, "gate_a_scan", _no_scan)
        monkeypatch.setattr(engine, "sync_is_abandoned", lambda: True)
        result = apply_migration(plan, surface="test_settings_migrate")
        assert result.target_written is False
        assert "abandoned" in result.warnings[0]

    def test_a_move_that_drifts_back_into_a_write_was_still_scanned(self, project_root):
        """An ``already_at_target`` move is scanned, because apply can write it.

        The planner freezes ``already_at_target`` from a snapshot;
        ``apply_migration`` re-classifies against the live tier, and an entry
        the target lost in between comes back as a write. Here the target
        carries the secret-bearing rule at plan time — so the only move is
        ``already_at_target`` — and the file is deleted before apply. Skipping
        those moves in the scan is invisible to every other assertion in this
        file: the run stays green and lands the secret in the tracked tier.
        """
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]},
        )
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        target = project_root / ".claude" / "settings.json"
        _write_settings(
            target,
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        plan = plan_migration(
            project_root, source_scope="project_local", target_scope="project_shared"
        )
        assert [m.already_at_target for m in plan.applicable_moves] == [True]

        target.unlink()  # the drift the planner could not see
        with pytest.raises(PrivacyBlockedError):
            apply_migration(plan, surface="test_settings_migrate")

        assert not target.exists()
        source_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert "PostToolUse" in source_doc["hooks"]

    def test_json_callers_get_the_gate_a_refusal_in_json(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """The last refusal this command can make is answered in JSON too.

        A caller that cleared Gate B with the flag and is then stopped by the
        scan is the one path where a machine caller could still get a bare
        stderr line — the plan-time failure and the Gate B refusal both
        answer in JSON. ``refusal`` is the discriminator, since the
        malformed-matcher case shares ``status="error"``. The consent already
        recorded stands: it reports the consent given, not the write landing.

        Parsed from ``stdout`` alone, which is the contract a machine caller
        actually has: the consent line is a log record and goes to stderr.
        Reading the merged ``output`` here would fail on a correct
        implementation — this is the only refusal in the file that emits a
        consent line first, so it is also the only place that distinction is
        observable.
        """
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]},
        )
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                settings_migrate_cmd,
                [
                    "--from=project_local",
                    "--to=project_shared",
                    "--apply",
                    "--json",
                    "--confirm-project-shared",
                ],
            )
        assert result.exit_code == 1, result.output
        assert "confirmed_via" not in result.stdout, "the consent log belongs on stderr"
        payload = json.loads(result.stdout)
        assert payload["status"] == "error"
        assert payload["refusal"] == "gate_a"
        assert payload["applied"] is False
        assert payload["target_path"] == str(project_root / ".claude" / "settings.json")
        assert "Gate A" in payload["error"]
        assert SECRET not in payload["error"]
        assert not (project_root / ".claude" / "settings.json").exists()
        assert len(consent_lines(caplog)) == 1

    def test_cli_translates_the_block_after_consent_is_recorded(
        self, project_root, fake_home, monkeypatch, caplog
    ):
        """Gate B clears first, then Gate A refuses — and both are visible."""
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]},
        )
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc({"PostToolUse": [_rule("Edit", inners=[_inner(f"echo {SECRET}")])]}),
        )
        monkeypatch.chdir(project_root)
        from memtomem.cli.context_cmd import settings_migrate_cmd

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = CliRunner().invoke(
                settings_migrate_cmd,
                [
                    "--from=project_local",
                    "--to=project_shared",
                    "--apply",
                    "--confirm-project-shared",
                ],
            )
        assert result.exit_code != 0, result.output
        assert "Gate A" in result.output
        # Scoped to the refusal, not the whole run: the plan preview above it
        # echoes the source-tier command by design, and that is the user's own
        # local file. What Gate A must not do is repeat the matched bytes in
        # the message it produces.
        gate_a_message = result.output[result.output.index("Gate A") :]
        assert SECRET not in gate_a_message
        assert not (project_root / ".claude" / "settings.json").exists()
        assert len(consent_lines(caplog)) == 1


# ── Reporting invariants ───────────────────────────────────────────


class TestFormatPlanSummary:
    """Pin the count-conservation invariant on ``format_plan_summary``.

    Per ``feedback_count_conservation_invariant_pin.md``: any per-
    category count surface must hold ``sum(parts) == total`` so a
    future status enum addition (e.g. a new ``conflict_kind``) is
    caught by tests rather than silently dropped from the summary.
    """

    def _plan(self, *moves: MigrateMove) -> MigratePlan:
        import tempfile
        from pathlib import Path

        root = Path(tempfile.gettempdir()) / "settings-migrate-summary"
        return MigratePlan(
            source_scope="user",
            target_scope="project_local",
            source_path=root / "from",
            target_path=root / "to",
            moves=tuple(moves),
            project_root=root,
        )

    def test_empty_plan(self):
        summary = format_plan_summary(self._plan())
        assert "0 entries" in summary

    def test_fresh_only(self):
        plan = self._plan(_make_move(command="mm session start"))
        summary = format_plan_summary(plan)
        assert "1 to add at target" in summary

    def test_already_only(self):
        plan = self._plan(_make_move(command="mm index", already=True))
        summary = format_plan_summary(plan)
        assert "1 already at target" in summary

    def test_conflict_only(self):
        plan = self._plan(_make_move(command="mm session end", conflict=True))
        summary = format_plan_summary(plan)
        assert "1 skipped (conflict)" in summary

    def test_conservation_across_three_categories(self):
        """fresh + already + conflict counts must sum to len(moves).

        If a future enum value is added without summary plumbing, the
        sum check fails and surfaces the gap.
        """
        plan = self._plan(
            _make_move(command="cmd-fresh-1"),
            _make_move(command="cmd-fresh-2"),
            _make_move(command="cmd-already-1", already=True),
            _make_move(command="cmd-conflict-1", conflict=True),
        )
        summary = format_plan_summary(plan)
        assert "2 to add at target" in summary
        assert "1 already at target" in summary
        assert "1 skipped (conflict)" in summary
        # Conservation: counts mentioned in the summary must equal len(moves).
        import re

        nums = [int(n) for n in re.findall(r"\b(\d+)\b", summary)]
        assert sum(nums) == len(plan.moves)


class TestApplyTimeDrift:
    """apply_migration re-reads + re-classifies the target at apply time, so a
    target that drifts between plan and apply is handled against its live
    state instead of the frozen plan-time snapshot (#1123 B4-3)."""

    def test_refuses_target_that_drifted_to_conflict_after_plan(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan.applicable_moves  # planner saw a clean, applicable move

        # Drift AFTER planning: target grows a DIFFERENT inner under the same
        # (event, matcher) — what the planner classified "missing" is now a
        # conflict.
        drift = {"PostToolUse": [_rule("Edit|Write", inners=[_inner("other-command")])]}
        _write_settings(project_root / ".claude" / "settings.local.json", _settings_doc(drift))

        result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert result.warnings  # drift surfaced for the CLI to print + exit 1
        # Source still carries the entry (NOT cleaned) so a re-plan retries.
        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        cmds = [
            i.get("command") for r in user_doc["hooks"]["PostToolUse"] for i in r.get("hooks", [])
        ]
        assert "mm session start" in cmds
        # Target's drifted rule was NOT duplicated (still exactly one rule).
        target_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert len(target_doc["hooks"]["PostToolUse"]) == 1

    def test_skips_redundant_write_when_target_gained_identical_entry(
        self, project_root, fake_home
    ):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan.moves[0].already_at_target is False  # planner saw an empty target

        # Drift AFTER planning: target now already carries the canonical rule.
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            _settings_doc(_bundled_hook()),
        )

        result = apply_migration(plan, surface="test_settings_migrate")

        # Re-classified as exact → no redundant append, but source IS cleaned.
        assert result.target_written is False
        assert result.source_written is True
        assert not result.warnings
        # Exactly one rule under the matcher — no same-matcher duplicate.
        target_doc = _read_settings(project_root / ".claude" / "settings.local.json")
        assert len(target_doc["hooks"]["PostToolUse"]) == 1

    def test_cli_surfaces_apply_drift_warning_and_exits_1(
        self, monkeypatch, project_root, fake_home
    ):
        """The settings-migrate CLI prints apply-time drift warnings (stderr)
        and exits 1 even when the plan itself was conflict-free."""
        from memtomem.cli import context_cmd as ctx

        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))

        def _fake_apply(plan: MigratePlan, *, surface: str) -> MigrateResult:
            assert surface == "cli_context_settings_migrate"
            res = MigrateResult(plan=plan)
            res.warnings.append(
                "target tier already has a rule under 'PostToolUse:Edit|Write' "
                "whose inner hooks differ from the canonical entry."
            )
            return res

        monkeypatch.setattr(ctx, "apply_migration", _fake_apply)
        monkeypatch.chdir(project_root)

        result = CliRunner().invoke(
            ctx.context,
            ["settings-migrate", "--from", "user", "--to", "project_local", "--apply", "--yes"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "PostToolUse:Edit|Write" in result.output


class TestApplyConcurrencyGuards:
    """apply_migration's classify → write → clean transaction runs while
    holding BOTH tiers' sidecar ``_file_lock``\\ s — the same locks
    ``generate_all_settings`` takes for these files (#1123 B3-3; #1229
    catalog) — acquired in sorted order (pair-lock discipline), with the
    ``st_mtime_ns`` recheck kept as a second layer against direct disk
    edits, a shared acquisition budget so a held lock aborts instead of
    blocking forever (#1145 shape), and a loud refusal to rewrite a
    malformed tier."""

    def _plan(self, project_root, fake_home) -> MigratePlan:
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan.applicable_moves
        return plan

    def test_apply_holds_both_tier_locks_across_transaction(
        self, project_root, fake_home, monkeypatch
    ):
        """BOTH sidecar locks are acquired (sorted order, nested) before any
        write and released only after all writes — holding the pair is what
        makes the source clean-up decision safe (see the opposite-direction
        regression below)."""
        import contextlib

        from memtomem.context import settings_migrate as migrate_mod
        from memtomem.context._atomic import _lock_path_for

        plan = self._plan(project_root, fake_home)
        events: list[str] = []
        orig_file_lock = migrate_mod._file_lock
        orig_write_json = migrate_mod._write_json

        @contextlib.contextmanager
        def spy_file_lock(lock_path, *, timeout=None):
            events.append(f"enter:{lock_path.name}")
            with orig_file_lock(lock_path, timeout=timeout):
                yield
            events.append(f"exit:{lock_path.name}")

        def spy_write_json(path, data):
            events.append(f"write:{path.name}")
            return orig_write_json(path, data)

        monkeypatch.setattr(migrate_mod, "_file_lock", spy_file_lock)
        monkeypatch.setattr(migrate_mod, "_write_json", spy_write_json)

        result = apply_migration(plan, surface="test_settings_migrate")
        assert result.target_written is True
        assert result.source_written is True

        first_lock, second_lock = [
            p.name
            for p in sorted(
                [_lock_path_for(plan.target_path), _lock_path_for(plan.source_path)],
                key=str,
            )
        ]
        writes = [i for i, e in enumerate(events) if e.startswith("write:")]
        assert writes  # both tier writes happened
        # Sorted acquisition, nested release (ExitStack unwinds in reverse).
        assert events.index(f"enter:{first_lock}") < events.index(f"enter:{second_lock}")
        assert events.index(f"exit:{second_lock}") < events.index(f"exit:{first_lock}")
        # Every write happens while BOTH locks are held.
        assert events.index(f"enter:{second_lock}") < min(writes)
        assert max(writes) < events.index(f"exit:{second_lock}")

    def test_held_target_lock_aborts_within_budget(self, project_root, fake_home, monkeypatch):
        """A foreign holder of the target sidecar (e.g. a concurrent
        ``mm context sync --include=settings``) makes apply abort cleanly
        within the budget — nothing written, warning surfaced (exit 1 via
        the existing CLI warnings plumbing)."""
        from memtomem.context import settings_migrate as migrate_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        plan = self._plan(project_root, fake_home)
        source_before = _read_settings(plan.source_path)
        monkeypatch.setattr(migrate_mod, "_SETTINGS_LOCK_BUDGET_S", 0.2)
        # Separate fd in the same process contends (portalocker locks are
        # per open-file-description).
        with _file_lock(_lock_path_for(plan.target_path)):
            result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert any("held a settings tier lock" in w for w in result.warnings)
        assert not plan.target_path.exists()
        assert _read_settings(plan.source_path) == source_before

    def test_held_source_lock_aborts_whole_apply(self, project_root, fake_home, monkeypatch):
        """A held SOURCE lock aborts the whole apply — both locks are needed
        before anything is written, so nothing moves and nothing is cleaned
        (no half-applied state to heal)."""
        from memtomem.context import settings_migrate as migrate_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        plan = self._plan(project_root, fake_home)
        source_before = _read_settings(plan.source_path)
        monkeypatch.setattr(migrate_mod, "_SETTINGS_LOCK_BUDGET_S", 0.2)
        with _file_lock(_lock_path_for(plan.source_path)):
            result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert any("held a settings tier lock" in w for w in result.warnings)
        assert not plan.target_path.exists()
        assert _read_settings(plan.source_path) == source_before

    def test_aborts_on_target_mtime_change(self, project_root, fake_home):
        """A direct disk edit landing on the target between migrate's read and
        write (bypassing the sidecar lock) trips the ``st_mtime_ns`` recheck:
        nothing is written, the concurrent edit survives intact."""
        import os
        import unittest.mock

        from memtomem.context import settings_migrate as migrate_mod

        plan = self._plan(project_root, fake_home)
        source_before = _read_settings(plan.source_path)
        concurrent = {"hooks": {}, "_concurrent": True}
        orig_read = migrate_mod._read_with_mtime

        def patched_read(path):
            result = orig_read(path)
            if path == plan.target_path:
                _write_settings(plan.target_path, concurrent)
                # Bump explicitly so the simulated concurrent write is
                # distinguishable regardless of OS timer granularity (same
                # discipline as the generate_all_settings mtime-abort test).
                st = plan.target_path.stat()
                os.utime(plan.target_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        with unittest.mock.patch.object(migrate_mod, "_read_with_mtime", patched_read):
            result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert any("modified by another process" in w for w in result.warnings)
        # The concurrent edit was NOT clobbered, the source NOT cleaned.
        assert _read_settings(plan.target_path) == concurrent
        assert _read_settings(plan.source_path) == source_before

    def test_all_exact_batch_rechecks_target_before_cleaning_source(self, project_root, fake_home):
        """All-"exact" two-tier data-loss guard: when every applicable move
        re-classifies "exact" (target already carries the rule), nothing is
        written to the target but the source clean-up still runs. The target
        ``st_mtime_ns`` recheck must therefore fire on this batch too — a
        concurrent edit that removes the entry from the target between the read
        and the source write must ABORT, else the entry is dropped from the
        source while the target no longer holds it: lost from BOTH tiers.

        Pre-fix the recheck lived inside ``if moves_to_write:`` and was skipped
        whenever ``moves_to_write`` was empty (the all-"exact" batch), so the
        source was cleaned against a stale target snapshot."""
        import os
        import unittest.mock

        from memtomem.context import settings_migrate as migrate_mod

        plan = self._plan(project_root, fake_home)
        # Drift to all-"exact": the target already carries the canonical rule,
        # so every applicable move re-classifies exact (moves_to_write empty).
        _write_settings(plan.target_path, _settings_doc(_bundled_hook()))
        source_before = _read_settings(plan.source_path)

        # A concurrent process removes the entry from the target after our read.
        concurrent = {"hooks": {}, "_concurrent": True}
        orig_read = migrate_mod._read_with_mtime

        def patched_read(path):
            result = orig_read(path)
            if path == plan.target_path:
                _write_settings(plan.target_path, concurrent)
                st = plan.target_path.stat()
                os.utime(plan.target_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        with unittest.mock.patch.object(migrate_mod, "_read_with_mtime", patched_read):
            result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        # The fix: the source is NOT cleaned, so the entry survives in the
        # source instead of vanishing from both tiers.
        assert result.source_written is False
        assert any("modified by another process" in w for w in result.warnings)
        assert _read_settings(plan.source_path) == source_before
        assert _read_settings(plan.target_path) == concurrent

    def test_all_exact_batch_aborts_on_target_deletion(self, project_root, fake_home):
        """All-"exact" path, target DELETED mid-apply (not merely edited): the
        recheck compares against the missing-file sentinel ``0`` (not a bare
        ``is_file()`` guard), so a concurrent delete still aborts and the
        source is not cleaned against a target that no longer exists — the
        entry survives in the source instead of vanishing from both tiers."""
        import unittest.mock

        from memtomem.context import settings_migrate as migrate_mod

        plan = self._plan(project_root, fake_home)
        _write_settings(plan.target_path, _settings_doc(_bundled_hook()))  # force all-"exact"
        source_before = _read_settings(plan.source_path)

        orig_read = migrate_mod._read_with_mtime

        def patched_read(path):
            result = orig_read(path)
            if path == plan.target_path:
                plan.target_path.unlink()  # concurrent delete after our read
            return result

        with unittest.mock.patch.object(migrate_mod, "_read_with_mtime", patched_read):
            result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert any("modified by another process" in w for w in result.warnings)
        assert _read_settings(plan.source_path) == source_before
        assert not plan.target_path.exists()

    def test_aborts_on_source_mtime_change_after_target_write(self, project_root, fake_home):
        """Same second layer on the source clean-up: a direct edit during the
        strip computation survives; only the clean-up is refused (the target
        write already landed and stays)."""
        import os
        import unittest.mock

        from memtomem.context import settings_migrate as migrate_mod

        plan = self._plan(project_root, fake_home)
        concurrent = _settings_doc(_bundled_hook())
        concurrent["_concurrent"] = True
        orig_read = migrate_mod._read_with_mtime

        def patched_read(path):
            result = orig_read(path)
            if path == plan.source_path:
                _write_settings(plan.source_path, concurrent)
                st = plan.source_path.stat()
                os.utime(plan.source_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        with unittest.mock.patch.object(migrate_mod, "_read_with_mtime", patched_read):
            result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is True
        assert result.source_written is False
        assert any("modified by another process" in w for w in result.warnings)
        assert _read_settings(plan.source_path) == concurrent

    def test_malformed_target_refused_nothing_written(self, project_root, fake_home):
        """A target tier that exists but is not a JSON object is never
        rewritten — treating it as empty would replace the user's file with
        just the migrated rules (the apply-side analogue of the sync engine's
        ``MalformedSettingsError`` refusal)."""
        plan = self._plan(project_root, fake_home)
        source_before = _read_settings(plan.source_path)
        plan.target_path.parent.mkdir(parents=True, exist_ok=True)
        # VALID JSON whose root is not an object — the sneakier flavor
        # (the source-side test below covers the decode-error flavor).
        plan.target_path.write_text("[1, 2]", encoding="utf-8")

        result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is False
        assert result.source_written is False
        assert any("not valid JSON" in w for w in result.warnings)
        assert plan.target_path.read_text(encoding="utf-8") == "[1, 2]"
        assert _read_settings(plan.source_path) == source_before

    def test_malformed_source_warns_and_is_left_alone(self, project_root, fake_home):
        """A source tier corrupted after planning blocks only the clean-up:
        the target write proceeds, the corrupt source is left byte-identical,
        and the warning says the entries were not cleaned (previously this
        case was silent)."""
        plan = self._plan(project_root, fake_home)
        plan.source_path.write_text("[1, 2", encoding="utf-8")

        result = apply_migration(plan, surface="test_settings_migrate")

        assert result.target_written is True
        assert result.source_written is False
        assert any("not cleaned" in w for w in result.warnings)
        assert plan.source_path.read_text(encoding="utf-8") == "[1, 2"

    def test_opposite_direction_applies_cannot_clean_both_tiers(self, project_root, fake_home):
        """Codex review blocker on the first cut of this fix: with sequential
        per-tier locking, two opposite-direction applies starting from a
        duplicate state (both tiers carry the entry, e.g. after a crash
        between migrate's two writes) could BOTH classify their target as
        ``exact`` and then each clean its own source — deleting the entry
        from both tiers. The pair lock forces the whole classify→clean
        transaction to serialize, so the second apply re-classifies against
        the first one's outcome and exactly one tier keeps the entry.

        The barrier rendezvous in ``_target_rule_lookup`` (called once per
        apply, right after the locked target read) deterministically forces
        the broken interleaving when it is possible: under sequential locks
        both threads classify together, then both clean. Under the pair lock
        the second thread cannot even reach classification until the first
        releases, so the barrier times out (broken) and both proceed
        serialized."""
        import threading
        import unittest.mock

        from memtomem.context import settings_migrate as migrate_mod

        _write_canonical(project_root, _bundled_hook())
        user_path = fake_home / ".claude" / "settings.json"
        local_path = project_root / ".claude" / "settings.local.json"
        # Duplicate state: BOTH tiers carry the canonical entry.
        _write_settings(user_path, _settings_doc(_bundled_hook()))
        _write_settings(local_path, _settings_doc(_bundled_hook()))

        plan_ab = plan_migration(project_root, source_scope="user", target_scope="project_local")
        plan_ba = plan_migration(project_root, source_scope="project_local", target_scope="user")
        assert plan_ab.applicable_moves and plan_ba.applicable_moves

        barrier = threading.Barrier(2)
        orig_lookup = migrate_mod._target_rule_lookup

        def rendezvous_lookup(target_hooks):
            try:
                barrier.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                pass
            return orig_lookup(target_hooks)

        results: dict[str, MigrateResult] = {}
        errors: list[BaseException] = []

        def run(name: str, plan: MigratePlan) -> None:
            try:
                results[name] = apply_migration(plan, surface="test_settings_migrate")
            except BaseException as exc:  # surfaced via the errors list
                errors.append(exc)

        with unittest.mock.patch.object(migrate_mod, "_target_rule_lookup", rendezvous_lookup):
            t1 = threading.Thread(target=run, args=("ab", plan_ab))
            t2 = threading.Thread(target=run, args=("ba", plan_ba))
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

        assert not t1.is_alive() and not t2.is_alive()
        assert not errors
        assert set(results) == {"ab", "ba"}

        def _has_entry(path) -> bool:
            doc = _read_settings(path)
            return any(
                i.get("command") == "mm session start"
                for r in doc.get("hooks", {}).get("PostToolUse", [])
                for i in r.get("hooks", [])
                if isinstance(i, dict)
            )

        # Exactly ONE tier still carries the canonical entry — never zero.
        assert [_has_entry(user_path), _has_entry(local_path)].count(True) == 1
