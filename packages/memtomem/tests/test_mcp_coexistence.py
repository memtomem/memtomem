"""Host inventory and reconciliation stay offline and preserve other settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli import _mcp_coexistence as checks

from .helpers import set_home


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture(params=["agy", "codex"])
def host(request, tmp_path, monkeypatch):
    client = request.param
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.chdir(project)
    root = home / (".codex" if client == "codex" else ".gemini/config")
    plugin = root / (
        "plugins/cache/memtomem/memtomem/0.1.0" if client == "codex" else "plugins/memtomem"
    )
    manifest = plugin / (".codex-plugin/plugin.json" if client == "codex" else "plugin.json")
    write(manifest, {"name": "memtomem"})
    bundled = plugin / (".mcp.json" if client == "codex" else "mcp_config.json")
    write(bundled, {"mcpServers": {"memtomem": {"command": "uvx", "args": ["memtomem-server"]}}})
    config = root / ("config.toml" if client == "codex" else "mcp_config.json")
    if client == "codex":
        config.write_text(
            'model = "test"\n[mcp_servers.memtomem]\ncommand = "memtomem-server"\n[mcp_servers.memtomem.env]\nTOKEN = "private-value"\n[mcp_servers.other]\ncommand = "other"\n'
        )
    else:
        write(
            config,
            {
                "otherSetting": "keep",
                "mcpServers": {
                    "memtomem": {"command": "memtomem-server", "env": {"TOKEN": "private-value"}},
                    "other": {"command": "other"},
                },
            },
        )
    row = {
        "name": "memtomem",
        "enabled": True,
        "path": str(plugin),
        "marketplaceName": "memtomem",
        "version": "0.1.0",
    }
    state = SimpleNamespace(
        client=client,
        root=root,
        plugin=plugin,
        bundled=bundled,
        config=config,
        rows=[row],
        error=None,
        calls=[],
    )
    monkeypatch.setattr(checks.shutil, "which", lambda name: str(tmp_path / name))

    def run(command, **kwargs):
        state.calls.append(command)
        assert command == [str(tmp_path / client), "plugin", "list", "--json"]
        assert kwargs["timeout"] == 10
        if state.error:
            raise state.error
        data = {"installed": state.rows} if client == "codex" else state.rows
        return SimpleNamespace(returncode=0, stdout=json.dumps(data))

    monkeypatch.setattr(checks.subprocess, "run", run)
    return state


def invoke(host, *args):
    return CliRunner().invoke(cli, ["doctor", "--mcp-client", host.client, "--json", *args])


def test_inventory_rules_read_only_without_secrets(host):
    before = {p: p.read_bytes() for p in host.root.rglob("*") if p.is_file()}
    result = invoke(host)
    assert result.exit_code == (0 if host.client == "codex" else 1), result.output
    codes = {f["code"] for f in json.loads(result.output)["findings"]}
    assert ("native_dedup" if host.client == "codex" else "duplicate_risk") in codes
    assert "private-value" not in result.output and "TOKEN" not in result.output
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.parametrize("choice", ["keep-manual", "keep-plugin"])
def test_apply_preserves_other_config_and_backup(host, choice):
    original = host.config.read_bytes()
    bundled = host.bundled.read_bytes()
    preview = invoke(host, "--reconcile", choice)
    assert preview.exit_code in (0, 1), preview.output
    assert host.config.read_bytes() == original and host.bundled.read_bytes() == bundled
    result = invoke(host, "--reconcile", choice, "--apply")
    assert result.exit_code == 0, result.output
    assert "private-value" not in result.output
    if choice == "keep-manual":
        assert host.config.read_bytes() == original
        if host.client == "agy":
            assert json.loads(host.bundled.read_bytes())["mcpServers"] == {}
        else:
            assert host.bundled.read_bytes() == bundled
    else:
        assert host.bundled.read_bytes() == bundled
        data = (
            tomllib.loads(host.config.read_text())
            if host.client == "codex"
            else json.loads(host.config.read_bytes())
        )
        key = "mcp_servers" if host.client == "codex" else "mcpServers"
        assert data[key] == {"other": {"command": "other"}}
        backup = next(host.config.parent.glob(host.config.name + ".mm-backup-*"))
        assert backup.read_bytes() == original
        if os.name != "nt":
            assert backup.stat().st_mode & 0o777 == 0o600
    again = invoke(host, "--reconcile", choice, "--apply")
    assert again.exit_code == 0, again.output


@pytest.mark.parametrize("omitted", ["absent", "empty"])
def test_skills_only(host, omitted):
    if omitted == "absent":
        host.bundled.unlink()
    else:
        write(host.bundled, {"mcpServers": {}})
    result = invoke(host)
    assert result.exit_code == 0, result.output
    assert "skills_only" in result.output
    host.config.unlink()
    assert invoke(host).exit_code == 2


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("inventory", 10)])
def test_inventory_failure_refuses_write(host, error):
    host.error = error
    before = host.bundled.read_bytes()
    result = invoke(host, "--reconcile", "keep-manual", "--apply")
    assert result.exit_code == 2
    assert host.bundled.read_bytes() == before
    assert "Reconciliation skipped: inspection is incomplete" in result.output


@pytest.mark.parametrize(
    "bad", [[], {"mcpServers": []}, {"mcpServers": {"memtomem": {"command": "secret-wrapper"}}}]
)
def test_invalid_plugin_refused(host, bad):
    write(host.bundled, bad)
    before = host.config.read_bytes()
    result = invoke(host, "--reconcile", "keep-plugin", "--apply")
    assert result.exit_code == 2
    assert host.config.read_bytes() == before
    assert "secret-wrapper" not in result.output


def test_disabled_plugin_is_not_keep_plugin_target(host):
    host.rows[0]["enabled"] = False
    before = host.config.read_bytes()
    assert invoke(host, "--reconcile", "keep-plugin", "--apply").exit_code == 2
    assert host.config.read_bytes() == before


def test_changed_config_refuses_write(host):
    report = checks.inspect_host(host.client)
    original = host.bundled.read_bytes()
    host.config.write_text("changed by client")
    checks.reconcile(report, host.client, "keep-plugin", apply=True)
    assert report.exit_code == 2
    assert host.config.read_text() == "changed by client"
    assert host.bundled.read_bytes() == original
    assert any(f.get("path") == str(host.config) for f in report.findings)


@pytest.mark.requires_symlinks
def test_symlink_target_refused(host):
    target = host.config.with_name("target")
    host.config.rename(target)
    host.config.symlink_to(target)
    before = target.read_bytes()
    assert invoke(host, "--reconcile", "keep-plugin", "--apply").exit_code == 2
    assert target.read_bytes() == before


def test_codex_alias_is_not_name_dedup(host):
    if host.client != "codex":
        return
    host.config.write_text(
        host.config.read_text().replace("mcp_servers.memtomem", "mcp_servers.alias")
    )
    assert invoke(host).exit_code == 1
    assert invoke(host, "--reconcile", "keep-manual", "--apply").exit_code == 0
    assert json.loads(host.bundled.read_bytes())["mcpServers"] == {}


@pytest.mark.parametrize(
    "text",
    [
        'mcp_servers = {memtomem = {command = "memtomem-server"}, other = {command = "other"}}\n',
        '[mcp_servers.memtomem]\ncommand = "memtomem-server"\n[mcp_servers.other]\ncommand = "other"\nnote = """\n[mcp_servers.memtomem]\nkeep this string\n"""\n',
    ],
)
def test_unsupported_toml_never_loses_other_content(text):
    with pytest.raises(ValueError):
        checks._remove_toml(text.encode(), ("mcp_servers", "memtomem"))


@pytest.mark.parametrize(
    "args", [["--apply"], ["--reconcile", "keep-manual"], ["--claude-mcp", "--mcp-client", "codex"]]
)
def test_invalid_options_are_usage_errors(args):
    assert CliRunner().invoke(cli, ["doctor", *args]).exit_code == 2


@pytest.mark.parametrize("flag", [True, "yes"])
def test_disabled_or_invalid_manual_is_not_keep_manual_target(host, flag):
    if host.client == "codex":
        value = "false" if flag is True else '"no"'
        host.config.write_text(
            host.config.read_text().replace(
                'command = "memtomem-server"', f'command = "memtomem-server"\nenabled = {value}'
            )
        )
    else:
        data = json.loads(host.config.read_bytes())
        data["mcpServers"]["memtomem"]["disabled"] = flag
        write(host.config, data)
    before = host.bundled.read_bytes()
    result = invoke(host, "--reconcile", "keep-manual", "--apply")
    assert result.exit_code == 2, result.output
    assert host.bundled.read_bytes() == before


def test_multiple_plugins_refuse_reconciliation(host):
    host.rows.append(dict(host.rows[0]))
    before = host.config.read_bytes()
    assert invoke(host, "--reconcile", "keep-plugin", "--apply").exit_code == 2
    assert host.config.read_bytes() == before


def test_plugin_enablement_override_cannot_remove_manual(host):
    data = json.loads(host.bundled.read_bytes())
    data["mcpServers"]["memtomem"]["enabled"] = False
    write(host.bundled, data)
    before = host.config.read_bytes()
    assert invoke(host, "--reconcile", "keep-plugin", "--apply").exit_code == 2
    assert host.config.read_bytes() == before


def test_custom_codex_home(host, monkeypatch):
    if host.client != "codex":
        return
    custom = host.root.with_name("custom-codex")
    host.root.rename(custom)
    monkeypatch.setenv("CODEX_HOME", str(custom))
    result = invoke(host)
    assert result.exit_code == 0, result.output
    assert "native_dedup" in result.output


def test_codex_project_layer_refuses_reconciliation(host):
    if host.client != "codex":
        return
    local = Path.cwd() / ".codex/config.toml"
    local.parent.mkdir()
    local.write_text('[mcp_servers.memtomem]\ncommand = "other"\n')
    before = host.config.read_bytes()
    assert invoke(host, "--reconcile", "keep-plugin", "--apply").exit_code == 2
    assert host.config.read_bytes() == before


def test_replace_failure_keeps_original_and_backup(host, monkeypatch):
    before = host.config.read_bytes()

    def fail(*args):
        raise OSError("private-value")

    monkeypatch.setattr(checks.os, "replace", fail)
    result = invoke(host, "--reconcile", "keep-plugin", "--apply")
    assert result.exit_code == 2, result.output
    assert "private-value" not in result.output
    assert host.config.read_bytes() == before
    backup = next(host.config.parent.glob(host.config.name + ".mm-backup-*"))
    assert backup.read_bytes() == before
    assert any(
        f["code"] == "reconcile_backup_retained" and f.get("backup") == str(backup)
        for f in json.loads(result.output)["findings"]
    )
    assert not list(host.config.parent.glob(host.config.name + ".mm-[!b]*"))


def test_remove_quoted_toml_table_preserves_unrelated_comments():
    raw = b'# top\n[mcp_servers."memtomem"]\ncommand = "memtomem-server"\n[mcp_servers."memtomem".env]\nTOKEN="private-value"\n[mcp_servers.other] # keep\ncommand="other"\n'
    result = checks._remove_toml(raw, ("mcp_servers", "memtomem"))
    assert result == b'# top\n[mcp_servers.other] # keep\ncommand="other"\n'


@pytest.mark.parametrize(
    "raw",
    ['{"mcpServers":', "[]", '{"mcpServers": []}', '{"mcpServers": {}, "mcpServers": {}}'],
)
@pytest.mark.parametrize("has_manual", [False, True])
def test_review_invalid_plugin_is_not_omitted(host, raw, has_manual):
    host.bundled.write_text(raw)
    if not has_manual:
        host.config.unlink()
    report = checks.inspect_host(host.client)
    assert report.exit_code == 2
    assert "skills_only" not in {f["code"] for f in report.findings}
    assert not any("Plugin MCP is omitted" in f["message"] for f in report.findings)
    assert not report.plugin_paths


@pytest.mark.parametrize("choice", ["keep-manual", "keep-plugin"])
def test_agy_source_plugin_refuses_edits(host, choice):
    if host.client != "agy":
        return
    source = Path.cwd() / "shared-plugin"
    shutil.copytree(host.plugin, source)
    host.rows[0]["path"] = str(source)
    before = {p: p.read_bytes() for p in [host.config, source / "mcp_config.json"]}
    result = invoke(host, "--reconcile", choice, "--apply")
    assert result.exit_code == 2, result.output
    assert "outside the user install directory" in result.output
    assert all(p.read_bytes() == raw for p, raw in before.items())
    assert not list(source.glob("*.mm-*"))


@pytest.mark.requires_symlinks
def test_agy_plugin_symlink_cannot_escape_install_directory(host):
    if host.client != "agy":
        return
    source = Path.cwd() / "shared-plugin"
    host.plugin.rename(source)
    host.plugin.symlink_to(source, target_is_directory=True)
    result = invoke(host, "--reconcile", "keep-manual", "--apply")
    assert result.exit_code == 2, result.output
    assert "outside the user install directory" in result.output
    assert "memtomem" in json.loads((source / "mcp_config.json").read_text())["mcpServers"]


@pytest.mark.parametrize("host", ["codex"], indirect=True)
@pytest.mark.parametrize("manual_name", ["memtomem", "mm-dev"])
@pytest.mark.parametrize("identity", ["memtomem", "memtomem@memtomem", "memtomem@team"])
@pytest.mark.parametrize(
    "policy",
    [
        "mcp_servers.memtomem.enabled = false",
        "mcp_servers.memtomem.enabled_tools = []",
        "unknown = true",
        'enabled = "yes"',
    ],
)
@pytest.mark.parametrize("choice", ["keep-manual", "keep-plugin"])
def test_codex_plugin_policy_refuses_state_claims_and_edits(
    host, manual_name, identity, policy, choice
):
    content = host.config.read_text().replace("mcp_servers.memtomem", f"mcp_servers.{manual_name}")
    host.config.write_text(content + f'\n[plugins."{identity}"]\n{policy}\n')
    before = {p: p.read_bytes() for p in [host.config, host.bundled]}
    result = invoke(host, "--reconcile", choice, "--apply")
    assert result.exit_code == 2, result.output
    findings = json.loads(result.output)["findings"]
    assert not {"native_dedup", "plugin_enabled"} & {f["code"] for f in findings}
    assert "plugin policy overrides" in result.output
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_codex_supported_and_unrelated_plugin_policy(host):
    if host.client != "codex":
        return
    host.config.write_text(
        host.config.read_text()
        + '\n[plugins."memtomem@memtomem"]\nenabled = true\n'
        + "[plugins.other.mcp_servers.docs]\nenabled = false\n"
    )
    assert invoke(host).exit_code == 0


@pytest.mark.parametrize("git_root", [False, True])
def test_custom_codex_home_ignores_default_home_and_above_repo_layers(host, monkeypatch, git_root):
    if host.client != "codex":
        return
    home = host.root.parent
    custom = home / "custom-codex"
    host.root.rename(custom)
    monkeypatch.setenv("CODEX_HOME", str(custom))
    host.root.mkdir()
    (host.root / "config.toml").write_text('model = "unused-default"\n')
    project = home / "parent" / "repo"
    project.mkdir(parents=True)
    if git_root:
        (project / ".git").mkdir()
        above = project.parent / ".codex"
        above.mkdir()
        (above / "config.toml").write_text('model = "above-repo"\n')
    monkeypatch.chdir(project)
    result = invoke(host)
    assert result.exit_code == 0, result.output
    assert "native_dedup" in result.output


@pytest.mark.parametrize("invalid", ["enablement", "launcher"])
@pytest.mark.parametrize("omitted", [False, True])
def test_review_unusable_manual_supplies_no_tools(host, invalid, omitted):
    if host.client == "codex":
        content = host.config.read_text()
        replacement = (
            'command = "memtomem-server"\nenabled = "no"'
            if invalid == "enablement"
            else 'command = "custom-wrapper"'
        )
        host.config.write_text(content.replace('command = "memtomem-server"', replacement))
    else:
        data = json.loads(host.config.read_bytes())
        entry = data["mcpServers"]["memtomem"]
        if invalid == "enablement":
            entry["disabled"] = "no"
        else:
            entry["command"] = "custom-wrapper"
        write(host.config, data)
    if omitted:
        host.bundled.unlink()
    report = checks.inspect_host(host.client)
    assert report.exit_code == 2
    assert not ({"skills_only", "native_dedup"} & {f["code"] for f in report.findings})
    assert not any(
        f["code"] == "manual_registration" and f.get("state") == "configured"
        for f in report.findings
    )


@pytest.mark.parametrize("invalid", ["missing", "malformed", "wrong-name", "unreadable"])
def test_invalid_manifest_does_not_supply_skills_only_plugin(host, monkeypatch, invalid):
    manifest = host.plugin / (
        ".codex-plugin/plugin.json" if host.client == "codex" else "plugin.json"
    )
    host.bundled.unlink()
    if invalid == "missing":
        manifest.unlink()
    elif invalid == "malformed":
        manifest.write_text("{")
    elif invalid == "wrong-name":
        write(manifest, {"name": "other"})
    else:
        original = Path.read_bytes

        def read_bytes(path):
            if path == manifest:
                raise PermissionError
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
    report = checks.inspect_host(host.client)
    assert report.exit_code == 2
    assert not ({"skills_only", "plugin_enabled"} & {f["code"] for f in report.findings})
    assert not any("Plugin MCP is omitted" in f["message"] for f in report.findings)
    assert not report.plugin_paths


@pytest.mark.parametrize("host", ["agy"], indirect=True)
@pytest.mark.parametrize("location", ["plugin", "parent"])
@pytest.mark.parametrize("marker", ["directory", "file"])
def test_agy_installed_plugin_in_git_worktree_is_not_edited(host, location, marker):
    directory = host.plugin if location == "plugin" else host.root
    git = directory / ".git"
    if marker == "directory":
        git.mkdir()
    else:
        git.write_text("gitdir: /shared/repository/worktrees/plugin\n")
    before = host.bundled.read_bytes()
    result = invoke(host, "--reconcile", "keep-manual", "--apply")
    assert result.exit_code == 2, result.output
    assert "Git worktree" in result.output
    assert host.bundled.read_bytes() == before
    assert not list(host.plugin.glob("*.mm-*"))


def test_omitted_manifest_name_uses_agy_directory_only(host):
    manifest = host.plugin / (
        ".codex-plugin/plugin.json" if host.client == "codex" else "plugin.json"
    )
    write(manifest, {})
    result = invoke(host, "--reconcile", "keep-manual", "--apply")
    assert result.exit_code == (2 if host.client == "codex" else 0), result.output
    if host.client == "agy":
        assert json.loads(host.bundled.read_bytes())["mcpServers"] == {}


@pytest.mark.parametrize("policy", ["enabled_tools", "disabled_tools"])
@pytest.mark.parametrize("bundled", [False, True])
@pytest.mark.parametrize("choice", ["keep-manual", "keep-plugin"])
def test_tool_filter_requires_review_before_reconciliation(host, policy, bundled, choice):
    target = host.bundled if bundled else host.config
    if not bundled and host.client == "codex":
        content = target.read_text().replace(
            'command = "memtomem-server"', f'command = "memtomem-server"\n{policy} = []'
        )
        target.write_text(content)
    else:
        data = json.loads(target.read_bytes())
        data["mcpServers"]["memtomem"][policy] = []
        write(target, data)
    before = {p: p.read_bytes() for p in [host.config, host.bundled]}
    result = invoke(host, "--reconcile", choice, "--apply")
    assert result.exit_code == 2, result.output
    assert "tool policy" in result.output
    assert "native_dedup" not in result.output
    assert all(p.read_bytes() == raw for p, raw in before.items())


def test_manual_tool_filter_cannot_justify_skills_only(host):
    host.bundled.unlink()
    if host.client == "codex":
        host.config.write_text(
            host.config.read_text().replace(
                'command = "memtomem-server"', 'command = "memtomem-server"\nenabled_tools = []'
            )
        )
    else:
        data = json.loads(host.config.read_bytes())
        data["mcpServers"]["memtomem"]["enabled_tools"] = []
        write(host.config, data)
    result = invoke(host, "--reconcile", "keep-manual", "--apply")
    assert result.exit_code == 2, result.output
    assert "skills_only" not in result.output and "reconcile_noop" not in result.output
    assert not json.loads(result.output)["reusable_registration"]


@pytest.mark.parametrize("host", ["codex"], indirect=True)
def test_apply_preserves_comments_before_next_toml_table(host):
    host.config.write_text(
        host.config.read_text().replace(
            "[mcp_servers.other]", "# docs for other\n\n[mcp_servers.other]"
        )
    )
    result = invoke(host, "--reconcile", "keep-plugin", "--apply")
    assert result.exit_code == 0, result.output
    assert host.config.read_text() == (
        'model = "test"\n# docs for other\n\n[mcp_servers.other]\ncommand = "other"\n'
    )


def test_toml_removal_does_not_move_removed_subtable_comments():
    raw = (
        b'[mcp_servers.memtomem]\ncommand="memtomem-server"\n'
        b'# docs for removed env\n[mcp_servers.memtomem.env]\nTOKEN="private-value"\n'
        b'# docs for other\n[mcp_servers.other]\ncommand="other"\n'
    )
    assert checks._remove_toml(raw, ("mcp_servers", "memtomem")) == (
        b'# docs for other\n[mcp_servers.other]\ncommand="other"\n'
    )
