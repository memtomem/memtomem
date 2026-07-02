"""Path-redaction pins for the MCP context tool error/reason surfaces.

The web wire boundary sanitizes raw engine exception text before it leaves the
loopback dashboard (``web/routes/_errors._redact_message`` +
``context_gateway.sanitize_diff_reason``). The MCP context tools in
``server/tools/context.py`` are a second wire boundary for the same reasons —
their string results flow into the calling agent's transcript and on to the
model provider — so they must strip the absolute host path (``$HOME`` / the OS
username) the same way. These tests pin the four legs the dedicated MCP-surface
audit found unredacted:

1. ``mem_context_diff`` diff-row ``reason`` and the generate / sync ``skipped``
   lines (a ``DiffRow.reason`` / skip tuple embeds the absolute source path);
2. ``mem_context_artifact_transfer`` — ``McpServerParseError`` → ``safe_message``
   (basename, not the resolved canonical path) and the adjacent
   ``FileNotFoundError`` → path-stripped;
3. ``mem_context_artifact_migrate`` — both ``FileNotFoundError`` legs
   (scope-tier ``migrate_scope`` + flat ``classify_migrate``);
4. ``mem_context_version`` create — the working-canonical read ``OSError`` echoes
   only the basename with a path-stripped errno message.

The remediation-critical ``privacy block: …`` message is intentionally left
round-tripping the full path (``test_sync_privacy_block_surfaces``) and is not
exercised here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.error_redact import redact_engine_reason, redact_message


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A project root (``.git`` so ``_find_project_root`` terminates) + isolated
    HOME + cwd at the project (mirrors the sibling MCP context tool tests)."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.chdir(project_root)
    return {"project_root": project_root.resolve(), "user_home": user_home.resolve()}


def _assert_no_abs_path(out: str, project_root: Path) -> None:
    """The absolute project root (both forms) must never reach the wire."""
    assert str(project_root) not in out, out
    assert str(project_root.resolve()) not in out, out


# ── neutral redactor contract (mirror of the web twins) ──────────────────────


class TestNeutralRedactor:
    def test_reason_strips_both_root_forms(self, tmp_path: Path) -> None:
        real = (tmp_path / "real").resolve()
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        # A reason built from the RESOLVED path but redacted with the UNRESOLVED
        # (symlinked) root — the single-form strip left the resolved path behind
        # pre-fix (the #1412 canonical-path-leak shape).
        reason = f"missing YAML frontmatter: {real}/.memtomem/agents/foo/agent.md"
        out = redact_engine_reason(reason, link)
        assert out is not None
        assert str(real) not in out
        assert str(link) not in out
        assert "missing YAML frontmatter" in out
        assert ".memtomem/agents/foo/agent.md" in out  # relative remainder survives

    def test_empty_reason_is_none(self) -> None:
        assert redact_engine_reason(None, Path("/x")) is None
        assert redact_engine_reason("", Path("/x")) is None

    def test_message_collapses_home_and_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``_HOME`` is frozen at import — patch the module constant to assert the
        # collapse without depending on the test runner's real home.
        from memtomem.context import error_redact

        monkeypatch.setattr(error_redact, "_HOME", "/Users/alice")
        assert error_redact.redact_message("boom at /Users/alice/x") == "boom at ~/x"
        assert len(error_redact.redact_message("x" * 500)) == 200

    def test_message_drops_secret_shape_whole(self) -> None:
        # AWS access-key shape trips the LTM secret-class scanner → whole-replace.
        assert redact_message("token AKIA1234567890ABCDEF leaked") == "<redacted: secret-shape>"


# ── leg 1: diff-row reason + generate/sync skipped lines ─────────────────────


@pytest.mark.anyio
async def test_diff_row_reason_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import agents as ctx_agents
    from memtomem.context._runtime_targets import DiffRow
    from memtomem.server.tools.context import mem_context_diff

    root = layout["project_root"]
    abs_path = root / ".memtomem" / "agents" / "foo" / "agent.md"
    reason = f"missing YAML frontmatter: {abs_path}"

    def fake_diff_agents(project_root, *, scope="project_shared"):
        return [DiffRow("claude", "foo", "parse error", reason)]

    monkeypatch.setattr(ctx_agents, "diff_agents", fake_diff_agents)

    out = await mem_context_diff(include="agents", scope="project_shared")

    _assert_no_abs_path(out, root)
    assert "parse error" in out
    assert "missing YAML frontmatter" in out  # the diagnostic survives, path-stripped
    assert ".memtomem/agents/foo/agent.md" in out  # relative remainder


def _skip_result(reason: str):
    """An ``AgentSyncResult`` whose only outcome is one path-bearing skip."""
    from memtomem.context.agents import AgentSyncResult

    return AgentSyncResult(generated=[], dropped=[], skipped=[("claude", reason, "parse_error")])


@pytest.mark.anyio
async def test_generate_skipped_reason_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import agents as ctx_agents
    from memtomem.server.tools.context import mem_context_generate

    root = layout["project_root"]
    reason = f"unreadable: [Errno 13] Permission denied: '{root}/.memtomem/agents/foo/agent.md'"
    monkeypatch.setattr(ctx_agents, "generate_all_agents", lambda *a, **k: _skip_result(reason))

    out = await mem_context_generate(include="agents", scope="project_shared")

    _assert_no_abs_path(out, root)
    assert "skipped claude:" in out
    assert "Permission denied" in out  # errno text survives, path-stripped


@pytest.mark.anyio
async def test_sync_skipped_reason_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import agents as ctx_agents
    from memtomem.server.tools.context import mem_context_sync

    root = layout["project_root"]
    reason = f"unreadable: [Errno 13] Permission denied: '{root}/.memtomem/agents/foo/agent.md'"
    monkeypatch.setattr(ctx_agents, "generate_all_agents", lambda *a, **k: _skip_result(reason))

    out = await mem_context_sync(include="agents", scope="project_shared")

    _assert_no_abs_path(out, root)
    assert "skipped claude:" in out
    assert "Permission denied" in out


# ── leg 2: artifact_transfer parse error + FileNotFoundError ─────────────────


@pytest.mark.anyio
async def test_transfer_parse_error_uses_safe_message(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import transfer as ctx_transfer
    from memtomem.context.mcp_servers import McpServerParseError
    from memtomem.server.tools.context import mem_context_artifact_transfer

    root = layout["project_root"]
    abs_path = root / ".memtomem" / "mcp-servers" / "x.json"

    def boom(*a, **k):
        raise McpServerParseError(
            f"invalid JSON in {abs_path}: Expecting value",
            safe_message="invalid JSON in x.json: Expecting value",
        )

    monkeypatch.setattr(ctx_transfer, "transfer_artifact", boom)

    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_scope="project_local"
    )

    _assert_no_abs_path(out, root)
    assert out == "error: invalid JSON in x.json: Expecting value"


@pytest.mark.anyio
async def test_transfer_filenotfound_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import transfer as ctx_transfer
    from memtomem.server.tools.context import mem_context_artifact_transfer

    root = layout["project_root"]
    abs_path = root / ".memtomem" / "agents" / "foo" / "agent.md"

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", str(abs_path))

    monkeypatch.setattr(ctx_transfer, "transfer_artifact", boom)

    out = await mem_context_artifact_transfer(
        asset_type="agents", name="foo", mode="copy", to_scope="project_local"
    )

    _assert_no_abs_path(out, root)
    assert out.startswith("error:")
    assert "No such file or directory" in out


# ── leg 3: artifact_migrate FileNotFoundError (both legs) ─────────────────────


@pytest.mark.anyio
async def test_migrate_scope_filenotfound_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import migrate as ctx_migrate
    from memtomem.server.tools.context import mem_context_artifact_migrate

    root = layout["project_root"]

    def boom(*a, **k):
        raise FileNotFoundError(f"project_root {root} is not a directory")

    monkeypatch.setattr(ctx_migrate, "migrate_scope", boom)

    out = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", to_scope="project_local"
    )

    _assert_no_abs_path(out, root)
    assert out.startswith("error:")
    assert "is not a directory" in out


@pytest.mark.anyio
async def test_migrate_flat_filenotfound_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import migrate as ctx_migrate
    from memtomem.server.tools.context import mem_context_artifact_migrate

    root = layout["project_root"]

    def boom(*a, **k):
        raise FileNotFoundError(f"no flat canonical to adopt at {root}/.memtomem/agents/foo.md")

    monkeypatch.setattr(ctx_migrate, "classify_migrate", boom)

    out = await mem_context_artifact_migrate(asset_type="agents", name="foo")

    _assert_no_abs_path(out, root)
    assert out.startswith("error:")
    assert "no flat canonical to adopt" in out


# ── leg 4: version create working-canonical read OSError ─────────────────────


@pytest.mark.anyio
async def test_version_create_oserror_echoes_basename_only(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.server.tools.context import mem_context_version

    root = layout["project_root"]
    artifact_dir = root / ".memtomem" / "agents" / "foo"
    artifact_dir.mkdir(parents=True)
    working_file = artifact_dir / "agent.md"
    working_file.write_text("---\nname: foo\ndescription: d\n---\n\nbody\n", encoding="utf-8")

    real_read_bytes = Path.read_bytes

    def boom(self: Path):
        if self == working_file:
            raise OSError(13, "Permission denied", str(working_file))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", boom)

    out = await mem_context_version("agents", "foo", action="create")

    _assert_no_abs_path(out, root)
    assert out.startswith("error: cannot read working canonical agent.md:")
    assert "Permission denied" in out
