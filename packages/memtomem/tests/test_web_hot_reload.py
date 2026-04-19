"""End-to-end tests for web config hot-reload.

These tests use a real ``~/.memtomem/config.json`` layout under a tmp HOME
rather than the FakeConfig-based fixture in ``test_web_routes.py``, because
hot-reload swaps ``app.state.config`` for a real :class:`Mem2MemConfig`
instance built via the canonical load path.

Covers (numbering matches ``project_web_hot_reload_bridge.md`` test plan):

1. read-through reload on stale GET /api/config
2. PATCH re-reads before merge (survives external edit)
3. All 4 write handlers honor reload
4. config.d fragment change detected
5. invalid JSON → 200 with config_reload_error + stale config preserved
6. after fix → GET clears the error
7. tokenizer change via reload triggers fanout (FTS rebuild + cache invalidate)
8. concurrent PATCH + disk edit: lock serialisation
9. V2 (issue #268): GET's lock-free reload yields to a concurrent writer's
   signature bump via compare-and-swap
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web import hot_reload as _hot_reload
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Fixture: real HOME, real config.json, lightweight component mocks
# ---------------------------------------------------------------------------


def _bump_mtime(path: Path) -> None:
    """Force mtime_ns to move forward — needed on filesystems where two
    consecutive writes can land in the same ns bucket on fast hardware."""
    st = path.stat()
    new_ns = st.st_mtime_ns + 1_000_000  # +1ms
    os.utime(path, ns=(new_ns, new_ns))


def _write_config(home: Path, data: dict[str, Any]) -> Path:
    cfg = home / ".memtomem" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data), encoding="utf-8")
    _bump_mtime(cfg)
    return cfg


def _write_fragment(home: Path, name: str, data: dict[str, Any]) -> Path:
    frag = home / ".memtomem" / "config.d" / name
    frag.parent.mkdir(parents=True, exist_ok=True)
    frag.write_text(json.dumps(data), encoding="utf-8")
    _bump_mtime(frag)
    return frag


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    # pydantic-settings reads env; clear any memtomem env vars that may leak
    # from the developer shell and make the test non-hermetic.
    for k in list(os.environ):
        if k.startswith("MEMTOMEM_"):
            monkeypatch.delenv(k, raising=False)
    return tmp_path


@pytest.fixture
def app(home: Path):
    application = create_app(lifespan=None)

    # Minimal component mocks — hot-reload path doesn't touch storage/embedder
    # in the read-through case, but some write handlers pass them through.
    storage = AsyncMock()
    storage.rebuild_fts = AsyncMock(return_value=0)
    search_pipeline = AsyncMock()
    search_pipeline.invalidate_cache = MagicMock()
    index_engine = AsyncMock()
    embedder = AsyncMock()

    application.state.storage = storage
    application.state.search_pipeline = search_pipeline
    application.state.index_engine = index_engine
    application.state.embedder = embedder
    application.state.dedup_scanner = AsyncMock()

    # Start with a real Mem2MemConfig built from whatever the tmp HOME
    # currently contains, and pin the signature so no reload fires until the
    # test mutates disk.
    application.state.config = _hot_reload._build_fresh_config()
    application.state.config_signature = _hot_reload.current_signature()
    application.state.last_reload_error = None

    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Test 1 — read-through reload on stale GET
# ---------------------------------------------------------------------------


async def test_get_config_picks_up_external_disk_edit(home: Path, app, client: AsyncClient):
    _write_config(home, {"mmr": {"enabled": False}})
    # Re-sync signature now that we wrote the initial file.
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    resp = await client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["mmr"]["enabled"] is False

    _write_config(home, {"mmr": {"enabled": True}})

    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mmr"]["enabled"] is True
    assert data["config_mtime_ns"] > 0
    assert data["config_reload_error"] is None


# ---------------------------------------------------------------------------
# Test 2 — PATCH re-reads before merge
# ---------------------------------------------------------------------------


async def test_patch_preserves_external_edit(home: Path, app, client: AsyncClient):
    _write_config(home, {"mmr": {"enabled": False}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    # External (CLI-like) edit mutates mmr.enabled while the server is running.
    _write_config(home, {"mmr": {"enabled": True}})

    # UI-side PATCH touches a different field — must merge, not overwrite.
    resp = await client.patch(
        "/api/config", params={"persist": "true"}, json={"search": {"default_top_k": 42}}
    )
    assert resp.status_code == 200, resp.text

    on_disk = json.loads((home / ".memtomem" / "config.json").read_text())
    assert on_disk.get("mmr", {}).get("enabled") is True  # CLI edit preserved
    assert on_disk.get("search", {}).get("default_top_k") == 42  # UI edit applied


# ---------------------------------------------------------------------------
# Test 3 — all 4 write handlers honor reload
# ---------------------------------------------------------------------------


async def test_save_endpoint_reloads_before_write(home: Path, app, client: AsyncClient):
    _write_config(home, {"mmr": {"enabled": False}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    _write_config(home, {"mmr": {"enabled": True}, "search": {"default_top_k": 77}})

    resp = await client.post("/api/config/save")
    assert resp.status_code == 200

    on_disk = json.loads((home / ".memtomem" / "config.json").read_text())
    assert on_disk.get("mmr", {}).get("enabled") is True
    assert on_disk.get("search", {}).get("default_top_k") == 77


async def test_memory_dirs_add_reloads_before_write(
    home: Path, app, client: AsyncClient, tmp_path: Path
):
    # Seed one memory_dir so removal is still possible later.
    first = tmp_path / "first"
    first.mkdir()
    _write_config(home, {"indexing": {"memory_dirs": [str(first)]}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    # External edit flips mmr on between server startup and this handler.
    _write_config(
        home,
        {"indexing": {"memory_dirs": [str(first)]}, "mmr": {"enabled": True}},
    )

    second = tmp_path / "second"
    resp = await client.post("/api/memory-dirs/add", json={"path": str(second)})
    assert resp.status_code == 200, resp.text

    on_disk = json.loads((home / ".memtomem" / "config.json").read_text())
    # External mmr edit survived the memory-dirs write.
    assert on_disk.get("mmr", {}).get("enabled") is True
    assert str(second.resolve()) in on_disk.get("indexing", {}).get("memory_dirs", [])


async def test_memory_dirs_remove_reloads_before_write(
    home: Path, app, client: AsyncClient, tmp_path: Path
):
    first = tmp_path / "first"
    first.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    _write_config(
        home,
        {"indexing": {"memory_dirs": [str(first), str(second)]}},
    )
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    _write_config(
        home,
        {
            "indexing": {"memory_dirs": [str(first), str(second)]},
            "mmr": {"enabled": True},
        },
    )

    resp = await client.post("/api/memory-dirs/remove", json={"path": str(second)})
    assert resp.status_code == 200, resp.text

    on_disk = json.loads((home / ".memtomem" / "config.json").read_text())
    assert on_disk.get("mmr", {}).get("enabled") is True
    remaining = on_disk.get("indexing", {}).get("memory_dirs", [])
    assert str(second.resolve()) not in remaining


# ---------------------------------------------------------------------------
# Test 4 — fragment stale signature
# ---------------------------------------------------------------------------


async def test_fragment_change_detected(home: Path, app, client: AsyncClient):
    _write_config(home, {})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    # Write a new fragment after startup — fragments participate in the
    # composite signature, so this must trigger a reload on the next GET.
    _write_fragment(home, "99-test.json", {"mmr": {"enabled": True}})

    resp = await client.get("/api/config")
    assert resp.status_code == 200
    assert resp.json()["mmr"]["enabled"] is True


# ---------------------------------------------------------------------------
# Test 5 — invalid JSON fallback
# ---------------------------------------------------------------------------


async def test_invalid_json_surfaces_error_but_keeps_stale_config(
    home: Path, app, client: AsyncClient
):
    _write_config(home, {"mmr": {"enabled": False}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    cfg_path = home / ".memtomem" / "config.json"
    cfg_path.write_text('{"search":', encoding="utf-8")  # truncated JSON
    _bump_mtime(cfg_path)

    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["config_reload_error"] is not None
    assert "JSONDecodeError" in data["config_reload_error"] or "JSON" in data["config_reload_error"]
    # Stale mmr.enabled=False is preserved.
    assert data["mmr"]["enabled"] is False


async def test_non_json_structural_error_also_surfaces(home: Path, app, client: AsyncClient):
    """JSON parses but the shape is wrong (root is a list, not a dict).

    Covers the non-``JSONDecodeError`` failure mode of ``_build_fresh_config``:
    the loader's ``section_obj = getattr(config, section_name)`` over a
    non-dict payload raises ``AttributeError``, which ``reload_if_stale``
    catches via the bare ``except Exception`` and converts into a
    ``ReloadError``. Regression guard against narrowing that except
    clause back to ``(OSError, JSONDecodeError)``.
    """
    _write_config(home, {"mmr": {"enabled": False}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    cfg_path = home / ".memtomem" / "config.json"
    cfg_path.write_text('["not", "a", "dict"]', encoding="utf-8")  # valid JSON, wrong shape
    _bump_mtime(cfg_path)

    resp = await client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["config_reload_error"] is not None
    # Stale value preserved.
    assert data["mmr"]["enabled"] is False


async def test_patch_refused_while_disk_is_broken(home: Path, app, client: AsyncClient):
    _write_config(home, {"mmr": {"enabled": False}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    cfg_path = home / ".memtomem" / "config.json"
    cfg_path.write_text('{"search":', encoding="utf-8")
    _bump_mtime(cfg_path)

    # Prime the error via a GET first.
    await client.get("/api/config")

    resp = await client.patch("/api/config", json={"search": {"default_top_k": 5}})
    assert resp.status_code == 409, resp.text
    assert "invalid" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test 6 — recovery after fix
# ---------------------------------------------------------------------------


async def test_reload_error_clears_after_fix(home: Path, app, client: AsyncClient):
    _write_config(home, {"mmr": {"enabled": False}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    cfg_path = home / ".memtomem" / "config.json"
    cfg_path.write_text('{"search":', encoding="utf-8")
    _bump_mtime(cfg_path)
    bad_resp = await client.get("/api/config")
    assert bad_resp.json()["config_reload_error"] is not None

    # Fix the file.
    _write_config(home, {"mmr": {"enabled": True}})

    good_resp = await client.get("/api/config")
    assert good_resp.status_code == 200
    data = good_resp.json()
    assert data["config_reload_error"] is None
    assert data["mmr"]["enabled"] is True


# ---------------------------------------------------------------------------
# Test 7 — tokenizer fanout on reload
# ---------------------------------------------------------------------------


async def test_tokenizer_change_via_reload_triggers_fanout(home: Path, app, client: AsyncClient):
    _write_config(home, {"search": {"tokenizer": "unicode61"}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    # Reset call counters that may have been bumped during fixture warm-up.
    app.state.storage.rebuild_fts.reset_mock()
    app.state.search_pipeline.invalidate_cache.reset_mock()

    _write_config(home, {"search": {"tokenizer": "kiwipiepy"}})

    resp = await client.get("/api/config")
    assert resp.status_code == 200

    # Cache invalidation is sync; wait a tick for the scheduled rebuild.
    await asyncio.sleep(0.05)

    app.state.search_pipeline.invalidate_cache.assert_called()
    app.state.storage.rebuild_fts.assert_awaited()


# ---------------------------------------------------------------------------
# Test 8 — lock serialisation under concurrent PATCH + disk edit
# ---------------------------------------------------------------------------


async def test_concurrent_patches_are_serialised_by_lock(
    home: Path, app, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    """Two concurrent PATCH requests must execute serially under ``_config_lock``.

    Without the lock, both writers would interleave inside the same
    read-merge-write region and the second writer's merge would see a
    pre-first-write snapshot. We prove serialisation by wrapping
    ``save_config_overrides`` to record (enter, exit) timestamps and
    asserting ``writer_1.exit <= writer_2.enter`` — i.e., no overlap.
    """
    _write_config(home, {"search": {"default_top_k": 10}})
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()

    spans: list[tuple[str, float, float]] = []

    import memtomem.web.routes.system as _sys_routes

    real_save = _sys_routes.save_config_overrides

    call_counter = {"n": 0}

    def instrumented_save(cfg, *args, **kwargs):
        call_counter["n"] += 1
        label = f"writer_{call_counter['n']}"
        enter = time.perf_counter()
        # Hold the lock long enough that a concurrent request would
        # visibly overlap if serialisation were broken. 50ms is well
        # above any scheduler jitter but still keeps the test fast.
        time.sleep(0.05)
        real_save(cfg, *args, **kwargs)
        exit_t = time.perf_counter()
        spans.append((label, enter, exit_t))

    monkeypatch.setattr(_sys_routes, "save_config_overrides", instrumented_save)

    async def do_patch(value: int) -> int:
        resp = await client.patch(
            "/api/config",
            params={"persist": "true"},
            json={"search": {"default_top_k": value}},
        )
        return resp.status_code

    status1, status2 = await asyncio.gather(do_patch(42), do_patch(99))
    assert status1 == 200 and status2 == 200

    # Both writers ran.
    assert len(spans) == 2, spans

    # Sort by enter time; the earlier writer must fully exit before the
    # later one enters. Tolerance covers scheduler clock skew.
    spans.sort(key=lambda s: s[1])
    (_, _, first_exit), (_, second_enter, _) = spans
    assert first_exit <= second_enter + 1e-3, (
        f"writers overlapped: first exited at {first_exit}, second entered at {second_enter}"
    )

    # And the second-merged value (whichever PATCH won scheduling) must
    # be reflected on disk — proving the late writer didn't silently
    # clobber the early one with stale state.
    on_disk = json.loads((home / ".memtomem" / "config.json").read_text())
    assert on_disk["search"]["default_top_k"] in (42, 99)


# ---------------------------------------------------------------------------
# Test 9 — V2: GET's lock-free reload must not overwrite a writer's bump
# ---------------------------------------------------------------------------


def test_reload_if_stale_cas_yields_to_concurrent_writer_bump(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Issue #268: the lock-free reader path had a race where its trailing
    ``app.state.config = new_cfg; _set_last_signature(sig)`` could overwrite
    a writer's commit that landed while ``_build_fresh_config`` was running.
    The writer's view is at least as fresh, so we yield.

    Proof: hook ``_build_fresh_config`` so that *between* its call and its
    return, a simulated writer updates ``app.state.config_signature``.
    Without CAS, ``reload_if_stale`` would then assign the reader-side
    cfg + reset the signature to the older ``sig`` it observed at entry,
    clobbering the writer's bump. With CAS, it detects the advance and
    returns ``False`` with no state mutation.

    Direct unit-level construction (no HTTP / no asyncio) keeps the race
    window deterministic: the writer "lands" exactly where the reader
    would race it, regardless of scheduler quirks.
    """
    from fastapi import FastAPI

    app = FastAPI()
    _write_config(home, {"mmr": {"enabled": False}, "search": {"default_top_k": 10}})

    # Baseline reader-visible state.
    app.state.config = _hot_reload._build_fresh_config()
    app.state.config_signature = _hot_reload.current_signature()
    app.state.last_reload_error = None

    sig_before = app.state.config_signature
    cfg_before = app.state.config

    # External disk edit bumps current_signature() past sig_before. This
    # is what triggers reload_if_stale to enter its rebuild branch at all.
    _write_config(home, {"mmr": {"enabled": True}, "search": {"default_top_k": 10}})

    # Simulate "a writer committed inside _config_lock while we were
    # inside _build_fresh_config" by mutating app.state.config_signature
    # from *within* the wrapped build call. When reload_if_stale returns
    # from this call, it will find _get_last_signature(app) != last and
    # must bail out without swapping.
    real_build = _hot_reload._build_fresh_config
    writer_sig = None

    def build_and_interleave_writer():
        nonlocal writer_sig
        # Do one more disk touch so commit_writer_signature picks up a
        # signature strictly newer than anything the reader saw.
        _write_config(home, {"mmr": {"enabled": True}, "search": {"default_top_k": 99}})
        _hot_reload.commit_writer_signature(app)
        writer_sig = app.state.config_signature
        return real_build()

    monkeypatch.setattr(_hot_reload, "_build_fresh_config", build_and_interleave_writer)

    swapped = _hot_reload.reload_if_stale(app)

    # CAS must have fired: no swap happened.
    assert swapped is False
    assert app.state.config_signature == writer_sig, (
        "writer's signature was overwritten by the reader's tail"
    )
    assert app.state.config is cfg_before, "writer's config view was clobbered"
    # And of course the writer's signature must differ from what the
    # reader saw at entry — otherwise there was no race to defend against.
    assert writer_sig != sig_before


# ---------------------------------------------------------------------------
# Unit tests for the helper itself
# ---------------------------------------------------------------------------


class TestSignature:
    def test_no_config_yields_stable_signature(self, home: Path):
        sig1 = _hot_reload.current_signature()
        sig2 = _hot_reload.current_signature()
        assert sig1 == sig2

    def test_signature_changes_on_config_write(self, home: Path):
        sig_before = _hot_reload.current_signature()
        _write_config(home, {"mmr": {"enabled": True}})
        sig_after = _hot_reload.current_signature()
        assert sig_before != sig_after

    def test_signature_changes_on_fragment_add(self, home: Path):
        sig_before = _hot_reload.current_signature()
        _write_fragment(home, "00.json", {})
        sig_after = _hot_reload.current_signature()
        assert sig_before != sig_after


class TestReloadIfStale:
    def test_no_change_returns_false(self, home: Path):
        app = create_app(lifespan=None)
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()
        app.state.last_reload_error = None

        assert _hot_reload.reload_if_stale(app) is False

    def test_change_swaps_config(self, home: Path):
        app = create_app(lifespan=None)
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()
        app.state.last_reload_error = None
        assert app.state.config.mmr.enabled is False

        _write_config(home, {"mmr": {"enabled": True}})
        assert _hot_reload.reload_if_stale(app) is True
        assert app.state.config.mmr.enabled is True

    def test_broken_disk_keeps_old_config(self, home: Path):
        app = create_app(lifespan=None)
        _write_config(home, {"mmr": {"enabled": False}})
        app.state.config = _hot_reload._build_fresh_config()
        app.state.config_signature = _hot_reload.current_signature()
        app.state.last_reload_error = None
        old = app.state.config

        cfg_path = home / ".memtomem" / "config.json"
        cfg_path.write_text('{"search":', encoding="utf-8")
        _bump_mtime(cfg_path)

        assert _hot_reload.reload_if_stale(app) is False
        assert app.state.config is old
        err = _hot_reload.get_reload_error(app)
        assert err is not None
        assert err.at_mtime_ns == _hot_reload.get_config_mtime_ns()


class TestApplyRuntimeConfigChanges:
    def test_tokenizer_change_fires_set_tokenizer_and_rebuild(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        set_tokenizer_calls: list[str] = []

        import memtomem.storage.fts_tokenizer as fts_tok

        monkeypatch.setattr(fts_tok, "set_tokenizer", lambda t: set_tokenizer_calls.append(t))

        old = MagicMock()
        old.search.tokenizer = "unicode61"
        new = MagicMock()
        new.search.tokenizer = "kiwi"

        storage = AsyncMock()
        storage.rebuild_fts = AsyncMock(return_value=5)
        search_pipeline = MagicMock()

        async def _run():
            _hot_reload.apply_runtime_config_changes(
                old, new, storage=storage, search_pipeline=search_pipeline
            )
            # Give the scheduled rebuild a chance to run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        asyncio.run(_run())

        assert set_tokenizer_calls == ["kiwi"]
        search_pipeline.invalidate_cache.assert_called_once()

    def test_no_tokenizer_change_skips_fts_rebuild(self):
        old = MagicMock()
        old.search.tokenizer = "unicode61"
        new = MagicMock()
        new.search.tokenizer = "unicode61"

        storage = AsyncMock()
        storage.rebuild_fts = AsyncMock(return_value=0)
        search_pipeline = MagicMock()

        _hot_reload.apply_runtime_config_changes(
            old, new, storage=storage, search_pipeline=search_pipeline
        )

        storage.rebuild_fts.assert_not_called()
        search_pipeline.invalidate_cache.assert_called_once()


# ---------------------------------------------------------------------------
# Mutex guard: the lock rename must keep the public name stable for other
# call sites that may import it. Regression guard against silent drift.
# ---------------------------------------------------------------------------


def test_system_module_exposes_renamed_lock():
    from memtomem.web.routes import system

    assert hasattr(system, "_config_lock")
    # The old name is intentionally removed — fail loudly if someone
    # re-adds an alias pointing at the same lock (splits guarantees).
    assert not hasattr(system, "_config_patch_lock")


# Silence "imported but unused" for ``time`` and ``pytest`` on trimmed runs.
_ = (time, pytest)
