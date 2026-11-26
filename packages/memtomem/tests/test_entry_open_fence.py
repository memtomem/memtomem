"""#3618: the check that an entry leaves no code fence open, read as the chunker reads it.

The writers that use it are pinned in ``test_entry_open_fence_surfaces.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from memtomem.chunking.markdown import MarkdownChunker, unclosed_fence
from memtomem.tools.memory_writer import entry_open_fence, format_entry_block

SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"


def _headings(text: str) -> list[str]:
    """The last heading of each chunk the Markdown chunker makes of ``text``."""
    return [
        c.metadata.heading_hierarchy[-1]
        for c in MarkdownChunker().chunk_file(Path("x.md"), text)
        if c.metadata.heading_hierarchy
    ]


# ``(text as the chunker reads it, the run it leaves open or None)``.
VIEWS = [
    ("```py\nx", "```"),
    ("~~~\nx", "~~~"),
    ("````\n```py\nx", "````"),  # a shorter run inside does not close it
    ("```\nx\n```py", "```"),  # a run with an info string does not close it
    ("```\nx\n~~~", "```"),  # nor does the other fence character
    ("```py\nx\n```", None),
    ("~~~\nx\n~~~~", None),  # a longer run closes it
    ("   ```\nx\n```", None),  # up to three spaces before the run
    ("    ```\nx", None),  # four is indented code, not a fence
    ("plain text", None),
    ("```py\nx\n```\n\n```\ny", "```"),  # the last fence is the one that counts
]


@pytest.mark.parametrize(("view", "run"), VIEWS)
def test_unclosed_fence_agrees_with_the_chunker(view, run):
    assert unclosed_fence(view) == run
    # The chunker's own reading: a heading after the text survives only when
    # the text ends outside a fence.
    if run is None:
        assert _headings(f"{view}\n\n## Next\n\nz\n")[-1] == "## Next"
    else:
        assert "## Next" not in _headings(f"# Top\n\n{view}\n\n## Next\n\nz\n")
        closed = f"{view}\n{run}"
        assert unclosed_fence(closed) is None
        assert _headings(f"{closed}\n\n## Next\n\nz\n")[-1] == "## Next"


def test_a_closer_on_the_last_line_and_an_opener_to_eof_cover_the_same_lines():
    """The line set alone cannot tell these apart, so each is pinned on its own."""
    from memtomem.chunking.markdown import _fence_line_set

    closed, still_open = "```\nx\n```", "```\nx\n```py"

    assert _fence_line_set(closed) == _fence_line_set(still_open) == frozenset({1, 2, 3})
    assert unclosed_fence(closed) is None
    assert unclosed_fence(still_open) == "```"


# ── the entry, as written ────────────────────────────────────────────────


def test_the_issue_case_is_refused():
    first = "```python\nprint(1)"
    text = "".join(
        [
            format_entry_block(first, "First", ["first"]),
            format_entry_block("A plain note.", "Second", ["second"]),
        ]
    )

    assert _headings(text) == ["## First"]  # what a write would have indexed
    assert entry_open_fence(first, "First", ["first"]) == "```"
    assert entry_open_fence("```python\nprint(1)\n```", "First", ["first"]) is None


def test_a_lone_carriage_return_is_a_line_break():
    content = "text\r```py\nx"

    assert unclosed_fence(content) is None  # not a fence until newlines are translated
    assert entry_open_fence(content) == "```"


def test_a_wikilink_that_resolves_to_an_opener():
    content = "[[```python]]\nx"

    assert unclosed_fence(content) is None
    assert entry_open_fence(content) == "```"


def test_a_wikilink_that_resolves_to_a_closer():
    # ``[[x| ]]`` resolves to its alias, a space: the line becomes a bare closer.
    assert entry_open_fence("```\nx\n```[[x| ]]") is None
    # ``[[x]]`` resolves to ``x``, an info string: the fence stays open.
    assert entry_open_fence("```\nx\n```[[x]]") == "```"


def test_a_title_can_open_the_fence():
    assert entry_open_fence("body", title="Title\n```") == "```"


def test_content_with_its_own_heading():
    assert entry_open_fence("## Own\n\n```py\nx") == "```"
    assert entry_open_fence("## Own\n\n```py\nx\n```") is None


# ── every entry writer makes the check, before its guard ─────────────────

# ``(module, function)`` of every call to ``entry_guard_text``. A new writer
# fails here until it is listed, which is the prompt to give it the check.
WRITERS = {
    ("cli/agent_cmd.py", "_run_share"),
    ("cli/memory.py", "_add"),
    ("cli/review_cmd.py", "_decide"),
    ("cli/shell.py", "_cmd_add"),
    ("integrations/langgraph.py", "add"),
    ("server/tools/memory_crud.py", "_mem_add_core"),
    ("server/tools/memory_crud.py", "mem_batch_add"),
    ("web/routes/scratch.py", "promote_scratch"),
    ("web/routes/system_indexing.py", "add_memory"),
}

_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _called(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_by_function() -> dict[tuple[str, str], list[dict[str, list[int]]]]:
    """For each function name, one entry per definition: the lines of its own
    calls to the two helpers. Calls in a nested definition or a lambda belong
    to it, not to the function around it: a check that may never run is no check."""
    found: dict[tuple[str, str], list[dict[str, list[int]]]] = {}

    def walk(node: ast.AST, rel: str, calls: dict[str, list[int]] | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _SCOPES):
                own: dict[str, list[int]] = {"entry_guard_text": [], "entry_open_fence": []}
                name = "<lambda>" if isinstance(child, ast.Lambda) else child.name
                found.setdefault((rel, name), []).append(own)
                walk(child, rel, own)
                continue
            if isinstance(child, ast.Call) and calls is not None:
                name = _called(child)
                if name in calls:
                    calls[name].append(child.lineno)
            walk(child, rel, calls)

    for path in sorted(SRC.rglob("*.py")):
        walk(ast.parse(path.read_text(encoding="utf-8")), path.relative_to(SRC).as_posix(), None)
    return found


def test_every_entry_writer_checks_for_an_open_fence_before_its_guard():
    found = _calls_by_function()
    guarded = {key for key, defs in found.items() for calls in defs if calls["entry_guard_text"]}

    assert guarded == WRITERS
    for key in sorted(WRITERS):
        (calls,) = [c for c in found[key] if c["entry_guard_text"]]
        guards, fences = calls["entry_guard_text"], calls["entry_open_fence"]
        # One check per scan, each ahead of the scan it goes with.
        assert len(fences) == len(guards), f"{key}: {len(fences)} checks for {len(guards)} scans"
        for fence, guard in zip(sorted(fences), sorted(guards)):
            assert fence < guard, f"{key} checks for an open fence after its privacy guard"
