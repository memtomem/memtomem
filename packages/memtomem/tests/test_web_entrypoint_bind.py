"""Bind-safety pins for the direct ``memtomem-web`` entrypoint (RFC #787).

``mm web`` refuses a non-loopback bind without ``--allow-remote-ui``
(``cli/web.py:_validate_bind``). The thin ``memtomem-web`` console script
(``memtomem.web.app:main``) must mirror that gate so the unauthenticated SPA
cannot be exposed off-loopback by accident.
"""

from __future__ import annotations

import pytest

from memtomem.web import app as webapp

# ``main()`` imports uvicorn lazily; skip the whole module if it is absent.
uvicorn = pytest.importorskip("uvicorn")


@pytest.fixture
def _spy_uvicorn(monkeypatch):
    """Replace ``uvicorn.run`` with a spy so ``main()`` never really binds."""
    calls: dict[str, object] = {}

    def _fake_run(target, **kwargs):
        calls["target"] = target
        calls.update(kwargs)
        calls["ran"] = True

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    return calls


def test_main_refuses_off_loopback_host(monkeypatch, _spy_uvicorn) -> None:
    """memtomem-web is loopback-only: a non-loopback --host is refused
    (it cannot configure the trusted-host/origin allow-list)."""
    monkeypatch.setattr("sys.argv", ["memtomem-web", "--host", "0.0.0.0"])
    monkeypatch.delenv("MEMTOMEM_WEB__HOST", raising=False)
    with pytest.raises(SystemExit) as exc:
        webapp.main()
    assert exc.value.code == 2
    assert "ran" not in _spy_uvicorn, "must not bind when refusing"


def test_main_refuses_off_loopback_via_env(monkeypatch, _spy_uvicorn) -> None:
    """A non-loopback bind smuggled in via MEMTOMEM_WEB__HOST is refused too."""
    monkeypatch.setattr("sys.argv", ["memtomem-web"])
    monkeypatch.setenv("MEMTOMEM_WEB__HOST", "0.0.0.0")
    with pytest.raises(SystemExit) as exc:
        webapp.main()
    assert exc.value.code == 2
    assert "ran" not in _spy_uvicorn


def test_main_loopback_default_binds(monkeypatch, _spy_uvicorn) -> None:
    monkeypatch.setattr("sys.argv", ["memtomem-web"])
    monkeypatch.delenv("MEMTOMEM_WEB__HOST", raising=False)
    webapp.main()
    assert _spy_uvicorn["host"] == "127.0.0.1"
    assert _spy_uvicorn["ran"] is True


def test_main_explicit_loopback_host_binds(monkeypatch, _spy_uvicorn) -> None:
    monkeypatch.setattr("sys.argv", ["memtomem-web", "--host", "::1"])
    monkeypatch.delenv("MEMTOMEM_WEB__HOST", raising=False)
    webapp.main()
    assert _spy_uvicorn["host"] == "::1"
    assert _spy_uvicorn["ran"] is True
