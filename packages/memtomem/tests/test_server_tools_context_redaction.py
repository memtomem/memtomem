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

The #1539 review round (Codex gate) found two more legs the audit's four-leg
list missed, pinned here as well:

5. ``mem_context_init`` — the import-engine ``_skip_line`` rows echo raw
   ``OSError`` reasons with the absolute source path;
6. the settings loops in ``mem_context_generate`` / ``mem_context_diff`` /
   ``mem_context_sync`` — ``SettingsSyncResult.reason`` embeds absolute
   canonical/target paths (``context/settings.py`` f-strings) and the ok-row
   ``target`` echo is an absolute path (``$HOME`` for user scope).

A parity guard (``TestWebParityGuard``) additionally pins the neutral
``context.error_redact`` twins against the web originals on representative
inputs so the two copies of this security boundary cannot silently drift
before the planned delegation refactor.

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
    # ``abs_path`` renders with ``os.sep``, so the surviving remainder does too
    # (Windows: ``.memtomem\agents\…``) — compare via ``Path`` (#838 discipline).
    assert str(Path(".memtomem/agents/foo/agent.md")) in out  # relative remainder


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


# ── leg 5 (#1539 review round): init import-engine skip lines ────────────────


@pytest.mark.anyio
async def test_init_skipped_reason_redacts_absolute_path(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import agents as ctx_agents
    from memtomem.context.agents import ExtractResult
    from memtomem.server.tools.context import mem_context_init

    root = layout["project_root"]
    reason = f"unreadable: [Errno 13] Permission denied: '{root}/.claude/agents/foo.md'"
    result = ExtractResult(imported=[], skipped=[("foo", reason, "parse_error")])
    monkeypatch.setattr(ctx_agents, "extract_agents_to_canonical", lambda *a, **k: result)

    out = await mem_context_init(include="agents")

    _assert_no_abs_path(out, root)
    assert "skipped foo:" in out
    assert "Permission denied" in out  # errno text survives, path-stripped


@pytest.mark.anyio
async def test_init_privacy_blocked_skip_keeps_relative_remainder(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocked rows redact like skips — the remediation full-path channel is
    the ``privacy block:`` exception return, not the per-item row."""
    from memtomem.context import _skip_reasons as skip_codes
    from memtomem.context import agents as ctx_agents
    from memtomem.context.agents import ExtractResult
    from memtomem.server.tools.context import mem_context_init

    root = layout["project_root"]
    reason = f"privacy hits in {root}/.claude/agents/foo.md (1 finding)"
    result = ExtractResult(imported=[], skipped=[("foo", reason, skip_codes.PRIVACY_BLOCKED)])
    monkeypatch.setattr(ctx_agents, "extract_agents_to_canonical", lambda *a, **k: result)

    out = await mem_context_init(include="agents")

    _assert_no_abs_path(out, root)
    assert "blocked foo:" in out
    assert ".claude/agents/foo.md" in out  # relative remainder stays actionable


# ── leg 6 (#1539 review round): settings reasons + ok-row target ─────────────


def _settings_results(root: Path) -> dict[str, object]:
    """One result per settings branch that renders a reason or a target."""
    from memtomem.context.settings import SettingsSyncResult

    return {
        "claude_settings": SettingsSyncResult(
            status="ok", target=root / ".claude" / "settings.json"
        ),
        "codex_settings": SettingsSyncResult(
            status="skipped",
            reason=f"{root}/.memtomem/settings.json is not valid JSON (or not a JSON object).",
        ),
        "kimi_settings": SettingsSyncResult(
            status="needs_confirmation",
            reason=f"{root}/.kimi/settings.json is outside the project root; pass "
            "allow_host_writes=True.",
        ),
        "gemini_settings": SettingsSyncResult(
            status="error",
            reason=f"{root}/.gemini/settings.json: boom. Fix the file manually.",
        ),
    }


@pytest.mark.anyio
async def test_generate_settings_reasons_and_target_redact_absolute_paths(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import settings as ctx_settings
    from memtomem.server.tools.context import mem_context_generate

    root = layout["project_root"]
    monkeypatch.setattr(
        ctx_settings, "generate_all_settings", lambda *a, **k: _settings_results(root)
    )

    out = await mem_context_generate(include="settings")

    _assert_no_abs_path(out, root)
    # The relativized target renders with ``os.sep`` — compare via ``Path``.
    assert f"Settings: claude_settings → {Path('.claude/settings.json')}" in out
    assert "skipped codex_settings:" in out
    assert "needs confirmation kimi_settings:" in out
    assert "error gemini_settings:" in out
    assert "is not valid JSON" in out  # diagnostics survive, path-stripped


@pytest.mark.anyio
async def test_sync_settings_reasons_and_target_redact_absolute_paths(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import settings as ctx_settings
    from memtomem.server.tools.context import mem_context_sync

    root = layout["project_root"]
    monkeypatch.setattr(
        ctx_settings, "generate_all_settings", lambda *a, **k: _settings_results(root)
    )

    out = await mem_context_sync(include="settings")

    _assert_no_abs_path(out, root)
    # Same ``os.sep`` rendering as the generate twin above.
    assert f"Settings: claude_settings → {Path('.claude/settings.json')}" in out
    assert "skipped codex_settings:" in out
    assert "needs confirmation kimi_settings:" in out
    assert "error gemini_settings:" in out


@pytest.mark.anyio
async def test_diff_settings_reasons_redact_absolute_paths(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.context import settings as ctx_settings
    from memtomem.context.settings import SettingsSyncResult
    from memtomem.server.tools.context import mem_context_diff

    root = layout["project_root"]
    results = {
        "claude_settings": SettingsSyncResult(
            status="skipped",
            reason=f"{root}/.memtomem/settings.json is not valid JSON (or not a JSON object).",
        ),
        "codex_settings": SettingsSyncResult(
            status="error",
            reason=f"{root}/.codex/settings.json: boom. Fix the file manually.",
        ),
    }
    monkeypatch.setattr(ctx_settings, "diff_settings", lambda *a, **k: results)

    out = await mem_context_diff(include="settings")

    _assert_no_abs_path(out, root)
    assert "skipped claude_settings:" in out
    assert "error codex_settings:" in out


@pytest.mark.anyio
async def test_generate_settings_dup_tier_warning_redacts_path_untruncated(
    layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dup-tier warning lines redact the tier path (#1550 — the leg #1539 missed).

    Only the PATH is substituted before ``format_warning`` runs: redacting the
    formatted line would hit the 200-char ``redact_message`` cap and truncate
    the migrate hint, so pin the tail's survival too. (The $HOME-collapse of a
    user-tier path is pinned at the web boundary and in the neutral-redactor
    contract tests; ``layout``'s fake HOME differs from the import-frozen
    ``_HOME`` anchor, so this pin uses an in-root project_local duplicate.)
    """
    from memtomem.context import settings as ctx_settings
    from memtomem.context import settings_doctor as ctx_doctor
    from memtomem.server.tools.context import mem_context_generate

    root = layout["project_root"]
    dup = ctx_doctor.DuplicateTier(
        tier="project_local",
        path=root / ".claude" / "settings.local.json",
        entries=(ctx_doctor.HookSignature("PreToolUse", "Bash", "echo ok"),),
    )
    monkeypatch.setattr(ctx_doctor, "detect_duplicate_tiers", lambda *a, **k: [dup])
    monkeypatch.setattr(ctx_settings, "generate_all_settings", lambda *a, **k: {})

    out = await mem_context_generate(include="settings")

    _assert_no_abs_path(out, root)
    assert f"({Path('.claude/settings.local.json')})" in out  # root-stripped, still named
    assert "settings-migrate" in out  # the remediation command survives
    assert "Active scope:" in out  # the tail survives — no 200-char cap bite


# ── parity guard: the neutral leaf must not drift from the web twins ─────────


class TestWebParityGuard:
    """Pin ``context.error_redact`` byte-for-byte against the web originals.

    The neutral leaf duplicates ``web/routes/_errors._redact_message`` and
    ``context_gateway.sanitize_diff_reason`` (the MCP layer may not import
    ``memtomem.web.*``). This is a security boundary — a silent drift between
    the twins reopens the leak on whichever surface got the stale copy — so
    representative inputs are compared against BOTH implementations until the
    planned delegation refactor collapses them.
    """

    def test_frozen_constants_match_web(self) -> None:
        from memtomem.context import error_redact
        from memtomem.web.routes import _errors as web_errors

        assert error_redact._HOME == web_errors._HOME
        assert error_redact._ERROR_MESSAGE_LIMIT == web_errors._ERROR_MESSAGE_LIMIT
        assert error_redact._SECRET_REDACTED_MARKER == web_errors._SECRET_REDACTED_MARKER

    def test_redact_message_matches_web(self) -> None:
        from memtomem.context import error_redact
        from memtomem.web.routes._errors import _redact_message as web_redact

        home = error_redact._HOME
        cases = [
            "plain diagnostic, no path",
            f"unreadable: [Errno 13] Permission denied: '{home}/proj/agent.md'",
            "x" * 500,  # truncation
            "token AKIA1234567890ABCDEF leaked",  # secret-shape → whole-replace
            "",
        ]
        for msg in cases:
            assert error_redact.redact_message(msg) == web_redact(msg), msg

    def test_engine_reason_matches_web_single_root(self, tmp_path: Path) -> None:
        from memtomem.context.error_redact import redact_engine_reason
        from memtomem.web.routes.context_gateway import sanitize_diff_reason

        real = (tmp_path / "real").resolve()
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        cases = [
            None,
            "",
            "no path at all",
            f"missing YAML frontmatter: {real}/.memtomem/agents/foo/agent.md",
            f"{real}: [Errno 2] No such file or directory",
            f"{real}{real}/nested repeat",
        ]
        for root in (real, link, tmp_path):
            for msg in cases:
                assert redact_engine_reason(msg, root) == sanitize_diff_reason(msg, root), (
                    msg,
                    root,
                )


# ── #1520 item 7: migrate/transfer success-path formatters collapse $HOME ────


class TestSuccessPathFormattersCollapseHome:
    """The migrate/transfer result formatters must ``~``-collapse every echoed
    path (src/dst headers, fan-out lists, engine notes) — those absolute paths
    embed the username on the MCP wire. Redaction is per path, not per line
    (#1550: a line-level pass lets the 200-char cap bite surrounding text).
    ``sync_command`` stays verbatim by doctrine — it is a runnable remediation
    command. Formatters are exercised directly with fabricated results whose
    paths sit under a patched ``error_redact._HOME``.

    Cross-platform (#838 discipline): the fabricated paths are ``Path``
    objects, so ``str(path)`` renders with ``os.sep`` (``\\`` on Windows) —
    the patched ``_HOME`` must therefore be ``str(Path(HOME))`` too, or the
    collapse's literal ``replace`` would miss on Windows exactly as
    production would not (there ``_HOME = str(Path.home())`` already carries
    ``os.sep``). Assertions compare in POSIX space via ``_norm``.
    """

    HOME = "/Users/alice"

    @staticmethod
    def _norm(out: str) -> str:
        return out.replace("\\", "/")

    @pytest.fixture(autouse=True)
    def _pin_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.context import error_redact

        # os.sep-native so the literal $HOME replace fires on Windows too.
        monkeypatch.setattr(error_redact, "_HOME", str(Path(self.HOME)))

    def _migrate_result(self, *, moved: bool):
        from memtomem.context.migrate import MigrateScopeResult

        home = Path(self.HOME)
        return MigrateScopeResult(
            kind="skills",
            name="demo",
            from_scope="user",
            to_scope="project_shared",
            src_path=home / ".memtomem" / "skills" / "demo",
            dst_path=home / "proj" / ".memtomem" / "skills" / "demo",
            layout="dir",
            moved=moved,
            fanout_cleaned=[home / ".claude" / "skills" / "demo" / "SKILL.md"],
            fanout_backed_up=[home / ".claude" / "skills" / "demo" / "SKILL.md.bak"],
            fanout_planned=[home / ".claude" / "skills" / "demo" / "SKILL.md"],
        )

    def _transfer_result(self, *, transferred: bool, **overrides):
        from memtomem.context.transfer import TransferResult

        home = Path(self.HOME)
        kwargs = dict(
            kind="skills",
            name="demo",
            dst_name="demo-copy",
            mode="copy",
            from_scope="project_shared",
            to_scope="user",
            src_project_root=home / "proj",
            dst_project_root=None,
            src_path=home / "proj" / ".memtomem" / "skills" / "demo",
            dst_path=home / ".memtomem" / "skills" / "demo-copy",
            layout="dir",
            transferred=transferred,
            fanout_cleaned=[home / ".claude" / "skills" / "demo" / "SKILL.md"],
            fanout_backed_up=[home / ".claude" / "skills" / "demo" / "SKILL.md.bak"],
            fanout_planned=[home / ".claude" / "skills" / "demo" / "SKILL.md"],
            needs_sync=True,
            sync_command="mm context sync --include=skills",
            notes=(
                "overrides travel verbatim: "
                + str(home / ".memtomem" / "skills" / "demo-copy" / "overrides"),
            ),
        )
        kwargs.update(overrides)
        return TransferResult(**kwargs)

    def test_migrate_dry_run_collapses_all_paths(self) -> None:
        from memtomem.server.tools.context import _format_artifact_scope_result

        norm = self._norm(
            _format_artifact_scope_result(self._migrate_result(moved=False), apply_=False)
        )

        assert self.HOME not in norm, norm  # native home gone (both sep forms)
        assert "from user: ~/.memtomem/skills/demo" in norm
        assert "    - ~/" in norm  # fanout_planned entries
        assert "Re-call with apply=True" in norm  # tail survives — no cap bite

    def test_migrate_apply_collapses_all_paths(self) -> None:
        from memtomem.server.tools.context import _format_artifact_scope_result

        norm = self._norm(
            _format_artifact_scope_result(self._migrate_result(moved=True), apply_=True)
        )

        assert self.HOME not in norm, norm
        assert "✓ moved skills/demo" in norm
        assert norm.count("    - ~/") == 2  # fanout_cleaned + fanout_backed_up

    def test_transfer_dry_run_collapses_paths_and_notes(self) -> None:
        from memtomem.server.tools.context import _format_transfer_result

        norm = self._norm(
            _format_transfer_result(self._transfer_result(transferred=False), apply_=False)
        )

        assert self.HOME not in norm, norm
        assert "  note: overrides travel verbatim: ~/" in norm
        # sync_command renders verbatim (runnable remediation command).
        assert "`mm context sync --include=skills`" in norm

    def test_transfer_apply_collapses_paths_and_notes(self) -> None:
        from memtomem.server.tools.context import _format_transfer_result

        norm = self._norm(
            _format_transfer_result(self._transfer_result(transferred=True), apply_=True)
        )

        assert self.HOME not in norm, norm
        assert "✓ copied skills/demo" in norm
        assert "  note: overrides travel verbatim: ~/" in norm
        assert norm.count("    - ~/") == 2  # fanout_cleaned + fanout_backed_up

    @pytest.mark.parametrize("apply_", [False, True])
    def test_transfer_provenance_reason_collapses_home(self, apply_: bool) -> None:
        # Codex review on the #1520 item 7 round: ``provenance_reason`` can
        # wrap an OSError from the destination lock.json write (absolute path
        # inside) and rendered verbatim on both the dry-run and apply legs.
        from memtomem.server.tools.context import _format_transfer_result

        # Native str(Path(...)), NOT repr — an os.sep path so the collapse
        # fires under the os-native _HOME on every platform (a repr would
        # double the Windows backslashes and dodge the literal replace).
        lock_path = str(Path(self.HOME) / ".memtomem" / "lock.json")
        result = self._transfer_result(
            transferred=apply_,
            provenance="not_carried",
            provenance_reason=(
                f"destination lock.json could not be written "
                f"([Errno 13] Permission denied: {lock_path})"
            ),
        )

        norm = self._norm(_format_transfer_result(result, apply_=apply_))

        assert self.HOME not in norm, norm
        assert "destination lock.json could not be written" in norm
        assert "~/.memtomem/lock.json" in norm  # collapsed, diagnostic survives


class TestResidualAbsoluteScrub:
    """The MCP posture: kill what is still absolute, keep what is relative.

    The web twin (``context_gateway.redact_wire_reason``) scrubs anything
    path-shaped; this surface must not, because the root-stripped remainder is
    what makes a row actionable. These pin both halves together, since either
    one alone is satisfiable by the wrong function.
    """

    def test_path_under_no_known_root_is_scrubbed(self) -> None:
        """The leak this closes: a runtime dir symlinked onto a shared volume
        resolves under neither the passed root nor ``$HOME``, so root stripping
        finds nothing and the full path reached the calling agent's transcript.
        """
        from memtomem.context.error_redact import scrub_residual_absolute_paths

        out = scrub_residual_absolute_paths("unreadable: /Volumes/Shared/team/.claude/skills/hello")

        assert "/Volumes/Shared/team" not in out
        assert "<path>" in out
        assert out.startswith("unreadable: ")

    @pytest.mark.parametrize(
        "kept",
        [
            "privacy hits in .claude/agents/foo.md (1 finding)",
            "missing YAML frontmatter: .memtomem/agents/foo/agent.md",
            "copied to ~/.claude/skills/hello",
            # Non-ASCII segment: an ASCII-only lookbehind read it as a boundary
            # and scrubbed the remainder to ``자료<path>`` (PR review).
            "unreadable: 자료/agents/foo.md",
        ],
    )
    def test_relative_and_home_collapsed_remainders_survive(self, kept: str) -> None:
        """What root stripping and ``$HOME`` collapse deliberately leave behind
        is remediation, not disclosure — and the success-path formatters render
        ``~``-collapsed paths as their intended output.
        """
        from memtomem.context.error_redact import scrub_residual_absolute_paths

        assert scrub_residual_absolute_paths(kept) == kept

    @pytest.mark.anyio
    async def test_mcp_skip_row_scrubs_an_out_of_root_path(
        self, layout, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The BOUNDARY, not the helper. Every other test here builds its path
        under a root that gets stripped, so none of them fails if
        ``_redact_reason`` drops the scrub.
        """
        from memtomem.context import _skip_reasons as skip_codes
        from memtomem.context import agents as ctx_agents
        from memtomem.context.agents import ExtractResult
        from memtomem.server.tools.context import mem_context_init

        reason = "unreadable: /Volumes/Shared/team/.claude/agents/foo.md"
        result = ExtractResult(imported=[], skipped=[("foo", reason, skip_codes.PARSE_ERROR)])
        monkeypatch.setattr(ctx_agents, "extract_agents_to_canonical", lambda *a, **k: result)

        out = await mem_context_init(include="agents")

        assert "/Volumes/Shared/team" not in out
        assert "<path>" in out
