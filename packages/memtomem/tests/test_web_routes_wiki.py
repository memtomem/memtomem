"""HTTP-layer tests for the read-only wiki browser routes (ADR-0008 PR-E).

Covers ``GET /api/wiki`` (list + per-vendor renderability), ``.../diff`` and
``.../lint`` across their states, and the error envelopes — most importantly
that an absent wiki is a structured 404 (not a 500), a codex-commands diff is a
400 ``vendor_unsupported`` (NotImplementedError caught, never a 500), and an
unknown ``asset_type`` is a 422 (Literal path param guards the path join).

``wiki_root`` / ``git_identity`` come from ``_wiki_fixtures`` via conftest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app
from memtomem.wiki.store import WikiStore


# ── seed helpers ──────────────────────────────────────────────────────────


def _git_commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True
    )


def _seed(root: Path) -> None:
    """Init a wiki and seed one skill (with two overrides), one agent, one command."""
    WikiStore.at_default().init()
    skill = root / "skills" / "alpha"
    (skill / "overrides").mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"# Alpha\n")
    (skill / "overrides" / "claude.md").write_bytes(b"# Alpha MODIFIED\n")  # out of sync
    (skill / "overrides" / "gemini.md").write_bytes(b"# Alpha\n")  # in sync
    agent = root / "agents" / "beta"
    agent.mkdir(parents=True)
    (agent / "agent.md").write_text(
        "---\nname: beta\ndescription: a test agent\n---\n\nBody.\n", encoding="utf-8"
    )
    cmd = root / "commands" / "gamma"
    cmd.mkdir(parents=True)
    (cmd / "command.md").write_text(
        "---\ndescription: a test command\n---\n\nBody.\n", encoding="utf-8"
    )
    _git_commit(root, "seed")


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_wiki(wiki_root: Path) -> Path:  # noqa: F811 — wiki_root from conftest
    _seed(wiki_root)
    return wiki_root


@pytest.fixture
async def client():
    # Prod tier: the wiki browser is read-only and must be available without
    # dev mode. The handlers read MEMTOMEM_WIKI_PATH at request time and use no
    # app.state, so a bare app suffices.
    app = create_app(lifespan=None, mode="prod")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── GET /api/wiki ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_wiki_populated(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["wiki_head"]) == 40  # full SHA
    assert data["is_dirty"] is False
    assert data["wiki_root"]  # POSIX path string
    by_name = {(i["type"], i["name"]): i for i in data["items"]}
    assert set(by_name) == {("skills", "alpha"), ("agents", "beta"), ("commands", "gamma")}
    # commands expose claude/gemini/codex, with codex NOT renderable (placeholder).
    cmd_vendors = {v["vendor"]: v["renderable"] for v in by_name[("commands", "gamma")]["vendors"]}
    assert cmd_vendors == {"claude": True, "gemini": True, "codex": False}
    # skills expose the full four, all renderable.
    skill_vendors = {v["vendor"]: v["renderable"] for v in by_name[("skills", "alpha")]["vendors"]}
    assert skill_vendors == {"claude": True, "gemini": True, "codex": True, "kimi": True}


@pytest.mark.asyncio
async def test_list_wiki_empty(wiki_root: Path) -> None:  # noqa: F811
    WikiStore.at_default().init()
    app = create_app(lifespan=None, mode="prod")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/api/wiki")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


@pytest.mark.asyncio
async def test_list_wiki_absent_is_404_not_500(client, wiki_root: Path) -> None:  # noqa: F811
    # wiki_root sets MEMTOMEM_WIKI_PATH but we never init → no wiki on disk.
    resp = await client.get("/api/wiki")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "wiki_absent"
    # The absolute wiki path must not leak into the message (Codex review on E-2).
    assert str(wiki_root) not in resp.text


# ── GET /api/wiki/{type}/{name}/diff ──────────────────────────────────────


@pytest.mark.asyncio
async def test_diff_out_of_sync(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki/skills/alpha/diff", params={"vendor": "claude"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["in_sync"] is False
    assert data["diff_lines"]  # non-empty unified diff


@pytest.mark.asyncio
async def test_diff_in_sync(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki/skills/alpha/diff", params={"vendor": "gemini"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["in_sync"] is True
    assert data["diff_lines"] == []


@pytest.mark.asyncio
async def test_diff_no_override(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki/agents/beta/diff", params={"vendor": "claude"})
    assert resp.status_code == 200
    assert resp.json()["exists"] is False


@pytest.mark.asyncio
async def test_diff_bad_vendor_is_400(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki/skills/alpha/diff", params={"vendor": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "unknown_vendor"


@pytest.mark.asyncio
async def test_diff_unknown_asset_type_is_422(client, seeded_wiki: Path) -> None:
    # Literal path param → FastAPI validation 422 before the handler runs.
    resp = await client.get("/api/wiki/widgets/alpha/diff", params={"vendor": "claude"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_diff_bad_name_is_400(client, seeded_wiki: Path) -> None:
    # Leading dash fails validate_name (URL-safe, unlike a path separator).
    resp = await client.get("/api/wiki/skills/-bad/diff", params={"vendor": "claude"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "invalid_name"


@pytest.mark.asyncio
async def test_diff_codex_command_is_400_not_500(client, seeded_wiki: Path) -> None:
    # ("commands", "codex") has no generator → render_seed_bytes raises
    # NotImplementedError; the route must classify it, never leak a 500.
    resp = await client.get("/api/wiki/commands/gamma/diff", params={"vendor": "codex"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "vendor_unsupported"


@pytest.mark.asyncio
async def test_diff_missing_asset_is_404(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki/skills/nope/diff", params={"vendor": "claude"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "asset_absent"


# ── GET /api/wiki/{type}/{name}/lint ──────────────────────────────────────


@pytest.mark.asyncio
async def test_lint_ok(client, seeded_wiki: Path) -> None:
    resp = await client.get("/api/wiki/skills/alpha/lint")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["asset_type"] == "skills"
    assert data["name"] == "alpha"


@pytest.mark.asyncio
async def test_lint_flags_stray_override(client, wiki_root: Path) -> None:  # noqa: F811
    WikiStore.at_default().init()
    skill = wiki_root / "skills" / "delta"
    (skill / "overrides").mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"# Delta\n")
    # ``.txt`` is not a registered <vendor>.<ext> → stray → lint error.
    (skill / "overrides" / "claude.txt").write_text("oops", encoding="utf-8")
    _git_commit(wiki_root, "seed delta")
    resp = await client.get("/api/wiki/skills/delta/lint")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert any(f["level"] == "error" for f in data["findings"])
