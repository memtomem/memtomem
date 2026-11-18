"""Gate A on the settings fan-out and the Web conflict resolve (#3343).

The canonical ``.memtomem/settings.json`` is tracked by git whatever
``hooks.target_scope`` is, so every write derived from it scans it the way
the skills / commands / agents and MCP server fan-outs scan theirs
(ADR-0011 §5): ``project_shared`` scope, no force valve.

The fan-out scans the canonical's text, the buffer it parses, so what parsing
drops (a shadowed duplicate key) is still seen. Resolve scans the fragment it
writes, as promote does.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem import privacy
from memtomem.context import settings as settings_mod
from memtomem.context._atomic import _lock_path_for
from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.settings import CANONICAL_SETTINGS_FILE, generate_all_settings
from memtomem.web.app import create_app

from .helpers import set_home

# A real credential shape: a placeholder would pass the scanner.
SECRET = "api_key=AKIA1234567890ABCDEF"
SECRET_BYTES = "AKIA1234567890ABCDEF"
PEM = "BEGIN\nRSA\nPRIVATE\nKEY"


def _rule(matcher: str = "", command: str = "echo ok") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def _write_canonical(project: Path, content: dict | str) -> Path:
    path = project / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    text = content if isinstance(content, str) else json.dumps(content, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _blocked(surface: str) -> int:
    return privacy.snapshot()["by_tool"].get(surface, {}).get("blocked", 0)


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    (h / ".claude").mkdir(parents=True)
    set_home(monkeypatch, h)
    return h


@pytest.fixture
def project(tmp_path, home) -> Path:
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
    (p / ".git").mkdir()
    return p


def _claude_target(project: Path, home: Path, scope: str) -> Path:
    return {
        "user": home / ".claude" / "settings.json",
        "project_shared": project / ".claude" / "settings.json",
        "project_local": project / ".claude" / "settings.local.json",
    }[scope]


# ── Engine: preflight refusal ──────────────────────────────────────


class TestFanoutPreflight:
    @pytest.mark.parametrize("scope", ["user", "project_shared", "project_local"])
    def test_secret_in_a_rule_refuses_before_any_write(self, project, home, scope):
        """Every target tier is refused: the source is the tracked canonical."""
        _write_canonical(project, {"hooks": {"PreToolUse": [_rule("Bash", SECRET)]}})
        target = _claude_target(project, home, scope)
        before = _blocked("cli_context_sync")

        with pytest.raises(PrivacyBlockedError) as excinfo:
            generate_all_settings(project, scope=scope, allow_host_writes=True)

        assert not target.exists()
        # Refused before the lock: not even the sidecar was created.
        assert not _lock_path_for(target).exists()
        message = excinfo.value.message
        assert "Gate A" in message
        assert SECRET_BYTES not in message
        assert str(project) not in message and str(home) not in message
        assert excinfo.value.scope == "project_shared"
        assert _blocked("cli_context_sync") == before + 1

    def test_surface_names_the_caller(self, project, home):
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})
        before = _blocked("some_surface")
        with pytest.raises(PrivacyBlockedError):
            generate_all_settings(project, scope="project_shared", surface="some_surface")
        assert _blocked("some_surface") == before + 1

    @pytest.mark.parametrize(
        "text",
        [
            pytest.param(json.dumps({"hooks": {SECRET: [_rule("", "echo ok")]}}), id="event-key"),
            pytest.param(json.dumps({"hooks": {"Stop": [_rule(SECRET, "echo ok")]}}), id="matcher"),
            # The shadowed first value is gone after json.loads; only the text has it.
            pytest.param(
                '{"hooks":{"Stop":[{"matcher":"","hooks":[{"type":"command","command":'
                + json.dumps(PEM)
                + ',"command":"echo ok"}]}]}}',
                id="duplicate-key",
            ),
            pytest.param(
                json.dumps({"hooks": {"Stop": [_rule("", SECRET)]}}).replace("A", "\\u0041"),
                id="unicode-escaped",
            ),
            # A canonical-file policy: a key no generator writes still refuses.
            pytest.param(
                json.dumps({"env": {"TOKEN": SECRET}, "hooks": {"Stop": [_rule()]}}),
                id="non-hooks-key",
            ),
        ],
    )
    def test_secret_anywhere_in_the_canonical_text_refuses(self, project, text):
        _write_canonical(project, text)
        json.loads(text)  # sanity: valid JSON, so the refusal is Gate A's
        with pytest.raises(PrivacyBlockedError):
            generate_all_settings(project, scope="project_shared")
        assert not (project / ".claude" / "settings.json").exists()

    def test_no_target_reached_means_no_refusal(self, project, home):
        """Gate A runs only when a target would take a lock: an unconfirmed host
        write stays ``needs_confirmation`` rather than raising."""
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "needs_confirmation"
        assert not (home / ".claude" / "settings.json").exists()

    def test_abandoned_sync_does_not_scan(self, project, monkeypatch):
        """Abandonment is checked first, as before any filesystem touch."""
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})
        monkeypatch.setattr(settings_mod, "_sync_is_abandoned", lambda: True)
        results = generate_all_settings(project, scope="project_shared")
        assert {r.status for r in results.values()} == {"aborted"}

    def test_clean_canonical_is_written(self, project):
        _write_canonical(project, {"hooks": {"Stop": [_rule("", "echo clean")]}})
        results = generate_all_settings(project, scope="project_shared")
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].reason_code is None
        written = json.loads((project / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert written["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo clean"


# ── Engine: canonical changes mid-sync ─────────────────────────────


def _poison_at_lock_of(monkeypatch, canonical: Path, trigger: Path) -> None:
    """Rewrite the canonical with a secret when *trigger*'s sidecar is acquired —
    after the preflight passed, before that target's under-lock reread."""
    real_lock = settings_mod._file_lock

    @contextlib.contextmanager
    def poisoning_lock(lock_path, *, timeout=None):
        if lock_path == _lock_path_for(trigger):
            canonical.write_text(
                json.dumps({"hooks": {"Stop": [_rule("", SECRET)]}}), encoding="utf-8"
            )
        with real_lock(lock_path, timeout=timeout):
            yield

    monkeypatch.setattr(settings_mod, "_file_lock", poisoning_lock)


class TestFanoutUnderLock:
    def test_hit_refuses_this_and_every_later_target_and_keeps_earlier_ones(
        self, project, monkeypatch
    ):
        for runtime_dir in (".codex", ".gemini", ".kimi"):
            (project / runtime_dir).mkdir()
        canonical = _write_canonical(project, {"hooks": {"Stop": [_rule("", "echo clean")]}})
        claude = project / ".claude" / "settings.json"
        codex = project / ".codex" / "hooks.json"
        later = [project / ".gemini" / "settings.json", project / ".kimi" / "config.toml"]
        _poison_at_lock_of(monkeypatch, canonical, codex)

        results = generate_all_settings(project, scope="project_shared")

        assert results["claude_settings"].status == "ok"
        assert "echo clean" in claude.read_text(encoding="utf-8")
        for name in ("codex_settings", "gemini_settings", "kimi_settings"):
            row = results[name]
            assert row.status == "error", (name, row)
            assert row.reason_code == "privacy_blocked"
            assert "Gate A" in row.reason
            assert SECRET_BYTES not in row.reason
        assert not codex.exists()
        for path in later:
            assert not path.exists()
            # Refused without taking its lock.
            assert not _lock_path_for(path).exists()


# ── CLI ────────────────────────────────────────────────────────────


class TestCli:
    @pytest.mark.parametrize("command", ["generate", "sync"])
    def test_refusal_is_a_click_error(self, project, monkeypatch, command):
        from memtomem.cli.context_cmd import context

        monkeypatch.chdir(project)
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})
        before = _blocked("cli_context_settings_sync")

        result = CliRunner().invoke(
            context, [command, "--include=settings", "--scope=project_shared", "--yes"]
        )

        assert result.exit_code == 1, result.output
        assert _blocked("cli_context_settings_sync") == before + 1
        assert "Gate A" in result.output
        assert SECRET_BYTES not in result.output
        assert not (project / ".claude" / "settings.json").exists()


# ── Web ────────────────────────────────────────────────────────────


@pytest.fixture
def app(project, tmp_path):
    from memtomem.config import ContextGatewayConfig, Mem2MemConfig

    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = project
    application.state.storage = AsyncMock()
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=tmp_path / "kp.json",
        experimental_claude_projects_scan=False,
    )
    application.state.config = config
    for attr in (
        "search_pipeline",
        "index_engine",
        "embedder",
        "dedup_scanner",
        "last_reload_error",
    ):
        setattr(application.state, attr, None)
    return application


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _assert_privacy_422(resp, *roots: Path) -> None:
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["reason_code"] == "privacy_blocked"
    assert isinstance(body["detail"], str)
    assert SECRET_BYTES not in resp.text
    for root in roots:
        assert str(root) not in resp.text


class TestWebSync:
    @pytest.mark.parametrize("url", ["/api/settings-sync", "/api/context/settings/sync"])
    async def test_standalone_sync_returns_a_coded_422(self, client, project, home, url):
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})
        before = _blocked("web_context_settings_sync")

        resp = await client.post(f"{url}?target_scope=project_shared")

        _assert_privacy_422(resp, project, home)
        assert not (project / ".claude" / "settings.json").exists()
        assert _blocked("web_context_settings_sync") == before + 1

    async def test_result_rows_carry_reason_code(self, client, project, monkeypatch):
        (project / ".codex").mkdir()
        canonical = _write_canonical(project, {"hooks": {"Stop": [_rule("", "echo clean")]}})
        _poison_at_lock_of(monkeypatch, canonical, project / ".codex" / "hooks.json")

        resp = await client.post("/api/context/settings/sync?target_scope=project_shared")

        assert resp.status_code == 200, resp.text
        rows = {r["name"]: r for r in resp.json()["results"]}
        assert rows["claude_settings"]["status"] == "ok"
        assert rows["claude_settings"]["reason_code"] is None
        assert rows["codex_settings"]["status"] == "error"
        assert rows["codex_settings"]["reason_code"] == "privacy_blocked"
        assert SECRET_BYTES not in resp.text

    async def test_sync_all_reports_a_422_phase_not_a_500(self, client, project):
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})

        resp = await client.post("/api/context/sync-all")

        assert resp.status_code == 200, resp.text
        phase = next(ph for ph in resp.json()["phases"] if ph["type"] == "settings")
        assert phase["status"] == "failed"
        assert phase["error"]["http_status"] == 422
        assert phase["error"]["reason_code"] == "privacy_blocked"
        assert phase["error"]["error_kind"] == "validation"
        assert SECRET_BYTES not in resp.text
        assert not (project / ".claude" / "settings.json").exists()

    async def test_sync_all_projects_reports_a_422_phase(self, client, project):
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})

        resp = await client.post("/api/context/sync-all-projects")

        assert resp.status_code == 200, resp.text
        entry = next(e for e in resp.json()["projects"] if e["root"] == str(project))
        phase = next(ph for ph in entry["phases"] if ph["type"] == "settings")
        assert phase["status"] == "failed"
        assert phase["error"]["http_status"] == 422
        assert phase["error"]["reason_code"] == "privacy_blocked"
        assert SECRET_BYTES not in resp.text


class TestWebResolve:
    @staticmethod
    def _seed(project: Path, home: Path, canonical_rules: dict) -> Path:
        _write_canonical(project, {"hooks": canonical_rules})
        target = home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"Stop": [_rule("", "echo user")]}}, indent=2) + "\n",
            encoding="utf-8",
        )
        return target

    async def _resolve(self, client, event: str = "Stop", **extra):
        return await client.post(
            "/api/context/settings/resolve?target_scope=user",
            json={"event": event, "matcher": "", "action": "use_proposed", **extra},
        )

    async def test_secret_in_the_proposed_rule_is_refused(self, client, project, home):
        target = self._seed(project, home, {"Stop": [_rule("", SECRET)]})
        before_bytes = target.read_bytes()
        before = _blocked("web_settings_rule_resolve")

        resp = await self._resolve(client)

        _assert_privacy_422(resp, project, home)
        assert target.read_bytes() == before_bytes
        assert _blocked("web_settings_rule_resolve") == before + 1

    async def test_secret_shaped_event_key_is_refused(self, client, project, home):
        _write_canonical(project, {"hooks": {SECRET: [_rule("", "echo canonical")]}})
        target = home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {SECRET: [_rule("", "echo user")]}}), encoding="utf-8"
        )
        before_bytes = target.read_bytes()

        resp = await self._resolve(client, event=SECRET)

        _assert_privacy_422(resp, project, home)
        assert target.read_bytes() == before_bytes

    async def test_escaped_secret_is_refused(self, client, project, home):
        text = json.dumps({"hooks": {"Stop": [_rule("", SECRET)]}}).replace("A", "\\u0041")
        _write_canonical(project, text)
        target = home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {"Stop": [_rule("", "echo user")]}}))
        resp = await self._resolve(client)
        _assert_privacy_422(resp, project, home)

    async def test_refusal_comes_before_a_missing_target(self, client, project, home):
        _write_canonical(project, {"hooks": {"Stop": [_rule("", SECRET)]}})
        assert not (home / ".claude" / "settings.json").exists()
        resp = await self._resolve(client)
        _assert_privacy_422(resp, project, home)

    async def test_refusal_comes_before_a_stale_rule(self, client, project, home):
        self._seed(project, home, {"Stop": [_rule("", SECRET)]})
        stamped = settings_mod._stamp_status_markers({"hooks": {"Stop": [_rule("", SECRET)]}})
        from memtomem.web.routes.settings_sync import _rule_hash

        resp = await self._resolve(
            client,
            rule_index=0,
            rule_hash="0" * 64,  # matches no target row: would be a 409
            proposed_hash=_rule_hash(stamped["hooks"]["Stop"][0]),
        )
        _assert_privacy_422(resp, project, home)

    async def test_clean_rule_resolves_beside_an_unsafe_one(self, client, project, home):
        """Resolve scans what it writes, not the whole canonical."""
        target = self._seed(
            project,
            home,
            {"Stop": [_rule("", "echo canonical")], "PreToolUse": [_rule("Bash", SECRET)]},
        )

        resp = await self._resolve(client)

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        written = target.read_text(encoding="utf-8")
        assert "echo canonical" in written
        assert SECRET_BYTES not in written
