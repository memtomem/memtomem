"""``GET /api/settings-sync`` reports the settings files no check could read (#2644).

The CLI (``mm context settings-doctor``) and the MCP context branches report a
settings file that exists but could not be read through
``find_unscanned_settings_files``. The Web route returned only
``duplicate_tier_warnings``, so a tier the duplicate check skipped — invalid
JSON, not an object, or (since #2476) a parent directory without search
permission — left that surface without a trace.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context import error_redact
from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.web.app import create_app

from .helpers import set_home


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_canonical_hook(project_root: Path) -> None:
    hooks = {
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": "mm session start", "timeout": 5000}],
            }
        ]
    }
    _write(project_root / CANONICAL_SETTINGS_FILE, json.dumps({"hooks": hooks}))


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude").mkdir()
    return root


@pytest.fixture
def app(project_root: Path, fake_home: Path):
    from memtomem.config import Mem2MemConfig

    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = project_root
    application.state.storage = AsyncMock()
    application.state.config = Mem2MemConfig()
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    application.state.last_reload_error = None
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _strings(value: object) -> list[str]:
    """Every decoded string in a JSON value. Checked instead of the raw body,
    where JSON escapes a Windows path's backslashes."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _rows_by_tier(data: dict) -> dict[str | None, dict]:
    rows = data["unscanned_settings"]
    by_tier = {row["tier"]: row for row in rows}
    assert len(by_tier) == len(rows), rows
    return by_tier


async def test_get_reports_unreadable_tiers_path_free(
    client, project_root: Path, fake_home: Path, tmp_path: Path, monkeypatch
) -> None:
    # The import-frozen collapse anchor, pointed at the isolated home.
    monkeypatch.setattr(error_redact, "_HOME", str(fake_home))
    _write_canonical_hook(project_root)
    _write(project_root / ".claude" / "settings.local.json", "{not json")
    _write(fake_home / ".claude" / "settings.json", "[1, 2]")

    response = await client.get("/api/settings-sync?target_scope=project_shared")

    assert response.status_code == 200, response.text
    data = response.json()
    rows = _rows_by_tier(data)
    assert set(rows) == {"project_local", "user"}, rows
    assert rows["project_local"] == {
        "source": "tier",
        "tier": "project_local",
        "path": os.path.join(".claude", "settings.local.json"),
        "reason": "invalid JSON",
    }
    assert rows["user"] == {
        "source": "tier",
        "tier": "user",
        "path": os.path.join("~", ".claude", "settings.json"),
        "reason": "not a JSON object",
    }
    assert not [s for s in _strings(data) if str(tmp_path) in s], data
    assert data["duplicate_tier_warnings"] == []


async def test_get_reports_an_unreadable_canonical(client, project_root: Path) -> None:
    _write(project_root / CANONICAL_SETTINGS_FILE, "{not json")

    response = await client.get("/api/settings-sync?target_scope=project_shared")

    assert response.status_code == 200, response.text
    rows = _rows_by_tier(response.json())
    assert rows == {
        None: {
            "source": "canonical",
            "tier": None,
            "path": str(Path(CANONICAL_SETTINGS_FILE)),
            "reason": "invalid JSON",
        }
    }


async def test_get_reports_nothing_when_every_file_reads(
    client, project_root: Path, fake_home: Path
) -> None:
    _write_canonical_hook(project_root)
    _write(fake_home / ".claude" / "settings.json", json.dumps({"hooks": {}}))

    response = await client.get("/api/settings-sync?target_scope=project_shared")

    assert response.status_code == 200, response.text
    assert response.json()["unscanned_settings"] == []


@pytest.mark.parametrize(
    ("home_rel", "frozen_rel"),
    [
        pytest.param("home-elsewhere", "frozen-home", id="unrelated-homes"),
        # A quote ends the residual path regex's segment mid-name.
        pytest.param("O'Brien", "frozen-home", id="quote-in-home"),
        # The frozen home is a string prefix of the live one.
        pytest.param("alice", "al", id="frozen-home-is-a-prefix"),
        # The live home is the project root's name plus more.
        pytest.param("project'private", "frozen-home", id="project-root-is-a-prefix"),
        # The live home sits under the project root, and the frozen home is it.
        pytest.param("project/alice", "project/alice", id="home-under-the-project-root"),
    ],
)
async def test_a_row_names_its_file_by_tier_and_no_host_path(
    client,
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
    home_rel: str,
    frozen_rel: str,
) -> None:
    """Each case made an earlier version, which trimmed the file's real path,
    keep part of the host path (``~ice/…``, ``<path>'private/…``,
    ``alice/…``). The row's ``path`` is the tier's fixed location instead."""
    live_home = tmp_path / home_rel
    live_home.mkdir(parents=True)
    set_home(monkeypatch, live_home)
    monkeypatch.setattr(error_redact, "_HOME", str(tmp_path / frozen_rel))
    _write(live_home / ".claude" / "settings.json", "{not json")
    _write(project_root / ".claude" / "settings.local.json", "{not json")

    response = await client.get("/api/settings-sync?target_scope=project_shared")

    assert response.status_code == 200, response.text
    data = response.json()
    rows = _rows_by_tier(data)
    assert rows["user"]["path"] == os.path.join("~", ".claude", "settings.json"), rows
    assert rows["project_local"]["path"] == os.path.join(".claude", "settings.local.json")
    home_name = Path(home_rel).name
    texts = [s for row in data["unscanned_settings"] for s in _strings(row)]
    assert not [s for s in texts if str(tmp_path) in s or home_name in s], texts


def test_display_paths_match_resolve_scope_path(tmp_path: Path, monkeypatch) -> None:
    """The table names each tier's file where ``resolve_scope_path`` puts it,
    so a tier that moves cannot keep its old name in the panel."""
    from memtomem.context._host_homes import host_home
    from memtomem.context.settings import resolve_scope_path
    from memtomem.context.settings_doctor import ALL_SCOPES
    from memtomem.web.routes import settings_sync

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    root = tmp_path / "project"
    assert set(settings_sync._TIER_DISPLAY_PATHS) == set(ALL_SCOPES)
    for scope in ALL_SCOPES:
        real = resolve_scope_path(root, scope)
        if scope == "user":
            expected = os.path.join("~", real.relative_to(host_home()))
        else:
            expected = str(real.relative_to(root))
        assert settings_sync._TIER_DISPLAY_PATHS[scope] == expected, scope
    assert settings_sync._CANONICAL_DISPLAY_PATH == str(
        (root / CANONICAL_SETTINGS_FILE).relative_to(root)
    )


async def test_a_deeply_nested_tier_is_reported_not_a_500(
    client, project_root: Path, fake_home: Path
) -> None:
    """With no canonical hooks the duplicate check returns before reading any
    tier, but the unscanned report reads every one: nesting deep enough to
    raise ``RecursionError`` in ``json.loads`` must be a row, not a 500."""
    depth = 100_000
    _write(project_root / ".claude" / "settings.local.json", "[" * depth + "]" * depth)

    response = await client.get("/api/settings-sync?target_scope=project_shared")

    assert response.status_code == 200, response.text
    rows = _rows_by_tier(response.json())
    assert rows["project_local"]["reason"] == "invalid JSON", rows


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("[" * 100_000 + "]" * 100_000, id="recursion-limit"),
        pytest.param('{"n": ' + "9" * 5_000 + "}", id="integer-digit-limit"),
        pytest.param("{not json", id="syntax"),
    ],
)
def test_read_settings_reports_a_decoder_failure_as_invalid_json(tmp_path: Path, text: str) -> None:
    """``json.loads`` raises ``RecursionError`` for deep nesting and a plain
    ``ValueError`` past the integer digit limit, neither a ``JSONDecodeError``;
    the CLI, MCP and Web readers of the tiers all go through this one."""
    from memtomem.context.settings_doctor import _read_settings

    path = tmp_path / "settings.json"
    _write(path, text)
    # The digit-limit case needs a limit; PYTHONINTMAXSTRDIGITS=0 removes it.
    saved = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        assert _read_settings(path) == (None, "invalid JSON")
    finally:
        sys.set_int_max_str_digits(saved)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX search permission")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores search permission"
)
async def test_a_tier_under_an_unsearchable_directory_is_reported(
    client, project_root: Path, fake_home: Path
) -> None:
    """#2476: such a tier used to raise; since then it is skipped, and the
    skip must be reported rather than silent. The selected target is the user
    tier, which stays readable (an unreadable *target* still raises, by design)."""
    _write_canonical_hook(project_root)
    claude_dir = project_root / ".claude"
    _write(claude_dir / "settings.json", json.dumps({"hooks": {}}))
    claude_dir.chmod(0)
    try:
        response = await client.get("/api/settings-sync?target_scope=user")
    finally:
        claude_dir.chmod(0o755)

    assert response.status_code == 200, response.text
    rows = _rows_by_tier(response.json())
    assert rows["project_shared"]["reason"] == "unreadable", rows
    assert rows["project_local"]["reason"] == "unreadable", rows
