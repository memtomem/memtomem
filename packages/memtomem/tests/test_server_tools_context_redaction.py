"""Path-redaction pins for the MCP context tool reason surfaces.

The web wire boundary sanitizes raw engine exception text before it leaves the
loopback dashboard (``web/routes/_errors._redact_message`` +
``context_gateway.sanitize_diff_reason``). The MCP context tools in
``server/tools/context.py`` are a second wire boundary for the same reasons —
their string results flow into the calling agent's transcript and on to the
model provider — so they must strip the absolute host path (``$HOME`` / the OS
username) the same way. Only the two read tools remain on MCP (ADR-0044); these
tests pin their legs:

1. ``mem_context_diff`` diff-row ``reason`` (a ``DiffRow.reason`` embeds the
   absolute source path), including a path under no known root;
2. the ``mem_context_diff`` settings loop — ``SettingsSyncResult.reason`` embeds
   absolute canonical/target paths (``context/settings.py`` f-strings).

A parity guard (``TestWebParityGuard``) additionally pins the neutral
``context.error_redact`` twins against the web originals on representative
inputs so the two copies of this security boundary cannot silently drift
before the planned delegation refactor.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from memtomem.context.error_redact import (
    redact_engine_reason,
    redact_message,
    scrub_absolute_paths,
    scrub_residual_absolute_paths,
)


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

    @pytest.mark.parametrize("suffix", ["-private", "_private", ".private", " private", ":private"])
    def test_prefix_colliding_sibling_is_scrubbed_on_both_wires(
        self, tmp_path: Path, suffix: str
    ) -> None:
        """A sibling is external, never a root-relative remediation (#1889)."""
        from memtomem.server.tools.context import _redact_reason
        from memtomem.web.routes.context_gateway import sanitize_diff_reason

        root = tmp_path / "project"
        sibling = root.with_name(root.name + suffix) / "team" / "secret.md"
        reason = f"unreadable: {sibling}"

        assert _redact_reason(reason, root) == "unreadable: <path>"
        assert sanitize_diff_reason(reason, root) == "unreadable: <path>"

    def test_prefix_collision_is_scrubbed_before_home_collapse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``~/...`` is normally actionable, but not when born from a collision."""
        from memtomem.context import error_redact
        from memtomem.server.tools.context import _redact_reason
        from memtomem.web.routes.context_gateway import sanitize_diff_reason

        monkeypatch.setattr(error_redact, "_HOME", "/home/alice")
        # Keep the fabricated POSIX root POSIX-shaped on Windows too. ``Path``
        # would turn it into ``\\home\\...`` there while the reason remains
        # slash-shaped, testing a representation mismatch rather than the
        # pre-HOME collision ordering.
        root = PurePosixPath("/home/alice/work/memtomem")
        reason = "unreadable: /home/alice/work/memtomem-stm/team/secret.md"

        assert _redact_reason(reason, root) == "unreadable: <path>"
        assert sanitize_diff_reason(reason, root) == "unreadable: <path>"

    def test_windows_prefix_colliding_sibling_is_scrubbed(self) -> None:
        from memtomem.server.tools.context import _redact_reason
        from memtomem.web.routes.context_gateway import sanitize_diff_reason

        root = PureWindowsPath(r"C:\project")
        reason = r"unreadable: C:\project-private\team\secret.md"

        assert _redact_reason(reason, root) == "unreadable: <path>"
        assert sanitize_diff_reason(reason, root) == "unreadable: <path>"

    def test_root_text_inside_another_absolute_path_is_scrubbed(self) -> None:
        from memtomem.server.tools.context import _redact_reason
        from memtomem.web.routes.context_gateway import sanitize_diff_reason

        # This is intentionally a synthetic POSIX reason on every CI host.
        root = PurePosixPath("/srv/project")
        reason = "unreadable: /prefix/srv/project/team/secret.md"

        assert _redact_reason(reason, root) == "unreadable: <path>"
        assert sanitize_diff_reason(reason, root) == "unreadable: <path>"

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

    def test_secret_value_scrub_is_value_scoped(self) -> None:
        """Field-value twin of the above: same marker, no cap, no path logic."""
        from memtomem.context.error_redact import redact_secret_value

        assert redact_secret_value("api_key=AKIA1234567890ABCDEF") == "<redacted: secret-shape>"
        # An ordinary field value is returned untouched, including one longer
        # than ``redact_message``'s 200-char cap — this scrub does not truncate.
        assert redact_secret_value("SessionStart") == "SessionStart"
        assert redact_secret_value("e" * 500) == "e" * 500


# ── leg 1: diff-row reason ───────────────────────────────────────────────────


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


# ── leg 2 (#1539 review round): settings reasons ──────────────────────────────


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


# ── parity guard: the neutral leaf must not drift from the web twins ─────────


class TestWebParityGuard:
    """Pin the neutral redactor byte-for-byte against the web delegates.

    ``redact_message`` still mirrors ``web/routes/_errors._redact_message``;
    ``context_gateway.sanitize_diff_reason`` now delegates root handling to the
    neutral leaf (the MCP layer may not import ``memtomem.web.*``). This is a
    security boundary, so representative inputs remain compared against both
    web entry points.
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


class TestSingleSegmentAbsoluteScrub:
    """Single-segment paths need stricter context than the broad path run."""

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("/secretmount", "<path>"),
            ("unreadable: /secretmount", "unreadable: <path>"),
            (r"unreadable: C:\secretmount", "unreadable: <path>"),
            ("unreadable: D:/shared/", "unreadable: <path>"),
            ("unreadable: /자료", "unreadable: <path>"),
            (
                "unreadable: '/Shared Drive' while loading",
                "unreadable: '<path>' while loading",
            ),
            (
                r'unreadable: "C:\\Shared Drive" while loading',
                'unreadable: "<path>" while loading',
            ),
        ],
    )
    @pytest.mark.parametrize(
        "scrubber",
        [scrub_absolute_paths, scrub_residual_absolute_paths],
        ids=["web", "mcp"],
    )
    def test_scrubs_conservatively_delimited_paths(self, message, expected, scrubber) -> None:
        assert scrubber(message) == expected

    @pytest.mark.parametrize(
        "message",
        [
            "slash / in prose",
            "choose read/write mode",
            "ratio 1/2",
            "visit /health",
            "unreadable: see /secretmount",
            "URL: https://example.com",
            "relative/path",
            "./relative",
            "../relative",
            "~/only",
            "자료/파일",
            r"folder\child",
            r"C:relative",
            "/",
            "C:\\",
        ],
    )
    @pytest.mark.parametrize(
        "scrubber",
        [scrub_absolute_paths, scrub_residual_absolute_paths],
        ids=["web", "mcp"],
    )
    def test_keeps_slash_prose_and_non_absolute_shapes(self, message, scrubber) -> None:
        assert scrubber(message) == message


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
    @pytest.mark.parametrize(
        ("reason", "leaked"),
        [
            (
                "unreadable: /Volumes/Shared/team/.claude/agents/foo.md",
                "/Volumes/Shared/team",
            ),
            ("unreadable: /secretmount", "/secretmount"),
            (r"unreadable: C:\secretmount", r"C:\secretmount"),
        ],
    )
    async def test_mcp_diff_row_scrubs_an_out_of_root_path(
        self,
        layout,
        monkeypatch: pytest.MonkeyPatch,
        reason: str,
        leaked: str,
    ) -> None:
        """The BOUNDARY, not the helper. Every other test here builds its path
        under a root that gets stripped, so none of them fails if
        ``_redact_reason`` drops the scrub.
        """
        from memtomem.context import agents as ctx_agents
        from memtomem.context._runtime_targets import DiffRow
        from memtomem.server.tools.context import mem_context_diff

        def fake_diff_agents(project_root, *, scope="project_shared"):
            return [DiffRow("claude", "foo", "parse error", reason)]

        monkeypatch.setattr(ctx_agents, "diff_agents", fake_diff_agents)

        out = await mem_context_diff(include="agents", scope="project_shared")

        assert leaked not in out
        assert "<path>" in out
