"""Regression tests for context-gateway write serialization (#277).

Two concurrent POSTs to ``/api/settings-sync`` (or any other gateway
mutator) must execute serially under ``_gateway_lock``. Without the
lock, two concurrent ``POST /api/settings-sync/resolve`` for the same
rule could interleave inside the read-merge-write region — the second
writer would see the pre-first-write snapshot, race past the
``st_mtime`` (float-second) CAS for sub-second writes, and silently
clobber the first.

These tests follow the same instrumentation pattern as
``test_web_hot_reload.py::test_concurrent_patches_are_serialised_by_lock``:
wrap a sync helper inside the lock with a small ``time.sleep`` and
assert ``writer_1.exit <= writer_2.enter``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures — sandbox HOME so ~/.claude writes stay inside tmp_path
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin ``HOME`` to ``tmp_path`` so settings-sync writes stay sandboxed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # ClaudeSettingsGenerator.is_available() requires the dir to exist.
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app(gateway_home: Path):
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = gateway_home
    application.state.storage = AsyncMock()
    application.state.config = None
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


def _rule(matcher: str = "", command: str = "echo ok") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def _make_canonical_settings(home: Path, hooks: dict) -> Path:
    canonical = home / ".memtomem" / "settings.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(json.dumps({"hooks": hooks}))
    return canonical


def _make_target_settings(home: Path, hooks: dict) -> Path:
    target = home / ".claude" / "settings.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"hooks": hooks}))
    return target


# ---------------------------------------------------------------------------
# settings-sync POST serialisation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_settings_sync_serialised_by_lock(
    gateway_home: Path,
    app,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two concurrent POST /api/settings-sync run serially under _gateway_lock.

    Without the lock both calls would invoke ``generate_all_settings``
    concurrently and the second's read+merge+write would interleave
    with the first. We instrument the sync function with a 50 ms hold
    and assert the spans don't overlap.
    """
    rule = _rule("Write", "echo ok")
    _make_canonical_settings(gateway_home, {"PostToolUse": [rule]})
    _make_target_settings(gateway_home, {})

    spans: list[tuple[str, float, float]] = []

    import memtomem.web.routes.settings_sync as _ss

    real_generate = _ss.generate_all_settings
    counter = {"n": 0}

    def instrumented(project_root, *args, **kwargs):
        counter["n"] += 1
        label = f"sync_{counter['n']}"
        enter = time.perf_counter()
        # Hold the critical section long enough that a concurrent
        # request would overlap if serialisation were broken.
        time.sleep(0.05)
        out = real_generate(project_root, *args, **kwargs)
        exit_t = time.perf_counter()
        spans.append((label, enter, exit_t))
        return out

    monkeypatch.setattr(_ss, "generate_all_settings", instrumented)

    async def do_post() -> int:
        resp = await client.post("/api/settings-sync")
        return resp.status_code

    s1, s2 = await asyncio.gather(do_post(), do_post())
    assert s1 == 200 and s2 == 200

    assert len(spans) == 2, spans
    spans.sort(key=lambda s: s[1])
    (_, _, first_exit), (_, second_enter, _) = spans
    assert first_exit <= second_enter + 1e-3, (
        f"settings-sync overlapped: first exit {first_exit}, second enter {second_enter}"
    )


# ---------------------------------------------------------------------------
# resolve same-rule serialisation + file integrity
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_concurrent_resolve_same_rule_serialised_by_lock(
    gateway_home: Path,
    app,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two concurrent POST /api/settings-sync/resolve for the same rule
    serialise under ``_gateway_lock`` and produce a well-formed file.

    Without the lock the second writer could read the pre-first-write
    target, race past the float-second mtime CAS, and clobber the
    first writer with stale state. With the lock, the second handler
    runs after the first completes, sees the freshly-applied rule,
    and replaces it again with the same canonical value (idempotent).
    """
    canonical_rule = _rule("Write", "echo new")
    target_rule = _rule("Write", "echo old")
    _make_canonical_settings(gateway_home, {"PostToolUse": [canonical_rule]})
    _make_target_settings(gateway_home, {"PostToolUse": [target_rule]})

    spans: list[tuple[str, float, float]] = []

    import memtomem.web.routes.settings_sync as _ss

    real_write = _ss._write_json
    counter = {"n": 0}

    def instrumented_write(path, data, *args, **kwargs):
        counter["n"] += 1
        label = f"write_{counter['n']}"
        enter = time.perf_counter()
        time.sleep(0.05)
        real_write(path, data, *args, **kwargs)
        exit_t = time.perf_counter()
        spans.append((label, enter, exit_t))

    monkeypatch.setattr(_ss, "_write_json", instrumented_write)

    body = {"event": "PostToolUse", "matcher": "Write", "action": "use_proposed"}

    async def do_resolve() -> dict:
        resp = await client.post("/api/settings-sync/resolve", json=body)
        return resp.json()

    r1, r2 = await asyncio.gather(do_resolve(), do_resolve())
    # With the lock, both calls succeed: the second one runs after the
    # first releases, re-reads the freshly-applied rule, and replaces
    # it with the same canonical value (idempotent). The mtime CAS
    # holds because nothing modifies the file between the second
    # handler's capture and check.
    assert r1["status"] == "ok"
    assert r2["status"] == "ok"

    # Both writes happened serially under the lock.
    assert len(spans) == 2, spans
    spans.sort(key=lambda s: s[1])
    (_, _, first_exit), (_, second_enter, _) = spans
    assert first_exit <= second_enter + 1e-3, (
        f"resolve writes overlapped: first exit {first_exit}, second enter {second_enter}"
    )

    # File is well-formed: a single rule with the canonical command.
    final = json.loads((gateway_home / ".claude" / "settings.json").read_text())
    rules = final["hooks"]["PostToolUse"]
    assert len(rules) == 1
    assert rules[0]["hooks"][0]["command"] == "echo new"


# ---------------------------------------------------------------------------
# Per-resource sync POST — smoke gather
# ---------------------------------------------------------------------------


def _make_canonical_skill(home: Path, name: str, content: str = "# Test\n") -> Path:
    skill_dir = home / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(content)
    return skill_dir


def _make_canonical_agent(
    home: Path,
    name: str,
    body: str = "---\nname: a\ndescription: d\n---\n# A\n",
) -> Path:
    agent_path = home / ".memtomem" / "agents" / f"{name}.md"
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    agent_path.write_text(body)
    return agent_path


def _make_canonical_command(home: Path, name: str, body: str = "# command body\n") -> Path:
    cmd_path = home / ".memtomem" / "commands" / f"{name}.md"
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    cmd_path.write_text(body)
    return cmd_path


@pytest.mark.anyio
async def test_concurrent_skills_sync_both_succeed(gateway_home: Path, app, client: AsyncClient):
    _make_canonical_skill(gateway_home, "alpha")
    _make_canonical_skill(gateway_home, "beta")

    async def do_sync() -> int:
        resp = await client.post("/api/context/skills/sync")
        return resp.status_code

    s1, s2 = await asyncio.gather(do_sync(), do_sync())
    assert s1 == 200 and s2 == 200


@pytest.mark.anyio
async def test_concurrent_agents_sync_both_succeed(gateway_home: Path, app, client: AsyncClient):
    _make_canonical_agent(gateway_home, "agent1")

    async def do_sync() -> int:
        resp = await client.post("/api/context/agents/sync")
        return resp.status_code

    s1, s2 = await asyncio.gather(do_sync(), do_sync())
    assert s1 == 200 and s2 == 200


@pytest.mark.anyio
async def test_concurrent_commands_sync_both_succeed(gateway_home: Path, app, client: AsyncClient):
    _make_canonical_command(gateway_home, "cmd1")

    async def do_sync() -> int:
        resp = await client.post("/api/context/commands/sync")
        return resp.status_code

    s1, s2 = await asyncio.gather(do_sync(), do_sync())
    assert s1 == 200 and s2 == 200


# ---------------------------------------------------------------------------
# Cross-resource serialisation — single shared lock
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_settings_sync_and_skills_sync_share_lock(
    gateway_home: Path,
    app,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """A settings-sync POST and a skills-sync POST serialise via the same lock.

    Proves the lock isn't keyed per-resource — any two gateway mutators
    are mutually exclusive, which is the whole point of a single
    ``_gateway_lock``.
    """
    rule = _rule("Write", "echo ok")
    _make_canonical_settings(gateway_home, {"PostToolUse": [rule]})
    _make_target_settings(gateway_home, {})
    _make_canonical_skill(gateway_home, "alpha")

    spans: list[tuple[str, float, float]] = []

    import memtomem.web.routes.settings_sync as _ss
    import memtomem.web.routes.context_skills as _cs

    real_generate_settings = _ss.generate_all_settings
    real_generate_skills = _cs.generate_all_skills

    def make_instrumented(label: str, real):
        def wrapped(*args, **kwargs):
            enter = time.perf_counter()
            time.sleep(0.05)
            out = real(*args, **kwargs)
            exit_t = time.perf_counter()
            spans.append((label, enter, exit_t))
            return out

        return wrapped

    monkeypatch.setattr(
        _ss, "generate_all_settings", make_instrumented("settings", real_generate_settings)
    )
    monkeypatch.setattr(
        _cs, "generate_all_skills", make_instrumented("skills", real_generate_skills)
    )

    async def do_settings() -> int:
        resp = await client.post("/api/settings-sync")
        return resp.status_code

    async def do_skills() -> int:
        resp = await client.post("/api/context/skills/sync")
        return resp.status_code

    s1, s2 = await asyncio.gather(do_settings(), do_skills())
    assert s1 == 200 and s2 == 200

    assert len(spans) == 2, spans
    spans.sort(key=lambda s: s[1])
    (_, _, first_exit), (_, second_enter, _) = spans
    assert first_exit <= second_enter + 1e-3, (
        f"settings-sync and skills-sync overlapped: first exit {first_exit}, "
        f"second enter {second_enter}"
    )


# ---------------------------------------------------------------------------
# Lock-held assertion — fails fast if the wrapper is removed
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_gateway_lock_is_held_during_each_post_handler(
    gateway_home: Path,
    app,
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Every wrapped POST handler must hold ``_gateway_lock`` when it runs.

    Stronger than the timing tests: this asserts the lock is *actually
    acquired* inside the handler. If a future refactor drops the
    ``async with _gateway_lock`` wrapper from any handler, this test
    fails immediately rather than passing on coincidental serialisation.
    """
    rule = _rule("Write", "echo ok")
    _make_canonical_settings(gateway_home, {"PostToolUse": [rule]})
    _make_target_settings(gateway_home, {})
    _make_canonical_skill(gateway_home, "alpha")
    _make_canonical_agent(gateway_home, "agent1")
    _make_canonical_command(gateway_home, "cmd1")

    from memtomem.web.routes._locks import _gateway_lock

    observed: dict[str, bool] = {}

    def make_probe(label: str, real):
        def wrapped(*args, **kwargs):
            observed[label] = _gateway_lock.locked()
            return real(*args, **kwargs)

        return wrapped

    import memtomem.web.routes.context_agents as _ca
    import memtomem.web.routes.context_commands as _cc
    import memtomem.web.routes.context_skills as _cs
    import memtomem.web.routes.settings_sync as _ss

    monkeypatch.setattr(
        _ss, "generate_all_settings", make_probe("settings_sync", _ss.generate_all_settings)
    )
    monkeypatch.setattr(
        _cs, "generate_all_skills", make_probe("skills_sync", _cs.generate_all_skills)
    )
    monkeypatch.setattr(
        _ca, "generate_all_agents", make_probe("agents_sync", _ca.generate_all_agents)
    )
    monkeypatch.setattr(
        _cc, "generate_all_commands", make_probe("commands_sync", _cc.generate_all_commands)
    )
    monkeypatch.setattr(
        _cs,
        "extract_skills_to_canonical",
        make_probe("skills_import", _cs.extract_skills_to_canonical),
    )
    monkeypatch.setattr(
        _ca,
        "extract_agents_to_canonical",
        make_probe("agents_import", _ca.extract_agents_to_canonical),
    )
    monkeypatch.setattr(
        _cc,
        "extract_commands_to_canonical",
        make_probe("commands_import", _cc.extract_commands_to_canonical),
    )

    # Every POST that the issue scopes — fire each, assert the lock
    # was held when the inner generator ran.
    assert (await client.post("/api/settings-sync")).status_code == 200
    assert (await client.post("/api/context/skills/sync")).status_code == 200
    assert (await client.post("/api/context/agents/sync")).status_code == 200
    assert (await client.post("/api/context/commands/sync")).status_code == 200
    assert (await client.post("/api/context/skills/import")).status_code == 200
    assert (await client.post("/api/context/agents/import")).status_code == 200
    assert (await client.post("/api/context/commands/import")).status_code == 200

    expected = {
        "settings_sync",
        "skills_sync",
        "agents_sync",
        "commands_sync",
        "skills_import",
        "agents_import",
        "commands_import",
    }
    missing = expected - set(observed)
    assert not missing, f"handlers never invoked the inner generator: {missing}"

    not_locked = [k for k, v in observed.items() if not v]
    assert not not_locked, f"lock NOT held during: {not_locked}"


# ---------------------------------------------------------------------------
# Lock object identity — every route imports the same singleton
# ---------------------------------------------------------------------------


def test_gateway_lock_is_module_singleton():
    """All gateway routes import the same ``_gateway_lock`` instance."""
    from memtomem.web.routes import _locks
    from memtomem.web.routes.context_agents import _gateway_lock as ca_lock
    from memtomem.web.routes.context_commands import _gateway_lock as cc_lock
    from memtomem.web.routes.context_skills import _gateway_lock as cs_lock
    from memtomem.web.routes.settings_sync import _gateway_lock as ss_lock

    assert ss_lock is _locks._gateway_lock
    assert cs_lock is _locks._gateway_lock
    assert cc_lock is _locks._gateway_lock
    assert ca_lock is _locks._gateway_lock


def test_config_lock_is_module_singleton():
    """``system.py`` imports the same ``_config_lock`` from ``_locks``."""
    from memtomem.web.routes import _locks
    from memtomem.web.routes.system import _config_lock as sys_lock

    assert sys_lock is _locks._config_lock
