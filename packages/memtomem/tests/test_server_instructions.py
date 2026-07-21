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
from memtomem.server.tools.meta import _CATEGORY_NOTES, _help


def _instructions(mode: str) -> str:
    """``build_instructions`` with the count the server itself passes.

    The core-tool count is a parameter (not an import inside
    ``instructions.py``) because the only production caller builds the text
    while ``memtomem.server`` is still executing its own module body.
    """
    return build_instructions(mode, core_count=len(_CORE_TOOLS))


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
)


def _documentation_surfaces() -> list[tuple[str, str]]:
    """Every text an MCP client can read, labelled.

    The workflow narrative lives in two places: the ``instructions`` string
    (short, and truncated by clients when rendered into the prompt) and the
    ``mem_do(action="help", params={"category": ...})`` notes (fetched on
    demand). The example-validating pins below run over both — an example
    that lies about a signature is just as harmful wherever it sits.
    """
    surfaces = [(mode, _instructions(mode)) for mode in VALID_TOOL_MODES]
    surfaces += [(f"note:{category}", text) for category, text in _CATEGORY_NOTES.items()]
    return surfaces


#: Tokens that moved out of ``instructions`` when it was cut to fit the client
#: truncation budget. They must still be reachable — just on the other surface.
_NOTE_TOKENS: dict[str, tuple[str, ...]] = {
    "sessions": (
        "namespace=",
        'action="agent_search"',
        'action="session_start"',
        "current namespace",
    ),
    "multi_agent": (
        'action="agent_register"',
        'action="agent_share"',
        'action="session_end"',
        "shared_namespace",
        "shared:projA",
        "ADR-0028",
    ),
}

# Mode-specific tokens: the multi-agent recipe rendered in the calling
# form that mode actually supports (#1608). ``core`` routes everything
# through mem_do; ``standard`` exposes session/crud tools directly but
# routes multi_agent actions; ``full`` shows direct calls.
_MODE_TOKENS: dict[str, tuple[str, ...]] = {
    "core": (
        "mem_do",
        'action="help"',
        "MEMTOMEM_TOOL_MODE",
        'action="session_start"',
        'action="agent_search"',
        'action="session_end"',
    ),
    "standard": (
        "mem_do",
        'action="help"',
        "MEMTOMEM_TOOL_MODE",
        "mem_session_start",
        "mem_session_end",
        'action="agent_search"',
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
    assert mcp.instructions == _instructions(_TOOL_MODE), (
        "FastMCP(instructions=...) must be passed build_instructions(_TOOL_MODE, "
        "core_count=len(_CORE_TOOLS)) verbatim; see memtomem/server/__init__.py"
    )


def test_unknown_mode_falls_back_to_core() -> None:
    """Pruning treats anything that isn't standard/full as core; the
    instructions builder must mirror that so text and surface can't split.
    """
    assert _instructions("bogus") == _instructions("core")


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
@pytest.mark.parametrize("token", _COMMON_TOKENS)
def test_instructions_mentions_common_token(mode: str, token: str) -> None:
    text = _instructions(mode)
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
    text = _instructions(mode)
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
    text = _instructions(mode)
    allowed = _expected_tools(mode)
    mentioned = set(re.findall(r"\bmem_\w+", text))
    ghosts = mentioned - allowed
    assert not ghosts, (
        f"{mode} instructions reference tools not exposed in {mode} mode: "
        f"{sorted(ghosts)}. Route them through mem_do(action=...) or move "
        f"them to the right mode section in memtomem/server/instructions.py."
    )


#: Claims each of these surfaces used to make and the implementation does not
#: honour. They are checked *negatively* on every surface, because a sentence
#: that moved between surfaces can just as easily come back on the wrong one.
_RETIRED_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        'falls back to the "default"',
        "#1875: the session *row* said default while every following mem_add was "
        "redirected into the hidden agent-runtime:default.",
    ),
    (
        "stay visible to a plain mem_search",
        "overclaims visibility — a namespace rule or auto_ns can still send an "
        "unbound write into a system-prefixed namespace that search hides.",
    ),
    (
        "needs an active session (or current_agent_id)",
        "#1877: a bare session_start no longer sets current_agent_id, so this "
        "points callers at a session that binds nothing.",
    ),
    (
        "search everything",
        "an unresolved agent_search is filtered by the system-namespace prefixes, "
        "it does not sweep every namespace.",
    ),
)


@pytest.mark.parametrize(
    ("label", "text"), _documentation_surfaces(), ids=lambda v: v if " " not in v else "text"
)
@pytest.mark.parametrize(("claim", "why"), _RETIRED_CLAIMS, ids=lambda v: v[:24])
def test_retired_claims_do_not_come_back(label: str, text: str, claim: str, why: str) -> None:
    """Each of these was contradicted by the source it described."""
    assert claim not in text, f"{label} reintroduced a retired claim {claim!r} — {why}"


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_unbound_session_routing_is_stated_in_every_mode(mode: str) -> None:
    """#1875's positive half: the summary that replaced the false claim.

    This lives in the shared single-agent block rather than per mode, so it
    is paid once — but it has to *render* in all three modes, which is what
    the parametrization checks. The routing wording is pinned verbatim
    because "binds no agent" alone would pass while the sentence around it
    said something false again.
    """
    text = _instructions(mode)
    assert "binds no agent" in text
    assert "as it would with no session at all" in text


def test_session_note_states_the_agent_search_resolution_order() -> None:
    """The detail that left ``instructions`` has to survive on the note.

    ``_resolve_agent_namespace`` (server/tools/multi_agent.py) resolves an
    explicit ``agent_id``, then the session's bound agent, then
    ``current_namespace``, and only then searches unpinned. An unbound
    session removes the second step and nothing else — so a note claiming it
    means "unpinned" is wrong whenever a current namespace is set. Pin all
    three steps and the ordering word.
    """
    note = _CATEGORY_NOTES["sessions"]
    # Scope the check to the sentence that makes the claim: "the current
    # namespace" also appears in the read-asymmetry paragraph above, and a
    # whole-note search would compare positions across different sentences.
    assert "in order" in note, "sessions note lost the resolution-order sentence"
    claim = note[note.index("in order") :]
    ordered = (
        "an explicit agent_id parameter",
        "session's bound agent",
        "the current namespace",
        "unpinned",
    )
    positions = []
    for fragment in ordered:
        assert fragment in claim, f"sessions note lost the resolution-order fragment {fragment!r}"
        positions.append(claim.index(fragment))
    # Presence alone would pass a note that lists the same four sources in the
    # wrong order, which is a different (and wrong) resolution rule.
    assert positions == sorted(positions), (
        f"sessions note lists the resolution sources out of order: {ordered} "
        f"appear at {positions}. The real order is explicit agent_id → the "
        "session's bound agent → the current namespace → unpinned."
    )


def test_session_note_names_only_the_paths_that_inherit_the_scope() -> None:
    """ "Every write action" was an overclaim.

    The agent scope is inherited by exactly the four call sites that resolve
    through ``_resolve_agent_namespace(app, None)`` — ``mem_add`` and
    ``mem_batch_add`` (server/tools/memory_crud.py), ``mem_index``
    (server/tools/indexing.py) and ``mem_fetch`` (server/tools/url_index.py).
    The Notion/Obsidian importers do not: they default to their own
    ``"notion"`` / ``"obsidian"`` namespaces (server/tools/importers.py), so a
    caller told "every write" lands in the agent scope would be misled about
    where imported content went.
    """
    note = _CATEGORY_NOTES["sessions"]
    for path in ("mem_add", "mem_index", "batch_add", "fetch"):
        assert path in note, f"sessions note stopped naming the inheriting path {path!r}"
    assert "every write" not in note
    assert "notion" in note and "obsidian" in note


def test_session_note_says_where_namespace_must_be_passed() -> None:
    """``namespace=`` on ``session_start`` re-points the session *record* only.

    ``mem_session_start``'s own docstring is explicit that the priority chain
    derives the record's namespace and not the write routing, so a note that
    says "unless namespace= is explicit" without saying *where* sends callers
    to the wrong call.
    """
    note = _CATEGORY_NOTES["sessions"]
    assert "pass namespace= on the write" in note
    assert "session *record* only" in note


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
@pytest.mark.parametrize("category", sorted(_CATEGORY_NOTES))
def test_instructions_point_at_every_category_note(mode: str, category: str) -> None:
    """The other half of "moved, but still reachable".

    ``test_relocated_tokens_are_reachable_through_help`` pins what the notes
    *contain*; nothing pinned that the instructions still send the client
    there. Deleting both ``params={"category": ...}`` pointers left the whole
    suite green — ``_MODE_TOKENS`` only requires ``action="help"``, which the
    generic "lists every action" sentence satisfies on its own. Since the
    entire point of the budget cut is that detail moved somewhere reachable,
    the pointer is the load-bearing part.
    """
    assert f'"category": "{category}"' in _instructions(mode), (
        f'{mode} instructions no longer point at mem_do(action="help", '
        f'params={{"category": "{category}"}}); the narrative that was moved '
        "out of this text is then documented nowhere the client will look."
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
    text = _instructions(mode)
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


# Tools whose ``foo(arg=...)`` example forms appear on a documentation
# surface. If a parameter is renamed at the source, the example must move
# with it or the string lies to the LLM (causes wrong-arg recovery round
# trips, observed concretely on Haiku 4.5 in the v0.1.30-pre instructions
# where ``mem_agent_share(memory_id=...)`` was shown but the real signature
# is ``chunk_id``).
#
# The set is *derived from the text*, not hand-listed: the hand-listed tuple
# this replaces omitted ``mem_add``, so an invented kwarg on the one tool the
# session narrative talks about most would have passed every validator below.
def _resolve_documented_tool(name: str) -> Callable[..., Any] | None:
    """Map a ``mem_x`` name shown on a surface to the function behind it.

    Two registries, because neither alone covers the surface: ``ACTIONS``
    holds every ``@register``-ed action (including ones pruned from the
    current mode, e.g. the multi-agent tools), while the core nine are
    registered straight on ``mcp`` and never appear in ``ACTIONS``.
    Returns ``None`` when a name resolves to neither — the caller fails on
    that rather than skipping it, since an unresolvable ``mem_*`` name in
    the text is itself the drift this guard exists to catch.

    ``mcp._tool_manager.list_tools()`` is FastMCP-private and is the first
    thing here to break on an upgrade. Test-only, and it fails loudly (every
    core-tool example stops resolving) rather than quietly certifying, so the
    coupling is accepted; ``server/__init__.py:_registered_tool_names`` is the
    production-side twin, guarded by tests/test_tool_mode_pruning.py.
    """
    info = ACTIONS.get(name.removeprefix("mem_"))
    if info is not None:
        return info.fn
    for tool in mcp._tool_manager.list_tools():
        if tool.name == name:
            return tool.fn
    return None


@pytest.mark.parametrize(
    ("label", "text"), _documentation_surfaces(), ids=lambda v: v if " " not in v else "text"
)
def test_documented_kwargs_match_signature(label: str, text: str) -> None:
    """Any ``tool_name(arg=...)`` example must use a real parameter name
    from the function's actual signature. The MCP ``ctx`` parameter is
    excluded since it's framework-injected and never appears in user-facing
    examples. Surfaces that show no direct call pass trivially.
    """
    # Match each ``mem_x(...)`` occurrence and pull out kwarg names. The
    # pattern stops at the first ``)``, but ``[^)]`` spans newlines, so the
    # two-line ``mem_do`` examples in the multi_agent note match fine; what it
    # cannot handle is a ``)`` inside an argument value.
    for match in re.finditer(r"\b(mem_\w+)\(([^)]*)\)", text):
        name, arglist = match.group(1), match.group(2)
        fn = _resolve_documented_tool(name)
        assert fn is not None, (
            f"{label} shows a call to {name}(...), which is neither a registered "
            f"action nor an individually-registered tool. Fix the example or the name."
        )
        sig = inspect.signature(fn)
        real_params = {p for p in sig.parameters if p != "ctx"}
        required = {
            n
            for n, p in sig.parameters.items()
            if n != "ctx" and p.default is inspect.Parameter.empty
        }
        kwargs = set(re.findall(r"(\w+)\s*=", arglist))
        for kwarg in kwargs:
            assert kwarg in real_params, (
                f"{label} shows {name}({kwarg}=...) but the real signature has "
                f"parameters {sorted(real_params)}. Either fix the example or "
                f"rename the parameter on {fn.__module__}.{fn.__name__}."
            )
        if name != "mem_do" and arglist.strip():
            # A non-empty direct-call example must be copy-paste runnable:
            # all required (no-default) parameters have to appear. Empty
            # ``name()`` forms are prose shorthand and stay exempt.
            assert required <= kwargs, (
                f"{label} shows {name}(...) missing required parameters "
                f"{sorted(required - kwargs)} — a client copying the example "
                f"verbatim would hit a TypeError."
            )


@pytest.mark.parametrize(
    ("label", "text"), _documentation_surfaces(), ids=lambda v: v if " " not in v else "text"
)
def test_mem_do_action_examples_exist_in_registry(label: str, text: str) -> None:
    """Every ``action="<name>"`` example must be a real registered action
    (or the built-in ``help``) — a renamed action must not leave the
    instructions steering clients into an unknown-action error.

    Every quoted action on the surface is swept, including the second and
    later alternatives of an "a or b" pair: a single-match-per-line sweep
    left the trailing name free to be renamed with the guard still green.
    """
    for action in re.findall(r'action="(\w+)"', text):
        assert action == "help" or action in ACTIONS, (
            f'{label} shows mem_do(action="{action}") but the '
            f"registry has no such action; see server/tool_registry.py."
        )


@pytest.mark.parametrize(
    ("label", "text"), _documentation_surfaces(), ids=lambda v: v if " " not in v else "text"
)
def test_mem_do_params_examples_match_action_signature(label: str, text: str) -> None:
    """Every full ``mem_do(action="X", params={...})`` example must use
    real parameter names of the routed function AND include all of its
    required (no-default) parameters — an example a client copies
    verbatim must not route into a TypeError. Bare ``mem_do(action=...)``
    name references (no ``params=``) are prose, not examples, and are
    exempt from the required-params check.
    """
    for action, params_body in re.findall(
        r'mem_do\(\s*action="(\w+)"\s*,\s*params=\{([^}]*)\}', text, re.DOTALL
    ):
        if action == "help":
            # ``help`` is handled inside mem_do itself, not through the
            # registry; its only parameter is the category key.
            assert re.fullmatch(r'"category":\s*"\w+"', params_body.strip()), (
                f'{label}: mem_do(action="help", params={{{params_body}}}) — the '
                'only supported key is "category"'
            )
            continue
        assert action in ACTIONS, f'unknown action "{action}" in {label}'
        sig = inspect.signature(ACTIONS[action].fn)
        real_params = {p for p in sig.parameters if p != "ctx"}
        required = {
            n
            for n, p in sig.parameters.items()
            if n != "ctx" and p.default is inspect.Parameter.empty
        }
        keys = set(re.findall(r'"(\w+)":', params_body))
        assert keys <= real_params, (
            f'{label} shows mem_do(action="{action}", params=...) with '
            f"unknown keys {sorted(keys - real_params)}; real parameters are "
            f"{sorted(real_params)}."
        )
        assert required <= keys, (
            f'{label} shows mem_do(action="{action}", params=...) missing '
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
        assert instructions == _instructions("core"), (
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


# ── Budget pins (the instructions were being silently cut) ────────────

#: Self-imposed ceiling on the per-mode ``instructions`` text.
#:
#: The only hard evidence is a direct observation: a Claude Code system
#: prompt carrying this server's instructions ended mid-word with a
#: ``[truncated]`` marker at ~2,048 characters. Everything else is
#: unspecified — MCP leaves rendering and truncation entirely to the client,
#: Claude Desktop publishes no cap but concatenates every connected server's
#: instructions into one hidden prompt (memtomem commonly runs beside
#: memtomem-stm), and Cursor does not inject the field at all. So this number
#: is a conservative budget derived from the one measurement, not a claim
#: about any client's documented limit.
#:
#: Before it existed, all three modes overflowed (core 3,364 / standard 3,219
#: / full 2,673 on the commit this budget was introduced against) and the
#: overflow was always the tail: the namespace table and the closing fallback
#: line, in every mode.
#:
#: When this fails, move text into a category note — do NOT raise the number.
#: The shared sections are counted in all three modes, so core mode has only a
#: few characters of headroom and a one-word wording fix can turn three tests
#: red at once. That pressure is the point: it is what keeps the relocation
#: from silently unwinding one sentence at a time.
_INSTRUCTIONS_BUDGET = 1700

#: Ceiling on a whole ``mem_do(action="help", params={"category": ...})``
#: response for a category that carries a note. Relocating narrative out of
#: ``instructions`` moves the cost rather than removing it, so the receiving
#: surface gets a budget too — this one is per response instead of per prompt,
#: hence the looser number.
_CATEGORY_HELP_BUDGET = 4096


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_instructions_fit_the_client_render_budget(mode: str) -> None:
    """Clients truncate ``instructions`` when rendering it into the prompt."""
    text = _instructions(mode)
    assert len(text) <= _INSTRUCTIONS_BUDGET, (
        f"{mode} instructions are {len(text)} chars; over the "
        f"{_INSTRUCTIONS_BUDGET} budget the tail risks being cut off in the "
        "client's prompt. Move detail into _CATEGORY_NOTES "
        "(server/tools/meta.py), which is fetched on demand."
    )


@pytest.mark.parametrize("category", sorted(_CATEGORY_NOTES))
def test_category_notes_fit_their_own_budget(category: str) -> None:
    """The relocation target is budgeted too, or it just grows unbounded."""
    rendered = _help(category)
    assert len(rendered) <= _CATEGORY_HELP_BUDGET, (
        f'mem_do(action="help", params={{"category": "{category}"}}) renders '
        f"{len(rendered)} chars, over the {_CATEGORY_HELP_BUDGET} budget. Trim "
        "the note in server/tools/meta.py or the action docstrings it lists."
    )


def test_core_tool_count_renders_the_real_set_size() -> None:
    """A literal "9" in the prose could drift from the exposed set.

    Asserts the complete phrase rather than ``str(len(...)) in text``: a bare
    digit check passes on any stray number in the text.
    """
    assert f"only the {len(_CORE_TOOLS)} core" in _instructions("core")


def test_core_tool_count_comes_from_the_template_not_a_literal() -> None:
    """Rendering the right number today is not the contract — not typing it is.

    Someone could replace the placeholder with the current count and the
    rendered-phrase test above would stay green until the set changed, which
    is exactly the drift the interpolation exists to prevent.
    """
    from memtomem.server.instructions import _CORE_MODE, _STANDARD_MODE

    for name, template in (("core", _CORE_MODE), ("standard", _STANDARD_MODE)):
        assert "{core_count}" in template, (
            f"the {name} mode template hard-codes its tool count; interpolate "
            "the core-tool count so prose and surface cannot split"
        )


_MUST_SURVIVE = (
    "Namespace conventions:",
    "agent-runtime:<id>",
    "cross-agent shared scope",
    "When in doubt, default to mem_add / mem_search",
)


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
@pytest.mark.parametrize("marker", _MUST_SURVIVE)
def test_generic_guidance_is_present_in_every_mode(mode: str, marker: str) -> None:
    """These lines used to be appended last and lost to truncation."""
    assert marker in _instructions(mode)


@pytest.mark.parametrize("mode", VALID_TOOL_MODES)
def test_generic_guidance_precedes_the_mode_block(mode: str) -> None:
    """Ordering, not just presence: the mode block is the least universally
    useful section, so it must sit after the namespace table and before the
    footer. Presence alone was true before this change too — the tail was
    simply cut off in the client.
    """
    text = _instructions(mode)
    mode_block = text.index("This server runs in")
    assert text.index("Namespace conventions:") < mode_block
    assert mode_block < text.index("When in doubt")


# ── The relocated narrative must actually be reachable ────────────────


@pytest.mark.parametrize(
    ("category", "token"),
    [(c, t) for c, tokens in _NOTE_TOKENS.items() for t in tokens],
)
def test_relocated_tokens_are_reachable_through_help(category: str, token: str) -> None:
    """What left the instructions must be readable via mem_do help."""
    assert token in _help(category), (
        f"the {category} category note lost {token!r}; it is no longer in the "
        "instructions either, so nothing documents it."
    )


@pytest.mark.parametrize("category", sorted(_CATEGORY_NOTES))
def test_category_notes_are_mode_neutral(category: str) -> None:
    """``_help`` has no mode awareness, so a note may only name core tools.

    ``ACTIONS`` is populated regardless of pruning, so these notes render in
    core mode too — naming ``mem_batch_add`` there would point at a tool that
    does not exist.
    """
    note = _CATEGORY_NOTES[category]
    mentioned = set(re.findall(r"\bmem_\w+", note))
    ghosts = mentioned - set(_CORE_TOOLS)
    assert not ghosts, (
        f"{category} note names non-core tools {sorted(ghosts)}; spell them as "
        'mem_do(action="...", params={...}) so the text holds in every mode.'
    )

    # A bare ``session_start(namespace="...")`` is callable in NO mode: core
    # needs mem_do, standard/full expose it as ``mem_session_start``. The
    # mem_-prefix sweep above cannot see that form.
    bare_calls = {
        call
        for call in re.findall(r"(?<![\w.\"])([a-z][a-z0-9_]+)\(", note)
        if call in ACTIONS and f"mem_{call}(" not in note
    }
    assert not bare_calls, (
        f"{category} note shows bare action calls {sorted(bare_calls)}; write them "
        'as mem_do(action="...", params={...}).'
    )
