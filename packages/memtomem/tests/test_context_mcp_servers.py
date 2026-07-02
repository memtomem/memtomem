from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from memtomem.context.mcp_servers import (
    McpServerParseError,
    diff_mcp_servers,
    generate_all_mcp_servers,
    list_canonical_mcp_servers,
    parse_canonical_mcp_server,
)


def _canonical(root: Path, name: str, definition: dict) -> Path:
    path = root / ".memtomem" / "mcp-servers" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    return path


def test_parse_requires_command_string(tmp_path: Path) -> None:
    path = _canonical(tmp_path, "demo", {"args": []})
    with pytest.raises(McpServerParseError, match="command"):
        parse_canonical_mcp_server(path)


def test_rejects_network_transport_definition(tmp_path: Path) -> None:
    """v1 accepts only stdio servers. A network (type/url SSE/HTTP) definition
    is rejected, and the message names the stdio limitation so it does not read
    as a generic schema bug."""
    path = _canonical(tmp_path, "remote", {"type": "http", "url": "https://example.com/mcp"})
    with pytest.raises(McpServerParseError, match="stdio"):
        parse_canonical_mcp_server(path)


def test_sync_merges_project_mcp_json_without_clobbering_other_entries(tmp_path: Path) -> None:
    _canonical(
        tmp_path,
        "demo",
        {"command": "uvx", "args": ["--from", "demo", "demo-server"]},
    )
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "comment": "keep me",
                "mcpServers": {
                    "existing": {"command": "node", "args": ["server.js"]},
                    "demo": {"command": "old"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = generate_all_mcp_servers(tmp_path)

    assert result.skipped == []
    assert result.generated == [("project_mcp", "demo", mcp_json.resolve())]
    written = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert written["comment"] == "keep me"
    assert written["mcpServers"]["existing"] == {"command": "node", "args": ["server.js"]}
    assert written["mcpServers"]["demo"] == {
        "command": "uvx",
        "args": ["--from", "demo", "demo-server"],
    }


def test_diff_reports_missing_and_in_sync(tmp_path: Path) -> None:
    definition = {"command": "uvx", "args": ["demo"]}
    _canonical(tmp_path, "demo", definition)

    assert diff_mcp_servers(tmp_path) == [("project_mcp", "demo", "missing target")]

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": definition}}, indent=2) + "\n",
        encoding="utf-8",
    )
    assert diff_mcp_servers(tmp_path) == [("project_mcp", "demo", "in sync")]


def test_no_canonical_root_returns_empty_skip(tmp_path: Path) -> None:
    result = generate_all_mcp_servers(tmp_path)
    assert result.generated == []
    assert result.skipped == [
        ("project_mcp", "No canonical MCP server definitions found", "no_canonical_root")
    ]


def test_diff_parse_error_reasons_distinguish_canonical_from_target(tmp_path: Path) -> None:
    """U7 (#1229): a canonical-parse failure names the canonical file; a
    broken .mcp.json marks every canonical row 'parse error' with a reason
    naming .mcp.json — so the user never chases N healthy canonical files."""
    bad = tmp_path / ".memtomem" / "mcp-servers" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    rows = diff_mcp_servers(tmp_path)
    assert rows[0][2] == "parse error"
    assert "bad.json" in (rows[0].reason or "")

    bad.write_text(json.dumps({"command": "uvx"}), encoding="utf-8")
    (tmp_path / ".mcp.json").write_text("{broken", encoding="utf-8")
    rows = diff_mcp_servers(tmp_path)
    assert rows[0][2] == "parse error"
    assert ".mcp.json" in (rows[0].reason or "")


# ── #1247 B8: id 40 — invalid-named canonical files must not abort ──────────


def _stray(root: Path, filename: str, definition: dict) -> Path:
    path = root / ".memtomem" / "mcp-servers" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    return path


def test_list_skips_invalid_named_canonical(tmp_path: Path) -> None:
    """One stray invalid-named file ('my server.json') used to raise
    InvalidNameError through the list — aborting the whole MCP panel, diff,
    and sync (#1247 id 40). The list now mirrors skills: skip with a warning,
    keep serving the valid entries."""
    _canonical(tmp_path, "good", {"command": "uvx"})
    _stray(tmp_path, "my server.json", {"command": "doom"})
    assert [p.stem for p in list_canonical_mcp_servers(tmp_path)] == ["good"]


def test_diff_surfaces_invalid_canonical_name_row(tmp_path: Path) -> None:
    """Filtered-from-sync is not invisible: the invalid name gets a dedicated
    'invalid name' diff row carrying the validate_name message (#1243 mirror)."""
    _canonical(tmp_path, "good", {"command": "uvx"})
    _stray(tmp_path, "my server.json", {"command": "doom"})
    rows = diff_mcp_servers(tmp_path)
    assert ("project_mcp", "good", "missing target") in rows
    invalid = [r for r in rows if r[2] == "invalid name"]
    assert [r[1] for r in invalid] == ["my server"]
    assert "invalid" in (invalid[0].reason or "")


def test_sync_fans_out_valid_despite_invalid_named_sibling(tmp_path: Path) -> None:
    _canonical(tmp_path, "good", {"command": "uvx"})
    _stray(tmp_path, "my server.json", {"command": "doom"})
    result = generate_all_mcp_servers(tmp_path)
    assert result.skipped == []
    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert set(written["mcpServers"]) == {"good"}


# ── #1247 B8: id 42 — generated rows carry the server name ──────────────────


def test_generated_rows_carry_server_names(tmp_path: Path) -> None:
    """generated used to be N identical nameless (runtime, path) tuples for
    one .mcp.json write — indistinguishable duplicate rows (#1247 id 42)."""
    _canonical(tmp_path, "alpha", {"command": "uvx"})
    _canonical(tmp_path, "beta", {"command": "node"})
    result = generate_all_mcp_servers(tmp_path)
    target = (tmp_path / ".mcp.json").resolve()
    assert sorted(result.generated) == [
        ("project_mcp", "alpha", target),
        ("project_mcp", "beta", target),
    ]


# ── #1247 B8: id 43 — in-sync runs must not rewrite .mcp.json ────────────────


def test_in_sync_rerun_skips_write_and_preserves_mtime_and_mode(tmp_path: Path) -> None:
    """A second sync with nothing to change used to rewrite the whole file
    anyway — churning mtime and chmodding to 0600 (#1247 id 43). It now
    returns a typed in_sync skip and leaves the file untouched."""
    _canonical(tmp_path, "demo", {"command": "uvx"})
    first = generate_all_mcp_servers(tmp_path)
    assert [(r[0], r[1]) for r in first.generated] == [("project_mcp", "demo")]
    target = tmp_path / ".mcp.json"
    st = target.stat()
    second = generate_all_mcp_servers(tmp_path)
    assert second.generated == []
    assert len(second.skipped) == 1
    assert second.skipped[0][0] == "project_mcp"
    assert second.skipped[0][2] == "in_sync"
    after = target.stat()
    assert after.st_mtime_ns == st.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(st.st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not round-tripped on Windows")
def test_create_writes_mcp_json_world_readable(tmp_path: Path) -> None:
    """A freshly created .mcp.json holds only Gate-A-scanned canonical
    content — 0644 like every other fan-out target, not the 0600 state-file
    default that made a typically git-tracked file owner-only."""
    _canonical(tmp_path, "demo", {"command": "uvx"})
    generate_all_mcp_servers(tmp_path)
    assert stat.S_IMODE((tmp_path / ".mcp.json").stat().st_mode) == 0o644


@pytest.mark.parametrize("mode", [0o600, 0o644])
@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not round-tripped on Windows")
def test_rewrite_preserves_existing_mcp_json_mode(tmp_path: Path, mode: int) -> None:
    """Rewrites preserve the user's existing mode in BOTH directions: a 0600
    file may hold unscanned secret env values in foreign entries we preserve
    verbatim (widening would expose them — Codex design gate), and a 0644
    file must not get chmodded down to 0600 (the original id 43 complaint)."""
    _canonical(tmp_path, "demo", {"command": "uvx"})
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps(
            {"mcpServers": {"foreign": {"command": "x", "env": {"TOKEN": "shh"}}}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    target.chmod(mode)
    result = generate_all_mcp_servers(tmp_path)
    assert [(r[0], r[1]) for r in result.generated] == [("project_mcp", "demo")]
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["mcpServers"]["foreign"]["env"]["TOKEN"] == "shh"
    assert stat.S_IMODE(target.stat().st_mode) == mode


# ── #1247 B8: id 31 — runtime-only .mcp.json servers become visible ──────────


def test_diff_surfaces_runtime_only_servers(tmp_path: Path) -> None:
    """A server present only in .mcp.json was invisible end-to-end — the
    panel implied no servers beyond canonicals (#1247 id 31). It now gets a
    'missing canonical' row like every sibling family; invalid-named runtime
    keys get 'invalid name' rows instead of being dropped."""
    _canonical(tmp_path, "managed", {"command": "uvx"})
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "managed": {"command": "uvx"},
                    "adhoc": {"command": "node"},
                    "bad name!": {"command": "x"},
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    rows = diff_mcp_servers(tmp_path)
    by_name = {r[1]: r for r in rows}
    assert by_name["managed"][2] != "missing target"
    assert by_name["adhoc"][2] == "missing canonical"
    assert by_name["bad name!"][2] == "invalid name"
    assert "invalid" in (by_name["bad name!"].reason or "")


def test_runtime_only_enumeration_tolerates_broken_mcp_json(tmp_path: Path) -> None:
    """A broken .mcp.json keeps the existing per-canonical 'parse error' rows
    and simply skips runtime-only enumeration — no crash, no phantom rows."""
    _canonical(tmp_path, "demo", {"command": "uvx"})
    (tmp_path / ".mcp.json").write_text("{broken", encoding="utf-8")
    rows = diff_mcp_servers(tmp_path)
    assert [(r[0], r[1], r[2]) for r in rows] == [("project_mcp", "demo", "parse error")]


# ── T1-5: .mcp.json read-merge-write is cross-process guarded ────────────────


def test_sync_aborts_when_sidecar_lock_held(tmp_path: Path) -> None:
    """A concurrent holder of the .mcp.json sidecar lock makes the merge
    self-abort with a typed LOCK_TIMEOUT skip (bounded budget) instead of
    blocking forever — the settings #1123 twin. Expiry-direction timing: a
    slow runner only delays the abort, it can never produce a false pass."""
    from memtomem.context import _skip_reasons as skip_codes
    from memtomem.context import mcp_servers as mod
    from memtomem.context._atomic import _file_lock, _lock_path_for

    _canonical(tmp_path, "demo", {"command": "uvx"})
    target = tmp_path / ".mcp.json"
    monkeypatch_budget = mod._MCP_LOCK_BUDGET_S
    try:
        mod._MCP_LOCK_BUDGET_S = 0.2
        with _file_lock(_lock_path_for(target)):
            result = mod.generate_all_mcp_servers(tmp_path)
    finally:
        mod._MCP_LOCK_BUDGET_S = monkeypatch_budget

    assert result.generated == []
    assert [(r[0], r[2]) for r in result.skipped] == [("project_mcp", skip_codes.LOCK_TIMEOUT)]
    # The lock protected the file: nothing was written under contention.
    assert not target.exists()


def test_sync_aborts_when_target_mtime_changes_mid_merge(tmp_path: Path) -> None:
    """The st_mtime_ns recheck (second layer, for a non-locking direct editor
    save mid-merge) aborts with a TARGET_CONFLICT skip and writes nothing."""
    from memtomem.context import _skip_reasons as skip_codes
    from memtomem.context import mcp_servers as mod

    _canonical(tmp_path, "demo", {"command": "uvx"})
    target = tmp_path / ".mcp.json"
    target.write_text(
        json.dumps({"mcpServers": {"other": {"command": "node"}}}, indent=2) + "\n",
        encoding="utf-8",
    )

    real_dumps = json.dumps
    state = {"bumped": False}

    def _dumps_then_touch(*args: object, **kwargs: object) -> str:
        # Simulate a concurrent write landing after the in-lock read but
        # before the mtime recheck: bump the file's mtime once.
        out = real_dumps(*args, **kwargs)
        if not state["bumped"]:
            state["bumped"] = True
            st = target.stat()
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        return out

    import unittest.mock as _mock

    with _mock.patch.object(mod.json, "dumps", _dumps_then_touch):
        result = mod.generate_all_mcp_servers(tmp_path)

    assert result.generated == []
    assert [(r[0], r[2]) for r in result.skipped] == [("project_mcp", skip_codes.TARGET_CONFLICT)]
    # Untouched: still only the pre-existing entry, demo NOT merged in.
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["mcpServers"] == {"other": {"command": "node"}}
