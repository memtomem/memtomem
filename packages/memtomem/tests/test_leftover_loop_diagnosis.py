"""The leftover-MainThread-loop diagnosis in ``conftest.py`` (#2099).

`tests/web/conftest.py` bans coroutine tests and async fixtures in that
directory, which covers every CI invocation and the default full-suite order.
Hand-ordered paths can still put a browser spec ahead of a coroutine test — or
an async fixture — elsewhere, and what that produced was a bare ``RuntimeError``
naming whichever test came next. The root hooks replace it with an explanation,
so what needs pinning is that they fire on the real condition and stay silent
otherwise.

Every probe runs via ``runpytest_subprocess``. These cases have to *stage* a
running loop on MainThread, and an in-process pytester session would leave that
registration on the thread this suite is running on — the exact hazard under
test, self-inflicted.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"
_LIFTED = ("_LEFTOVER_LOOP_HINT", "_drives_a_loop", "pytest_runtest_setup", "pytest_fixture_setup")


def _lifted_hook_source() -> str:
    """The shipped hooks' source, decorators included, lifted verbatim.

    Reading them out of the real conftest instead of restating them means this
    cannot pin a stale copy. Decorators are part of the span deliberately:
    ``pytest_fixture_setup``'s ``tryfirst`` is load-bearing, and a
    ``get_source_segment`` starting at the ``def`` would silently drop it.
    """

    source = _CONFTEST.read_text(encoding="utf-8")
    lines = source.splitlines(keepends=True)
    chunks = []
    for node in ast.parse(source).body:
        name = getattr(node, "name", None) or (
            node.targets[0].id
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            else None
        )
        if name not in _LIFTED:
            continue
        start = min([node.lineno, *(d.lineno for d in getattr(node, "decorator_list", []))])
        chunks.append("".join(lines[start - 1 : node.end_lineno]))
    assert len(chunks) == len(_LIFTED), f"lifted {len(chunks)} of {len(_LIFTED)} hook objects"
    header = "import asyncio.events\nimport inspect\n\nimport pytest\nimport pytest_asyncio\n\n"
    return header + "\n\n".join(chunks) + "\n\n"


_PARK_A_LOOP = """

# Stand in for Playwright's parked dispatcher greenlet: a loop registered as
# running on MainThread that nothing is going to clear.
def pytest_configure(config):
    config._probe_loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(config._probe_loop)
"""

_COROUTINE_TEST = """
async def test_coroutine() -> None:
    assert True


def test_sync() -> None:
    assert True
"""

_ASYNC_GEN_FIXTURE_CONSUMER = """
import pytest


@pytest.fixture
async def an_async_gen_fixture():
    # The shape the motivating real fixture has (``components`` yields), which
    # reaches ``isasyncgenfunction`` rather than ``iscoroutinefunction``.
    yield 1


def test_sync_body_async_gen_fixture(an_async_gen_fixture: int) -> None:
    assert an_async_gen_fixture == 1
"""

_ASYNC_FIXTURE_CONSUMER = """
import pytest


@pytest.fixture
async def an_async_fixture() -> int:
    return 1


def test_sync_body_async_fixture(an_async_fixture: int) -> None:
    assert an_async_fixture == 1
"""


@pytest.fixture
def rooted(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        asyncio_default_fixture_loop_scope = function
        """
    )
    return pytester


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("coroutine", _COROUTINE_TEST),
        ("async_fixture", _ASYNC_FIXTURE_CONSUMER),
        ("async_gen_fixture", _ASYNC_GEN_FIXTURE_CONSUMER),
    ],
)
def test_a_leftover_loop_is_explained(rooted: pytest.Pytester, name: str, source: str) -> None:
    rooted.makeconftest(_lifted_hook_source() + _PARK_A_LOOP)
    rooted.makepyfile(**{f"test_{name}": source})

    result = rooted.runpytest_subprocess()

    assert "#2099" in result.stdout.str()
    assert result.parseoutcomes().get("passed", 0) == (1 if name == "coroutine" else 0)
    # The diagnosis is the whole message: a second RuntimeWarning from tearing
    # pytest-asyncio's runner down under the parked loop would bury it, which is
    # why the fixture hook is an outer wrapper.
    assert "asyncio.Runner" not in result.stdout.str()


@pytest.mark.parametrize(
    ("name", "source", "expected_passed"),
    [
        ("coroutine", _COROUTINE_TEST, 2),
        ("async_fixture", _ASYNC_FIXTURE_CONSUMER, 1),
        ("async_gen_fixture", _ASYNC_GEN_FIXTURE_CONSUMER, 1),
    ],
)
def test_without_a_leftover_loop_the_hooks_are_silent(
    rooted: pytest.Pytester, name: str, source: str, expected_passed: int
) -> None:
    """The mirror that keeps the diagnosis from becoming a ban."""

    rooted.makeconftest(_lifted_hook_source())
    rooted.makepyfile(**{f"test_{name}": source})

    result = rooted.runpytest_subprocess()

    result.assert_outcomes(passed=expected_passed)
    assert "#2099" not in result.stdout.str()


def test_the_hint_stays_ascii() -> None:
    """A non-ASCII character in the message breaks the Windows shard.

    ``runpytest_subprocess`` reads the child's output as UTF-8, but on Windows
    the child writes it in the console codepage, so an em dash in the message
    arrives as 0x97 and every probe above dies with ``UnicodeDecodeError``
    instead of asserting anything. Pinning it here fails on any platform, so
    the next em dash is caught before CI has to say it.
    """

    from .conftest import _LEFTOVER_LOOP_HINT

    offenders = sorted({c for c in _LEFTOVER_LOOP_HINT if ord(c) > 127})
    assert not offenders, f"non-ASCII in the emitted hint: {offenders}"
