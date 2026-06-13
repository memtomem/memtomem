"""Adapter tests for cross-project mcp-servers copy (#1282, campaign #1270 A-12).

Pins :func:`memtomem.context.mcp_servers_copy.copy_mcp_server` — the
engine-primitive reuse (staging, pair lock, collision typing), the two
deliberate divergences from the artifact engine (staged-bytes parse
validation; no-clobber promote), the Gate A standard envelope on an
``env``-block secret (issue acceptance 1), and the panel + additive
``.mcp.json`` sync round-trip at the destination (issue acceptance 2).
Surface layers are pinned separately (``test_cli_context_transfer.py``,
``test_web_routes_context_transfer.py``).
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest

from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.context.mcp_servers import (
    McpServerParseError,
    generate_all_mcp_servers,
    list_canonical_mcp_servers,
)
from memtomem.context.mcp_servers_copy import (
    McpServerCopyResult,
    _promote_no_clobber,
    copy_mcp_server,
)
from memtomem.context.migrate import ArtifactNotFoundError
from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.projects import compute_scope_id
from memtomem.context.transfer import TransferCollisionError, TransferResult

_CLEAN_DEFINITION = {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-postgres"],
    "env": {"PG_HOST": "localhost"},
}
#: AWS-key shape — caught by the privacy scan (the env block is the
#: issue-named hotspot for acceptance criterion 1).
_SECRET_DEFINITION = {
    "command": "npx",
    "env": {"AWS_ACCESS_KEY": "AKIA1234567890ABCDEF"},
}
#: Valid JSON, invalid definition: the v1 schema is stdio-only, so the
#: network shape is exactly what ``generate_all_mcp_servers`` would
#: refuse at the destination.
_NETWORK_DEFINITION = {"type": "sse", "url": "https://example.com/sse"}


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Two project roots + isolated HOME (privacy audit writes stay hermetic)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    for proj in (proj_a, proj_b):
        (proj / ".memtomem").mkdir(parents=True)
    return {"a": proj_a.resolve(), "b": proj_b.resolve()}


def _seed_server(root: Path, name: str = "pg", definition: dict | None = None) -> Path:
    store = root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.json"
    path.write_text(
        json.dumps(definition if definition is not None else _CLEAN_DEFINITION, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _copy(roots: dict[str, Path], *, apply_: bool, name: str = "pg", **kwargs):
    return copy_mcp_server(
        name,
        src_project_root=roots["a"],
        dst_project_root=roots["b"],
        apply_=apply_,
        **kwargs,
    )


def _dst_path(roots: dict[str, Path], name: str = "pg") -> Path:
    return roots["b"] / ".memtomem" / "mcp-servers" / f"{name}.json"


# ── result duck contract ─────────────────────────────────────────────


def test_result_field_surface_superset_of_transfer_result() -> None:
    """The CLI renderer and web serializer consume either result through
    one attribute surface (Codex design-gate fold) — a field added to
    ``TransferResult`` must be mirrored here or those surfaces break."""
    missing = set(TransferResult.__dataclass_fields__) - set(
        McpServerCopyResult.__dataclass_fields__
    )
    assert not missing, f"McpServerCopyResult lacks TransferResult fields: {sorted(missing)}"


# ── plan / apply happy paths ─────────────────────────────────────────


def test_dry_run_plan_mutates_nothing(roots) -> None:
    src = _seed_server(roots["a"])
    result = _copy(roots, apply_=False)

    assert result.transferred is False
    assert not _dst_path(roots).exists()
    assert src.is_file()
    assert result.kind == "mcp-servers"
    assert result.dst_name == "pg"
    assert (result.from_scope, result.to_scope) == ("project_shared", "project_shared")
    assert result.layout == "flat"
    assert result.needs_sync is True
    # Since #1311 the follow-up is a runnable cd-prefixed CLI command; the prose
    # sync_hint mirrors it and still carries the copy-pasteable API call.
    assert result.sync_command is not None
    assert result.sync_command.startswith("cd ")
    assert str(roots["b"]) in result.sync_command
    assert "mm context sync --include=mcp-servers --scope project_shared" in result.sync_command
    assert "mm context sync --include=mcp-servers" in result.sync_hint
    assert (
        f"POST /api/context/mcp-servers/sync?project_scope_id={compute_scope_id(roots['b'])}"
        in result.sync_hint
    )
    assert result.notes == ()
    assert result.provenance == "not_applicable"


def test_apply_copies_bytes_and_keeps_source(roots) -> None:
    src = _seed_server(roots["a"])
    result = _copy(roots, apply_=True)

    assert result.transferred is True
    dst = _dst_path(roots)
    assert dst.read_bytes() == src.read_bytes()
    assert src.is_file()
    # Staging consumed on promote — no .migrate-* residue at the destination.
    assert not list(dst.parent.glob(".migrate-*"))


# ── refusals (typed, full-literal pins) ──────────────────────────────


def test_collision_pre_flight_hard_fails(roots) -> None:
    _seed_server(roots["a"])
    dst = _seed_server(roots["b"], definition={"command": "other"})
    before = dst.read_bytes()

    with pytest.raises(TransferCollisionError) as exc_info:
        _copy(roots, apply_=True)
    assert exc_info.value.message == (
        f"destination already exists: {dst}. "
        "Resolve manually or remove the existing entry first "
        "(no --force overwrite; ADR-0023 §6 collision policy)."
    )
    assert dst.read_bytes() == before


def test_same_project_refused(roots) -> None:
    _seed_server(roots["a"])
    with pytest.raises(click.ClickException) as exc_info:
        copy_mcp_server(
            "pg",
            src_project_root=roots["a"],
            dst_project_root=roots["a"],
            apply_=False,
        )
    assert exc_info.value.message == (
        f"source and destination are the same project ({roots['a']}); "
        f"mcp-servers copy is cross-project only — the canonical is "
        f"single-tier, so within one project there is nothing to copy to."
    )


def test_missing_source_typed_not_found(roots) -> None:
    with pytest.raises(ArtifactNotFoundError) as exc_info:
        _copy(roots, apply_=False)
    expected = roots["a"] / ".memtomem" / "mcp-servers" / "pg.json"
    assert exc_info.value.message == (f"mcp-servers/pg not found at the source project: {expected}")


def test_invalid_source_definition_refused(roots) -> None:
    """Stricter than the artifact engine on purpose: one bad canonical at B
    aborts B's whole mcp sync phase, so the copy refuses up front."""
    _seed_server(roots["a"], definition=_NETWORK_DEFINITION)
    with pytest.raises(McpServerParseError, match="Only stdio servers are supported"):
        _copy(roots, apply_=True)
    assert not _dst_path(roots).exists()


def test_staged_bytes_are_parse_validated(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source edit racing past the pre-flight parse cannot smuggle invalid
    bytes: the authoritative parse runs on the STAGED bytes inside the lock
    (Codex design-gate fold). Simulated by corrupting staging post-copy."""
    _seed_server(roots["a"])
    import memtomem.context.mcp_servers_copy as mod

    real_stage = mod._stage_copy

    def corrupting_stage(src: Path, dst_parent: Path, name_hint: str) -> Path:
        staging = real_stage(src, dst_parent, name_hint=name_hint)
        staging.write_text(json.dumps(_NETWORK_DEFINITION), encoding="utf-8")
        return staging

    monkeypatch.setattr(mod, "_stage_copy", corrupting_stage)
    with pytest.raises(McpServerParseError, match="Only stdio servers are supported"):
        _copy(roots, apply_=True)
    dst = _dst_path(roots)
    assert not dst.exists()
    assert not list(dst.parent.glob(".migrate-*"))  # staging cleaned on refusal


def test_symlinked_source_refused_pre_flight(roots, tmp_path: Path) -> None:
    """A symlinked canonical would alias the TARGET inode into the
    destination's git-tracked tree via the hard-link promote (Codex
    review blocker) — refused loudly, in both run modes."""
    target = tmp_path / "outside-target.json"
    target.write_text(json.dumps(_CLEAN_DEFINITION), encoding="utf-8")
    store = roots["a"] / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True)
    src = store / "pg.json"
    src.symlink_to(target)

    for apply_ in (False, True):
        with pytest.raises(click.ClickException, match="is a symlink") as exc_info:
            _copy(roots, apply_=apply_)
        assert "refuses symlinked canonicals" in exc_info.value.message
    assert not _dst_path(roots).exists()


def test_staging_turned_symlink_refused_in_lock(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    """A source that becomes a symlink between pre-flight and staging is
    caught by the in-lock regular-file guard; staging is cleaned and the
    destination stays empty."""
    _seed_server(roots["a"])
    import memtomem.context.mcp_servers_copy as mod

    real_stage = mod._stage_copy

    def symlinking_stage(src: Path, dst_parent: Path, name_hint: str) -> Path:
        staging = real_stage(src, dst_parent, name_hint=name_hint)
        staging.unlink()
        staging.symlink_to(src)
        return staging

    monkeypatch.setattr(mod, "_stage_copy", symlinking_stage)
    with pytest.raises(click.ClickException, match="refuses symlinked canonicals"):
        _copy(roots, apply_=True)
    dst = _dst_path(roots)
    assert not dst.exists()
    assert not list(dst.parent.glob(".migrate-*"))


# ── Gate A (issue acceptance 1) ──────────────────────────────────────


def test_gate_a_env_secret_blocks_with_standard_envelope(roots) -> None:
    src = _seed_server(roots["a"], definition=_SECRET_DEFINITION)
    with pytest.raises(PrivacyBlockedError) as exc_info:
        _copy(roots, apply_=True)

    # Standard project_shared block envelope (format_scan_block_message
    # with the source-anchored hint), attributed to the SOURCE file the
    # user can edit — not the transient staging entry.
    message = exc_info.value.message
    assert message.startswith(
        "Gate A: pg.json contains 1 privacy pattern hit(s); "
        "write to scope='project_shared' rejected."
    )
    assert f"Offending file: {src}" in message
    assert "env block" in message
    assert exc_info.value.scope == "project_shared"
    assert exc_info.value.kind == "MCP server"
    assert exc_info.value.artifact_name == "pg"

    # Zero residue at the destination: no canonical, no staging leftovers
    # (the ``.pg.json.lock`` sidecar is the pair lock's own artifact and
    # expected to remain).
    dst = _dst_path(roots)
    assert not dst.exists()
    assert not list(dst.parent.glob(".migrate-*"))
    assert not list(dst.parent.glob("*.json"))
    assert src.is_file()  # source never consumed


# ── concurrency: in-lock re-check, no-clobber promote, lock budget ───


def test_collision_detected_inside_lock_window(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    """A destination created after pre-flight but before the pair lock is
    caught by the in-lock re-check (engine literal)."""
    _seed_server(roots["a"])
    import memtomem.context.mcp_servers_copy as mod

    real_pair_lock = mod._acquire_pair_lock
    dst = _dst_path(roots)

    def racing_pair_lock(path_a: Path, path_b: Path, *, timeout: float | None = None):
        _seed_server(roots["b"], definition={"command": "raced"})
        return real_pair_lock(path_a, path_b, timeout=timeout)

    monkeypatch.setattr(mod, "_acquire_pair_lock", racing_pair_lock)
    with pytest.raises(TransferCollisionError) as exc_info:
        _copy(roots, apply_=True)
    assert exc_info.value.message == f"destination appeared during lock acquire: {dst}."
    assert json.loads(dst.read_text(encoding="utf-8")) == {"command": "raced"}
    assert not list(dst.parent.glob(".migrate-*"))


def test_promote_no_clobber_never_overwrites(tmp_path: Path) -> None:
    """The Codex design-gate blocker: a writer outside the sidecar locks
    (mcp web CRUD holds only the in-process gateway lock) landing a
    canonical between check and rename must NOT be overwritten."""
    staging = tmp_path / ".migrate-pg-1.tmp"
    staging.write_text('{"command": "ours"}', encoding="utf-8")
    dst = tmp_path / "pg.json"
    dst.write_text('{"command": "theirs"}', encoding="utf-8")

    with pytest.raises(TransferCollisionError):
        _promote_no_clobber(staging, dst)
    assert dst.read_text(encoding="utf-8") == '{"command": "theirs"}'
    assert staging.is_file()  # caller owns staging cleanup on refusal


def test_lock_timeout_self_aborts_with_nothing_committed(roots) -> None:
    """Held destination sidecar + bounded budget → TimeoutError, zero
    filesystem change (the web route's #1145 orphan-thread guard).
    portalocker contends across separate fds in-process."""
    src = _seed_server(roots["a"])
    dst = _dst_path(roots)

    with _file_lock(_lock_path_for(dst)):
        with pytest.raises(TimeoutError, match="held by another process"):
            _copy(roots, apply_=True, lock_timeout=0.2)

    assert src.is_file()
    assert not dst.exists()
    assert not dst.parent.is_dir() or not list(dst.parent.glob(".migrate-*"))


# ── destination .mcp.json disclosure notes ───────────────────────────


def test_notes_disclose_same_name_runtime_entry(roots) -> None:
    _seed_server(roots["a"])
    (roots["b"] / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"pg": {"command": "old"}}}), encoding="utf-8"
    )
    result = _copy(roots, apply_=False)
    assert result.notes == (
        "destination .mcp.json already defines 'pg'; the destination's next "
        "mcp-servers sync will overwrite that entry with the copied canonical",
    )


def test_notes_disclose_broken_dst_mcp_json(roots) -> None:
    _seed_server(roots["a"])
    (roots["b"] / ".mcp.json").write_text("{not json", encoding="utf-8")
    result = _copy(roots, apply_=False)
    assert len(result.notes) == 1
    note = result.notes[0]
    assert note.startswith("destination .mcp.json cannot be parsed (invalid JSON in .mcp.json:")
    assert note.endswith("its mcp-servers sync will fail until the file is fixed")


def test_notes_disclose_every_shape_sync_would_refuse(roots) -> None:
    """The disclosure must use the SYNC's parser: shapes that are valid
    JSON but refused by ``generate_all_mcp_servers`` (top-level
    non-object, non-object ``mcpServers``) still warn (Codex review
    fold — a bare json.loads went quiet exactly on these)."""
    _seed_server(roots["a"])
    for body in ('["top-level array"]', '{"mcpServers": []}'):
        (roots["b"] / ".mcp.json").write_text(body, encoding="utf-8")
        result = _copy(roots, apply_=False)
        assert len(result.notes) == 1, body
        assert result.notes[0].startswith("destination .mcp.json cannot be parsed ("), body


def test_notes_quiet_when_dst_mcp_json_unrelated(roots) -> None:
    _seed_server(roots["a"])
    (roots["b"] / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8"
    )
    assert _copy(roots, apply_=False).notes == ()


# ── issue acceptance 2: panel + additive .mcp.json sync at B ─────────


def test_acceptance_panel_listing_and_additive_sync(roots) -> None:
    src = _seed_server(roots["a"])
    foreign = {"command": "uvx", "args": ["foreign-server"]}
    (roots["b"] / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"foreign": foreign}}, indent=2) + "\n", encoding="utf-8"
    )

    result = _copy(roots, apply_=True)
    assert result.transferred is True

    # Panel source of truth at B: the copied canonical is listed.
    assert [p.name for p in list_canonical_mcp_servers(roots["b"])] == ["pg.json"]

    # B's sync merges additively: the foreign runtime entry survives and
    # the copied server lands with the canonical definition.
    sync = generate_all_mcp_servers(roots["b"])
    assert [(runtime, name) for runtime, name, _path in sync.generated] == [("project_mcp", "pg")]
    merged = json.loads((roots["b"] / ".mcp.json").read_text(encoding="utf-8"))
    assert merged["mcpServers"]["foreign"] == foreign
    assert merged["mcpServers"]["pg"] == json.loads(src.read_text(encoding="utf-8"))
