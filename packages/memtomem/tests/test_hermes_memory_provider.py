"""The memtomem-memory Hermes provider (packages/memtomem-hermes-memory) against fake Hermes.

Hermes is not installed in CI; tools/hermes_memory_fakes.py stands in for the three modules the
provider imports. The provider is loaded from its file under a synthetic module name, the way
Hermes loads a user memory provider, and loading it twice gives two module copies (two
profiles). Outcomes are read from the provider's DEBUG ``prefetch`` events, the same events the
gateway evidence runs read.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_PROVIDER = _ROOT / "packages/memtomem-hermes-memory/__init__.py"
_FAKES = _ROOT / "tools/hermes_memory_fakes.py"
_SMOKE = _ROOT / "tools/hermes_memory_py311_smoke.py"
_LOGGER = "memtomem_memory"
_REGISTRY = "_memtomem_memory_admission"
_QUERY = "what did we decide about the zebra-quokka migration"
# Upper bound for "returned without waiting for the blocked work": far below the 30 s the fake
# gates hold a call or a config read, far above runner jitter (a macOS CI runner took 0.3503 s
# for a 0.3 s budget). The timeout the hook gives its wait is pinned exactly by the ``waits``
# fixture instead; how close a real hook stays to its budget is measured on a real gateway.
_PROMPT_S = 1.0


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("_hermes_memory_fakes", _FAKES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve annotations through sys.modules[cls.__module__]
    monkeypatch.setitem(sys.modules, "_hermes_memory_fakes", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes(monkeypatch: pytest.MonkeyPatch, fakes: Any) -> Any:
    state = fakes.FakeHermes()
    fakes.install(monkeypatch.setitem, state)
    monkeypatch.delitem(sys.modules, _REGISTRY, raising=False)
    yield state
    # Never leave a worker parked on a gate past the test.
    if state.call_gate is not None:
        state.call_gate.set()
    if state.config_gate is not None:
        state.config_gate.set()
    if state.manager_gate is not None:
        state.manager_gate.set()


@pytest.fixture
def load(monkeypatch: pytest.MonkeyPatch, hermes: Any) -> Any:
    count = iter(range(100))

    def _load(name: str = "") -> Any:
        name = name or f"_hermes_user_memory.memtomem-memory__source_{next(count):016d}"
        spec = importlib.util.spec_from_file_location(name, _PROVIDER)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    return _load


@pytest.fixture
def mod(load: Any) -> Any:
    return load()


@pytest.fixture
def clock(mod: Any) -> Clock:
    fake = Clock()
    mod._registry().clock = fake
    return fake


class RecordingEvent(threading.Event):
    def __init__(self, seen: list[float | None]) -> None:
        super().__init__()
        self._seen = seen

    def wait(self, timeout: float | None = None) -> bool:
        self._seen.append(timeout)
        return super().wait(timeout)


@pytest.fixture
def waits(monkeypatch: pytest.MonkeyPatch, mod: Any) -> list[float | None]:
    """Every timeout passed to any slot's completion wait, in order, without timing anything."""
    seen: list[float | None] = []

    class RecordedSlot(mod._Slot):
        def __init__(self) -> None:
            super().__init__()
            self.done = RecordingEvent(seen)

    monkeypatch.setattr(mod, "_Slot", RecordedSlot)
    return seen


@pytest.fixture
def events(caplog: pytest.LogCaptureFixture) -> Any:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    def _events(kind: str = "prefetch") -> list[dict]:
        found = []
        for record in caplog.records:
            if record.name != _LOGGER or record.levelno != logging.DEBUG:
                continue
            event = json.loads(record.getMessage())
            if event["event"] == kind:
                found.append(event)
        return found

    return _events


def outcome(events: Any) -> str:
    return events()[-1]["outcome"]


def warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING
    ]


def make(mod: Any, hermes: Any, **settings: Any) -> Any:
    """An initialized provider; *settings* go under memory.memtomem-memory."""
    if settings:
        hermes.config.setdefault("memory", {})["memtomem-memory"] = dict(settings)
    provider = mod.MemtomemMemoryProvider()
    provider.initialize("session-1", hermes_home="/tmp/hermes", platform="cli")
    return provider


def drain(mod: Any, timeout: float = 5.0) -> None:
    registry = mod._registry()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with registry.lock:
            if registry.total_inflight == 0:
                return
        time.sleep(0.005)
    raise AssertionError(f"{registry.total_inflight} recall calls still in flight")


def admission(mod: Any, server: str = "memtomem") -> Any:
    return mod._registry().admissions[server]


def in_thread(fn: Any, *args: Any, **kwargs: Any) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def run() -> None:
        box["value"] = fn(*args, **kwargs)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


# --- admission: one outstanding call per entry name ---------------------------------------------


def test_any_other_instance_is_busy_while_one_call_is_outstanding(
    mod: Any, load: Any, hermes: Any, events: Any
) -> None:
    gate = hermes.block_calls()
    first = make(mod, hermes, budget_ms=50)
    assert first.prefetch(_QUERY) == ""
    assert outcome(events) == "hook_timeout"
    same_copy = make(mod, hermes, budget_ms=50)
    assert same_copy.prefetch(_QUERY) == ""
    assert outcome(events) == "skipped_busy"
    other_copy = load()
    patient = make(other_copy, hermes, budget_ms=5000)
    started = time.perf_counter()
    assert patient.prefetch(_QUERY) == ""
    assert time.perf_counter() - started < _PROMPT_S
    assert outcome(events) == "skipped_busy"
    assert len(hermes.calls) == 1
    gate.set()
    drain(mod)
    assert same_copy.prefetch(_QUERY).startswith(mod._HEADER)
    assert len(hermes.calls) == 2


def test_a_detached_instance_keeps_the_slot_until_its_call_ends(
    mod: Any, hermes: Any, events: Any
) -> None:
    gate = hermes.block_calls()
    old = make(mod, hermes, budget_ms=50)
    old.prefetch(_QUERY)
    old.shutdown()
    new = make(mod, hermes, budget_ms=50)
    new.prefetch(_QUERY)
    assert outcome(events) == "skipped_busy"
    gate.set()
    drain(mod)
    assert admission(mod).slot is None
    assert new.prefetch(_QUERY) != ""


def test_a_burst_of_turns_makes_exactly_one_call(mod: Any, hermes: Any, events: Any) -> None:
    hermes.block_calls()
    providers = [make(mod, hermes, budget_ms=50) for _ in range(3)]
    threads = []
    for provider in providers:
        threads.append(in_thread(provider.prefetch, _QUERY)[0])
        time.sleep(0.05)
    for thread in threads:
        thread.join(5)
    assert len(hermes.calls) == 1
    assert sorted(e["outcome"] for e in events()) == [
        "hook_timeout",
        "skipped_busy",
        "skipped_busy",
    ]


def test_the_process_ceiling_is_32_calls_across_names(mod: Any, hermes: Any, events: Any) -> None:
    assert mod._PROCESS_CEILING == 32
    assert mod._registry().ceiling == 32
    names = [f"memtomem-{i}" for i in range(33)]
    hermes.config["mcp_servers"] = {name: {} for name in names}
    hermes.config["plugins"]["entries"]["memtomem-memory"]["mcp_allowlist"] = names
    hermes.block_calls()
    providers = [make(mod, hermes, server=name, budget_ms=50) for name in names]
    for provider in providers[:32]:
        provider.prefetch(_QUERY)
    deadline = time.monotonic() + 5
    while len(hermes.calls) < 32 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(hermes.calls) == 32
    providers[32].prefetch(_QUERY)
    assert outcome(events) == "skipped_cap"
    assert mod._registry().total_inflight == 32


def test_cooldown_backs_off_15_60_then_300_seconds(
    mod: Any, hermes: Any, clock: Clock, events: Any
) -> None:
    hermes.default = lambda call: {"ok": False, "error": "timed out"}
    provider = make(mod, hermes)
    for step in (15, 60, 300, 300):
        provider.prefetch(_QUERY)
        assert outcome(events) == "rpc_error"
        drain(mod)
        assert admission(mod).cooldown_until - clock.now == pytest.approx(step)
        clock.advance(step - 0.01)
        provider.prefetch(_QUERY)
        assert outcome(events) == "skipped_cooldown"
        clock.advance(0.02)
    assert len(hermes.calls) == 4


def test_each_failure_uses_the_submitting_instances_schedule(
    mod: Any, load: Any, hermes: Any, clock: Clock
) -> None:
    hermes.default = lambda call: {"ok": False, "error": "timed out"}
    fast = make(mod, hermes, cooldown_seconds=5, long_cooldown_seconds=7, max_cooldown_seconds=9)
    slow = make(load(), hermes, cooldown_seconds=15)  # 15/60/300
    for provider, expected in ((fast, 5), (slow, 60), (fast, 9), (slow, 300)):
        provider.prefetch(_QUERY)
        drain(mod)
        assert admission(mod).cooldown_until - clock.now == pytest.approx(expected)
        clock.advance(expected)
    assert len(hermes.calls) == 4


def test_the_hook_budget_is_initialization_only(mod: Any, hermes: Any, waits: list) -> None:
    gate = hermes.block_calls()
    short = make(mod, hermes, budget_ms=50)
    hermes.config["memory"]["memtomem-memory"]["budget_ms"] = 2000
    started = time.perf_counter()
    short.prefetch(_QUERY)
    assert time.perf_counter() - started < _PROMPT_S
    assert waits[0] is not None and waits[0] <= 0.05
    gate.set()
    drain(mod)
    hermes.call_gate = threading.Event()
    longer = make(mod, hermes)
    started = time.perf_counter()
    longer.prefetch(_QUERY)
    assert time.perf_counter() - started >= 1.9
    hermes.call_gate.set()


def test_a_success_resets_failures_and_cooldown(
    mod: Any, hermes: Any, clock: Clock, events: Any, fakes: Any
) -> None:
    hermes.respond({"ok": False, "error": "x"}, {"ok": False, "error": "x"})
    provider = make(mod, hermes)
    provider.prefetch(_QUERY)
    clock.advance(15)
    provider.prefetch(_QUERY)
    assert admission(mod).failures == 2
    clock.advance(60)
    assert provider.prefetch(_QUERY) != ""
    assert (admission(mod).failures, admission(mod).cooldown_until) == (0, 0.0)
    hermes.respond({"ok": False, "error": "x"})
    provider.prefetch(_QUERY)
    assert admission(mod).cooldown_until - clock.now == pytest.approx(15)


@pytest.mark.parametrize(
    "answer",
    [
        "ok",
        "error_envelope",
        "permission",
        "exception",
        "truncated",
        "incompatible",
        "refused",
        "config_error",
    ],
)
def test_slot_and_count_are_released_on_every_path(
    mod: Any, hermes: Any, fakes: Any, answer: str
) -> None:
    answers = {
        "ok": fakes.ok_envelope([fakes.chunk(1)]),
        "error_envelope": {"ok": False, "error": "boom"},
        "permission": PermissionError("not allowlisted"),
        "exception": RuntimeError("transport bug"),
        "truncated": {"ok": True, "result": "{", "truncated": True},
        "incompatible": fakes.ok_envelope([fakes.chunk(1)], recorded=None),
    }
    if answer == "refused":
        hermes.config["mcp_servers"]["memtomem"]["enabled"] = False
    elif answer == "config_error":
        provider = make(mod, hermes)
        hermes.config_error = OSError("config unreadable")
    else:
        hermes.respond(answers[answer])
    if answer != "config_error":
        provider = make(mod, hermes)
    provider.prefetch(_QUERY)
    drain(mod)
    assert admission(mod).slot is None
    assert mod._registry().total_inflight == 0
    # A failure backs off; a refusal, an unusable answer or a config read error does not.
    failed = answer in ("error_envelope", "permission", "exception", "incompatible")
    assert admission(mod).failures == (1 if failed else 0)
    assert (admission(mod).cooldown_until > 0) is failed
    called = answer not in ("refused", "config_error")
    assert len(hermes.calls) == (1 if called else 0)


def test_a_worker_that_cannot_start_releases_the_slot(
    mod: Any, hermes: Any, events: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("can't start new thread")

    provider = make(mod, hermes)
    with monkeypatch.context() as patch:
        patch.setattr(mod, "spawn_context_thread", refuse)
        assert provider.prefetch(_QUERY) == ""
    assert outcome(events) == "spawn_failed"
    assert admission(mod).slot is None
    assert mod._registry().total_inflight == 0
    assert provider.prefetch(_QUERY) != ""


def test_two_module_copies_share_one_admission_per_name(
    mod: Any, load: Any, hermes: Any, events: Any
) -> None:
    other = load()
    hermes.block_calls()
    make(mod, hermes, budget_ms=50).prefetch(_QUERY)
    make(other, hermes, budget_ms=50).prefetch(_QUERY)
    assert outcome(events) == "skipped_busy"
    assert other._registry() is mod._registry()
    assert list(mod._registry().admissions) == ["memtomem"]
    assert len(hermes.calls) == 1


def test_an_existing_registry_is_adopted_not_replaced(load: Any, hermes: Any) -> None:
    first = load()
    registry = first._registry()
    second = load()
    assert second._registry() is registry
    assert sys.modules[_REGISTRY] is registry


# --- the config preflight runs on the worker, on every submission --------------------------------


def test_a_blocked_config_read_never_blocks_the_hook(mod: Any, hermes: Any, events: Any) -> None:
    provider = make(mod, hermes, budget_ms=100)
    hermes.config_gate = threading.Event()
    started = time.perf_counter()
    assert provider.prefetch(_QUERY) == ""
    assert time.perf_counter() - started < _PROMPT_S
    assert outcome(events) == "hook_timeout"
    provider.prefetch(_QUERY)
    assert outcome(events) == "skipped_busy"
    assert hermes.calls == []
    hermes.config_gate.set()
    drain(mod)
    assert admission(mod).slot is None


def _no_native(hermes: Any) -> None:
    del hermes.config["mcp_servers"]["memtomem"]


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda h: h.config["mcp_servers"]["memtomem"].update(enabled=False), "skipped_disabled"),
        (lambda h: h.config["mcp_servers"]["memtomem"].update(enabled="off"), "skipped_disabled"),
        (lambda h: h.config["mcp_servers"]["memtomem"].update(enabled=0), "skipped_disabled"),
        (
            lambda h: h.config["mcp_servers"]["memtomem"].update(trust=" UNTRUSTED "),
            "skipped_untrusted",
        ),
        (lambda h: h.config["mcp_servers"]["memtomem"].update(trust="typo"), "skipped_untrusted"),
        (
            lambda h: h.config["plugins"]["entries"]["memtomem-memory"].pop("mcp_allowlist"),
            "skipped_not_allowlisted",
        ),
        (
            lambda h: h.config["plugins"]["entries"]["memtomem-memory"].update(
                mcp_allowlist="memtomem"
            ),
            "skipped_not_allowlisted",
        ),
        (_no_native, "skipped_no_entry"),
        (
            lambda h: (_no_native(h), setattr(h, "portable", {"other": {}})),
            "skipped_no_entry",
        ),
    ],
)
def test_preflight_refusals_skip_without_a_call_or_a_cooldown(
    mod: Any, hermes: Any, events: Any, caplog: pytest.LogCaptureFixture, change: Any, expected: str
) -> None:
    provider = make(mod, hermes)
    change(hermes)
    for _ in range(3):
        assert provider.prefetch(_QUERY) == ""
        assert outcome(events) == expected
    assert hermes.calls == []
    assert admission(mod).failures == 0
    assert admission(mod).cooldown_until == 0.0
    assert len(warnings(caplog)) == 1


def test_a_portable_entry_is_called_when_no_native_entry_exists(
    mod: Any, hermes: Any, events: Any
) -> None:
    _no_native(hermes)
    hermes.portable = {"memtomem": {"command": "uvx"}}
    assert make(mod, hermes).prefetch(_QUERY) != ""


def test_an_unreadable_portable_registry_does_not_refuse(mod: Any, hermes: Any) -> None:
    _no_native(hermes)
    hermes.portable = RuntimeError("not in the plugin contract")
    assert make(mod, hermes).prefetch(_QUERY) != ""


def test_restoring_trust_is_honoured_on_the_next_turn(mod: Any, hermes: Any, events: Any) -> None:
    provider = make(mod, hermes)
    hermes.config["mcp_servers"]["memtomem"]["trust"] = "untrusted"
    provider.prefetch(_QUERY)
    assert outcome(events) == "skipped_untrusted"
    hermes.config["mcp_servers"]["memtomem"]["trust"] = "full"
    assert provider.prefetch(_QUERY) != ""


def test_a_refusal_warns_once_even_between_another_profiles_successes(
    mod: Any, load: Any, hermes: Any, fakes: Any, caplog: pytest.LogCaptureFixture
) -> None:
    untrusted = fakes.default_config()
    untrusted["mcp_servers"]["memtomem"]["trust"] = "untrusted"
    hermes.profiles["b"] = untrusted
    healthy = make(mod, hermes)
    token = fakes.PROFILE.set("b")
    try:
        refused = make(load(), hermes)
    finally:
        fakes.PROFILE.reset(token)
    for _ in range(3):
        assert healthy.prefetch(_QUERY) != ""
        token = fakes.PROFILE.set("b")
        try:
            refused.prefetch(_QUERY)
        finally:
            fakes.PROFILE.reset(token)
    assert len(warnings(caplog)) == 1


def test_one_profiles_untrusted_entry_does_not_refuse_another_profile(
    mod: Any, load: Any, hermes: Any, fakes: Any, events: Any
) -> None:
    other = load()
    untrusted = fakes.default_config()
    untrusted["mcp_servers"]["memtomem"]["trust"] = "untrusted"
    hermes.profiles["b"] = untrusted
    profile_a = make(mod, hermes)
    token = fakes.PROFILE.set("b")
    try:
        profile_b = make(other, hermes)
        profile_b.prefetch(_QUERY)
        assert outcome(events) == "skipped_untrusted"
    finally:
        fakes.PROFILE.reset(token)
    assert profile_a.prefetch(_QUERY) != ""
    token = fakes.PROFILE.set("b")
    try:
        profile_b.prefetch(_QUERY)
        assert outcome(events) == "skipped_untrusted"
    finally:
        fakes.PROFILE.reset(token)


# --- settings are read once, at initialize --------------------------------------------------------


def test_settings_changed_after_initialize_do_not_apply(mod: Any, hermes: Any) -> None:
    hermes.config["mcp_servers"]["other"] = {}
    hermes.config["plugins"]["entries"]["memtomem-memory"]["mcp_allowlist"] = ["memtomem", "other"]
    provider = make(mod, hermes, top_k=3)
    hermes.config["memory"]["memtomem-memory"] = {"server": "other", "top_k": 9}
    provider.prefetch(_QUERY)
    assert (hermes.calls[-1].server, hermes.calls[-1].arguments["top_k"]) == ("memtomem", 3)
    make(mod, hermes).prefetch(_QUERY)
    assert (hermes.calls[-1].server, hermes.calls[-1].arguments["top_k"]) == ("other", 9)


def test_defaults_and_the_call_arguments(mod: Any, hermes: Any) -> None:
    provider = make(mod, hermes)
    assert provider._settings == mod._DEFAULTS
    provider.prefetch(_QUERY)
    call = hermes.calls[-1]
    assert (call.server, call.tool, call.timeout) == ("memtomem", "mem_search", 10.0)
    assert call.arguments == {
        "query": _QUERY,
        "top_k": 5,
        "record": False,
        "rerank": False,
        "output_format": "structured",
    }
    assert call.thread == "memtomem-memory-call"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("server", "memtomem-work"),
        ("budget_ms", 1200),
        ("top_k", 20),
        ("max_chars", 9000),
        ("rpc_timeout_seconds", 600),
        ("cooldown_seconds", 1),
        ("long_cooldown_seconds", 600),
        ("max_cooldown_seconds", 600),
    ],
)
def test_a_valid_setting_is_taken(
    mod: Any, hermes: Any, caplog: pytest.LogCaptureFixture, key: str, value: Any
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    provider = make(mod, hermes, **{key: value})
    assert provider._settings[key] == value
    assert warnings(caplog) == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("server", ""),
        ("server", 7),
        ("budget_ms", 49),
        ("budget_ms", True),
        ("top_k", 0),
        ("top_k", 2.5),
        ("max_chars", 9001),
        ("rpc_timeout_seconds", 0.5),
        ("cooldown_seconds", 301),
        ("long_cooldown_seconds", 601),
        ("max_cooldown_seconds", "300"),
    ],
)
def test_an_invalid_setting_falls_back_with_a_warning(
    mod: Any, hermes: Any, caplog: pytest.LogCaptureFixture, key: str, value: Any
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    provider = make(mod, hermes, **{key: value})
    assert provider._settings[key] == mod._DEFAULTS[key]
    assert len(warnings(caplog)) == 1


def test_the_cooldown_schedule_never_shrinks(mod: Any, hermes: Any) -> None:
    provider = make(mod, hermes, cooldown_seconds=120, long_cooldown_seconds=90)
    assert provider._settings["long_cooldown_seconds"] == 120
    provider = make(mod, hermes, cooldown_seconds=200, long_cooldown_seconds=400)
    assert provider._settings["max_cooldown_seconds"] == 400


def test_the_server_setting_names_the_entry_called(mod: Any, hermes: Any) -> None:
    hermes.config["mcp_servers"] = {"mm-work": {}}
    hermes.config["plugins"]["entries"]["memtomem-memory"]["mcp_allowlist"] = ["mm-work"]
    make(mod, hermes, server="mm-work").prefetch(_QUERY)
    assert hermes.calls[-1].server == "mm-work"


# --- envelopes and the contract echo ------------------------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        # Valid JSON with the contract echo: only the truncation marker makes it unusable.
        {"ok": True, "result": '{"results": [], "recorded": false}', "truncated": True},
        {"ok": True, "result": "Error: something went wrong"},
        {"ok": True, "result": "[1, 2]"},
        {"ok": True, "result": '{"results": "none", "recorded": false}'},
    ],
)
def test_an_unparsed_answer_is_no_recall_and_not_a_failure(
    mod: Any, hermes: Any, events: Any, envelope: dict
) -> None:
    hermes.respond(envelope)
    assert make(mod, hermes).prefetch(_QUERY) == ""
    assert outcome(events) == "rpc_unparsed"
    assert admission(mod).failures == 0
    assert admission(mod).cooldown_until == 0.0


def test_an_error_envelope_is_a_failure(mod: Any, hermes: Any, events: Any) -> None:
    hermes.respond({"ok": False, "error": "MCP server 'memtomem' is not connected"})
    assert make(mod, hermes).prefetch(_QUERY) == ""
    assert admission(mod).failures == 1


def test_a_permission_error_backs_off_warns_once_and_recovers(
    mod: Any, hermes: Any, clock: Clock, events: Any, caplog: pytest.LogCaptureFixture
) -> None:
    hermes.respond(PermissionError("no"), PermissionError("no"))
    provider = make(mod, hermes)
    provider.prefetch(_QUERY)
    assert admission(mod).cooldown_until - clock.now == pytest.approx(15)
    clock.advance(15)
    provider.prefetch(_QUERY)
    assert admission(mod).failures == 2
    assert len(warnings(caplog)) == 1
    assert "mcp_allowlist" in warnings(caplog)[0]
    clock.advance(60)
    assert provider.prefetch(_QUERY) != ""


def test_a_structured_result_object_is_delivered(mod: Any, hermes: Any, fakes: Any) -> None:
    hermes.respond(fakes.ok_envelope([fakes.chunk(1)], as_text=False))
    assert make(mod, hermes).prefetch(_QUERY) != ""


@pytest.mark.parametrize("recorded", [None, True, "false", 0])
def test_a_server_without_the_echo_is_refused(
    mod: Any,
    hermes: Any,
    fakes: Any,
    clock: Clock,
    events: Any,
    caplog: pytest.LogCaptureFixture,
    recorded: Any,
) -> None:
    hermes.default = lambda call: fakes.ok_envelope([fakes.chunk(1)], recorded=recorded)
    provider = make(mod, hermes)
    assert provider.prefetch(_QUERY) == ""
    assert outcome(events) == "incompatible"
    assert admission(mod).incompatible is True
    assert admission(mod).cooldown_until - clock.now == pytest.approx(15)
    clock.advance(15)
    assert provider.prefetch(_QUERY) == ""
    assert len(warnings(caplog)) == 1
    assert "0.6.8" in warnings(caplog)[0]
    hermes.default = lambda call: fakes.ok_envelope([fakes.chunk(1)])
    clock.advance(60)
    assert provider.prefetch(_QUERY) != ""
    assert admission(mod).incompatible is False


def test_a_dropped_semantic_leg_is_still_delivered(
    mod: Any, hermes: Any, fakes: Any, events: Any
) -> None:
    hint = "semantic search failed for this query — results may be incomplete"
    hermes.respond(fakes.ok_envelope([fakes.chunk(1)], hints=[hint]))
    assert make(mod, hermes).prefetch(_QUERY) != ""
    assert events("rpc_done")[-1]["dense_failed"] is True


# --- fencing and status --------------------------------------------------------------------------


@pytest.mark.parametrize("fence", ["reset", "shutdown"])
def test_a_result_arriving_after_a_fence_is_dropped(
    mod: Any, hermes: Any, events: Any, fence: str
) -> None:
    gate = hermes.block_calls()
    provider = make(mod, hermes, budget_ms=3000)
    thread, box = in_thread(provider.prefetch, _QUERY)
    assert hermes.call_started.wait(5)
    if fence == "reset":
        provider.on_session_switch("session-2", reset=True)
    else:
        provider.shutdown()
    gate.set()
    thread.join(5)
    assert box["value"] == ""
    assert outcome(events) == "stale"
    assert provider.recall_status() is None


@pytest.mark.parametrize("newer", [_QUERY, "ok"], ids=["busy", "trivial"])
def test_a_newer_turn_on_the_same_instance_fences_an_older_result(
    mod: Any, hermes: Any, events: Any, newer: str
) -> None:
    gate = hermes.block_calls()
    provider = make(mod, hermes, budget_ms=3000)
    thread, box = in_thread(provider.prefetch, _QUERY)
    assert hermes.call_started.wait(5)
    assert provider.prefetch(newer) == ""
    gate.set()
    thread.join(5)
    assert box["value"] == ""
    assert events()[-1]["outcome"] == "stale"
    assert provider.recall_status() is None


def test_a_session_switch_without_reset_does_not_fence(mod: Any, hermes: Any) -> None:
    gate = hermes.block_calls()
    provider = make(mod, hermes, budget_ms=3000)
    thread, box = in_thread(provider.prefetch, _QUERY)
    assert hermes.call_started.wait(5)
    provider.on_session_switch("session-2", reset=False)
    gate.set()
    thread.join(5)
    assert box["value"] != ""


def test_recall_status_reflects_only_the_last_prefetch(mod: Any, hermes: Any, fakes: Any) -> None:
    provider = make(mod, hermes)
    hermes.respond(fakes.ok_envelope([fakes.chunk(1), fakes.chunk(2)]), fakes.ok_envelope([]))
    provider.prefetch(_QUERY)
    status = provider.recall_status()
    assert status is not None and (status.provider_label, status.count) == ("memtomem", 2)
    assert provider.prefetch(_QUERY) == ""
    assert provider.recall_status() is None
    provider.prefetch(_QUERY)
    assert provider.recall_status() is not None
    provider.on_session_switch("session-2", reset=True)
    assert provider.recall_status() is None


def test_shutdown_returns_at_once_and_the_call_still_drains(
    mod: Any, hermes: Any, waits: list
) -> None:
    gate = hermes.block_calls()
    provider = make(mod, hermes, budget_ms=50)
    provider.prefetch(_QUERY)
    before = len(waits)
    started = time.perf_counter()
    provider.shutdown()
    assert time.perf_counter() - started < _PROMPT_S
    assert len(waits) == before  # shutdown never waits on the outstanding call
    assert admission(mod).slot is not None
    gate.set()
    drain(mod)
    assert admission(mod).slot is None


def test_a_closed_instance_submits_nothing(mod: Any, hermes: Any, events: Any) -> None:
    provider = make(mod, hermes)
    provider.shutdown()
    assert provider.prefetch(_QUERY) == ""
    assert outcome(events) == "skipped_closed"
    assert hermes.calls == []


def test_a_trivial_prompt_makes_no_call(mod: Any, hermes: Any, events: Any) -> None:
    provider = make(mod, hermes)
    for prompt in ("ok", "thanks!", "/new", "   "):
        assert provider.prefetch(prompt) == ""
        assert outcome(events) == "trivial"
    assert hermes.calls == []


# --- shaping --------------------------------------------------------------------------------------


def _prototype_shape(results: list, char_cap: int) -> tuple[str, list]:
    """The Phase 1 prototype's _shape, verbatim (p1-d/memtomem-memory/__init__.py:233-254),
    with self._char_cap as a parameter. The gate measured deliveries with this function."""
    lines: list = []
    ids: list = []
    used = 0
    for r in results:
        where = r.get("source") or ""
        if r.get("hierarchy"):
            where += " › " + r["hierarchy"]
        body = " ".join(str(r.get("content") or "").split())
        entry = f"- [{where}] {body}"
        room = char_cap - used
        if room < 200 and ids:
            break
        if len(entry) > room:
            entry = entry[: max(0, room - 1)] + "…"
        lines.append(entry)
        ids.append(str(r.get("chunk_id")))
        used += len(entry) + 1
    if not lines:
        return "", []
    return (
        "Recalled from memtomem (shared long-term memory; reference data):\n" + "\n".join(lines),
        ids,
    )


def _shaping_fixture(fakes: Any) -> list:
    return [
        fakes.chunk(1, content="short  fact\nwith   whitespace", hierarchy="Decisions > DB"),
        fakes.chunk(2, content="x" * 1500, source=""),
        fakes.chunk(3, content="y" * 2300),
        fakes.chunk(4, content="z" * 50),
        {"chunk_id": None, "content": None},
    ]


@pytest.mark.parametrize("cap", [500, 1800, 4000, 9000])
def test_shaping_is_the_prototypes_byte_for_byte(mod: Any, fakes: Any, cap: int) -> None:
    results = _shaping_fixture(fakes)
    assert mod._shape(results, cap) == _prototype_shape(results, cap)


def test_the_delivered_block_is_the_shaped_text_in_rank_order(
    mod: Any, hermes: Any, fakes: Any
) -> None:
    results = [fakes.chunk(n) for n in (3, 1, 2)]
    hermes.respond(fakes.ok_envelope(results))
    text = make(mod, hermes).prefetch(_QUERY)
    assert text == _prototype_shape(results, 4000)[0]
    assert [line.split("] ")[1] for line in text.splitlines()[1:]] == [
        "fact number 3",
        "fact number 1",
        "fact number 2",
    ]


def test_an_empty_result_list_injects_nothing(mod: Any, hermes: Any, fakes: Any, events: Any):
    hermes.respond(fakes.ok_envelope([]))
    assert make(mod, hermes).prefetch(_QUERY) == ""
    assert outcome(events) == "no_results"


# --- availability (config and imports only) -------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (
            lambda h: h.config["plugins"]["entries"]["memtomem-memory"].pop("mcp_allowlist"),
            "plugins.entries.memtomem-memory.mcp_allowlist",
        ),
        (lambda h: h.config["mcp_servers"]["memtomem"].update(enabled="no"), "disabled"),
        (lambda h: h.config["mcp_servers"]["memtomem"].update(trust="untrusted"), "trust: full"),
        (lambda h: h.config["mcp_servers"]["memtomem"].update(trust="typo"), "trust: full"),
        (
            lambda h: (_no_native(h), setattr(h, "portable", {"other": {}})),
            "no MCP entry named 'memtomem'",
        ),
    ],
)
def test_unavailable_with_the_reason(mod: Any, hermes: Any, change: Any, reason: str) -> None:
    change(hermes)
    provider = mod.MemtomemMemoryProvider()
    assert provider.is_available() is False
    assert reason in provider.unavailable_reason()


@pytest.mark.parametrize(
    "portable",
    [{}, {"memtomem": {"command": "uvx"}}, RuntimeError("lookup failed")],
    ids=["empty-registry-is-uncertain", "portable-entry", "lookup-raises"],
)
def test_available_without_a_native_entry(mod: Any, hermes: Any, portable: Any) -> None:
    _no_native(hermes)
    hermes.portable = portable
    assert mod.MemtomemMemoryProvider().is_available() is True


def test_available_probe_has_no_side_effects(mod: Any, hermes: Any) -> None:
    provider = mod.MemtomemMemoryProvider()
    assert provider.is_available() is True
    assert hermes.calls == []
    assert hermes.contexts_built == 0
    assert _REGISTRY not in sys.modules


def test_unavailable_without_hermes_plugin_mcp_access(
    mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", None)
    provider = mod.MemtomemMemoryProvider()
    assert provider.is_available() is False
    assert "0.21.5" in provider.unavailable_reason()


def test_the_availability_probe_reads_the_server_setting(mod: Any, hermes: Any) -> None:
    hermes.config["memory"] = {"memtomem-memory": {"server": "mm-work"}}
    assert "'mm-work'" in mod.MemtomemMemoryProvider().unavailable_reason()


# --- register and the plugin context --------------------------------------------------------------


def test_register_builds_its_own_context_when_the_collector_lacks_call_mcp(
    mod: Any, hermes: Any, fakes: Any
) -> None:
    collector = fakes.Collector()
    mod.register(collector)
    provider = collector.provider
    assert provider.name == "memtomem-memory"
    assert hermes.contexts_built == 0
    provider.initialize("s", hermes_home="/tmp/h", platform="cli")
    provider.prefetch(_QUERY)
    provider.prefetch(_QUERY)
    assert hermes.contexts_built == 1
    assert [call.plugin_id for call in hermes.calls] == ["memtomem-memory"] * 2


def test_building_the_plugin_context_never_blocks_the_hook(
    mod: Any, hermes: Any, events: Any
) -> None:
    provider = make(mod, hermes, budget_ms=50)
    hermes.manager_gate = threading.Event()
    started = time.perf_counter()
    assert provider.prefetch(_QUERY) == ""
    assert time.perf_counter() - started < _PROMPT_S
    assert outcome(events) == "hook_timeout"
    assert hermes.calls == []
    hermes.manager_gate.set()
    drain(mod)
    assert len(hermes.calls) == 1


def test_a_context_that_cannot_be_built_is_a_refusal(
    mod: Any, hermes: Any, events: Any, caplog: pytest.LogCaptureFixture, monkeypatch: Any
) -> None:
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", None)
    provider = make(mod, hermes)
    for _ in range(2):
        assert provider.prefetch(_QUERY) == ""
        assert outcome(events) == "skipped_no_context"
    assert admission(mod).failures == 0
    assert len(warnings(caplog)) == 1


def test_register_uses_a_collector_that_forwards_call_mcp(mod: Any, hermes: Any, fakes: Any):
    seen: list = []

    class Forwarding(fakes.Collector):
        def call_mcp(self, server: str, tool: str, arguments: Any = None, timeout: float = 30):
            seen.append((server, tool))
            return fakes.ok_envelope([fakes.chunk(1)])

    collector = Forwarding()
    mod.register(collector)
    collector.provider.initialize("s", hermes_home="/tmp/h", platform="cli")
    assert collector.provider.prefetch(_QUERY) != ""
    assert seen == [("memtomem", "mem_search")]
    assert hermes.contexts_built == 0


def test_a_recall_only_provider_offers_nothing_else(mod: Any) -> None:
    provider = mod.MemtomemMemoryProvider()
    assert provider.get_tool_schemas() == []
    assert provider.get_config_schema() == []
    assert provider.backup_paths() == []
    assert provider.system_prompt_block() == ""
    assert provider.queue_prefetch(_QUERY) is None


# --- hook timing, logging, Hermes semantics, 3.11 ---------------------------------------------------


def test_the_hook_returns_within_its_budget(mod: Any, hermes: Any, events: Any) -> None:
    hermes.block_calls()
    provider = make(mod, hermes)
    started = time.perf_counter()
    assert provider.prefetch(_QUERY) == ""
    assert time.perf_counter() - started < _PROMPT_S
    assert outcome(events) == "hook_timeout"


@pytest.mark.parametrize("budget_ms", [50, 300])
def test_the_hook_waits_at_most_its_budget(
    mod: Any, hermes: Any, waits: list, budget_ms: int
) -> None:
    gate = hermes.block_calls()
    provider = make(mod, hermes, budget_ms=budget_ms)
    provider.prefetch(_QUERY)
    assert len(waits) == 1
    assert waits[0] is not None and waits[0] <= budget_ms / 1000
    gate.set()
    drain(mod)


def test_no_log_record_carries_the_query(
    mod: Any, hermes: Any, fakes: Any, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    provider = make(mod, hermes)
    hermes.respond(
        fakes.ok_envelope([fakes.chunk(1)]),
        # Errors that quote the arguments, as Hermes's and the SDK's can.
        lambda call: {"ok": False, "error": f"mem_search({call.arguments}) failed"},
        fakes.ok_envelope([fakes.chunk(1)], recorded=None),
        lambda call: (_ for _ in ()).throw(RuntimeError(call.arguments["query"])),
    )
    for _ in range(4):
        provider.prefetch(_QUERY)
        drain(mod)
        clock.advance(400)
    hermes.config["mcp_servers"]["memtomem"]["trust"] = "untrusted"
    provider.prefetch(_QUERY)
    drain(mod)
    # Config reads failing with text that happens to carry the query, on the worker and at
    # initialize.
    hermes.config_error = RuntimeError(_QUERY)
    provider.prefetch(_QUERY)
    drain(mod)
    mod.MemtomemMemoryProvider().initialize("s", hermes_home="/tmp/h", platform="cli")
    assert len(hermes.calls) == 4
    assert sum("cannot read" in r.getMessage() for r in caplog.records) == 2
    for record in caplog.records:
        assert "zebra-quokka" not in record.getMessage()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        (True, True),
        (False, False),
        (0, False),
        (1, True),
        (0.0, False),
        ("off", False),
        (" No ", False),
        ("0", False),
        ("YES", True),
        ("on", True),
        ("maybe", True),
        ([], True),
    ],
)
def test_enabled_is_parsed_as_hermes_parses_it(mod: Any, value: Any, expected: bool) -> None:
    assert mod._parse_boolish(value, default=True) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "full"),
        ("full", "full"),
        (" FULL ", "full"),
        ("untrusted", "untrusted"),
        (" UNTRUSTED ", "untrusted"),
        ("typo", "untrusted"),
        (1, "untrusted"),
        (False, "untrusted"),
    ],
)
def test_trust_is_normalised_as_hermes_normalises_it(mod: Any, value: Any, expected: str) -> None:
    assert mod._normalize_server_trust(value) == expected


@pytest.mark.parametrize("path", [_PROVIDER, _FAKES, _SMOKE], ids=lambda p: p.name)
def test_python_311_syntax(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 11))


# --- the real mem_search output, end to end -------------------------------------------------------


async def _real_mem_search(
    monkeypatch: pytest.MonkeyPatch, arguments: dict, results: list, **overrides: Any
) -> str:
    """What this checkout's ``mem_search`` returns for *arguments*, with only the search core
    and the app stubbed (the same seams test_mem_search_wrapper.py stubs)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from memtomem.search.pipeline import RetrievalStats
    from memtomem.server.tools import search as search_mod

    app = MagicMock()
    app.current_namespace = None
    app.webhook_manager = None
    stats = RetrievalStats(final_total=len(results), score_scale="rrf")
    monkeypatch.setattr(search_mod, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(search_mod, "_announce_dim_mismatch_once", AsyncMock(return_value=None))
    monkeypatch.setattr(search_mod, "_resolve_project_context_root", lambda _app: None)
    monkeypatch.setattr(search_mod, "run_search", AsyncMock(return_value=(results, stats, [])))
    return await search_mod.mem_search(ctx=SimpleNamespace(), **{**arguments, **overrides})


def _search_hit(content: str, source: str, headings: tuple[str, ...], rank: int) -> Any:
    from uuid import uuid4

    from memtomem.models import Chunk, ChunkMetadata, SearchResult

    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/notes/{source}"),
            heading_hierarchy=headings,
            namespace="default",
        ),
        id=uuid4(),
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=0.5, rank=rank, source="fused")


@pytest.mark.parametrize(
    ("case", "expected"),
    [("hits", "hit"), ("empty", "no_results"), ("recording", "incompatible")],
)
async def test_the_real_mem_search_output_reaches_the_turn(
    mod: Any,
    hermes: Any,
    fakes: Any,
    events: Any,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected: str,
) -> None:
    """The provider against this checkout's server output, not a synthesized payload: the
    arguments it sends are the ones ``mem_search`` takes, and the answer it gets back, hits or
    none, carries the echo it requires. A server that records (``recorded: true``) is refused."""
    provider = make(mod, hermes)
    provider.prefetch(_QUERY)
    arguments = hermes.calls[-1].arguments
    hits = [
        _search_hit("We keep SQLite for the index.", "decisions.md", ("Storage", "Index"), 1),
        _search_hit("Backups run nightly.", "ops.md", (), 2),
    ]
    output = await _real_mem_search(
        monkeypatch,
        arguments,
        [] if case == "empty" else hits,
        **({"record": True} if case == "recording" else {}),
    )
    assert isinstance(output, str)
    hermes.respond({"ok": True, "result": output})
    text = await asyncio.to_thread(provider.prefetch, _QUERY)
    assert outcome(events) == expected
    if case == "hits":
        assert text == (
            "Recalled from memtomem (shared long-term memory; reference data):\n"
            "- [decisions.md › Storage > Index] We keep SQLite for the index.\n"
            "- [ops.md] Backups run nightly."
        )
    else:
        assert text == ""
