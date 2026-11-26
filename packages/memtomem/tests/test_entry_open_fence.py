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


# ── wikilink resolution, linear in the text ──────────────────────────────


def _spec(text: str) -> str:
    """What ``resolve_wikilinks`` must return: the grammar it implements, run as a regex."""
    from memtomem.chunking.markdown import _WIKILINK_RE

    return _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(1), text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no links",
        "[[a]] and [[b|c]]",
        "[[[a]]",  # the target may hold a ``[``
        "[[a|b|c]]",  # the alias may hold a ``|``
        "[[]] [[|a]] [[a|]]",  # an empty target or alias is not a link
        "[[a]b]] [[a\nb]] [[a|b\n]]",  # nor one crossing ``]`` or a newline
        "[[a| ]]",  # an alias of one space is kept
        "[[a][[b]]",
        "x[[" * 50 + "[[ok]]",
    ],
)
def test_resolve_wikilinks_matches_the_grammar(text):
    from memtomem.chunking.markdown import resolve_wikilinks

    assert resolve_wikilinks(text) == _spec(text)


def test_resolve_wikilinks_matches_the_grammar_on_random_text():
    import random

    from memtomem.chunking.markdown import resolve_wikilinks

    tokens = ["[", "]", "|", "\n", "a", " ", "[[", "]]", "|]]", "b|c"]
    rng = random.Random(3618)
    for _ in range(20000):
        text = "".join(rng.choice(tokens) for _ in range(rng.randint(0, 24)))
        assert resolve_wikilinks(text) == _spec(text), repr(text)


def test_resolve_wikilinks_end_searches_read_at_most_twice_the_text(monkeypatch):
    """A line of unmatched openers made the regex rescan the rest of the line
    from each one. Counted, not timed: the characters the target and alias end
    searches read stay within twice the text, however many openers there are,
    and the backtracking regex is not used. Only those searches are counted."""
    from memtomem.chunking import markdown

    read = [0]

    class Counting:
        def __init__(self, pattern):
            self._pattern = pattern

        def search(self, text, pos):
            m = self._pattern.search(text, pos)
            read[0] += (m.start() if m else len(text)) - pos
            return m

    class NotRun:
        def __getattr__(self, name):
            raise AssertionError(f"the backtracking regex was used ({name})")

    for name in ("_WIKILINK_TARGET_END", "_WIKILINK_ALIAS_END"):
        monkeypatch.setattr(markdown, name, Counting(getattr(markdown, name)))
    monkeypatch.setattr(markdown, "_WIKILINK_RE", NotRun())

    for text in ("x[[" * 20000, "x[[a|" * 20000, ("x[[" * 2000 + "\n") * 10):
        read[0] = 0
        assert markdown.resolve_wikilinks(text) == text
        assert 0 < read[0] <= 2 * len(text), (len(text), read[0])


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
    ("server/tools/decision_record.py", "mem_decision_record"),
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
