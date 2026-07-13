"""Repository-level supply-chain and Claude plugin mapping guards."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
_DOCKER_RE = re.compile(r"^docker://[^\s@]+@sha256:[0-9a-f]{64}$")
_USES_LINE_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")


def _assert_pinned_ref(reference: str) -> None:
    reference = reference.strip("'\"")
    if reference.startswith("./"):
        return
    if reference.startswith("docker://"):
        assert _DOCKER_RE.fullmatch(reference), f"docker action is not digest-pinned: {reference}"
        return
    assert _ACTION_RE.fullmatch(reference), f"action is not full-SHA pinned: {reference}"


def _workflow_files(root: Path) -> list[Path]:
    result = list((root / ".github" / "workflows").glob("*.yml"))
    result.extend((root / ".github" / "workflows").glob("*.yaml"))
    actions = root / ".github" / "actions"
    if actions.is_dir():
        result.extend(actions.rglob("action.yml"))
        result.extend(actions.rglob("action.yaml"))
    return sorted(result)


def _json(path: str) -> dict:
    return json.loads((_ROOT / path).read_text(encoding="utf-8"))


def _contract() -> dict:
    with (_ROOT / "packages/memtomem-plugin-assets/contract.toml").open("rb") as handle:
        return tomllib.load(handle)


def _assert_mcp_pin(document: dict, version: str, tool_mode: str) -> None:
    server = document.get("mcpServers", {}).get("memtomem", {})
    assert server.get("command") == "uvx"
    assert server.get("args") == ["--from", f"memtomem=={version}", "memtomem-server"]
    assert server.get("env") == {"MEMTOMEM_TOOL_MODE": tool_mode}


def _marketplace_entry(marketplace: dict, name: str) -> dict:
    entries = [row for row in marketplace.get("plugins", []) if row.get("name") == name]
    assert len(entries) == 1
    return entries[0]


def test_every_external_workflow_action_is_immutable() -> None:
    seen = 0
    for path in _workflow_files(_ROOT):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if match := _USES_LINE_RE.match(line):
                seen += 1
                try:
                    _assert_pinned_ref(match.group(1))
                except AssertionError as exc:
                    raise AssertionError(f"{path.relative_to(_ROOT)}:{line_number}: {exc}") from exc
    assert seen > 0


def test_uv_toolchain_pin_matches_ci_release_and_sbom_workflows() -> None:
    versions: dict[str, str] = {}
    for name in ("ci.yml", "release.yml", "release-sbom.yml"):
        text = (_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        matches = re.findall(r'^\s*UV_VERSION:\s*"([^"]+)"\s*$', text, re.MULTILINE)
        assert len(matches) == 1, f"{name} must declare one UV_VERSION"
        versions[name] = matches[0]
    assert len(set(versions.values())) == 1, f"uv toolchain pins drifted: {versions}"


@pytest.mark.parametrize(
    "reference",
    [
        "actions/checkout@v7",
        "actions/checkout@main",
        "owner/action@0123456789abcdef",
        "docker://alpine:latest",
        "docker://alpine@sha256:abc",
        "${{ matrix.action }}",
    ],
)
def test_mutable_or_malformed_action_refs_are_rejected(reference: str) -> None:
    with pytest.raises(AssertionError):
        _assert_pinned_ref(reference)


def test_local_and_immutable_action_refs_are_allowed() -> None:
    _assert_pinned_ref("./.github/actions/local")
    _assert_pinned_ref("actions/checkout@" + "a" * 40)
    _assert_pinned_ref("docker://alpine@sha256:" + "b" * 64)


def test_claude_plugins_match_contract_and_marketplace() -> None:
    contract = _contract()
    versions = contract["plugins"]
    marketplace = _json(".claude-plugin/marketplace.json")

    base = _json("packages/memtomem-claude-plugin/.claude-plugin/plugin.json")
    base_entry = _marketplace_entry(marketplace, "memtomem")
    assert base["version"] == versions["claude_version"]
    assert base_entry["version"] == base["version"]
    assert base_entry["source"] == "./packages/memtomem-claude-plugin"

    automation = _json("packages/memtomem-claude-automation-plugin/.claude-plugin/plugin.json")
    automation_entry = _marketplace_entry(marketplace, "memtomem-automation")
    assert automation["version"] == versions["automation_version"]
    assert automation_entry["version"] == automation["version"]
    assert automation_entry["source"] == "./packages/memtomem-claude-automation-plugin"

    _assert_mcp_pin(
        _json("packages/memtomem-claude-plugin/.mcp.json"),
        contract["core"]["version"],
        contract["core"]["tool_mode"],
    )


def test_codex_plugin_matches_contract_and_marketplace() -> None:
    contract = _contract()
    manifest = _json("plugins/memtomem/.codex-plugin/plugin.json")
    marketplace = _json(".agents/plugins/marketplace.json")
    entry = _marketplace_entry(marketplace, "memtomem")

    assert manifest["version"] == contract["plugins"]["codex_version"]
    assert entry["source"] == {"source": "local", "path": "./plugins/memtomem"}
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    _assert_mcp_pin(
        _json("plugins/memtomem/.mcp.json"),
        contract["core"]["version"],
        contract["core"]["tool_mode"],
    )


def test_opencode_plugin_matches_contract() -> None:
    contract = _contract()
    package = _json("packages/opencode-memtomem/package.json")
    generated = (_ROOT / "packages/opencode-memtomem/src/generated.ts").read_text(encoding="utf-8")

    assert package["name"] == contract["opencode"]["package"]
    assert package["version"] == contract["plugins"]["opencode_version"]
    assert package["engines"]["opencode"] == contract["opencode"]["version_range"]
    assert package["license"] == "Apache-2.0"
    assert package.get("dependencies", {}) == {}
    assert f'CORE_VERSION = "{contract["core"]["version"]}"' in generated
    assert f"MCP_TIMEOUT_MS = {contract['opencode']['mcp_timeout_ms']}" in generated

    plugin_version = contract["plugins"]["opencode_version"]
    install_command = f"opencode plugin add {package['name']}@{plugin_version}"
    for path in (
        "packages/opencode-memtomem/README.md",
        "docs/guides/integrations/opencode.md",
        "docs/guides/mcp-clients.md",
    ):
        assert install_command in (_ROOT / path).read_text(encoding="utf-8")

    compatibility = f"compatibility: OpenCode {contract['opencode']['version_range']}"
    for skill in (_ROOT / "packages/opencode-memtomem/skills").glob("*/SKILL.md"):
        assert compatibility in skill.read_text(encoding="utf-8")


def test_every_plugin_version_is_semver() -> None:
    contract = _contract()
    for version in contract["plugins"].values():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
