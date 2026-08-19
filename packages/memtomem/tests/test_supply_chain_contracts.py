"""Repository-level supply-chain and Claude plugin mapping guards."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


_ROOT = Path(__file__).resolve().parents[3]
_ACTION_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?@[0-9a-f]{40}$")
_DOCKER_RE = re.compile(r"^docker://[^\s@]+@sha256:[0-9a-f]{64}$")
_USES_LINE_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)")
# The exact requirement contract for the pywin32 pin — see
# test_windows_shared_locks_declare_pywin32 for why these are matched
# literally rather than evaluated over a sampled environment matrix.
_PYWIN32_SPECIFIER = ">=226"
_WINDOWS_ONLY_MARKERS = frozenset({'sys_platform == "win32"', 'platform_system == "Windows"'})


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


def test_kimi_skill_bundle_matches_contract() -> None:
    contract = _contract()
    root = _ROOT / "packages/memtomem-kimi-skills"
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == contract["plugins"][
        "kimi_version"
    ]
    assert "Kimi Code skill bundle, not a plugin" in (root / "README.md").read_text(
        encoding="utf-8"
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
    plugin_spec = f'"plugin": ["{package["name"]}@{plugin_version}"]'
    mcp_spec = f"memtomem[all]=={contract['core']['version']}"
    for path in (
        "packages/opencode-memtomem/README.md",
        "docs/guides/integrations/opencode.md",
        "docs/guides/mcp-clients.md",
    ):
        text = (_ROOT / path).read_text(encoding="utf-8")
        assert f"opencode plugin add {package['name']}" not in text
        assert plugin_spec in text
        assert mcp_spec in text

    compatibility = f"compatibility: OpenCode {contract['opencode']['version_range']}"
    for skill in (_ROOT / "packages/opencode-memtomem/skills").glob("*/SKILL.md"):
        assert compatibility in skill.read_text(encoding="utf-8")


def test_opencode_package_lock_matches_contract() -> None:
    """package-lock.json versions track the contract like package.json does.

    ``npm publish`` reads package.json, but a lockfile left behind on a bump
    ships a wrong version claim in-tree and breaks ``npm ci`` for anyone
    developing the plugin (#1923 audit: the lockfile was unguarded).
    """
    contract = _contract()
    lock = _json("packages/opencode-memtomem/package-lock.json")
    expected = contract["plugins"]["opencode_version"]
    assert lock["version"] == expected
    assert lock["packages"][""]["version"] == expected
    assert lock["name"] == contract["opencode"]["package"]


def test_every_plugin_version_is_semver() -> None:
    contract = _contract()
    for version in contract["plugins"].values():
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)


def _shared_lock_mentions() -> list[str]:
    """Files under ``src`` that name a shared lock flag in executable code.

    A deletion tripwire, deliberately **over-approximating**. It matches any
    attribute access named ``LOCK_SH`` or ``SHARED``, so it also counts the
    POSIX-gated ``cli/_liveness`` site and would count an unrelated enum
    member spelled ``SHARED``. That is the safe direction: over-counting keeps
    the pywin32 pin required, while under-counting would release it. It is
    *not* a claim about which sites run on Windows — that judgment lives in
    the pyproject comment, which names the lifecycle barrier as the whole
    obligation.

    AST rather than a substring scan: ``"LOCK_SH" in text`` also fires on the
    prose explaining the lock, and misses a call spelled
    ``LockFlags.SHARED``. The first makes the tripwire un-clearable for the
    wrong reason, the second silently disarms it.
    """
    shared = {"LOCK_SH", "SHARED"}
    found: set[str] = set()
    for path in (_ROOT / "packages/memtomem/src/memtomem").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in shared:
                found.add(path.relative_to(_ROOT).as_posix())
                break
    return sorted(found)


def test_windows_shared_locks_declare_pywin32() -> None:
    """The shared lifecycle barrier obliges a direct ``pywin32`` declaration.

    ``_instance_registry.acquire_server_lifecycle_barrier`` takes the barrier
    with ``LOCK_SH`` on every OS, Windows included. portalocker 3.x listed
    ``pywin32; platform_system == "Windows"`` unconditionally, so that worked
    for free; 4.0.0 moved it behind a ``win32`` extra. ``MsvcrtLocker`` has
    been the default Windows locker since 3.2.0 (a separate change, often
    conflated with this one) and msvcrt has no shared lock, so it delegates
    ``LockFlags.SHARED`` to ``Win32Locker`` and raises ``ImportError`` when
    pywin32 is absent. ``ImportError`` is in neither ``_LOCK_CONTENDED`` nor
    ``_BARRIER_LOCK_ERRORS``, so it escapes the barrier unhandled — a crash,
    not a degraded lock.

    ``mcp`` supplies pywin32 transitively today, which is precisely why this
    needs pinning: the resolve is correct by luck, not by contract.

    Deliberately **no skip branch**. An earlier version released the
    requirement when it found no shared-lock call site, which turns any
    refactor of the spelling into a silent disarm. The absence of a shared
    lock is asserted as a failure instead, so dropping the pin has to be a
    deliberate edit to this test.
    """
    users = _shared_lock_mentions()
    assert users, (
        "no shared-lock call site found under src/ — if the lifecycle barrier "
        "no longer takes LOCK_SH, drop the pywin32 pin and this guard together, "
        "deliberately"
    )

    with (_ROOT / "packages/memtomem/pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    pins = [
        requirement
        for requirement in map(Requirement, project["dependencies"])
        if canonicalize_name(requirement.name) == "pywin32"
    ]
    assert pins, (
        "shared locks are taken in "
        + ", ".join(users)
        + ", but pyproject declares no pywin32 dependency — they raise "
        "ImportError on Windows without it (portalocker >= 4.0)"
    )

    # Match the marker's canonical form exactly rather than sampling it.
    # `requires-python` has no upper bound, so no finite sample of Python
    # versions can prove a marker is version-independent: a marker reading
    # `sys_platform == "win32" and python_version < "3.16"` satisfies every
    # sampled minor while excluding a supported one. Pinning the normalized
    # string removes the whole class — `packaging` normalizes whitespace and
    # quoting, so only the *shape* is pinned, not the spelling. A different
    # but equivalent marker is a deliberate edit here, not a silent pass.
    for pin in pins:
        assert pin.url is None, (
            f"pywin32 pin {str(pin)!r} resolves from a direct URL, bypassing "
            "the index and its version contract"
        )
        assert str(pin.specifier) == _PYWIN32_SPECIFIER, (
            f"pywin32 pin {str(pin)!r} has specifier {str(pin.specifier)!r}, "
            f"expected {_PYWIN32_SPECIFIER!r} — portalocker 3.x asked for the "
            "same floor, so anything lower reintroduces the gap this pin closes"
        )
        assert pin.marker is not None, (
            f"pywin32 pin {str(pin)!r} has no environment marker; it would "
            "install on every platform"
        )
        assert str(pin.marker) in _WINDOWS_ONLY_MARKERS, (
            f"pywin32 pin {str(pin)!r} has marker {str(pin.marker)!r}; expected "
            f"one of {sorted(_WINDOWS_ONLY_MARKERS)} — a marker that also "
            "mentions python_version is not Windows-only across every "
            "supported interpreter"
        )
