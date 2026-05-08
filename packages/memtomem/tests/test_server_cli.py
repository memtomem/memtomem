"""CLI-facing behavior for ``memtomem-server``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


def _isolate_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> list[tuple[Callable, tuple[object, ...], dict[str, object]]]:
    import memtomem._runtime_paths as runtime_paths

    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    legacy_pid = tmp_path / "home" / ".memtomem" / ".server.pid"
    callbacks: list[tuple[Callable, tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(runtime_paths, "ensure_runtime_dir", lambda: runtime_dir)
    monkeypatch.setattr(runtime_paths, "legacy_server_pid_path", lambda: legacy_pid)
    monkeypatch.setattr(
        "atexit.register",
        lambda fn, *args, **kwargs: callbacks.append((fn, args, kwargs)) or fn,
    )
    return callbacks


def _run_callbacks(callbacks: list[tuple[Callable, tuple[object, ...], dict[str, object]]]) -> None:
    for fn, args, kwargs in reversed(callbacks):
        fn(*args, **kwargs)


def test_stdio_direct_terminal_prints_help_and_exits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from memtomem import server as server_mod

    monkeypatch.setattr(server_mod, "_is_direct_stdio_terminal", lambda: True)
    monkeypatch.setattr(
        server_mod.mcp,
        "run",
        lambda *args, **kwargs: pytest.fail("stdio TTY launch must not run the server"),
    )

    with pytest.raises(SystemExit) as exc:
        server_mod.main(["--transport", "stdio"])

    assert exc.value.code == 2
    out = capsys.readouterr().out
    assert "memtomem-server is an MCP stdio server." in out
    assert "No MCP client is connected; exiting." in out
    assert "mm status" not in out


def test_stdio_pipe_runs_without_terminal_help(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from memtomem import server as server_mod

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_is_direct_stdio_terminal", lambda: False)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    try:
        server_mod.main(["--transport", "stdio"])
    finally:
        _run_callbacks(callbacks)

    assert calls == [((), {})]
    assert "MCP stdio server" not in capsys.readouterr().out


def test_http_transport_alias_runs_streamable_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memtomem import server as server_mod

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    try:
        server_mod.main(
            [
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                "8765",
                "--http-path",
                "/custom-mcp",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.host == "127.0.0.1"
    assert server_mod.mcp.settings.port == 8765
    assert server_mod.mcp.settings.streamable_http_path == "/custom-mcp"
    assert calls == [((), {"transport": "streamable-http"})]


def test_sse_transport_passes_mount_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memtomem import server as server_mod

    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    try:
        server_mod.main(
            [
                "--transport",
                "sse",
                "--host",
                "0.0.0.0",
                "--port",
                "8766",
                "--mount-path",
                "/memtomem",
                "--sse-path",
                "/events",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.host == "0.0.0.0"
    assert server_mod.mcp.settings.port == 8766
    assert server_mod.mcp.settings.sse_path == "/events"
    assert calls == [((), {"transport": "sse", "mount_path": "/memtomem"})]
