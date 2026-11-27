"""``fences_after_edit``: which edits put the text after them inside a fence (#3633)."""

from __future__ import annotations

import pytest

from memtomem.chunking.markdown import fences_after_edit

TAIL = "## Next\n\nbody\n"


@pytest.mark.parametrize(
    ("before", "after", "tail_lines", "expected"),
    [
        pytest.param("a\n" + TAIL, "```\n" + TAIL, 3, "```", id="opens-over-the-tail"),
        pytest.param("a\n" + TAIL, "a\n```py\nx\n```\n" + TAIL, 3, None, id="closed-in-span"),
        pytest.param("a\n", "~~~~\n", 0, "~~~~", id="newly-open-at-eof-no-tail"),
        pytest.param("```\n" + TAIL, "```\n```\n" + TAIL, 3, None, id="repair-exposes-the-tail"),
        pytest.param("```\na\n", "```\nb\n", 0, None, id="already-open-at-eof"),
        pytest.param(
            "a\n" + TAIL + "```t\nx\n",
            "```\n" + TAIL + "```t\nx\n",
            5,
            "```",
            id="both-end-open-tail-newly-fenced",
        ),
        pytest.param(
            "a\n" + TAIL + "```t\nx\n```\n",
            "```\n" + TAIL + "```t\nx\n```\n",
            6,
            "```",
            id="eof-outside-tail-newly-fenced",
        ),
        pytest.param(
            "a\n## Next\n```t\nx",
            "```\n## Next\n```t\nx",
            3,
            "```",
            id="no-final-newline-both-end-open-tail-newly-fenced",
        ),
        pytest.param("a\n", "", 0, None, id="empty-result"),
        pytest.param("```\nx\n", "", 0, None, id="empty-result-after-an-open-fence"),
        pytest.param(
            "old\n\n~~~text\nexample\n~~~\n## Next\nbody\n",
            "new\n~~~text\n\n~~~text\nexample\n~~~\n## Next\nbody\n",
            6,
            None,
            id="only-a-blank-line-newly-fenced",
        ),
    ],
)
def test_fences_after_edit(before, after, tail_lines, expected):
    assert fences_after_edit(before, after, tail_lines) == expected
