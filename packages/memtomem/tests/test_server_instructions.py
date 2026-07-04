"""Pin tests for the FastMCP ``instructions=`` field.

The instructions string built in ``memtomem/server/instructions.py`` is
auto-injected into every MCP client's ``initialize`` response. For most
LLMs this is the only workflow-level signal they get before deciding
which memtomem tool to call — drift here means clients silently fall
back to docstring-only inference and pick the wrong tool (e.g. plain
``mem_add`` when the user asked for per-agent isolation).

Three layers:

* Per-mode token pins assert each mode's text mentions every workflow
  token an LLM has to recognize. If a tool is renamed or a namespace
  convention changes, both ``server/instructions.py`` and the token
  tables below must move in lockstep.
* An exposure-parity pin asserts every ``mem_*`` tool named in a mode's
  text is actually registered in that mode (#1608) — the default
  ``core`` mode must never show a direct call to a pruned tool.
* An end-to-end test drives the ``initialize`` RPC against a real
  subprocess and asserts the ``instructions`` field round-trips, so a
  future FastMCP release that drops the parameter (or strips it during
  startup) is still caught.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from memtomem.constants import SHARED_NAMESPACE
from memtomem.server import (
    _ALL_REGISTERED_TOOLS,
    _CORE_TOOLS,
    _STANDARD_PACKS,
    _TOOL_MODE,
    mcp,
)
from memtomem.server.instructions import VALID_TOOL_MODES, build_instructions
from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.multi_agent import (
    mem_agent_register,
    mem_agent_search,
    mem_agent_share,
)
from memtomem.server.tools.session import mem_session_end, mem_session_start


def _expected_tools(mode: str) -> frozenset[str]:
    """The individually-exposed tool set for ``mode`` — derived from the
    same sources the pruning in ``server/__init__.py`` uses.

    The standard set intersects with ``_ALL_REGISTERED_TOOLS`` because
    ``ACTIONS`` also carries register-only actions (e.g. ``ingest``)
    that never exist as individual MCP tools in any mode.
    """
    if mode == "full":
        return _ALL_REGISTERED_TOOLS
    if mode == "standard":
        pack_tools = {
            f"mem_{name}" for name, info in ACTIONS.items() if info.category in _STANDARD_PACKS
        }
        return frozenset(_CORE_TOOLS) | (pack_tools & _ALL_REGISTERED_TOOLS)
    return frozenset(_CORE_TOOLS)


# Tokens every mode's text must contain — the single-agent quickstart
# plus the namespace vocabulary.
# ``agent-runtime:`` is a prefix (colon + id); ``shared`` and ``default``
# are exact namespaces with no colon (SHARED_NAMESPACE = "shared").
_COMMON_TOKENS: tuple[str, ...] = (
    "mem_add",
    "mem_search",
    "agent-runtime:",
    "shared",
    "default",
    # ADR-0028 — per-project team shared-bucket override surfaced in the workflow.
    "shared_namespace",
)

# Mode-specific tokens: the multi-agent recipe rendered in the calling
# form that mode actually supports (#1608). ``core`` routes everything
# through mem_do; ``standard`` exposes session/crud tools directly but
# routes multi_agent actions; ``full`` shows direct calls.
_MODE_TOKENS: dict[str, tuple[str, ...]] = {
    "core": (
        "mem_do",
        'action="help"',
        "MEMTOMEM_TOOL_MODE",
        'action="agent_register"',
        'action="session_start"',
        'action="agent_search"',
        'action="agent_share"',
        'action="session_end"',
        'action="batch_add"',
    ),
    "standard": (
        "mem_do",
        'action="help"',
        "MEMTOMEM_TOOL_MODE",
        "mem_session_start",
        "mem_session_end",
        "mem_batch_add",
        'action="agent_register"',
        'action="agent_search"',
        'action="agent_share"',
    ),
    "full": (
        "mem_agent_register",
        "mem_session_start",
        "mem_agent_search",
        "mem_agent_share",
        "mem_session_end",
    ),
}


def test_instructions_is_wired_to_fastmcp() -> None:
    """The mode-selected text from ``server/instructions.py`` reaches
    ``mcp.instructions`` (and therefore the ``initialize`` response).
    Without this, MCP clients have nothing but tool docstrings to go on.
    """
    assert mcp.instructions == build_instructions(_TOOL_MODE), (
        "FastMCP(instructions=...) must be passed build_instructions(_TOOL_MODE) "
        "verbatim; see memtomem/server/__init__.py"
    )


def test_unknown_mode_falls_back_to_core() -> None:
    """Pruning treats anything that isn't standard/full as core; the
    instructions builder must mirror that so text and surface can't split.
    """
    assert build_instructions("bogus") == build_instructions("core")


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
@pytest.mark.parametrize("token", _COMMON_TOKENS)
def test_instructions_mentions_common_token(mode: str, token: str) -> None:
    text = build_instructions(mode)
    assert token in text, (
        f"{mode} instructions lost reference to {token!r}; if the "
        f"workflow changed, update memtomem/server/instructions.py "
        f"and the token tables in lockstep."
    )


@pytest.mark.parametrize(
    ("mode", "token"),
    [(m, t) for m, tokens in _MODE_TOKENS.items() for t in tokens],
)
def test_instructions_mentions_mode_token(mode: str, token: str) -> None:
    """Each token names a tool, action, or escape hatch an LLM must
    recognize to route a request correctly *in that mode*.
    """
    text = build_instructions(mode)
    assert token in text, (
        f"{mode} instructions lost reference to {token!r}; update "
        f"memtomem/server/instructions.py and _MODE_TOKENS in lockstep."
    )


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_instructions_only_name_tools_exposed_in_mode(mode: str) -> None:
    """Exposure parity (#1608): every ``mem_*`` name in a mode's text
    must be an individually-registered tool in that mode. The shipping
    default (core) previously walked clients into calling five
    multi-agent tools that don't exist there.
    """
    text = build_instructions(mode)
    allowed = _expected_tools(mode)
    mentioned = set(re.findall(r"\bmem_\w+", text))
    ghosts = mentioned - allowed
    assert not ghosts, (
        f"{mode} instructions reference tools not exposed in {mode} mode: "
        f"{sorted(ghosts)}. Route them through mem_do(action=...) or move "
        f"them to the right mode section in memtomem/server/instructions.py."
    )


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_shared_namespace_label_has_no_colon(mode: str) -> None:
    """The cross-agent namespace is exactly ``SHARED_NAMESPACE`` (``shared``),
    with no trailing colon — unlike the ``agent-runtime:`` *prefix*.

    The substring token pin can't guard this: ``"shared" in "shared:"`` is
    True, so a regression to the ``shared:`` typo would slip past it. Assert
    the exact namespace-conventions row and reject the colon form explicitly.
    Derived from ``SHARED_NAMESPACE`` so the constant stays the single source
    of truth.
    """
    text = build_instructions(mode)
    ns = re.escape(SHARED_NAMESPACE)
    assert re.search(rf"^  {ns}\s+cross-agent shared scope$", text, re.MULTILINE), (
        f"namespace-conventions table ({mode}) must list the shared namespace "
        f"as the colon-free {SHARED_NAMESPACE!r} (SHARED_NAMESPACE); see "
        f"memtomem/server/instructions.py"
    )
    assert not re.search(rf"^  {ns}:", text, re.MULTILINE), (
        f"namespace-conventions table ({mode}) reintroduced the "
        f"{SHARED_NAMESPACE + ':'!r} typo; the shared namespace has no colon "
        f"(SHARED_NAMESPACE = {SHARED_NAMESPACE!r})."
    )


# Tools whose ``foo(arg=...)`` example forms appear in the instructions.
# If a parameter is renamed at the source, the example must move with it
# or the string lies to the LLM (causes wrong-arg recovery round trips,
# observed concretely on Haiku 4.5 in the v0.1.30-pre instructions where
# ``mem_agent_share(memory_id=...)`` was shown but the real signature is
# ``chunk_id``). ``mem_do`` is included because the core/standard texts
# render the multi-agent workflow through it.
_DOCUMENTED_TOOLS: tuple[Callable[..., Any], ...] = (
    mem_agent_register,
    mem_agent_search,
    mem_agent_share,
    mem_session_start,
    mem_session_end,
    mem_do,
)


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
@pytest.mark.parametrize("fn", _DOCUMENTED_TOOLS, ids=lambda fn: fn.__name__)
def test_instructions_kwargs_match_signature(mode: str, fn: Callable[..., Any]) -> None:
    """Any ``tool_name(arg=...)`` example in a mode's text must use a
    real parameter name from the function's actual signature. The MCP
    ``ctx`` parameter is excluded since it's framework-injected and
    never appears in user-facing examples. Modes that don't show a given
    tool simply have no matches and pass trivially.
    """
    text = build_instructions(mode)
    sig = inspect.signature(fn)
    real_params = {p for p in sig.parameters if p != "ctx"}
    name = fn.__name__

    # Match each ``name(...)`` occurrence and pull out kwarg names.
    # The pattern stops at the first ``)`` so multi-line examples need
    # to fit on one line — which all current examples do.
    required = {
        n for n, p in sig.parameters.items() if n != "ctx" and p.default is inspect.Parameter.empty
    }
    for match in re.finditer(rf"{re.escape(name)}\(([^)]*)\)", text):
        arglist = match.group(1)
        kwargs = set(re.findall(r"(\w+)\s*=", arglist))
        for kwarg in kwargs:
            assert kwarg in real_params, (
                f"{mode} instructions show {name}({kwarg}=...) but the real "
                f"signature has parameters {sorted(real_params)}. Either "
                f"fix the example in memtomem/server/instructions.py or "
                f"rename the parameter on {fn.__module__}.{name}."
            )
        if name != "mem_do" and arglist.strip():
            # A non-empty direct-call example must be copy-paste runnable:
            # all required (no-default) parameters have to appear. Empty
            # ``name()`` forms are prose shorthand and stay exempt.
            assert required <= kwargs, (
                f"{mode} instructions show {name}(...) missing required "
                f"parameters {sorted(required - kwargs)} — a client copying "
                f"the example verbatim would hit a TypeError."
            )


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_mem_do_action_examples_exist_in_registry(mode: str) -> None:
    """Every ``action="<name>"`` example must be a real registered action
    (or the built-in ``help``) — a renamed action must not leave the
    instructions steering clients into an unknown-action error.
    """
    text = build_instructions(mode)
    for action in re.findall(r'action="(\w+)"', text):
        assert action == "help" or action in ACTIONS, (
            f'{mode} instructions show mem_do(action="{action}") but the '
            f"registry has no such action; see server/tool_registry.py."
        )


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_mem_do_params_examples_match_action_signature(mode: str) -> None:
    """Every full ``mem_do(action="X", params={...})`` example must use
    real parameter names of the routed function AND include all of its
    required (no-default) parameters — an example a client copies
    verbatim must not route into a TypeError. Bare ``mem_do(action=...)``
    name references (no ``params=``) are prose, not examples, and are
    exempt from the required-params check.
    """
    text = build_instructions(mode)
    for action, params_body in re.findall(r'mem_do\(action="(\w+)", params=\{([^}]*)\}', text):
        assert action in ACTIONS, f'unknown action "{action}" in {mode} instructions'
        sig = inspect.signature(ACTIONS[action].fn)
        real_params = {p for p in sig.parameters if p != "ctx"}
        required = {
            n
            for n, p in sig.parameters.items()
            if n != "ctx" and p.default is inspect.Parameter.empty
        }
        keys = set(re.findall(r'"(\w+)":', params_body))
        assert keys <= real_params, (
            f'{mode} instructions show mem_do(action="{action}", params=...) with '
            f"unknown keys {sorted(keys - real_params)}; real parameters are "
            f"{sorted(real_params)}."
        )
        assert required <= keys, (
            f'{mode} instructions show mem_do(action="{action}", params=...) missing '
            f"required parameters {sorted(required - keys)} — a client copying the "
            f"example verbatim would hit a TypeError."
        )


@pytest.mark.skipif(sys.platform == "win32", reason="server is POSIX-only")
def test_initialize_response_carries_instructions(tmp_path: Path) -> None:
    """End-to-end: drive the ``initialize`` RPC in the default (core)
    mode and assert the ``instructions`` field on the response matches
    the core text.

    Isolates ``HOME`` + ``XDG_RUNTIME_DIR`` under ``tmp_path`` so the
    server doesn't touch the developer's real state during the probe
    (mirrors ``test_server_version_reporting.py``).
    """
    home = tmp_path / "home"
    home.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    # Pin the mode rather than inherit whatever the developer's shell
    # exports — this test asserts the shipping-default text.
    env["MEMTOMEM_TOOL_MODE"] = "core"

    initialize_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test-probe", "version": "0.1"},
        },
    }

    proc = subprocess.Popen(
        [sys.executable, "-m", "memtomem.server"],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(initialize_request) + "\n").encode())
        proc.stdin.flush()

        deadline = time.monotonic() + 15
        response_line: bytes | None = None
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    pytest.fail(
                        f"Server exited before responding (rc={proc.returncode}). stderr:\n{stderr}"
                    )
                continue
            response_line = line
            break
        if response_line is None:
            stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
            pytest.fail(f"No initialize response within 15s. stderr:\n{stderr}")

        response = json.loads(response_line)
        result = response.get("result", {})
        instructions = result.get("instructions")
        assert instructions == build_instructions("core"), (
            "initialize response's `instructions` field must round-trip "
            "the core-mode text; if this fails, FastMCP may have dropped "
            "the parameter or stripped it during startup."
        )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
