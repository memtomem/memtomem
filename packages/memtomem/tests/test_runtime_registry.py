"""Tests for the read-only runtime/client registration registry (ADR-0021 §B).

Covers: per-client multi-location detection, the Antigravity ``servers`` key,
Codex TOML, Kimi ``$KIMI_SHARE_DIR``, coarse error classification, the
secret-non-egress trust boundary, the gemini->antigravity client replacement,
and the ``docs/guides/mcp-clients.md`` source-of-truth conformance.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memtomem.context import runtime_registry as rr


@pytest.fixture(autouse=True)
def _clear_kimi_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Isolate from a runner that happens to export KIMI_SHARE_DIR.
    monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- installed / unregistered baseline ---------------------------------------


def test_empty_home_not_installed(tmp_path: Path) -> None:
    status = rr.probe_runtime("claude", home=tmp_path)
    assert status.installed is False
    assert status.memtomem_registered is False
    assert status.mms_registered is False
    assert status.registered_locations == ()
    assert status.config_paths == ()
    assert status.error_kind is None


def test_installed_but_unregistered(tmp_path: Path) -> None:
    # Config file present, but no memtomem/mms server entry.
    _write_json(tmp_path / ".claude.json", {"mcpServers": {"other": {}}})
    status = rr.probe_runtime("claude", home=tmp_path)
    assert status.installed is True
    assert status.memtomem_registered is False
    assert status.registered_locations == ()


# --- Claude: three locations --------------------------------------------------


def test_claude_user_scope(tmp_path: Path) -> None:
    _write_json(tmp_path / ".claude.json", {"mcpServers": {"memtomem": {"command": "x"}}})
    status = rr.probe_runtime("claude", home=tmp_path)
    assert status.memtomem_registered is True
    assert status.registered_locations == ("user",)
    assert status.config_paths == ("~/.claude.json",)


def test_claude_local_scope_keyed_by_project_root(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    _write_json(
        tmp_path / ".claude.json",
        {"projects": {str(project): {"mcpServers": {"memtomem": {}}}}},
    )
    status = rr.probe_runtime("claude", project, home=tmp_path)
    assert status.registered_locations == ("local",)
    # A different project root must NOT see this local registration.
    other = rr.probe_runtime("claude", tmp_path / "elsewhere", home=tmp_path)
    assert other.memtomem_registered is False


def test_claude_project_mcp_json(tmp_path: Path) -> None:
    # Project root outside home so the committed .mcp.json renders absolute.
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "proj"
    _write_json(project / ".mcp.json", {"mcpServers": {"mms": {}}})
    status = rr.probe_runtime("claude", project, home=home)
    assert status.mms_registered is True
    assert status.memtomem_registered is False
    assert status.registered_locations == ("project",)
    assert status.config_paths == ((project / ".mcp.json").as_posix(),)


# --- Antigravity: gemini-family, three locations incl. the ``servers`` key ----


def test_antigravity_cli(tmp_path: Path) -> None:
    _write_json(
        tmp_path / ".gemini" / "antigravity-cli" / "mcp_config.json",
        {"mcpServers": {"memtomem": {}}},
    )
    status = rr.probe_runtime("antigravity", home=tmp_path)
    assert status.installed is True
    assert status.memtomem_registered is True
    assert status.registered_locations == ("cli",)


def test_antigravity_ide_vscode_uses_servers_key(tmp_path: Path) -> None:
    # VS Code-side Antigravity config nests under ``servers`` (not mcpServers).
    cfg = tmp_path / "Library" / "Application Support" / "Antigravity" / "User" / "mcp.json"
    _write_json(cfg, {"servers": {"memtomem": {}}})
    status = rr.probe_runtime("antigravity", home=tmp_path)
    assert status.memtomem_registered is True
    assert status.registered_locations == ("ide_vscode",)
    # A wrong-key payload (mcpServers in the VS Code file) must not register.
    _write_json(cfg, {"mcpServers": {"memtomem": {}}})
    assert rr.probe_runtime("antigravity", home=tmp_path).memtomem_registered is False


# --- Codex TOML ---------------------------------------------------------------


def test_codex_toml(tmp_path: Path) -> None:
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[mcp_servers.memtomem]\ncommand = "uv"\n', encoding="utf-8")
    status = rr.probe_runtime("codex", home=tmp_path)
    assert status.installed is True
    assert status.memtomem_registered is True
    assert status.registered_locations == ("user",)


# --- Kimi: default dir and $KIMI_SHARE_DIR override ---------------------------


def test_kimi_default_dir(tmp_path: Path) -> None:
    _write_json(tmp_path / ".kimi" / "mcp.json", {"mcpServers": {"memtomem": {}}})
    assert rr.probe_runtime("kimi", home=tmp_path).memtomem_registered is True


def test_kimi_share_dir_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    share = tmp_path / "share"
    monkeypatch.setenv("KIMI_SHARE_DIR", str(share))
    _write_json(share / "mcp.json", {"mcpServers": {"mms": {}}})
    status = rr.probe_runtime("kimi", home=tmp_path)
    assert status.installed is True
    assert status.mms_registered is True


# --- error classification (coarse, no message) --------------------------------


def test_malformed_json_is_parse_error(tmp_path: Path) -> None:
    (tmp_path / ".claude.json").write_text("{not json", encoding="utf-8")
    status = rr.probe_runtime("claude", home=tmp_path)
    assert status.error_kind == "parse"
    assert status.memtomem_registered is False


def test_malformed_toml_is_parse_error(tmp_path: Path) -> None:
    cfg = tmp_path / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("this = = broken", encoding="utf-8")
    assert rr.probe_runtime("codex", home=tmp_path).error_kind == "parse"


# --- secret non-egress (trust boundary) ---------------------------------------


def test_no_secret_egress(tmp_path: Path) -> None:
    from memtomem.privacy import scan

    # Use a token the repo's own secret scanner detects (sk- + 20+ alnum,
    # DEFAULT_PATTERNS), so the trust boundary is pinned to the real scanner.
    secret = "sk-" + "abcdEFGH1234ijklMNOP5678qrst"
    assert scan(secret), "test token must be detectable by memtomem.privacy.scan"
    _write_json(
        tmp_path / ".claude.json",
        {"mcpServers": {"memtomem": {"env": {"API_KEY": secret}}}},
    )
    status = rr.probe_runtime("claude", home=tmp_path)
    assert status.memtomem_registered is True
    # No secret/value/key may appear in the returned status (dict, repr, JSON),
    # and the repo scanner must find nothing in the serialized output.
    serialized = json.dumps(status.to_dict()) + repr(status)
    assert secret not in serialized
    assert "API_KEY" not in serialized
    assert scan(serialized) == []


# --- gemini -> antigravity replacement + ordering -----------------------------


def test_probe_all_runtimes_client_set(tmp_path: Path) -> None:
    names = [s.name for s in rr.probe_all_runtimes(home=tmp_path)]
    assert names == ["claude", "antigravity", "codex", "kimi"]
    # gemini-family is represented by antigravity; no standalone "gemini" client.
    assert "gemini" not in names
    assert rr.IN_SCOPE_CLIENTS == ("claude", "antigravity", "codex", "kimi")


# --- mcp-clients.md source-of-truth conformance (ADR-0021 §B) ------------------


def _find_repo_doc() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "docs" / "guides" / "mcp-clients.md"
        if candidate.exists():
            return candidate
    raise AssertionError("docs/guides/mcp-clients.md not found above test file")


# Concrete MCP-config filenames (precise — avoids matching prose like GEMINI.md).
_CONFIG_TOKEN_RE = re.compile(
    r"`([^`]*(?:claude\.json|\.mcp\.json|mcp_config\.json|mcp\.json|config\.toml))`"
)
_INSCOPE_TITLES = ("Claude Code", "Kimi CLI", "Codex CLI", "Antigravity")
_OUT_OF_SCOPE_TITLES = ("Cursor", "Windsurf", "Claude Desktop", "Gemini CLI")
# Documented in-scope tokens the registry intentionally does not probe (none today).
_DOC_TOKENS_NOT_PROBED: frozenset[str] = frozenset()


def _inscope_doc_config_paths(doc: str) -> set[str]:
    """Backtick-quoted MCP-config paths under the in-scope client sections only."""
    found: set[str] = set()
    inscope = False
    for line in doc.splitlines():
        header = re.match(r"^##\s+\d+\.\s+(.*\S)\s*$", line)
        if header:
            title = header.group(1)
            inscope = any(t in title for t in _INSCOPE_TITLES) and not any(
                t in title for t in _OUT_OF_SCOPE_TITLES
            )
            continue
        if inscope:
            found.update(_CONFIG_TOKEN_RE.findall(line))
    return found


def _norm_path(token: str) -> str:
    """Reduce a documented/registry path to a comparable tail."""
    return token.replace("<project-root>", "").replace("~", "").lstrip("/")


def test_registry_covers_documented_locations() -> None:
    """Every in-scope MCP-config path documented in mcp-clients.md is probed.

    Expectations are derived BY PARSING the in-scope doc sections — not a
    hardcoded list — so a newly-documented or renamed in-scope location that the
    registry does not probe fails here. Out-of-scope sections (Cursor / Windsurf
    / Claude Desktop / standalone Gemini CLI) are excluded.
    """
    doc = _find_repo_doc().read_text(encoding="utf-8")
    documented = _inscope_doc_config_paths(doc)

    # Sanity: the parser must find a known anchor per in-scope client. A failure
    # here means an in-scope ## heading was renamed in mcp-clients.md so the
    # section is no longer matched by _INSCOPE_TITLES (i.e. update the titles) —
    # this makes the "silently missed section" failure mode loud and explained.
    rename_hint = "in-scope section not parsed — was a ## heading renamed in mcp-clients.md?"
    assert "~/.claude.json" in documented, rename_hint
    assert "~/.codex/config.toml" in documented, rename_hint
    assert "~/.kimi/mcp.json" in documented, rename_hint
    assert any("antigravity-cli/mcp_config.json" in t for t in documented), rename_hint
    assert any("Antigravity/User/mcp.json" in t for t in documented), rename_hint

    home = Path("/home/u")
    project = Path("/work/proj")  # outside home so the committed .mcp.json is absolute
    resolved = [p for ps in rr.registry_location_paths(home, project).values() for p in ps]
    registry_tails = {_norm_path(p) for p in resolved}

    for token in documented:
        if token in _DOC_TOKENS_NOT_PROBED:
            continue
        tail = _norm_path(token)
        assert any(
            rt == tail or rt.endswith("/" + tail) or rt.endswith(tail) for rt in registry_tails
        ), (
            f"documented in-scope location {token!r} (tail {tail!r}) is not probed by the "
            f"registry; resolved tails: {sorted(registry_tails)}"
        )

    # Standalone Gemini CLI settings.json must never be probed (out of scope).
    assert not any(p.endswith("/.gemini/settings.json") for p in resolved)
