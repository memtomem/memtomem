"""CLI tests for ``mm context projects`` (#1272, campaign #1270).

The group is the CLI face of the same ``KnownProjectsStore`` /
``discover_project_scopes`` surface the web portal uses; these tests pin:

- ``list`` shows the server-cwd scope plus registered entries, with the
  ``--json`` payload carrying the web-parity field names;
- ``add`` is idempotent and distinguishes "Registered" from "Already
  registered";
- ``pause`` / ``resume`` flip the enrollment flag the ``--all`` batches and
  the web Sync gate honor, and refuse unregistered scopes with a hint;
- ``remove`` refuses scopes that have no registry entry;
- a corrupt ``known_projects.json`` surfaces the named strict-load error and
  is NOT re-baselined by a mutation (#1247 id 16 parity).

Isolation: ``ContextGatewayConfig`` is monkeypatched (the established
``_patch_known_projects_path`` pattern from ``test_context_update.py``)
and HOME/USERPROFILE point into ``tmp_path`` so the optional
``~/.claude/projects`` scan can never read the host (Windows resolves
``Path.home()`` via USERPROFILE first — patch both).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.projects import KnownProjectsStore, compute_scope_id


@pytest.fixture()
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    kp = tmp_path / "known_projects.json"

    class _FakeCfg:
        known_projects_path = kp
        # Production defaults — the HOME patch above keeps the scan hermetic.
        experimental_claude_projects_scan = False
        auto_display_configured_projects = True

    monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())

    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    other = tmp_path / "other"
    other.mkdir()
    return {"kp": kp, "cwd": cwd, "other": other}


def test_list_shows_cwd_and_registered(cli_env: dict[str, Path]) -> None:
    KnownProjectsStore(cli_env["kp"]).add(cli_env["other"], label="Other")
    result = CliRunner().invoke(context, ["projects", "list"])
    assert result.exit_code == 0, result.output
    assert "2 project scope(s):" in result.output
    assert "Server CWD" in result.output
    assert "Other" in result.output


def test_list_json_carries_web_parity_fields(cli_env: dict[str, Path]) -> None:
    KnownProjectsStore(cli_env["kp"]).add(cli_env["other"])
    result = CliRunner().invoke(context, ["projects", "list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert {s["scope_id"] for s in payload["scopes"]} == {
        compute_scope_id(cli_env["cwd"]),
        compute_scope_id(cli_env["other"]),
    }
    row = payload["scopes"][0]
    for field in (
        "scope_id",
        "label",
        "root",
        "tier",
        "sources",
        "missing",
        "stale",
        "experimental",
        "enabled",
        "sync_eligible",
    ):
        assert field in row, f"missing web-parity field {field!r}"


def test_add_is_idempotent_with_distinct_copy(cli_env: dict[str, Path]) -> None:
    runner = CliRunner()
    first = runner.invoke(context, ["projects", "add", str(cli_env["other"])])
    assert first.exit_code == 0, first.output
    assert "Registered:" in first.output
    second = runner.invoke(context, ["projects", "add", str(cli_env["other"])])
    assert second.exit_code == 0, second.output
    assert "Already registered:" in second.output
    assert len(KnownProjectsStore(cli_env["kp"]).load()) == 1


def test_pause_resume_round_trip(cli_env: dict[str, Path]) -> None:
    KnownProjectsStore(cli_env["kp"]).add(cli_env["other"])
    scope_id = compute_scope_id(cli_env["other"])
    runner = CliRunner()

    paused = runner.invoke(context, ["projects", "pause", scope_id])
    assert paused.exit_code == 0, paused.output
    listing = runner.invoke(context, ["projects", "list", "--json"])
    rows = {s["scope_id"]: s for s in json.loads(listing.output)["scopes"]}
    assert rows[scope_id]["enabled"] is False
    assert rows[scope_id]["sync_eligible"] is False

    resumed = runner.invoke(context, ["projects", "resume", str(cli_env["other"])])
    assert resumed.exit_code == 0, resumed.output
    listing = runner.invoke(context, ["projects", "list", "--json"])
    rows = {s["scope_id"]: s for s in json.loads(listing.output)["scopes"]}
    assert rows[scope_id]["enabled"] is True
    assert rows[scope_id]["sync_eligible"] is True


def test_pause_unregistered_scope_hints_add(cli_env: dict[str, Path]) -> None:
    # Server cwd is discovered but has no registry entry.
    result = CliRunner().invoke(context, ["projects", "pause", str(cli_env["cwd"])])
    assert result.exit_code != 0
    assert "mm context projects add" in result.output


def test_remove_registered_and_unregistered(cli_env: dict[str, Path]) -> None:
    KnownProjectsStore(cli_env["kp"]).add(cli_env["other"])
    runner = CliRunner()
    removed = runner.invoke(context, ["projects", "remove", str(cli_env["other"])])
    assert removed.exit_code == 0, removed.output
    assert KnownProjectsStore(cli_env["kp"]).load() == []

    again = runner.invoke(context, ["projects", "remove", str(cli_env["other"])])
    assert again.exit_code != 0
    assert "no known_projects entry" in again.output


def test_unknown_selector_is_a_usage_error(cli_env: dict[str, Path]) -> None:
    result = CliRunner().invoke(context, ["projects", "pause", "p-000000000000"])
    assert result.exit_code != 0
    assert "no discovered project has scope_id" in result.output


def test_corrupt_registry_named_error_and_no_rebaseline(cli_env: dict[str, Path]) -> None:
    corrupt = b"{not json"
    cli_env["kp"].write_bytes(corrupt)
    result = CliRunner().invoke(context, ["projects", "add", str(cli_env["other"])])
    assert result.exit_code != 0
    assert "not valid JSON" in result.output
    # Strict-load refusal must leave the corrupt bytes untouched (#1247 id 16):
    # a tolerant fallback would have re-baselined the file to one entry.
    assert cli_env["kp"].read_bytes() == corrupt
