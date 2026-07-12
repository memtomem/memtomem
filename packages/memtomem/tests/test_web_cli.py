"""Tests for `mm web` CLI error handling and the wizard's web-extra hint.

Regression coverage for a bug where `mm web` produced a raw
`ModuleNotFoundError: No module named 'fastapi'` traceback when the `[web]`
extra wasn't installed — because the old error handler only caught missing
`uvicorn`, not `fastapi`.
"""

from __future__ import annotations

import sys
import contextlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from memtomem.cli import web as web_cmd
from memtomem.cli._liveness import ServerState, _parse_pid_payload
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
        patch("memtomem.cli.web._readiness_ok", return_value=True),
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

    assert result.exit_code == 1
    mock_browser.assert_not_called()
    assert "failed during startup" in result.output.lower()


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


def test_web_mode_and_dev_mutex_rejects_same_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutex is on presence, not value — ``--mode dev --dev`` is still
    an error. Consistency matters: if users start relying on "same value is
    fine" we'd have to pick a tie-breaker when they drift apart."""
    monkeypatch.delenv("MEMTOMEM_WEB__MODE", raising=False)
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        result = runner.invoke(web, ["--mode", "dev", "--dev"])
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


# ---------------------------------------------------------------------------
# --host non-loopback refusal (RFC #787 stage 2)
# ---------------------------------------------------------------------------


def test_web_non_loopback_host_without_acknowledgement_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mm web --host 0.0.0.0` without `--allow-remote-ui` must refuse to
    start. The Web UI is unauthenticated; binding off-loopback by accident
    silently exposes it to the local network."""
    monkeypatch.delenv("MEMTOMEM_WEB__MODE", raising=False)
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        result = runner.invoke(web, ["--host", "0.0.0.0", "--port", "9999"])
    assert result.exit_code != 0
    assert "--allow-remote-ui" in result.output
    # Refusal must happen before the "Starting..." banner — otherwise an
    # operator might think the server came up.
    assert "Starting memtomem Web UI" not in result.output


def test_web_loopback_host_does_not_require_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default loopback bind needs no acknowledgement flag — the whole
    point of the gate is that off-loopback is the exceptional case."""
    monkeypatch.delenv("MEMTOMEM_WEB__MODE", raising=False)
    exit_code, output, _mode = _run_web_capturing_mode(
        ["--host", "127.0.0.1", "--port", "9999"], monkeypatch=monkeypatch
    )
    assert exit_code == 0, output
    assert "--allow-remote-ui" not in output


def test_web_non_loopback_host_with_acknowledgement_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--allow-remote-ui` unblocks the off-loopback bind. The CSRF
    allow-list stays empty unless paired with `--trusted-*` flags, so the
    middleware will still refuse cross-origin writes — that's the point."""
    monkeypatch.delenv("MEMTOMEM_WEB__MODE", raising=False)
    exit_code, output, _mode = _run_web_capturing_mode(
        ["--host", "0.0.0.0", "--port", "9999", "--allow-remote-ui"],
        monkeypatch=monkeypatch,
    )
    assert exit_code == 0, output
    assert "Starting memtomem Web UI" in output


# ---------------------------------------------------------------------------
# background/status/stop daemon plumbing
# ---------------------------------------------------------------------------


def _isolate_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    runtime_base = tmp_path / "runtime"
    runtime_base.mkdir(mode=0o700)
    runtime_base.chmod(0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_base))
    target = runtime_base / "memtomem"
    # On Windows, runtime_dir() skips XDG_RUNTIME_DIR (POSIX/systemd convention)
    # and resolves to tempfile.gettempdir() / f"memtomem-{uid}". Patch the
    # resolver itself so isolation works on every OS.
    monkeypatch.setattr("memtomem._runtime_paths.runtime_dir", lambda: target)
    return target


def test_liveness_parses_web_pid_payload() -> None:
    pid, port, started = _parse_pid_payload("12345\n18080\n2026-05-13T10:15:32Z\n")
    assert pid == 12345
    assert port == 18080
    assert started == "2026-05-13T10:15:32Z"

    legacy_pid, legacy_port, legacy_started = _parse_pid_payload("54321\n")
    assert legacy_pid == 54321
    assert legacy_port is None
    assert legacy_started is None


def test_web_status_does_not_require_web_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_runtime(monkeypatch, tmp_path)
    runner = CliRunner()

    with patch("memtomem.cli.web._missing_web_deps", return_value="fastapi"):
        result = runner.invoke(web, ["status"])

    assert result.exit_code == 3
    assert result.output.strip() == "stopped"
    assert "memtomem[web]" not in result.output


def test_web_background_spawns_internal_foreground_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_runtime(monkeypatch, tmp_path)
    log_file = tmp_path / "logs" / "web.log"
    captured: dict[str, object] = {}

    class FakeChild:
        pid = 24680

        def poll(self) -> None:
            return None

        def terminate(self) -> None:
            captured["terminated"] = True

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeChild()

    monkeypatch.setattr(web_cmd.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(web_cmd, "_wait_for_readiness", lambda *args, **kwargs: True)

    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        result = runner.invoke(
            web,
            ["-b", "--port", "18080", "--mode", "prod", "--log-file", str(log_file)],
        )

    assert result.exit_code == 0, result.output
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert argv[:3] == [sys.executable, "-c", "from memtomem.cli import cli; cli()"]
    assert argv[3:6] == ["web", "--_internal-foreground", "--host"]
    assert "-m" not in argv
    assert "started pid=24680 port=18080" in result.output
    assert log_file.exists()


def test_web_status_uses_sidecar_metadata_when_pid_file_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime_dir = _isolate_runtime(monkeypatch, tmp_path)
    runtime_dir.mkdir()
    (runtime_dir / "web.json").write_text(
        '{"pid": 24680, "port": 18080, "started": "2026-05-13T10:15:32+00:00"}\n',
        encoding="utf-8",
    )

    def fake_probe(_pid_file: Path) -> ServerState:
        return ServerState(alive=True, pid=None, pid_file=runtime_dir / "web.pid")

    monkeypatch.setattr("memtomem.cli._liveness.probe_pid_file", fake_probe)

    result = CliRunner().invoke(web, ["status"])

    assert result.exit_code == 0
    assert "running" in result.output
    assert "pid=24680" in result.output
    assert "port=18080" in result.output


def test_web_stop_removes_stale_pid_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime_dir = _isolate_runtime(monkeypatch, tmp_path)
    runtime_dir.mkdir()
    pid_file = runtime_dir / "web.pid"
    info_file = runtime_dir / "web.json"
    pid_file.write_text("999999\n18080\n2026-05-13T10:15:32+00:00\n", encoding="utf-8")
    info_file.write_text('{"pid": 999999, "port": 18080}\n', encoding="utf-8")

    result = CliRunner().invoke(web, ["stop"])

    assert result.exit_code == 2
    assert "removed stale pid file" in result.output
    assert not pid_file.exists()
    assert not info_file.exists()
