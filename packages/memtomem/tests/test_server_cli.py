"""CLI-facing behavior for ``memtomem-server``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _restore_mcp_settings():
    from memtomem import server as server_mod

    settings = server_mod.mcp.settings
    original = {
        "host": settings.host,
        "port": settings.port,
        "sse_path": settings.sse_path,
        "streamable_http_path": settings.streamable_http_path,
        "transport_security": settings.transport_security,
    }

    yield

    for name, value in original.items():
        setattr(settings, name, value)


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
    assert "--url http://127.0.0.1:8000/mcp" in out
    assert (
        "claude mcp add memtomem -s user -- uvx --isolated "
        f"--from 'memtomem[all]=={server_mod._memtomem_version}' memtomem-server"
    ) in out
    assert "-- memtomem-server\n" not in out
    # Direct server launches should point users to MCP setup/network transports.
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
                "--url",
                "https://mcp.example.test/custom-mcp",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.host == "127.0.0.1"
    assert server_mod.mcp.settings.port == 8765
    assert server_mod.mcp.settings.streamable_http_path == "/custom-mcp"
    assert "mcp.example.test" in server_mod.mcp.settings.transport_security.allowed_hosts
    assert "https://mcp.example.test" in server_mod.mcp.settings.transport_security.allowed_origins
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
                "--url",
                "https://mcp.example.test/memtomem/events",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.host == "0.0.0.0"
    assert server_mod.mcp.settings.port == 8766
    assert server_mod.mcp.settings.sse_path == "/events"
    assert calls == [((), {"transport": "sse", "mount_path": "/memtomem"})]


def test_network_url_trailing_slash_is_normalized(
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
                "8767",
                "--url",
                "https://mcp.example.test/mcp/",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.streamable_http_path == "/mcp"
    assert calls == [((), {"transport": "streamable-http"})]


def test_disable_dns_rebinding_protection_skips_allowed_hosts(
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
                "--url",
                "https://mcp.example.test/mcp",
                "--disable-dns-rebinding-protection",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    security = server_mod.mcp.settings.transport_security
    assert security.enable_dns_rebinding_protection is False
    assert security.allowed_hosts == []
    assert security.allowed_origins == []
    assert calls == [((), {"transport": "streamable-http"})]


def test_sse_transport_uses_default_endpoint_when_url_omitted(
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
        server_mod.main(["--transport", "sse", "--host", "127.0.0.1", "--port", "8768"])
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.host == "127.0.0.1"
    assert server_mod.mcp.settings.port == 8768
    assert server_mod.mcp.settings.sse_path == "/sse"
    assert "127.0.0.1" in server_mod.mcp.settings.transport_security.allowed_hosts
    assert "http://127.0.0.1:8768" in server_mod.mcp.settings.transport_security.allowed_origins
    assert calls == [((), {"transport": "sse", "mount_path": None})]


def test_http_transport_uses_default_endpoint_when_url_omitted(
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
        server_mod.main(["--transport", "http", "--host", "127.0.0.1", "--port", "8769"])
    finally:
        _run_callbacks(callbacks)

    assert server_mod.mcp.settings.host == "127.0.0.1"
    assert server_mod.mcp.settings.port == 8769
    assert server_mod.mcp.settings.streamable_http_path == "/mcp"
    assert "127.0.0.1" in server_mod.mcp.settings.transport_security.allowed_hosts
    assert "http://127.0.0.1:8769" in server_mod.mcp.settings.transport_security.allowed_origins
    assert calls == [((), {"transport": "streamable-http"})]


def test_default_network_url_uses_loopback_for_wildcard_host(
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
        server_mod.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8770"])
    finally:
        _run_callbacks(callbacks)

    security = server_mod.mcp.settings.transport_security
    assert server_mod.mcp.settings.host == "0.0.0.0"
    assert server_mod.mcp.settings.port == 8770
    assert server_mod.mcp.settings.streamable_http_path == "/mcp"
    assert "0.0.0.0" not in security.allowed_hosts
    assert "0.0.0.0:*" not in security.allowed_hosts
    assert "http://127.0.0.1:8770" in security.allowed_origins
    assert "http://0.0.0.0:8770" not in security.allowed_origins
    assert calls == [((), {"transport": "streamable-http"})]


def test_network_url_requires_endpoint_path() -> None:
    from memtomem import server as server_mod

    with pytest.raises(SystemExit) as exc:
        server_mod.main(["--transport", "http", "--url", "https://mcp.example.test/"])

    assert "must include an endpoint path" in str(exc.value)


@pytest.mark.parametrize("url", ["file:///tmp/mcp", "127.0.0.1:8000/mcp"])
def test_network_url_requires_full_http_url(url: str) -> None:
    from memtomem import server as server_mod

    with pytest.raises(SystemExit) as exc:
        server_mod.main(["--transport", "http", "--url", url])

    assert "must be a full http(s) URL" in str(exc.value)


def test_network_banner_prints_internal_and_public_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from memtomem import server as server_mod

    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: None)

    try:
        server_mod.main(
            [
                "--transport",
                "http",
                "--host",
                "127.0.0.1",
                "--port",
                "8771",
                "--url",
                "https://mcp.example.test/mcp",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    out = capsys.readouterr().out
    assert "Transport: http (streamable-http)" in out
    assert "Internal URL: http://127.0.0.1:8771/mcp" in out
    assert "Public URL:   https://mcp.example.test/mcp" in out
    # The no-first-party-auth posture must fire at bind time for every network
    # transport, mirroring the ``--help`` epilog (ADR-0029). This is
    # unconditional — not gated on the wildcard-host hint — so a loopback bind
    # with a public ``--url`` still surfaces it.
    assert "no first-party authentication" in out
    # Symmetric with the stdio TTY guard test — the hint should not pollute
    # network-server output.
    assert "memtomem-server is an MCP stdio server." not in out


def test_network_banner_emits_wildcard_host_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from memtomem import server as server_mod

    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: None)

    try:
        server_mod.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8772"])
    finally:
        _run_callbacks(callbacks)

    out = capsys.readouterr().out
    assert "Note: bound on 0.0.0.0 but only loopback Host/Origin headers are" in out
    # Hint must recommend --url only. A bare ``--allowed-host <hostname>``
    # would not match the typical ``Host: <hostname>:<port>`` header — the
    # MCP SDK does exact-match unless the allow-list entry ends in ``:*``
    # (see ``mcp/server/transport_security.py``). Pinning both arms here
    # catches a regression that re-introduces the misleading flag pair.
    assert "Pass --url http://<reachable-host>:<port>/..." in out
    assert "--allowed-host" not in out


def test_network_banner_suppressed_when_url_overrides_wildcard_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mutation-validates the hint gate: --url present means user knows.

    Without the ``args.url is None`` guard in ``_print_network_server_info``,
    the hint would fire whenever ``args.host == "0.0.0.0"`` regardless of
    whether a public ``--url`` was supplied — which would be wrong because
    the URL hostname is in the allow-list. Asserting absence here pins that
    branch.
    """
    from memtomem import server as server_mod

    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: None)

    try:
        server_mod.main(
            [
                "--transport",
                "http",
                "--host",
                "0.0.0.0",
                "--port",
                "8773",
                "--url",
                "https://mcp.example.test/mcp",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    out = capsys.readouterr().out
    assert "bound on 0.0.0.0" not in out


@pytest.mark.parametrize(
    "extra_args",
    [
        pytest.param(["--disable-dns-rebinding-protection"], id="disable-rebind"),
        pytest.param(["--allowed-host", "lan.example.test:*"], id="allowed-host"),
        pytest.param(["--allowed-origin", "http://lan.example.test:9000"], id="allowed-origin"),
    ],
)
def test_network_banner_suppresses_hint_for_advanced_configurations(
    extra_args: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The hint must not fire when the user has signalled an advanced setup.

    ``--disable-dns-rebinding-protection`` skips Host/Origin validation
    entirely, and explicit ``--allowed-host`` / ``--allowed-origin``
    values mean the user has already authorized additional headers — so
    "only loopback Host/Origin headers are accepted" would be false. Each
    parametrised case mutation-validates one arm of the gate.
    """
    from memtomem import server as server_mod

    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: None)

    try:
        server_mod.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8774", *extra_args])
    finally:
        _run_callbacks(callbacks)

    out = capsys.readouterr().out
    assert "bound on 0.0.0.0" not in out
    # The rest of the banner should still print — the suppression only
    # drops the hint block, not the whole network-server info.
    assert "Public URL:" in out


def test_disable_dns_rebinding_protection_pins_empty_allow_lists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disable-rebind branch must pass allow-lists explicitly, not rely on defaults.

    Mutation-validates the pin: this captures the kwargs passed to
    ``TransportSecuritySettings`` rather than the resulting attributes, so
    a regression that drops the explicit ``allowed_hosts=[]`` /
    ``allowed_origins=[]`` would surface here even if the SDK's current
    default happens to be ``[]``.
    """
    import mcp.server.transport_security as ts_mod

    from memtomem import server as server_mod

    callbacks = _isolate_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(server_mod, "_try_hold_legacy_flock", lambda _path: None)
    monkeypatch.setattr(server_mod, "_install_sigterm_handler", lambda *_paths: None)
    monkeypatch.setattr(server_mod.mcp, "run", lambda *args, **kwargs: None)

    captured_kwargs: list[dict[str, object]] = []
    real_cls = ts_mod.TransportSecuritySettings

    def _capture(**kwargs: object) -> object:
        captured_kwargs.append(kwargs)
        return real_cls(**kwargs)

    monkeypatch.setattr(ts_mod, "TransportSecuritySettings", _capture)

    try:
        server_mod.main(
            [
                "--transport",
                "http",
                "--url",
                "https://mcp.example.test/mcp",
                "--disable-dns-rebinding-protection",
            ]
        )
    finally:
        _run_callbacks(callbacks)

    assert len(captured_kwargs) == 1
    kwargs = captured_kwargs[0]
    assert kwargs["enable_dns_rebinding_protection"] is False
    assert kwargs["allowed_hosts"] == []
    assert kwargs["allowed_origins"] == []
