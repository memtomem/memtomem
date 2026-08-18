"""The leftover-MainThread-loop message in ``conftest.pytest_runtest_setup`` (#2099).

`tests/web/conftest.py` bans coroutine tests and async fixtures in that
directory, which covers every CI invocation and the default full-suite order.
Hand-ordered paths can still put a browser spec ahead of a coroutine test
elsewhere, and what that produced before was a bare ``RuntimeError`` pointing
at whichever test came next. The root hook replaces it with an explanation, so
what needs pinning is that the hook fires on the real condition and stays
silent otherwise.
"""

from __future__ import annotations

import asyncio.events

import pytest


def _root_hook_source() -> str:
    """The shipped ``pytest_runtest_setup`` source, lifted verbatim.

    Reading it out of the real conftest instead of restating it means this
    cannot pin a stale copy of the hook.
    """

    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
    node = next(
        n for n in ast.parse(source).body if getattr(n, "name", None) == "pytest_runtest_setup"
    )
    return (
        "import asyncio.events\nimport inspect\n\nimport pytest\nimport pytest_asyncio\n\n"
        + ast.get_source_segment(source, node)
        + "\n\n"
    )


pytest_plugins = ["pytester"]

_ROOT_HOOK = _root_hook_source()

_PROBE = """
async def test_coroutine() -> None:
    assert True


def test_sync() -> None:
    assert True
"""


@pytest.fixture
def rooted(pytester: pytest.Pytester) -> pytest.Pytester:
    """A session running this repo's root conftest hook against real items."""

    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        asyncio_default_fixture_loop_scope = function
        """
    )
    return pytester


_PARK_A_LOOP = """
import asyncio.events

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # Stand in for Playwright's parked dispatcher greenlet: a loop registered as
    # running on MainThread that nothing is going to clear.
    config._probe_loop = asyncio.new_event_loop()
    asyncio.events._set_running_loop(config._probe_loop)


def pytest_sessionfinish(session, exitstatus):
    asyncio.events._set_running_loop(None)
    session.config._probe_loop.close()
"""


def test_a_coroutine_test_under_a_parked_loop_says_why(rooted: pytest.Pytester) -> None:
    rooted.makeconftest(_ROOT_HOOK + _PARK_A_LOOP)
    rooted.makepyfile(test_probe=_PROBE)

    result = rooted.runpytest()

    # Setup-phase failure, so it lands as an error; the sync neighbour still runs.
    result.assert_outcomes(errors=1, passed=1)
    result.stdout.fnmatch_lines(["*#2099*"])


def test_without_a_parked_loop_the_hook_is_silent(rooted: pytest.Pytester) -> None:
    rooted.makeconftest(_ROOT_HOOK)
    rooted.makepyfile(test_probe=_PROBE)

    rooted.runpytest().assert_outcomes(passed=2)


def test_the_real_session_has_no_leftover_loop() -> None:
    """This suite's own state — the condition the hook watches for must be absent."""

    assert asyncio.events._get_running_loop() is None
