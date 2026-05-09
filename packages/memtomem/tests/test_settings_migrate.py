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

import pytest
from click.testing import CliRunner

from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_migrate import (
    apply_migration,
    plan_migration,
)
from .helpers import set_home


# ── Helpers ────────────────────────────────────────────────────────


def _inner(command: str = "mm session start", *, type_: str = "command") -> dict:
    """One inner hook entry."""
    return {"type": type_, "command": command, "timeout": 5000}


def _rule(matcher: str = "Edit|Write", *, inners: list[dict] | None = None) -> dict:
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


# ── Apply unit tests ───────────────────────────────────────────────


class TestApplyMigration:
    def test_round_trip_user_to_project_local(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan = plan_migration(project_root, source_scope="user", target_scope="project_local")
        result = apply_migration(plan)
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

    def test_idempotent_rerun(self, project_root, fake_home):
        """After a successful apply, a second plan/apply is a no-op."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _settings_doc(_bundled_hook()))
        plan1 = plan_migration(project_root, source_scope="user", target_scope="project_local")
        apply_migration(plan1)

        plan2 = plan_migration(project_root, source_scope="user", target_scope="project_local")
        assert plan2.is_noop is True
        result2 = apply_migration(plan2)
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
        result = apply_migration(plan)
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
        apply_migration(plan)

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
        result = apply_migration(plan)
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
        apply_migration(plan)

        user_doc = _read_settings(fake_home / ".claude" / "settings.json")
        assert user_doc["permissions"] == {"allow": ["Bash(ls *)"]}
        assert user_doc["model"] == "claude-3-5-sonnet"


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

    def test_project_to_project_no_host_prompt(self, project_root, fake_home, monkeypatch):
        """project_shared → project_local stays in-tree, so no
        host-write prompt fires even without --yes."""
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
        )
        assert result.exit_code == 0, result.output
        assert (project_root / ".claude" / "settings.local.json").is_file()
