"""HTTP-layer tests for the dev-tier wiki override-seed route (ADR-0008 PR-E E-2).

Companion to ``test_web_routes_wiki.py`` (the read-only browser). Covers
``POST /api/wiki/{type}/{name}/override``: a fresh seed, the collision /
``force`` overwrite (+ ``.bak``) path, the error envelopes shared with the
read-only routes (wiki absent → 404, codex-commands → 400 ``vendor_unsupported``,
missing canonical → 404, bad vendor/name → 400, unknown type → 422), and — most
importantly — that the route is **absent in the prod tier** (the mutation only
mounts under ``mode="dev"``).

``wiki_root`` / ``git_identity`` come from ``_wiki_fixtures`` via conftest. The
test app leaves ``app.state.csrf_enforce`` unset, so the CSRF middleware is
observe-only and a plain POST reaches the handler (matching
``test_web_routes_context_mutators``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app
from memtomem.wiki.store import WikiStore

_SKILL_BODY = "# Alpha\n\nBody.\n"


# ── seed helpers ──────────────────────────────────────────────────────────


def _git_commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True
    )


def _seed(root: Path) -> None:
    """Init a wiki with canonical-only skill / agent / command (no overrides yet)."""
    WikiStore.at_default().init()
    skill = root / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_SKILL_BODY, encoding="utf-8")
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
async def dev_client():
    app = create_app(lifespan=None, mode="dev")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def prod_client():
    app = create_app(lifespan=None, mode="prod")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_creates_override_fresh(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["seeded"] is True
    assert data["vendor"] == "claude"
    assert data["forced"] is False
    assert data["dropped"] == []  # skills are byte-copies — never drop fields
    assert data["wiki_dirty"] is True  # new uncommitted file
    assert data["override_path"].endswith("skills/alpha/overrides/claude.md")
    # Seeded bytes are the canonical SKILL.md verbatim (skill parity).
    seeded = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    assert seeded.read_text(encoding="utf-8") == _SKILL_BODY


@pytest.mark.asyncio
async def test_seed_agent_returns_dropped_list(dev_client, seeded_wiki: Path) -> None:
    # Agents render through the vendor generator; ``dropped`` is always a list
    # (possibly empty) and must round-trip to the client unchanged.
    resp = await dev_client.post("/api/wiki/agents/beta/override", json={"vendor": "gemini"})
    assert resp.status_code == 200
    assert isinstance(resp.json()["dropped"], list)


# ── collision / force ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_existing_without_force_is_409(dev_client, seeded_wiki: Path) -> None:
    first = await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    assert first.status_code == 200
    again = await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    assert again.status_code == 409
    assert again.json()["detail"]["reason_code"] == "override_exists"


@pytest.mark.asyncio
async def test_reseed_with_force_writes_bak(dev_client, seeded_wiki: Path) -> None:
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    assert (
        await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    ).status_code == 200
    # User edits the override, then force re-seeds: canonical is restored and the
    # edit is preserved in the .bak sibling.
    target.write_text("# Alpha EDITED\n", encoding="utf-8")
    resp = await dev_client.post(
        "/api/wiki/skills/alpha/override", json={"vendor": "claude", "force": True}
    )
    assert resp.status_code == 200
    assert resp.json()["forced"] is True
    assert target.read_text(encoding="utf-8") == _SKILL_BODY
    bak = target.with_suffix(".md.bak")
    assert bak.read_text(encoding="utf-8") == "# Alpha EDITED\n"


# ── error envelopes ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_codex_command_is_400_not_500(dev_client, seeded_wiki: Path) -> None:
    # ("commands", "codex") has no generator → NotImplementedError → 400, not 500.
    resp = await dev_client.post("/api/wiki/commands/gamma/override", json={"vendor": "codex"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "vendor_unsupported"
    # And nothing was written to disk for the failed seed.
    assert not (seeded_wiki / "commands" / "gamma" / "overrides").exists()


@pytest.mark.asyncio
async def test_seed_missing_canonical_is_404(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.post("/api/wiki/skills/nope/override", json={"vendor": "claude"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "canonical_absent"


@pytest.mark.asyncio
async def test_seed_bad_vendor_is_400(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "bogus"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "unknown_vendor"


@pytest.mark.asyncio
async def test_seed_unknown_asset_type_is_422(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.post("/api/wiki/widgets/alpha/override", json={"vendor": "claude"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_seed_bad_name_is_400(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.post("/api/wiki/skills/-bad/override", json={"vendor": "claude"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "invalid_name"


@pytest.mark.asyncio
async def test_seed_wiki_absent_is_404(dev_client, wiki_root: Path) -> None:  # noqa: F811
    # wiki_root sets MEMTOMEM_WIKI_PATH but we never init → no wiki on disk.
    resp = await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "wiki_absent"
    # The absolute wiki path must not leak into the envelope (Codex review).
    assert str(wiki_root) not in resp.text


# ── tier gating ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_override_route_absent_in_prod(prod_client, seeded_wiki: Path) -> None:
    # The mutation only mounts in dev; in prod the path does not exist at all.
    resp = await prod_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    assert resp.status_code == 404
    # The read-only browser is still mounted in prod (sanity check).
    assert (await prod_client.get("/api/wiki")).status_code == 200
