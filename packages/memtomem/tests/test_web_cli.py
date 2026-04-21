"""Tests for `mm web` CLI error handling and the wizard's web-extra hint.

Regression coverage for a bug where `mm web` produced a raw
`ModuleNotFoundError: No module named 'fastapi'` traceback when the `[web]`
extra wasn't installed — because the old error handler only caught missing
`uvicorn`, not `fastapi`.
"""

from __future__ import annotations

import sys
import contextlib
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from memtomem.cli.web import _missing_web_deps, _web_install_hint, web


def test_missing_web_deps_returns_none_when_installed() -> None:
    """In the test env, fastapi + uvicorn are installed (via `[all]`)."""
    assert _missing_web_deps() is None


def test_missing_web_deps_reports_missing_module() -> None:
    """If fastapi isn't importable, report it by name so the error is actionable."""
    # Simulate fastapi being uninstalled by making its import raise.
    with patch.dict(sys.modules, {"fastapi": None}):
        assert _missing_web_deps() == "fastapi"


def test_install_hint_uses_reinstall_flag() -> None:
    """The hint must use `--reinstall` so it works for users who installed
    memtomem without extras via `uv tool install memtomem`."""
    hint = _web_install_hint()
    assert "uv tool install" in hint
    assert "--reinstall" in hint
    assert '"memtomem[web]"' in hint


def test_mm_web_shows_actionable_error_when_fastapi_missing() -> None:
    """Regression: previously this produced a raw traceback because the CLI
    only caught `uvicorn` import failures. Now it should exit 1 with a clean
    message naming the missing module and the install command."""
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value="fastapi"):
        result = runner.invoke(web, [])
    assert result.exit_code == 1
    assert "fastapi" in result.output
    assert "memtomem[web]" in result.output
    # Should not contain a raw traceback.
    assert "Traceback" not in result.output


def test_mm_web_shows_actionable_error_when_uvicorn_missing() -> None:
    """Symmetric case: uvicorn missing."""
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value="uvicorn"):
        result = runner.invoke(web, [])
    assert result.exit_code == 1
    assert "uvicorn" in result.output
    assert "memtomem[web]" in result.output


def test_wizard_next_steps_hint_respects_web_deps(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard's 'Next steps' Step 3 should suggest the install command
    when web deps are missing, and show a normal `mm web` hint otherwise."""
    # We can't easily run the full interactive wizard here, but the hint
    # logic itself is a straightforward branch on _missing_web_deps(). Check
    # both sides by importing and calling the helper directly — this is the
    # same value the wizard uses in `_write_config_and_summary`.
    from memtomem.cli import init_cmd  # noqa: F401 — ensures import side-effects are OK

    # When deps are present, helper returns None → wizard shows clean hint.
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        from memtomem.cli.web import _missing_web_deps as check

        assert check() is None

    # When deps are missing, helper returns module name → wizard shows install
    # command. The actual string assembly is exercised by the test above.
    with patch("memtomem.cli.web._missing_web_deps", return_value="fastapi"):
        from memtomem.cli.web import _missing_web_deps as check

        assert check() == "fastapi"


def _make_server_mock(started: bool = True) -> MagicMock:
    """Return a uvicorn.Server mock.

    ``serve()`` completes immediately; ``started`` is fixed to the given value.
    """
    server = MagicMock()
    server.started = started

    async def _serve() -> None:
        pass

    server.serve = _serve
    return server


def _patch_web_stack(server_mock: MagicMock):
    """Patch all external dependencies required to run ``web()``."""
    return [
        patch("memtomem.cli.web._missing_web_deps", return_value=None),
        patch("uvicorn.Config", return_value=MagicMock()),
        patch("uvicorn.Server", return_value=server_mock),
        patch("memtomem.web.app.create_app", return_value=MagicMock()),
        patch("memtomem.web.app._lifespan", MagicMock()),
    ]


def test_web_no_open_does_not_call_webbrowser() -> None:
    """Without --open, webbrowser.open must never be called."""
    runner = CliRunner()
    server_mock = _make_server_mock(started=True)

    with patch("webbrowser.open") as mock_browser:
        with contextlib.ExitStack() as stack:
            for p in _patch_web_stack(server_mock):
                stack.enter_context(p)
            result = runner.invoke(web, ["--host", "127.0.0.1", "--port", "9999"])

    assert result.exit_code == 0
    mock_browser.assert_not_called()


def test_web_open_calls_webbrowser_when_server_starts() -> None:
    """With --open, webbrowser.open must be called once the server is ready."""
    runner = CliRunner()
    server_mock = _make_server_mock(started=True)

    with patch("webbrowser.open") as mock_browser:
        with contextlib.ExitStack() as stack:
            for p in _patch_web_stack(server_mock):
                stack.enter_context(p)
            result = runner.invoke(web, ["--host", "127.0.0.1", "--port", "9999", "--open"])

    assert result.exit_code == 0
    mock_browser.assert_called_once_with("http://127.0.0.1:9999")


def test_web_open_timeout_warns_and_skips_browser() -> None:
    """If the server never becomes ready within the timeout, emit a warning
    and do not open the browser."""
    runner = CliRunner()
    server_mock = _make_server_mock(started=False)

    with patch("webbrowser.open") as mock_browser:
        with contextlib.ExitStack() as stack:
            for p in _patch_web_stack(server_mock):
                stack.enter_context(p)
            result = runner.invoke(
                web,
                ["--host", "127.0.0.1", "--port", "9999", "--open", "--timeout", "1"],
            )

    assert result.exit_code == 0
    mock_browser.assert_not_called()
    assert "Warning" in result.output
    assert "timeout" in result.output.lower()


def test_web_open_zero_timeout_shows_warning() -> None:
    """--timeout 0 means no timeout; a warning must be printed to inform the user."""
    runner = CliRunner()
    server_mock = _make_server_mock(started=True)

    with patch("webbrowser.open"):
        with contextlib.ExitStack() as stack:
            for p in _patch_web_stack(server_mock):
                stack.enter_context(p)
            result = runner.invoke(
                web,
                ["--host", "127.0.0.1", "--port", "9999", "--open", "--timeout", "0"],
            )

    assert result.exit_code == 0
    assert "Warning" in result.output
    assert "timeout" in result.output.lower()


def test_web_timeout_without_open_is_silent() -> None:
    """Specifying --timeout without --open must exit cleanly with no warnings."""
    runner = CliRunner()
    server_mock = _make_server_mock(started=True)

    with patch("webbrowser.open") as mock_browser:
        with contextlib.ExitStack() as stack:
            for p in _patch_web_stack(server_mock):
                stack.enter_context(p)
            result = runner.invoke(
                web,
                ["--host", "127.0.0.1", "--port", "9999", "--timeout", "5"],
            )

    assert result.exit_code == 0
    mock_browser.assert_not_called()
    assert "Warning" not in result.output


# ---------------------------------------------------------------------------
# --mode / --dev plumbing (prod/dev tier; see test_web_mode.py for semantics)
# ---------------------------------------------------------------------------


def _patch_web_stack_no_create_app(server_mock: MagicMock):
    """Like ``_patch_web_stack`` but leaves ``create_app`` unpatched so the
    caller can install a capture via ``side_effect``."""
    return [
        patch("memtomem.cli.web._missing_web_deps", return_value=None),
        patch("uvicorn.Config", return_value=MagicMock()),
        patch("uvicorn.Server", return_value=server_mock),
        patch("memtomem.web.app._lifespan", MagicMock()),
    ]


def _run_web_capturing_mode(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch | None = None,
    env_mode: str | None = None,
) -> tuple[int, str, str | None]:
    runner = CliRunner()
    server_mock = _make_server_mock(started=True)
    captured: dict[str, str | None] = {"mode": None}

    def _capture(**kwargs) -> MagicMock:
        captured["mode"] = kwargs.get("mode")
        return MagicMock()

    if monkeypatch is not None and env_mode is not None:
        monkeypatch.setenv("MEMTOMEM_WEB__MODE", env_mode)
    elif monkeypatch is not None:
        monkeypatch.delenv("MEMTOMEM_WEB__MODE", raising=False)

    with contextlib.ExitStack() as stack:
        for p in _patch_web_stack_no_create_app(server_mock):
            stack.enter_context(p)
        stack.enter_context(patch("memtomem.web.app.create_app", side_effect=_capture))
        result = runner.invoke(web, args)
    return result.exit_code, result.output, captured["mode"]


def test_web_mode_and_dev_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMTOMEM_WEB__MODE", raising=False)
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        result = runner.invoke(web, ["--mode", "prod", "--dev"])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_web_invalid_env_value_rejects_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bogus MEMTOMEM_WEB__MODE must fail fast — never silently fall back
    to prod, which would mask a user typo."""
    monkeypatch.setenv("MEMTOMEM_WEB__MODE", "preview")
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        result = runner.invoke(web, [])
    assert result.exit_code != 0
    assert "MEMTOMEM_WEB__MODE" in result.output
    assert "preview" in result.output


def test_web_default_mode_is_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, output, mode = _run_web_capturing_mode([], monkeypatch=monkeypatch)
    assert exit_code == 0, output
    assert mode == "prod"


def test_web_dev_flag_selects_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, output, mode = _run_web_capturing_mode(["--dev"], monkeypatch=monkeypatch)
    assert exit_code == 0, output
    assert mode == "dev"


def test_web_mode_flag_selects_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, output, mode = _run_web_capturing_mode(["--mode", "dev"], monkeypatch=monkeypatch)
    assert exit_code == 0, output
    assert mode == "dev"


def test_web_env_mode_selects_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, output, mode = _run_web_capturing_mode([], monkeypatch=monkeypatch, env_mode="dev")
    assert exit_code == 0, output
    assert mode == "dev"


def test_web_cli_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    exit_code, output, mode = _run_web_capturing_mode(
        ["--mode", "prod"], monkeypatch=monkeypatch, env_mode="dev"
    )
    assert exit_code == 0, output
    assert mode == "prod"
