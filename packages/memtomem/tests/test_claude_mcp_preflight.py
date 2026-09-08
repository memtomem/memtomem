"""Registration diagnostics must not start servers or edit client settings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from memtomem._claude_plugin_contract import CORE_VERSION, TOOL_MODE
from memtomem.cli import cli, init_cmd
from memtomem.cli import _claude_mcp as checks

from .helpers import set_home


def write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def pinned() -> dict:
    return {
        "command": "uvx",
        "args": ["--from", f"memtomem=={CORE_VERSION}", "memtomem-server"],
        "env": {"MEMTOMEM_TOOL_MODE": TOOL_MODE},
    }


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.chdir(project)
    monkeypatch.setattr(checks, "_managed_paths", lambda: [])
    executable = str((tmp_path / "bin/claude").absolute())
    original_which = checks.shutil.which
    monkeypatch.setattr(
        checks.shutil,
        "which",
        lambda name: executable if name == "claude" else original_which(name),
    )
    state = SimpleNamespace(home=home, project=project, rows=[], calls=[], error=None)

    def run(command, **kwargs):
        state.calls.append(command)
        assert command == [executable, "plugin", "list", "--json"]
        assert kwargs["timeout"] == 10
        assert kwargs["cwd"] == project
        if state.error:
            raise state.error
        return SimpleNamespace(returncode=0, stdout=json.dumps(state.rows))

    monkeypatch.setattr(checks.subprocess, "run", run)
    return state


def plugin(sandbox, *, entry=None, enabled=True, scope="user", project=None):
    root = sandbox.home / "plugin"
    write(root / ".claude-plugin/plugin.json", {"name": "memtomem"})
    write(root / ".mcp.json", {"mcpServers": {"memtomem": entry or pinned()}})
    sandbox.rows.append(
        {
            "id": "memtomem@memtomem",
            "enabled": enabled,
            "scope": scope,
            "installPath": str(root),
            "projectPath": str(project or sandbox.project),
        }
    )
    return root


def manual(sandbox, *, entry=None, name="memtomem", scope="user", local_extra=None):
    row = {name: entry or {"command": "memtomem-server"}}
    if scope == "project":
        write(sandbox.project / ".mcp.json", {"mcpServers": row})
        write(
            sandbox.home / ".claude.json",
            {
                "projects": {
                    str(sandbox.project): {
                        "enabledMcpjsonServers": [name],
                        **(local_extra or {}),
                    }
                }
            },
        )
    else:
        write(
            sandbox.home / ".claude.json",
            {
                "mcpServers": row if scope == "user" else {},
                "projects": {
                    str(sandbox.project): {
                        "mcpServers": row if scope == "local" else {},
                        **(local_extra or {}),
                    }
                },
            },
        )


def codes(report):
    return {item["code"] for item in report.findings}


def test_empty_inventory_is_read_only_and_starts_no_mcp(sandbox):
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 0 and not report.reusable
    assert list(sandbox.home.iterdir()) == []
    assert list(sandbox.project.iterdir()) == []
    assert len(sandbox.calls) == 1


def test_plugin_only_reused(sandbox):
    plugin(sandbox)
    assert checks.inspect_claude_mcp().reusable
    assert checks.preflight() is False


@pytest.mark.parametrize("scope", ["user", "local", "project"])
def test_manual_only_reports_future_risk_but_reuses_existing(sandbox, scope):
    manual(sandbox, scope=scope)
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 1 and report.install_risk and not report.current_risk
    assert checks.preflight() is False
    finding = next(f for f in report.findings if f["code"] == "manual_registration")
    assert finding["remediation"] == f"claude mcp remove memtomem -s {scope}"


@pytest.mark.parametrize("name", ["memtomem", "memory_alias"])
def test_different_command_refused_and_names_are_reported(sandbox, name):
    plugin(sandbox)
    manual(sandbox, name=name)
    report = checks.inspect_claude_mcp()
    assert report.complete and report.current_risk and report.exit_code == 1
    with pytest.raises(click.exceptions.Exit) as exc:
        checks.preflight()
    assert exc.value.exit_code == 1
    assert name in json.dumps(report.payload())


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "uvx", "args": ["--from", "memtomem==0.1.0", "memtomem-server"]},
        {
            "command": "uvx",
            "args": ["--isolated", "--from", "memtomem[all]==0.5.0", "memtomem-server"],
        },
        {"command": "/opt/uv", "args": ["run", "--directory", "/src", "memtomem-server"]},
        {"command": "C:\\venv\\python.exe", "args": ["-m", "memtomem.server"]},
    ],
)
def test_versions_extras_source_and_python_are_different_launches(sandbox, entry):
    plugin(sandbox)
    manual(sandbox, name="alias", entry=entry)
    assert checks.inspect_claude_mcp().current_risk


def test_exact_launch_dedup_ignores_env_but_warns_without_secrets(sandbox):
    plugin(sandbox)
    entry = pinned()
    entry["env"] = {"TOKEN": "private-value"}
    manual(sandbox, entry=entry)
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 0 and report.reusable
    assert {"native_dedup", "environment_difference"} <= codes(report)
    assert "private-value" not in json.dumps(report.payload())
    assert "TOKEN" not in json.dumps(report.payload())


def test_stm_is_not_memtomem(sandbox):
    write(
        sandbox.home / ".claude.json",
        {
            "mcpServers": {
                "memtomem-stm": {
                    "command": "uvx",
                    "args": ["--from", "memtomem-stm", "mms", "server"],
                }
            }
        },
    )
    assert checks.inspect_claude_mcp().exit_code == 0
    assert checks.preflight() is True


def test_scope_precedence_applies_to_all_names_even_non_memtomem(sandbox):
    plugin(sandbox)
    write(
        sandbox.home / ".claude.json",
        {
            "mcpServers": {"alias": {"command": "memtomem-server"}},
            "projects": {str(sandbox.project): {"mcpServers": {"alias": {"command": "other"}}}},
        },
    )
    assert checks.inspect_claude_mcp().exit_code == 0


def test_local_wins_over_project_and_user(sandbox):
    plugin(sandbox)
    manual(sandbox)
    write(
        sandbox.project / ".mcp.json", {"mcpServers": {"memtomem": {"command": "memtomem-server"}}}
    )
    write(
        sandbox.home / ".claude.json",
        {
            "mcpServers": {"memtomem": {"command": "memtomem-server"}},
            "projects": {str(sandbox.project): {"mcpServers": {"memtomem": pinned()}}},
        },
    )
    assert checks.inspect_claude_mcp().exit_code == 0


@pytest.mark.parametrize("enabled,scope,other", [(False, "user", False), (True, "local", True)])
def test_disabled_and_other_project_plugins_do_not_supply_connection(
    sandbox, enabled, scope, other
):
    plugin(sandbox, enabled=enabled, scope=scope, project=sandbox.home if other else None)
    assert checks.preflight() is True


def test_disabled_manual_preserved_and_not_replaced(sandbox):
    manual(sandbox, local_extra={"disabledMcpServers": ["memtomem"]})
    assert checks.inspect_claude_mcp().exit_code == 0
    with pytest.raises(click.exceptions.Exit):
        checks.preflight()
    plugin(sandbox)
    assert checks.preflight() is False


def test_pending_project_is_incomplete(sandbox):
    manual(sandbox, scope="project", local_extra={"enabledMcpjsonServers": []})
    assert checks.inspect_claude_mcp().exit_code == 2


def test_untrusted_repo_cannot_approve_itself(sandbox):
    manual(sandbox, scope="project", local_extra={"enabledMcpjsonServers": []})
    write(sandbox.project / ".claude/settings.json", {"enableAllProjectMcpServers": True})
    assert checks.inspect_claude_mcp().exit_code == 2


def test_rejected_project_does_not_conflict_with_plugin(sandbox):
    plugin(sandbox)
    manual(sandbox, scope="project", local_extra={"disabledMcpjsonServers": ["memtomem"]})
    assert checks.inspect_claude_mcp().exit_code == 0


@pytest.mark.parametrize(
    "error", [FileNotFoundError(), subprocess.TimeoutExpired("claude", 10), OSError()]
)
def test_cli_failures_are_incomplete_without_leaking_errors(sandbox, error):
    sandbox.error = error
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 2 and not report.complete
    with pytest.raises(click.exceptions.Exit) as exc:
        checks.preflight()
    assert exc.value.exit_code == 2


@pytest.mark.parametrize("value", [[], {"mcpServers": []}, {"projects": []}, {"projects": {}}])
def test_malformed_config_is_not_clean(sandbox, value):
    path = sandbox.home / ".claude.json"
    write(path, value)
    if value == {"projects": {}}:
        path.write_text('{"secret": "do-not-echo",', encoding="utf-8")
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 2
    assert "do-not-echo" not in json.dumps(report.payload())


def test_custom_config_dir_never_falls_back_to_default_account(sandbox, monkeypatch):
    manual(sandbox)
    custom = sandbox.home / "custom"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom))
    assert checks.inspect_claude_mcp().exit_code == 0
    write(custom / ".claude.json", {"mcpServers": {"alias": {"command": "memtomem-server"}}})
    assert checks.inspect_claude_mcp().install_risk


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "sh", "args": ["-c", "memtomem-server --token secret"]},
        {"command": "${SERVER}"},
        {"type": "http", "url": "https://secret.example/?token=secret"},
    ],
)
def test_unclassifiable_launches_never_run_or_leak(sandbox, entry):
    manual(sandbox, entry=entry)
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 2
    assert "secret" not in json.dumps(report.payload())


def test_installed_plugin_pin_used_instead_of_package_version(sandbox):
    entry = pinned()
    entry["args"][1] = "memtomem==0.2.0"
    plugin(sandbox, entry=entry)
    manual(sandbox, entry=entry)
    assert checks.inspect_claude_mcp().exit_code == 0


def test_corrupt_plugin_manifest_is_incomplete(sandbox):
    root = plugin(sandbox)
    (root / ".mcp.json").write_text("invalid", encoding="utf-8")
    assert checks.inspect_claude_mcp().exit_code == 2


def test_managed_or_parent_config_is_incomplete(sandbox, monkeypatch):
    managed = sandbox.home / "managed.json"
    write(managed, {})
    monkeypatch.setattr(checks, "_managed_paths", lambda: [managed])
    assert checks.inspect_claude_mcp().exit_code == 2
    monkeypatch.setattr(checks, "_managed_paths", lambda: [])
    write(sandbox.project.parent / ".mcp.json", {})
    assert checks.inspect_claude_mcp().exit_code == 2


def test_doctor_json_and_exit_codes(sandbox):
    manual(sandbox)
    result = CliRunner().invoke(cli, ["doctor", "--claude-mcp", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["plugin_install_risk"] is True
    sandbox.error = FileNotFoundError()
    result = CliRunner().invoke(cli, ["doctor", "--claude-mcp", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["complete"] is False


@pytest.mark.parametrize("mode", ["claude", "json"])
def test_init_refuses_before_writes_and_never_uses_fallback(sandbox, mode):
    plugin(sandbox)
    manual(sandbox)
    original = (sandbox.home / ".claude.json").read_bytes()
    result = CliRunner().invoke(cli, ["init", "--non-interactive", "--mcp", mode])
    assert result.exit_code == 1
    assert (sandbox.home / ".claude.json").read_bytes() == original
    assert not (sandbox.project / ".mcp.json").exists()
    assert not (sandbox.home / ".memtomem").exists()
    assert not (sandbox.home / "memories").exists()


@pytest.mark.parametrize("mode", ["claude", "json"])
def test_init_unknown_before_writes(sandbox, mode):
    sandbox.error = FileNotFoundError()
    result = CliRunner().invoke(cli, ["init", "--non-interactive", "--mcp", mode])
    assert result.exit_code == 2
    assert list(sandbox.home.iterdir()) == []
    assert list(sandbox.project.iterdir()) == []


@pytest.mark.parametrize("choice", [3, 4])
def test_skip_and_kimi_do_not_inspect_claude(sandbox, choice):
    assert init_cmd._preflight_mcp_choice(choice)
    assert sandbox.calls == []


@pytest.mark.parametrize("mode", ["claude", "json"])
@pytest.mark.parametrize("existing", ["plugin", "manual"])
def test_init_preserves_existing_connection_and_initializes_store(
    sandbox, monkeypatch, mode, existing
):
    if existing == "plugin":
        plugin(sandbox)
    else:
        manual(sandbox)
    original = {p: p.read_bytes() for p in sandbox.home.rglob("*.json")}

    def no_registration(*args, **kwargs):
        raise AssertionError("Existing connection must not be registered again")

    monkeypatch.setattr(init_cmd, "_run", no_registration)
    result = CliRunner().invoke(
        cli, ["init", "--preset", "minimal", "--non-interactive", "--mcp", mode]
    )
    assert result.exit_code == 0, result.output
    assert "additional MCP registration skipped" in result.output
    assert (sandbox.home / ".memtomem/config.json").is_file()
    assert not (sandbox.project / ".mcp.json").exists()
    assert all(p.read_bytes() == value for p, value in original.items())


def test_init_rechecks_new_plugin_before_registering(sandbox, monkeypatch):
    from .test_init_cmd import _make_init_state

    state = _make_init_state(sandbox.home)
    state["mcp_choice"] = 1
    real_inspect = checks.inspect_claude_mcp
    calls = 0

    def changing_inventory():
        nonlocal calls
        calls += 1
        if calls == 2:
            plugin(sandbox)
        return real_inspect()

    monkeypatch.setattr(checks, "inspect_claude_mcp", changing_inventory)
    monkeypatch.setattr(init_cmd, "_run", lambda *a, **k: pytest.fail("must not register"))
    init_cmd._write_config_and_summary(state, sandbox.home)
    assert calls == 2
    assert not (sandbox.project / ".mcp.json").exists()
    assert not (sandbox.home / ".claude.json").exists()


def test_init_skip_works_when_claude_is_unavailable(sandbox):
    sandbox.error = FileNotFoundError()
    result = CliRunner().invoke(
        cli, ["init", "--preset", "minimal", "--non-interactive", "--mcp", "skip"]
    )
    assert result.exit_code == 0, result.output
    assert sandbox.calls == []
    assert (sandbox.home / ".memtomem/config.json").is_file()


@pytest.mark.parametrize(
    "mutation",
    [
        {"scope": []},
        {"enabled": "true"},
        {"installPath": ""},
        {"scope": "project", "projectPath": None},
    ],
)
def test_malformed_plugin_rows_are_incomplete(sandbox, mutation):
    plugin(sandbox)
    sandbox.rows[0].update(mutation)
    assert checks.inspect_claude_mcp().exit_code == 2


def test_multiple_aliases_require_review(sandbox):
    write(
        sandbox.home / ".claude.json",
        {
            "mcpServers": {
                "memtomem": pinned(),
                "another": pinned(),
            }
        },
    )
    assert checks.inspect_claude_mcp().current_risk


def test_unsafe_server_name_is_not_emitted_as_a_command(sandbox):
    manual(sandbox, name="--user")
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 2
    assert "remove --user" not in json.dumps(report.payload())


@pytest.mark.parametrize(
    "settings",
    [
        {"enableAllProjectMcpServers": "true"},
        {"allowedMcpServers": []},
    ],
)
def test_unresolved_settings_policy_is_incomplete(sandbox, settings):
    write(sandbox.home / ".claude/settings.json", settings)
    assert checks.inspect_claude_mcp().exit_code == 2


@pytest.mark.parametrize("enabled", [True, False])
def test_same_plugin_id_uses_local_installation_over_user(sandbox, enabled):
    plugin(sandbox)
    plugin(sandbox, scope="local", enabled=enabled)
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 0
    assert report.reusable is enabled


def test_ancestor_scoped_plugin_requires_project_root(sandbox):
    plugin(sandbox, scope="project", project=sandbox.project.parent)
    assert checks.inspect_claude_mcp().exit_code == 2


def test_remediation_uses_installed_marketplace_and_scope(sandbox):
    plugin(sandbox, scope="local")
    sandbox.rows[0]["id"] = "memtomem@team-marketplace"
    manual(sandbox)
    result = CliRunner().invoke(cli, ["doctor", "--claude-mcp"])
    assert result.exit_code == 1
    assert "claude plugin uninstall memtomem@team-marketplace -s local" in result.output
    assert "uninstall memtomem@memtomem" not in result.output


def test_missing_cli_on_path_does_not_start_a_process(sandbox, monkeypatch):
    monkeypatch.setattr(checks.shutil, "which", lambda name: None)
    assert checks.inspect_claude_mcp().exit_code == 2
    assert sandbox.calls == []


@pytest.mark.parametrize(
    "script",
    [
        "python -m memtomem.server",
        "exec python3 -m memtomem.server",
        'python -c "import memtomem.server; memtomem.server.main()"',
        "python -m memtomem.server.__main__",
    ],
)
def test_aliased_module_wrapper_defers_registration(sandbox, script):
    manual(sandbox, name="my-memory", entry={"command": "sh", "args": ["-c", script]})
    before = (sandbox.home / ".claude.json").read_bytes()
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 2
    assert not report.reusable
    assert "manual_registration" in codes(report)
    result = CliRunner().invoke(cli, ["init", "--non-interactive", "--mcp", "claude"])
    assert result.exit_code == 2, result.output
    assert (sandbox.home / ".claude.json").read_bytes() == before
    assert not (sandbox.project / ".mcp.json").exists()
    assert not (sandbox.home / ".memtomem").exists()


@pytest.mark.parametrize(
    "module", ["memtomem.serverish", "other.memtomem.server", "memtomem_stm.server"]
)
def test_other_module_wrappers_do_not_become_memtomem_candidates(sandbox, module):
    manual(sandbox, name="other", entry={"command": "sh", "args": ["-c", f"python -m {module}"]})
    assert checks.preflight() is True


@pytest.mark.parametrize("scope", ["user", "project", "local"])
@pytest.mark.parametrize("mode", ["claude", "json"])
def test_disabled_plugin_server_is_not_a_reusable_connection(sandbox, scope, mode):
    plugin(sandbox, scope=scope)
    write(
        sandbox.home / ".claude.json",
        {
            "projects": {
                str(sandbox.project): {
                    "disabledMcpServers": ["plugin:memtomem:memtomem"],
                }
            }
        },
    )
    before = (sandbox.home / ".claude.json").read_bytes()
    report = checks.inspect_claude_mcp()
    assert report.complete
    assert not report.reusable
    assert "plugin_server_disabled" in codes(report)
    assert "plugin_enabled" not in codes(report)
    result = CliRunner().invoke(cli, ["init", "--non-interactive", "--mcp", mode])
    assert result.exit_code == 1, result.output
    assert "disabled" in result.output
    assert "additional MCP registration skipped" not in result.output
    assert (sandbox.home / ".claude.json").read_bytes() == before
    assert not (sandbox.home / ".memtomem").exists()
    assert not (sandbox.project / ".mcp.json").exists()


def test_active_manual_connection_can_be_reused_with_disabled_plugin_server(sandbox):
    plugin(sandbox)
    manual(sandbox, local_extra={"disabledMcpServers": ["plugin:memtomem:memtomem"]})
    report = checks.inspect_claude_mcp()
    assert report.reusable and not report.current_risk
    assert checks.preflight() is False


def test_disabling_other_plugin_server_does_not_disable_memtomem(sandbox):
    plugin(sandbox)
    write(
        sandbox.home / ".claude.json",
        {
            "projects": {
                str(sandbox.project): {
                    "disabledMcpServers": ["plugin:other:memtomem"],
                }
            }
        },
    )
    assert checks.inspect_claude_mcp().reusable


def test_disabled_server_marker_does_not_hide_other_plugin_servers(sandbox):
    root = plugin(sandbox)
    write(root / ".mcp.json", {"mcpServers": {"another-server": pinned()}})
    write(
        sandbox.home / ".claude.json",
        {
            "projects": {
                str(sandbox.project): {
                    "disabledMcpServers": ["plugin:memtomem:memtomem"],
                }
            }
        },
    )
    report = checks.inspect_claude_mcp()
    assert report.exit_code == 2
    assert "plugin_server_disabled" not in codes(report)
    with pytest.raises(click.exceptions.Exit) as exc:
        checks.preflight()
    assert exc.value.exit_code == 2
