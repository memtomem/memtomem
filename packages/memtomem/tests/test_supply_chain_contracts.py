"""Repository-level supply-chain and Claude plugin mapping guards."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
_DOCKER_RE = re.compile(r"^docker://[^\s@]+@sha256:[0-9a-f]{64}$")
_USES_LINE_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")
_PLUGIN_CORE_MAP = {"0.2.3": "0.3.7"}


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


def _validate_plugin_documents(manifest: dict, marketplace: dict, mcp: dict) -> None:
    plugin_version = manifest.get("version")
    assert isinstance(plugin_version, str) and re.fullmatch(r"\d+\.\d+\.\d+", plugin_version)
    entries = [row for row in marketplace.get("plugins", []) if row.get("name") == "memtomem"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry.get("version") == plugin_version
    assert entry.get("source", {}).get("path") == "packages/memtomem-claude-plugin"
    expected_core = _PLUGIN_CORE_MAP.get(plugin_version)
    assert expected_core is not None, f"unreviewed plugin release mapping: {plugin_version}"
    server = mcp.get("mcpServers", {}).get("memtomem", {})
    assert server.get("command") == "uvx"
    assert server.get("args") == ["--from", f"memtomem=={expected_core}", "memtomem-server"]


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


def _plugin_documents() -> tuple[dict, dict, dict]:
    manifest = json.loads(
        (_ROOT / "packages/memtomem-claude-plugin/.claude-plugin/plugin.json").read_text()
    )
    marketplace = json.loads((_ROOT / ".claude-plugin/marketplace.json").read_text())
    mcp = json.loads((_ROOT / "packages/memtomem-claude-plugin/.mcp.json").read_text())
    return manifest, marketplace, mcp


def test_plugin_manifest_marketplace_and_core_pin_match_reviewed_mapping() -> None:
    _validate_plugin_documents(*_plugin_documents())


@pytest.mark.parametrize("drift", ["marketplace", "mcp", "manifest"])
def test_plugin_mapping_guard_rejects_drift(drift: str) -> None:
    manifest, marketplace, mcp = map(copy.deepcopy, _plugin_documents())
    if drift == "marketplace":
        marketplace["plugins"][0]["version"] = "0.0.0"
    elif drift == "mcp":
        mcp["mcpServers"]["memtomem"]["args"][1] = "memtomem>=0.3.5"
    else:
        manifest["version"] = "0.0.0"
    with pytest.raises(AssertionError):
        _validate_plugin_documents(manifest, marketplace, mcp)
