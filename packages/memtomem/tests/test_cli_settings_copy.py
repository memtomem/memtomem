"""CLI tests for ``mm context settings-copy`` (#1281, A-11).

Engine semantics are covered in ``test_settings_copy.py``; this file pins
the surface: option gates, Gate B / host-write confirmation flows, the
``--json`` schema, destination-project resolution, and exit codes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.projects import KnownProjectsStore, compute_scope_id
from memtomem.context.settings import CANONICAL_SETTINGS_FILE

SECRET = "api_key=AKIA1234567890ABCDEF"


def _inner(command: str = "mm session start") -> dict:
    return {"type": "command", "command": command, "timeout": 5000}


def _seed_canonical(root: Path, command: str = "mm session start") -> None:
    path = root / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [_inner(command)]}]}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def cli_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two project roots + isolated HOME, cwd at project A (source)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for proj in (proj_a, proj_b):
        (proj / ".git").mkdir(parents=True)
        (proj / ".memtomem").mkdir()
    _seed_canonical(proj_a)

    kp = tmp_path / "known_projects.json"

    class _FakeCfg:
        known_projects_path = kp
        experimental_claude_projects_scan = False
        auto_display_configured_projects = True

    monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
    monkeypatch.chdir(proj_a)
    return {"a": proj_a.resolve(), "b": proj_b.resolve(), "home": home.resolve(), "kp": kp}


def _invoke(args: list[str], **kwargs):
    return CliRunner().invoke(context, args, **kwargs)


def _copy_args(dst: Path, *extra: str) -> list[str]:
    return [
        "settings-copy",
        "--event",
        "PostToolUse",
        "--matcher",
        "Edit|Write",
        "--to-project",
        str(dst),
        *extra,
    ]


# ── dry-run / gates ──────────────────────────────────────────────────


def test_dry_run_default_writes_nothing(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local"))
    assert result.exit_code == 0, result.output
    assert "Plan: copy hook [PostToolUse:Edit|Write]" in result.output
    assert "Run with --apply --confirm-project-shared to execute." in result.output
    assert not (cli_projects["b"] / CANONICAL_SETTINGS_FILE).exists()
    assert not (cli_projects["b"] / ".claude").exists()


def test_yes_requires_apply(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["b"], "--yes"))
    assert result.exit_code != 0
    assert "--yes is only valid with --apply" in result.output


def test_apply_with_yes_alone_refuses_gate_b(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply", "--yes"))
    assert result.exit_code != 0
    assert "--confirm-project-shared" in result.output
    assert "--yes alone is not sufficient" in result.output
    assert not (cli_projects["b"] / CANONICAL_SETTINGS_FILE).exists()


def test_apply_interactive_decline_aborts(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply"), input="n\n")
    assert result.exit_code != 0
    assert not (cli_projects["b"] / CANONICAL_SETTINGS_FILE).exists()


def test_apply_happy_path_project_local(cli_projects) -> None:
    result = _invoke(
        _copy_args(
            cli_projects["b"], "--to", "project_local", "--apply", "--confirm-project-shared"
        )
    )
    assert result.exit_code == 0, result.output
    assert "wrote canonical entry" in result.output
    assert "wrote stamped rule" in result.output
    assert "mm context sync --include=settings --scope project_local" in result.output

    canonical = json.loads(
        (cli_projects["b"] / CANONICAL_SETTINGS_FILE).read_text(encoding="utf-8")
    )
    assert canonical["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "mm session start"
    tier = json.loads(
        (cli_projects["b"] / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    )
    assert tier["hooks"]["PostToolUse"][0]["hooks"][0]["statusMessage"].startswith("memtomem · ")


def test_idempotent_rerun_needs_no_confirmation(cli_projects) -> None:
    first = _invoke(
        _copy_args(
            cli_projects["b"], "--to", "project_local", "--apply", "--confirm-project-shared"
        )
    )
    assert first.exit_code == 0, first.output
    # Second run is a no-op: no gate flags, no prompt, exit 0.
    second = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply"))
    assert second.exit_code == 0, second.output
    assert "nothing to do" in second.output


def test_user_tier_host_write_needs_yes(cli_projects) -> None:
    declined = _invoke(
        _copy_args(cli_projects["b"], "--to", "user", "--apply", "--confirm-project-shared"),
        input="n\n",  # Gate B satisfied by the flag; decline the host-write prompt
    )
    assert declined.exit_code != 0
    assert "outside the destination project" in declined.output
    assert not (cli_projects["home"] / ".claude" / "settings.json").exists()

    accepted = _invoke(
        _copy_args(
            cli_projects["b"],
            "--to",
            "user",
            "--apply",
            "--confirm-project-shared",
            "--yes",
        )
    )
    assert accepted.exit_code == 0, accepted.output
    assert (cli_projects["home"] / ".claude" / "settings.json").is_file()


def test_conflict_skips_and_exits_1_without_prompting(cli_projects) -> None:
    """A canonical conflict blocks both legs — no pending write, so no
    confirmation prompt; the report names the colliding entry."""
    dst_canonical = cli_projects["b"] / CANONICAL_SETTINGS_FILE
    dst_canonical.parent.mkdir(parents=True, exist_ok=True)
    dst_canonical.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [_inner("rival")]}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    before = dst_canonical.read_bytes()
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply"))
    assert result.exit_code == 1
    assert "'rival'" in result.output
    assert dst_canonical.read_bytes() == before


# ── destination resolution ───────────────────────────────────────────


def test_paused_registered_destination_refuses_with_resume_hint(cli_projects) -> None:
    store = KnownProjectsStore(cli_projects["kp"])
    store.add(cli_projects["b"])
    sid = compute_scope_id(cli_projects["b"])
    store.set_enabled_by_scope_id(sid, False)
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply"))
    assert result.exit_code != 0
    assert "paused" in result.output
    assert f"mm context projects resume {sid}" in result.output


def test_scope_id_selector_resolves(cli_projects) -> None:
    KnownProjectsStore(cli_projects["kp"]).add(cli_projects["b"])
    sid = compute_scope_id(cli_projects["b"])
    result = _invoke(
        [
            "settings-copy",
            "--event",
            "PostToolUse",
            "--matcher",
            "Edit|Write",
            "--to-project",
            sid,
            "--to",
            "project_local",
            "--apply",
            "--confirm-project-shared",
        ]
    )
    assert result.exit_code == 0, result.output
    assert (cli_projects["b"] / CANONICAL_SETTINGS_FILE).is_file()


def test_destination_without_store_gets_init_hint(cli_projects, tmp_path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    result = _invoke(_copy_args(bare, "--to", "project_local", "--apply"))
    assert result.exit_code != 0
    assert "no .memtomem/ store" in result.output
    assert "mm context init" in result.output


def test_unknown_selector_listed(cli_projects) -> None:
    result = _invoke(
        [
            "settings-copy",
            "--event",
            "SessionStart",
            "--to-project",
            str(cli_projects["b"]),
            "--to",
            "project_local",
        ]
    )
    assert result.exit_code != 0
    assert "available: PostToolUse:Edit|Write" in result.output


def test_same_project_destination_points_at_settings_migrate(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["a"], "--to", "project_local"))
    assert result.exit_code != 0
    assert "settings-migrate" in result.output


# ── Gate A via the CLI ───────────────────────────────────────────────


def test_gate_a_block_surfaces_and_writes_nothing(cli_projects) -> None:
    _seed_canonical(cli_projects["a"], command=f"echo {SECRET}")
    result = _invoke(
        _copy_args(
            cli_projects["b"], "--to", "project_local", "--apply", "--confirm-project-shared"
        )
    )
    assert result.exit_code != 0
    assert "git history is forever" in result.output
    # The plan preview echoes the user's own local command (the
    # settings-migrate/doctor display convention); the BLOCK MESSAGE must
    # not echo the matched bytes (privacy contract).
    error_tail = result.output.split("Error:", 1)[1]
    assert SECRET not in error_tail
    assert not (cli_projects["b"] / CANONICAL_SETTINGS_FILE).exists()


# ── --json schema ────────────────────────────────────────────────────


def test_json_dry_run_schema(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--json"))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "ok"
    assert payload["applied"] is False
    assert payload["event"] == "PostToolUse"
    assert payload["matcher"] == "Edit|Write"
    assert payload["command_preview"] == "mm session start"
    assert payload["dst_scope"] == "project_local"
    assert payload["canonical"] == {"state": "missing", "reason": ""}
    assert payload["target"] == {"state": "missing", "reason": ""}


def test_json_gate_b_needs_confirmation_exit_1(cli_projects) -> None:
    result = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply", "--json"))
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "needs_confirmation"
    assert "confirm-project-shared" in payload["hint"]


def test_json_user_tier_host_writes_listed(cli_projects) -> None:
    result = _invoke(
        _copy_args(
            cli_projects["b"],
            "--to",
            "user",
            "--apply",
            "--confirm-project-shared",
            "--json",
        )
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["status"] == "needs_confirmation"
    assert payload["host_writes"] == [str(cli_projects["home"] / ".claude" / "settings.json")]


def test_json_apply_reports_written_legs(cli_projects) -> None:
    result = _invoke(
        _copy_args(
            cli_projects["b"],
            "--to",
            "project_local",
            "--apply",
            "--confirm-project-shared",
            "--json",
        )
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["applied"] is True
    assert payload["canonical"]["written"] is True
    assert payload["target"]["written"] is True
    assert payload["sync_command"].endswith("--scope project_local")

    rerun = _invoke(_copy_args(cli_projects["b"], "--to", "project_local", "--apply", "--json"))
    assert rerun.exit_code == 0
    rerun_payload = json.loads(rerun.output)
    assert rerun_payload["status"] == "noop"
    assert rerun_payload["canonical"]["state"] == "exact"
    assert rerun_payload["target"]["state"] == "exact"
