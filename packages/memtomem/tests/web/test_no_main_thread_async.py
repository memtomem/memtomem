"""Armed-ness pins for the ``tests/web`` coroutine-test ban (issue #2099).

The ban is enforced by ``pytest_collection_modifyitems`` in ``conftest.py`` so
it fires whenever these modules are collected, not only when a guard test is
selected. What needs pinning here is that the hook would actually fire: a
predicate that silently answered "no", or a hook that a selector can outrun,
makes the ban vacuous and hands #2099 back.

Two layers, because each misses what the other catches:

* ``pytester`` cases run a real pytest session over real async items — the only
  way to pin pytest-asyncio's own classification (plain coroutines, async
  ``staticmethod``s) and the hook's ordering against ``-m``/``-k``.
* Fake-item cases pin the path axis directly, including the case a ``pytester``
  run cannot stage: a coroutine collected from *outside* this directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import is_main_thread_async_item

_WEB_TESTS = Path(__file__).resolve().parent
pytest_plugins = ["pytester"]

_OFFENDERS = """
class TestProbe:
    @staticmethod
    async def test_async_staticmethod() -> None:
        assert True


async def test_plain_coroutine() -> None:
    assert True


def test_sync_neighbour() -> None:
    assert True
"""


@pytest.fixture
def web_like_dir(pytester: pytest.Pytester) -> pytest.Pytester:
    """A pytest session whose conftest is *this* directory's, hook and all.

    The conftest computes its own directory from ``__file__``, so a verbatim
    copy makes the temporary directory a stand-in for ``tests/web`` — the hook
    under test runs against genuinely collected items rather than a re-import.
    """

    # The temporary session gets no ini of its own, and pytest-asyncio's default
    # (strict) mode would not classify a bare coroutine as an async test at all
    # — the run has to match the repo's ``asyncio_mode = "auto"`` for the
    # rejection to mean anything here.
    pytester.makeini(
        """
        [pytest]
        asyncio_mode = auto
        asyncio_default_fixture_loop_scope = function
        markers =
            browser: mirrors the repo marker so ``-m browser`` parses
        """
    )
    # Two async fixtures the copied conftest does not have, so the cases below
    # can pin behaviour that only a *parent* declaration produces: a sync
    # override of an async name, and a session-scoped async fixture whose scope
    # node is the session root rather than the requesting test.
    pytester.makeconftest(
        (_WEB_TESTS / "conftest.py").read_text(encoding="utf-8")
        + """

@pytest.fixture
async def overridable_fixture() -> int:
    return 1


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def a_session_scoped_async_fixture() -> int:
    return 1
"""
    )
    return pytester


@pytest.mark.parametrize("selector", [[], ["-m", "browser"], ["-k", "sync_neighbour"]])
def test_a_real_async_item_is_rejected_whatever_the_selector(
    web_like_dir: pytest.Pytester, selector: list[str]
) -> None:
    """Deselection must not hide an offender — that is why the hook is tryfirst."""

    web_like_dir.makepyfile(test_probe=_OFFENDERS)

    result = web_like_dir.runpytest(*selector)

    result.stderr.fnmatch_lines(["*#2099*"])
    assert "test_async_staticmethod" in result.stderr.str()
    assert "test_plain_coroutine" in result.stderr.str()
    assert "test_sync_neighbour" not in result.stderr.str()


def test_a_directory_of_sync_tests_runs_clean(web_like_dir: pytest.Pytester) -> None:
    """The positive control's mirror: the hook must not fail an honest module."""

    web_like_dir.makepyfile(
        test_ok="""
        def test_sync() -> None:
            assert True
        """
    )

    web_like_dir.runpytest().assert_outcomes(passed=1)


class _FakeItem:
    """Enough of ``pytest.Item`` for the predicate: a path and asyncio-ness."""

    def __init__(self, *, path: Path, is_async: bool) -> None:
        self.path = path
        self.nodeid = f"{path}::fake"
        self._is_async = is_async


@pytest.fixture
def classify(monkeypatch: pytest.MonkeyPatch):
    import pytest_asyncio

    monkeypatch.setattr(
        pytest_asyncio, "is_async_test", lambda item: getattr(item, "_is_async", False)
    )
    return is_main_thread_async_item


def test_a_coroutine_collected_outside_this_directory_is_not_flagged(classify) -> None:
    """``test_upload_quarantine.py``'s cases collect through ``tests/`` and are safe."""

    item = _FakeItem(path=_WEB_TESTS.parent / "test_web_routes.py", is_async=True)
    assert not classify(item)


def test_a_coroutine_collected_from_this_directory_is_flagged(classify) -> None:
    item = _FakeItem(path=_WEB_TESTS / "test_hypothetical.py", is_async=True)
    assert classify(item)


_ASYNC_FIXTURE_CONSUMER = """
import pytest


@pytest.fixture
async def an_async_fixture() -> int:
    return 1


def test_sync_body_async_fixture(an_async_fixture: int) -> None:
    assert an_async_fixture == 1
"""

_ASYNC_GEN_FIXTURE_CONSUMER = """
import pytest


@pytest.fixture
async def an_async_gen_fixture():
    yield 1


def test_sync_body_async_gen_fixture(an_async_gen_fixture: int) -> None:
    assert an_async_gen_fixture == 1
"""

_DYNAMIC_FIXTURE_REQUEST = """
import pytest


@pytest.fixture
async def an_async_fixture() -> int:
    return 1


def test_dynamic_request(request: pytest.FixtureRequest) -> None:
    assert request.getfixturevalue("an_async_fixture") == 1
"""

_ISOLATED_ASYNCIO_CASE = """
import unittest


class ProbeCase(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_asyncio(self) -> None:
        self.assertTrue(True)
"""

_SYNC_OVERRIDE_OF_AN_ASYNC_FIXTURE = """
import pytest


@pytest.fixture
def overridable_fixture() -> str:
    return "the sync definition here is the one pytest resolves"


def test_sync_override(overridable_fixture: str) -> None:
    assert overridable_fixture
"""

_SESSION_SCOPED_ASYNC_FIXTURE = """
def test_dynamic_session_scoped(request) -> None:
    assert request.getfixturevalue("a_session_scoped_async_fixture") == 1
"""


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("async_fixture", _ASYNC_FIXTURE_CONSUMER),
        ("async_gen_fixture", _ASYNC_GEN_FIXTURE_CONSUMER),
        ("dynamic_fixture", _DYNAMIC_FIXTURE_REQUEST),
        ("isolated_asyncio", _ISOLATED_ASYNCIO_CASE),
        ("session_scoped", _SESSION_SCOPED_ASYNC_FIXTURE),
    ],
)
def test_other_ways_of_driving_a_loop_are_rejected_too(
    web_like_dir: pytest.Pytester, name: str, source: str
) -> None:
    """A sync body is not proof: each of these still spins a loop on this thread."""

    web_like_dir.makepyfile(**{f"test_{name}": source})

    result = web_like_dir.runpytest()

    # Rejection lands as a setup error (fixtures) or a session abort (async test
    # functions); either way nothing may run.
    assert "#2099" in result.stderr.str() + result.stdout.str()
    assert result.parseoutcomes().get("passed", 0) == 0


def test_a_sync_fixture_is_legal_even_where_an_async_one_is_declared(
    web_like_dir: pytest.Pytester,
) -> None:
    """The ban is on fixtures pytest actually resolves, not on every definition."""

    web_like_dir.makepyfile(test_override=_SYNC_OVERRIDE_OF_AN_ASYNC_FIXTURE)

    web_like_dir.runpytest().assert_outcomes(passed=1)
