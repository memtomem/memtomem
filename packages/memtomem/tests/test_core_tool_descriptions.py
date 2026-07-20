"""The core tools' wire descriptions are always in the model's context.

In the default core mode nine tools are exposed, and an MCP client puts every
one of their descriptions in the prompt on every turn — unlike the category
notes, which are fetched only when asked for. They were 11,375 characters
(~2.8k tokens) of permanent overhead, a large share of it explaining *why the
server behaves as it does* (STM scan asymmetry, reranker candidate-pool
derivation, the warning-schema stability contract for dashboards) rather than
anything a caller needs in order to call.

Two pins, because size alone is the wrong contract: a budget that can be met
by deleting the safety semantics is worse than no budget. The must-survive
list is what the diet may compress but not drop.
"""

from __future__ import annotations

import asyncio

import pytest

from memtomem.server import _CORE_TOOLS, mcp


def _wire_descriptions() -> dict[str, str]:
    """Descriptions exactly as FastMCP puts them on the wire.

    Not ``inspect.getdoc`` / ``__doc__``: those differ from the wire strings
    by a character or two, which would make a byte budget quietly off-by-N.
    """
    tools = asyncio.run(mcp.list_tools())
    return {t.name: (t.description or "") for t in tools if t.name in _CORE_TOOLS}


#: Total wire size of the nine core descriptions. Set just above the measured
#: 8,833 after the trim (was 11,375). This is a budget, not a target: the
#: floor is roughly "one clear line per parameter", and mem_search alone has
#: thirteen parameters. Adding text here costs every request in every session,
#: so an increase should be argued for, and detail that explains the server's
#: reasoning belongs in a module comment or a guide instead.
_TOTAL_BUDGET = 9_000


def test_core_descriptions_fit_the_budget() -> None:
    descriptions = _wire_descriptions()
    assert set(descriptions) == set(_CORE_TOOLS), "core tool surface changed"
    total = sum(len(d) for d in descriptions.values())
    assert total <= _TOTAL_BUDGET, (
        f"core tool descriptions total {total} chars (budget {_TOTAL_BUDGET}). "
        f"Per tool: { {k: len(v) for k, v in sorted(descriptions.items())} }. "
        "Move server-internal rationale into module comments or docs/guides/."
    )


def test_no_core_description_is_empty() -> None:
    """A budget must not be satisfiable by deleting a description."""
    empty = [name for name, text in _wire_descriptions().items() if len(text) < 100]
    assert not empty, f"suspiciously short core descriptions: {empty}"


#: Caller-visible contracts. Each is something a client cannot discover by
#: calling the tool — it either governs consent, or it changes how the result
#: must be interpreted. The trim may reword these; it may not drop them.
_MUST_SURVIVE: tuple[tuple[str, str, str], ...] = (
    ("mem_add", "force_unsafe", "the redaction-bypass valve must stay documented"),
    ("mem_add", "redaction guard", "what force_unsafe bypasses"),
    ("mem_add", "project_shared", "the tier that is never bypassable"),
    ("mem_add", "confirm_project_shared", "consent for a git-tracked write"),
    ("mem_add", "idempotency_key", "retry semantics"),
    ("mem_add", "scope", "which tier the write lands in"),
    ("mem_search", "scope", "which tiers are searched by default"),
    ("mem_search", "structured", "the machine-readable output mode"),
    ("mem_search", "score_scale", "scores are only comparable within one scale"),
    ("mem_search", "rerank", "the latency escape hatch"),
    ("mem_recall", "scope", "which tiers are recalled by default"),
    ("mem_status", "stored", "DB side of an embedding mismatch"),
    ("mem_status", "configured", "runtime side of an embedding mismatch"),
)


@pytest.mark.parametrize(
    "tool,marker,why", _MUST_SURVIVE, ids=[f"{t}-{m}" for t, m, _ in _MUST_SURVIVE]
)
def test_caller_visible_contracts_survive_the_trim(tool: str, marker: str, why: str) -> None:
    description = _wire_descriptions()[tool]
    assert marker in description, f"{tool} description lost {marker!r} — {why}"


_INTERNAL_RATIONALE = (
    # Why the LTM scan differs from STM's — a server design decision.
    "compression-side",
    # How the reranker's candidate pool is derived from config.
    "rerank.oversample",
    "min_pool",
    # The dashboard-facing stability promise for warning kinds.
    "uptime probes",
)


@pytest.mark.parametrize("phrase", _INTERNAL_RATIONALE)
def test_server_internal_rationale_stays_out_of_the_wire(phrase: str) -> None:
    """These explain the implementation to a maintainer, not the API to a caller.

    They live in module comments (``memory_crud._mem_add_core``) or the
    guides now. Re-adding one here would put it in every prompt of every
    session for every user.
    """
    offenders = [name for name, text in _wire_descriptions().items() if phrase in text]
    assert not offenders, (
        f"{offenders} reintroduced server-internal rationale ({phrase!r}) into an "
        "always-loaded description; keep it in a comment or a guide."
    )


# ── First-line style: what mem_do(action="help") prints per action ────


def _tool_first_lines() -> dict[str, str]:
    """First docstring line of every tool, by function name.

    ``tool_registry`` takes exactly this line as the action ``description``,
    so it is what an agent scanning the help catalog reads before choosing.
    """
    import ast
    from pathlib import Path

    tools_dir = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "server" / "tools"
    first_lines: dict[str, str] = {}
    for path in sorted(tools_dir.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = [ast.unparse(d) for d in node.decorator_list]
            if not any(d.startswith("mcp.tool") or d.startswith("register(") for d in decorators):
                continue
            doc = (ast.get_docstring(node) or "").strip()
            if doc:
                first_lines[node.name] = doc.splitlines()[0]
    return first_lines


def test_every_tool_has_a_summary_line() -> None:
    assert len(_tool_first_lines()) > 90


def test_first_lines_are_one_sentence() -> None:
    """A catalog entry is a line, not a paragraph.

    ``mem_ns_set`` packed two sentences into 91 characters, and only the
    first half survived as a summary; the rest read as a fragment.
    """
    offenders = {name: len(line) for name, line in _tool_first_lines().items() if len(line) > 90}
    assert not offenders, (
        f"first docstring lines over 90 chars: {offenders}. Keep the summary to "
        "one line and move the rest into the body."
    )


def test_first_lines_end_with_a_period() -> None:
    offenders = [name for name, line in _tool_first_lines().items() if not line.endswith(".")]
    assert not offenders, f"first docstring lines without a closing period: {offenders}"


def test_first_lines_carry_no_markup() -> None:
    """RST markup renders verbatim in the help catalog.

    ``mem_do(action="help")`` emits plain text, so ``\\`\\`name\\`\\``` shows up as
    literal backticks in front of an agent.
    """
    offenders = [name for name, line in _tool_first_lines().items() if "``" in line or "**" in line]
    assert not offenders, (
        f"first docstring lines with RST/markdown markup: {offenders}; the help "
        "catalog prints them literally."
    )
