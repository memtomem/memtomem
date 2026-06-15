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

import logging
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
    # newline="\n": Path.write_text uses text mode, which on Windows would
    # translate \n -> \r\n. The override editor's GET returns the override's
    # VERBATIM bytes (no newline translation), so a CRLF canonical would make the
    # round-tripped content mismatch the LF _SKILL_BODY on Windows. Pin LF so the
    # fixture is platform-deterministic (the existing read_text-based assertions
    # are unaffected — read_text translates CRLF->LF on read regardless).
    skill = root / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(_SKILL_BODY, encoding="utf-8", newline="\n")
    agent = root / "agents" / "beta"
    agent.mkdir(parents=True)
    (agent / "agent.md").write_text(
        "---\nname: beta\ndescription: a test agent\n---\n\nBody.\n",
        encoding="utf-8",
        newline="\n",
    )
    cmd = root / "commands" / "gamma"
    cmd.mkdir(parents=True)
    (cmd / "command.md").write_text(
        "---\ndescription: a test command\n---\n\nBody.\n",
        encoding="utf-8",
        newline="\n",
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


# ════════════════════════════════════════════════════════════════════════════
# Override editor — GET + PUT (ADR-0027 Editor-A)
# ════════════════════════════════════════════════════════════════════════════


async def _seed_and_commit(client, root: Path, vendor: str = "claude") -> str:
    """Seed an override, commit it (clean tree), return its mtime_ns as a string."""
    resp = await client.post("/api/wiki/skills/alpha/override", json={"vendor": vendor})
    assert resp.status_code == 200
    _git_commit(root, "seed override")
    target = root / "skills" / "alpha" / "overrides" / f"{vendor}.md"
    return str(target.stat().st_mtime_ns)


# ── GET …/override (read pane) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_override_not_seeded(dev_client, seeded_wiki: Path) -> None:
    # No override yet → exists=False, blank pane, mtime_ns="0" (canonical present).
    resp = await dev_client.get("/api/wiki/skills/alpha/override", params={"vendor": "claude"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is False
    assert data["content"] == ""
    assert data["mtime_ns"] == "0"


@pytest.mark.asyncio
async def test_get_override_existing_returns_content_and_mtime(
    dev_client, seeded_wiki: Path
) -> None:
    await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    resp = await dev_client.get("/api/wiki/skills/alpha/override", params={"vendor": "claude"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["exists"] is True
    assert data["content"] == _SKILL_BODY
    assert int(data["mtime_ns"]) > 0


@pytest.mark.asyncio
async def test_get_override_missing_canonical_is_404(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.get("/api/wiki/skills/nope/override", params={"vendor": "claude"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "canonical_absent"


@pytest.mark.asyncio
async def test_get_override_wiki_absent_is_404(dev_client, wiki_root: Path) -> None:  # noqa: F811
    resp = await dev_client.get("/api/wiki/skills/alpha/override", params={"vendor": "claude"})
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "wiki_absent"
    assert str(wiki_root) not in resp.text  # no host path leak


# ── PUT …/override (save) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_override_happy_path(dev_client, seeded_wiki: Path) -> None:
    m = await _seed_and_commit(dev_client, seeded_wiki)
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    new = "# Alpha EDITED\n"
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": new, "mtime_ns": m},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["vendor"] == "claude"
    assert data["wiki_dirty"] is True  # the edit dirtied the (committed) tree
    assert data["privacy_warning"] == 0
    assert int(data["mtime_ns"]) > 0
    assert target.read_text(encoding="utf-8") == new
    # Editing an existing override keeps the prior bytes as a .bak sibling.
    assert target.with_suffix(".md.bak").read_text(encoding="utf-8") == _SKILL_BODY


@pytest.mark.asyncio
async def test_edit_override_create_new_no_bak(dev_client, seeded_wiki: Path) -> None:
    # mtime_ns="0" + no existing override → create from blank (no .bak written).
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "# fresh\n", "mtime_ns": "0"},
    )
    assert resp.status_code == 200
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    assert target.read_text(encoding="utf-8") == "# fresh\n"
    assert not target.with_suffix(".md.bak").exists()


@pytest.mark.asyncio
async def test_edit_stale_mtime_is_409(dev_client, seeded_wiki: Path) -> None:
    # Unlocked pre-check: a stale token is refused and the current mtime echoed.
    await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "# x\n", "mtime_ns": "1"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason_code"] == "stale_mtime"
    assert int(body["mtime_ns"]) == target.stat().st_mtime_ns
    assert target.read_text(encoding="utf-8") == _SKILL_BODY  # not overwritten


@pytest.mark.asyncio
async def test_edit_inside_lock_restat_is_409(dev_client, seeded_wiki: Path, monkeypatch) -> None:
    # Codex major: the request passes the UNLOCKED pre-check (its reported mtime
    # matches the client token) but the real file's mtime differs, so only the
    # in-lock re-stat catches the race. Monkeypatch the pre-check reader to report
    # a matching token while pointing at the real (different-mtime) file.
    from memtomem.web.routes import wiki_mutations as wm
    from memtomem.wiki.inspect import OverrideContent

    m = await _seed_and_commit(dev_client, seeded_wiki)
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    real_mtime = target.stat().st_mtime_ns
    assert str(real_mtime) == m

    def _fake_read(store, asset_type, name, vendor):  # noqa: ANN001
        # exists/content irrelevant; mtime_ns matches the client token so the
        # unlocked pre-check passes, override_path is the REAL file.
        return OverrideContent(override_path=target, exists=True, content="x", mtime_ns=4242)

    monkeypatch.setattr(wm, "read_override", _fake_read)
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "# never\n", "mtime_ns": "4242"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason_code"] == "stale_mtime"
    assert int(body["mtime_ns"]) == real_mtime  # the authoritative in-lock value
    assert target.read_text(encoding="utf-8") == _SKILL_BODY  # nothing written


@pytest.mark.asyncio
async def test_edit_timeout_is_503(dev_client, seeded_wiki: Path, monkeypatch) -> None:
    # Codex major: a lock-acquire timeout maps to 503 busy.
    from memtomem.web.routes import wiki_mutations as wm

    m = await _seed_and_commit(dev_client, seeded_wiki)

    class _BoomTimeout:
        def __init__(self, *a, **k) -> None: ...

        async def __aenter__(self):
            raise TimeoutError

        async def __aexit__(self, *a) -> bool:
            return False

    monkeypatch.setattr(wm.asyncio, "timeout", lambda *a, **k: _BoomTimeout())
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "# x\n", "mtime_ns": m},
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_kind"] == "busy"


@pytest.mark.asyncio
async def test_edit_force_bypasses_stale_and_writes_bak(dev_client, seeded_wiki: Path) -> None:
    await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "# forced\n", "mtime_ns": "1", "force": True},
    )
    assert resp.status_code == 200
    assert target.read_text(encoding="utf-8") == "# forced\n"
    assert target.with_suffix(".md.bak").read_text(encoding="utf-8") == _SKILL_BODY


@pytest.mark.asyncio
async def test_edit_missing_canonical_is_404_and_creates_no_dir(
    dev_client, seeded_wiki: Path
) -> None:
    # Codex blocker: a PUT to a valid-but-nonexistent asset must 404 and NOT
    # create overrides/ (which would surface an orphan asset breaking diff/lint).
    resp = await dev_client.put(
        "/api/wiki/skills/nope/override",
        json={"vendor": "claude", "content": "# x\n", "mtime_ns": "0"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "canonical_absent"
    assert not (seeded_wiki / "skills" / "nope").exists()


@pytest.mark.asyncio
async def test_edit_bad_mtime_is_422(dev_client, seeded_wiki: Path) -> None:
    await dev_client.post("/api/wiki/skills/alpha/override", json={"vendor": "claude"})
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "# x\n", "mtime_ns": "not-an-int"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "invalid_mtime"


@pytest.mark.asyncio
async def test_edit_non_renderable_vendor_is_400(dev_client, seeded_wiki: Path) -> None:
    # ("commands", "codex") has no renderer → editing its override is rejected
    # (parity with the seed verb's NotImplementedError → 400).
    resp = await dev_client.put(
        "/api/wiki/commands/gamma/override",
        json={"vendor": "codex", "content": "# x\n", "mtime_ns": "0"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "vendor_unsupported"


@pytest.mark.asyncio
async def test_edit_privacy_warning_is_non_blocking(dev_client, seeded_wiki: Path) -> None:
    # D-E: a secret in the content yields a non-blocking warning count — the write
    # still succeeds (the handler is _REDACTION_EXEMPT, not _REDACTION_PROTECTED).
    m = await _seed_and_commit(dev_client, seeded_wiki)
    target = seeded_wiki / "skills" / "alpha" / "overrides" / "claude.md"
    secret = "AKIA" + "A" * 16
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": f"key: {secret}\n", "mtime_ns": m},
    )
    assert resp.status_code == 200  # NON-blocking
    assert resp.json()["privacy_warning"] >= 1
    assert secret in target.read_text(encoding="utf-8")  # bytes were written anyway


@pytest.mark.asyncio
async def test_editor_routes_absent_in_prod(prod_client, seeded_wiki: Path) -> None:
    # The editor mounts dev-only. In prod neither verb reaches a handler: the GET
    # falls through to the ``/api/{path:path}`` catch-all 404, and the PUT hits
    # 405 (that catch-all lists GET/POST/PATCH/DELETE but not PUT — a pre-existing
    # quirk). Either way the editor is unreachable.
    get = await prod_client.get("/api/wiki/skills/alpha/override", params={"vendor": "claude"})
    assert get.status_code == 404
    put = await prod_client.put(
        "/api/wiki/skills/alpha/override",
        json={"vendor": "claude", "content": "x", "mtime_ns": "0"},
    )
    assert put.status_code in (404, 405)


# ════════════════════════════════════════════════════════════════════════════
# Canonical editor — GET + PUT (ADR-0027 Editor-B)
# ════════════════════════════════════════════════════════════════════════════

_AGENT_BODY = "---\nname: beta\ndescription: a test agent\n---\n\nBody.\n"


# ── GET …/canonical (read pane) ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_canonical_returns_content_and_mtime(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.get("/api/wiki/skills/alpha/canonical")
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == _SKILL_BODY
    assert int(data["mtime_ns"]) > 0


@pytest.mark.asyncio
async def test_get_canonical_agent_returns_content(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.get("/api/wiki/agents/beta/canonical")
    assert resp.status_code == 200
    assert resp.json()["content"] == _AGENT_BODY


@pytest.mark.asyncio
async def test_get_canonical_missing_is_404(dev_client, seeded_wiki: Path) -> None:
    # Unlike an override, a missing canonical is an error (Editor-B edits an
    # existing asset — it never opens a blank pane to author a new one).
    resp = await dev_client.get("/api/wiki/skills/nope/canonical")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "canonical_absent"


@pytest.mark.asyncio
async def test_get_canonical_wiki_absent_is_404(dev_client, wiki_root: Path) -> None:  # noqa: F811
    resp = await dev_client.get("/api/wiki/skills/alpha/canonical")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "wiki_absent"
    assert str(wiki_root) not in resp.text  # no host path leak


# ── PUT …/canonical (save) ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_edit_canonical_happy_path(dev_client, seeded_wiki: Path) -> None:
    # The fixture commits the canonical, so the tree is clean; editing dirties it.
    m = (await dev_client.get("/api/wiki/skills/alpha/canonical")).json()["mtime_ns"]
    target = seeded_wiki / "skills" / "alpha" / "SKILL.md"
    new = "# Alpha EDITED\n\nNew body.\n"
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": new, "mtime_ns": m}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["wiki_dirty"] is True
    assert data["privacy_warning"] == 0
    assert int(data["mtime_ns"]) > 0
    assert target.read_text(encoding="utf-8") == new
    # The prior canonical is kept as a .bak sibling.
    assert target.with_suffix(".md.bak").read_text(encoding="utf-8") == _SKILL_BODY


@pytest.mark.asyncio
async def test_edit_canonical_agent_parseable_ok(dev_client, seeded_wiki: Path) -> None:
    m = (await dev_client.get("/api/wiki/agents/beta/canonical")).json()["mtime_ns"]
    new = "---\nname: beta\ndescription: edited\n---\n\nEdited body.\n"
    resp = await dev_client.put(
        "/api/wiki/agents/beta/canonical", json={"content": new, "mtime_ns": m}
    )
    assert resp.status_code == 200
    assert (seeded_wiki / "agents" / "beta" / "agent.md").read_text(encoding="utf-8") == new


@pytest.mark.asyncio
async def test_edit_canonical_unparseable_agent_is_400_writes_nothing(
    dev_client, seeded_wiki: Path
) -> None:
    # ADR-0027 Decision 6 + Validation: an unparseable agent canonical must 400
    # and leave the file BYTE-UNCHANGED (assert disk bytes, not just the status).
    target = seeded_wiki / "agents" / "beta" / "agent.md"
    before = target.read_text(encoding="utf-8")
    m = (await dev_client.get("/api/wiki/agents/beta/canonical")).json()["mtime_ns"]
    resp = await dev_client.put(
        "/api/wiki/agents/beta/canonical",
        json={"content": "no frontmatter at all\n", "mtime_ns": m},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "canonical_unparseable"
    assert target.read_text(encoding="utf-8") == before  # nothing written
    assert not target.with_suffix(".md.bak").exists()  # not even a .bak
    assert str(seeded_wiki) not in resp.text  # path-safe parse-error message


@pytest.mark.asyncio
async def test_edit_canonical_skill_has_no_parse_gate(dev_client, seeded_wiki: Path) -> None:
    # Skills are byte-copied to every vendor — there is no structured parse, so
    # any UTF-8 markdown saves (only agents/commands are parse-gated).
    m = (await dev_client.get("/api/wiki/skills/alpha/canonical")).json()["mtime_ns"]
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical",
        json={"content": "literally anything :: not yaml\n", "mtime_ns": m},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_edit_canonical_stale_mtime_is_409(dev_client, seeded_wiki: Path) -> None:
    target = seeded_wiki / "skills" / "alpha" / "SKILL.md"
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": "# x\n", "mtime_ns": "1"}
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason_code"] == "stale_mtime"
    assert int(body["mtime_ns"]) == target.stat().st_mtime_ns
    assert target.read_text(encoding="utf-8") == _SKILL_BODY  # not overwritten


@pytest.mark.asyncio
async def test_edit_canonical_inside_lock_restat_is_409(
    dev_client, seeded_wiki: Path, monkeypatch
) -> None:
    # The request passes the UNLOCKED pre-check (its reported mtime matches the
    # client token) but the real file's mtime differs, so only the in-lock re-stat
    # catches the race (mirrors the override editor's in-lock test).
    from memtomem.web.routes import wiki_mutations as wm
    from memtomem.wiki.inspect import CanonicalContent

    target = seeded_wiki / "skills" / "alpha" / "SKILL.md"
    real_mtime = target.stat().st_mtime_ns

    def _fake_read(store, asset_type, name):  # noqa: ANN001
        # mtime_ns matches the client token so the unlocked pre-check passes;
        # canonical_path is the REAL (different-mtime) file.
        return CanonicalContent(canonical_path=target, content="x", mtime_ns=4242)

    monkeypatch.setattr(wm, "read_canonical", _fake_read)
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": "# never\n", "mtime_ns": "4242"}
    )
    assert resp.status_code == 409
    assert resp.json()["reason_code"] == "stale_mtime"
    assert int(resp.json()["mtime_ns"]) == real_mtime  # authoritative in-lock value
    assert target.read_text(encoding="utf-8") == _SKILL_BODY  # nothing written


@pytest.mark.asyncio
async def test_edit_canonical_timeout_is_503(dev_client, seeded_wiki: Path, monkeypatch) -> None:
    from memtomem.web.routes import wiki_mutations as wm

    m = (await dev_client.get("/api/wiki/skills/alpha/canonical")).json()["mtime_ns"]

    class _BoomTimeout:
        def __init__(self, *a, **k) -> None: ...

        async def __aenter__(self):
            raise TimeoutError

        async def __aexit__(self, *a) -> bool:
            return False

    monkeypatch.setattr(wm.asyncio, "timeout", lambda *a, **k: _BoomTimeout())
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": "# x\n", "mtime_ns": m}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error_kind"] == "busy"


@pytest.mark.asyncio
async def test_edit_canonical_force_bypasses_stale_and_writes_bak(
    dev_client, seeded_wiki: Path, caplog
) -> None:
    target = seeded_wiki / "skills" / "alpha" / "SKILL.md"
    with caplog.at_level(logging.WARNING):
        resp = await dev_client.put(
            "/api/wiki/skills/alpha/canonical",
            json={"content": "# forced\n", "mtime_ns": "1", "force": True},
        )
    assert resp.status_code == 200
    assert target.read_text(encoding="utf-8") == "# forced\n"
    assert target.with_suffix(".md.bak").read_text(encoding="utf-8") == _SKILL_BODY
    # The force bypass emits a WARNING audit logging both mtimes (D-D parity).
    assert any(
        "force-save bypassed wiki canonical mtime check" in r.message
        for r in caplog.records
        if r.levelname == "WARNING"
    )


@pytest.mark.asyncio
async def test_edit_canonical_missing_is_404_and_creates_no_asset(
    dev_client, seeded_wiki: Path
) -> None:
    # A PUT to a valid-but-nonexistent asset must 404 and NOT create the asset
    # (the editor edits an existing canonical, it never authors a new one).
    resp = await dev_client.put(
        "/api/wiki/skills/nope/canonical", json={"content": "# x\n", "mtime_ns": "0"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "canonical_absent"
    assert not (seeded_wiki / "skills" / "nope").exists()


@pytest.mark.asyncio
async def test_edit_canonical_bad_mtime_is_422(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": "# x\n", "mtime_ns": "not-an-int"}
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "invalid_mtime"


@pytest.mark.asyncio
async def test_edit_canonical_unknown_asset_type_is_422(dev_client, seeded_wiki: Path) -> None:
    resp = await dev_client.put(
        "/api/wiki/widgets/alpha/canonical", json={"content": "# x\n", "mtime_ns": "0"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_edit_canonical_privacy_warning_is_non_blocking(
    dev_client, seeded_wiki: Path
) -> None:
    # D-E: a secret in the canonical yields a non-blocking warning — the write
    # still succeeds (_REDACTION_EXEMPT, not _REDACTION_PROTECTED).
    m = (await dev_client.get("/api/wiki/skills/alpha/canonical")).json()["mtime_ns"]
    target = seeded_wiki / "skills" / "alpha" / "SKILL.md"
    secret = "AKIA" + "A" * 16
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": f"key: {secret}\n", "mtime_ns": m}
    )
    assert resp.status_code == 200  # NON-blocking
    assert resp.json()["privacy_warning"] >= 1
    assert secret in target.read_text(encoding="utf-8")  # bytes written anyway


@pytest.mark.asyncio
async def test_canonical_editor_routes_absent_in_prod(prod_client, seeded_wiki: Path) -> None:
    # The editor mounts dev-only. In prod the GET falls through to the catch-all
    # 404 and the PUT hits the catch-all 405 (it lists GET/POST/PATCH/DELETE, not
    # PUT) — either way the canonical editor is unreachable.
    get = await prod_client.get("/api/wiki/skills/alpha/canonical")
    assert get.status_code == 404
    put = await prod_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": "x", "mtime_ns": "0"}
    )
    assert put.status_code in (404, 405)


@pytest.mark.asyncio
async def test_get_canonical_command_returns_content(dev_client, seeded_wiki: Path) -> None:
    # The third asset type (commands) round-trips through the canonical GET too.
    resp = await dev_client.get("/api/wiki/commands/gamma/canonical")
    assert resp.status_code == 200
    assert resp.json()["content"] == "---\ndescription: a test command\n---\n\nBody.\n"


@pytest.mark.asyncio
async def test_edit_canonical_unparseable_command_is_400_writes_nothing(
    dev_client, seeded_wiki: Path
) -> None:
    # Decision 6 parse-gates BOTH agents and commands. A command fails to parse on
    # an invalid frontmatter name (commands without frontmatter are tolerated, so a
    # bad name is the parse failure) — 400 + byte-unchanged on disk, no .bak, and a
    # path-safe message (parity with the agent parse-gate test).
    target = seeded_wiki / "commands" / "gamma" / "command.md"
    before = target.read_text(encoding="utf-8")
    m = (await dev_client.get("/api/wiki/commands/gamma/canonical")).json()["mtime_ns"]
    resp = await dev_client.put(
        "/api/wiki/commands/gamma/canonical",
        json={"content": "---\nname: ../../etc/passwd\n---\n\nbody\n", "mtime_ns": m},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "canonical_unparseable"
    assert target.read_text(encoding="utf-8") == before  # nothing written
    assert not target.with_suffix(".md.bak").exists()
    assert str(seeded_wiki) not in resp.text  # path-safe parse-error message


@pytest.mark.asyncio
async def test_edit_canonical_bad_name_is_400(dev_client, seeded_wiki: Path) -> None:
    # Both the PUT and GET canonical handlers run _validate_name_or_error first.
    put = await dev_client.put(
        "/api/wiki/skills/-bad/canonical", json={"content": "# x\n", "mtime_ns": "0"}
    )
    assert put.status_code == 400
    assert put.json()["detail"]["reason_code"] == "invalid_name"
    get = await dev_client.get("/api/wiki/skills/-bad/canonical")
    assert get.status_code == 400
    assert get.json()["detail"]["reason_code"] == "invalid_name"


@pytest.mark.asyncio
async def test_edit_canonical_write_vanished_is_404_no_leak(
    dev_client, seeded_wiki: Path, monkeypatch
) -> None:
    # TOCTOU hardening: if the canonical is removed between the in-lock re-stat and
    # the write, write_canonical raises FileNotFoundError (which embeds the absolute
    # wiki path). The handler must convert it to a fixed-message 404 — never a 500
    # whose traceback would leak the host path.
    from memtomem.web.routes import wiki_mutations as wm

    m = (await dev_client.get("/api/wiki/skills/alpha/canonical")).json()["mtime_ns"]
    leaky = seeded_wiki / "skills" / "alpha" / "SKILL.md"

    def _vanish(store, asset_type, name, content):  # noqa: ANN001
        raise FileNotFoundError(f"wiki has no {asset_type}/{name} canonical at {leaky}")

    monkeypatch.setattr(wm, "write_canonical", _vanish)
    resp = await dev_client.put(
        "/api/wiki/skills/alpha/canonical", json={"content": "# x\n", "mtime_ns": m}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "canonical_absent"
    assert str(seeded_wiki) not in resp.text  # the absolute path must not leak
