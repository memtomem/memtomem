from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.search.pipeline import RetrievalStats


def _mock_empty_search(namespaces: list[tuple[str, int]] | None = None) -> tuple:
    """Mock cli_components returning empty search results.

    ``namespaces`` is what the store would report; the default (none) is an
    empty index, where the index hint is the right answer.
    """
    pipeline_mock = AsyncMock(return_value=([], RetrievalStats()))
    config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[]))
    comp = SimpleNamespace(
        search_pipeline=SimpleNamespace(search=pipeline_mock),
        storage=SimpleNamespace(list_namespaces=AsyncMock(return_value=namespaces or [])),
        config=config,
    )

    @asynccontextmanager
    async def fake():
        yield comp

    return fake, pipeline_mock


def _mock_empty_recall(namespaces: list[tuple[str, int]] | None = None) -> tuple:
    """Mock cli_components returning empty recall results."""
    storage = SimpleNamespace(
        recall_chunks=AsyncMock(return_value=[]),
        list_namespaces=AsyncMock(return_value=namespaces or []),
    )
    config = SimpleNamespace(
        search=SimpleNamespace(system_namespace_prefixes=()),
        indexing=SimpleNamespace(project_memory_dirs=[]),
    )
    comp = SimpleNamespace(storage=storage, config=config)

    @asynccontextmanager
    async def fake():
        yield comp

    return fake, storage


HINT = "No results found. See `mm status` to confirm your index has chunks."


class TestSearchEmptyState:
    """mm search prints a friendly hint on stderr when results are empty."""

    @pytest.mark.parametrize("fmt", ["table", "plain"])
    def test_non_json_formats_print_hint_to_stderr(self, monkeypatch, fmt: str) -> None:
        fake, _ = _mock_empty_search()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", "--format", fmt, "hello"])

        assert result.exit_code == 0
        assert HINT in result.stderr

    def test_json_format_stdout_unchanged(self, monkeypatch) -> None:
        fake, _ = _mock_empty_search()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", "--format", "json", "hello"])

        assert result.exit_code == 0
        assert result.output.strip() == "[]"
        assert result.stderr == ""


class TestRecallEmptyState:
    """mm recall prints a friendly hint on stderr when results are empty."""

    @pytest.mark.parametrize("fmt", ["table", "plain"])
    def test_non_json_formats_print_hint_to_stderr(self, monkeypatch, fmt: str) -> None:
        fake, _ = _mock_empty_recall()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["recall", "--format", fmt])

        assert result.exit_code == 0
        assert HINT in result.stderr

    def test_json_format_stdout_unchanged(self, monkeypatch) -> None:
        fake, _ = _mock_empty_recall()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["recall", "--format", "json"])

        assert result.exit_code == 0
        assert result.output.strip() == "[]"
        assert result.stderr == ""


def _raising_namespace_recall_store() -> tuple:
    """Recall's equivalent: a store whose ``list_namespaces`` fails."""

    async def _explode() -> list[tuple[str, int]]:
        raise RuntimeError("list_namespaces must not be called for this format")

    storage = SimpleNamespace(recall_chunks=AsyncMock(return_value=[]), list_namespaces=_explode)
    config = SimpleNamespace(
        search=SimpleNamespace(system_namespace_prefixes=()),
        indexing=SimpleNamespace(project_memory_dirs=[]),
    )
    comp = SimpleNamespace(storage=storage, config=config)

    @asynccontextmanager
    async def fake():
        yield comp

    return fake, comp


def _raising_namespace_store() -> tuple:
    """A store whose ``list_namespaces`` fails, for the formats that must
    never call it."""
    pipeline_mock = AsyncMock(return_value=([], RetrievalStats()))
    config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[]))

    async def _explode() -> list[tuple[str, int]]:
        raise RuntimeError("list_namespaces must not be called for this format")

    comp = SimpleNamespace(
        search_pipeline=SimpleNamespace(search=pipeline_mock),
        storage=SimpleNamespace(list_namespaces=_explode),
        config=config,
    )

    @asynccontextmanager
    async def fake():
        yield comp

    return fake, comp


class TestEmptyResultNamesTheFilter:
    """Issue #2255: a filter that emptied the result must not be reported as
    an index problem.

    The old text sent the reader to ``mm status`` for the one subsystem that
    was not wrong — and zero results is a plausible enough answer that the
    stated cause gets believed.
    """

    def _search(self, monkeypatch, argv: list[str], namespaces=None):
        fake, _ = _mock_empty_search(namespaces)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        return CliRunner().invoke(cli, ["search", *argv])

    def _recall(self, monkeypatch, argv: list[str], namespaces=None):
        fake, _ = _mock_empty_recall(namespaces)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
        return CliRunner().invoke(cli, ["recall", *argv])

    def test_an_unknown_namespace_is_named_instead_of_the_index(self, monkeypatch) -> None:
        result = self._search(
            monkeypatch, ["-n", "nosuch", "hello"], namespaces=[("default", 7), ("work", 2)]
        )

        assert result.exit_code == 0
        assert "--namespace 'nosuch' matches none of the namespaces" in result.stderr
        assert "default (7), work (2)" in result.stderr
        assert HINT not in result.stderr

    def test_an_integer_namespace_suggests_the_count_flag(self, monkeypatch) -> None:
        """``-n`` is one keystroke from ``-k`` and reads as "number" in most
        other CLIs — the case that motivated the issue."""
        result = self._search(monkeypatch, ["-n", "3", "hello"], namespaces=[("default", 7)])

        assert "did you mean `-k 3`?" in result.stderr

    def test_recall_suggests_its_own_count_flag(self, monkeypatch) -> None:
        """``mm recall`` counts with ``-l``, so suggesting ``-k`` would send
        the reader to a flag that does not exist there."""
        result = self._recall(monkeypatch, ["-n", "3"], namespaces=[("default", 7)])

        assert "did you mean `-l 3`?" in result.stderr

    def test_a_glob_that_matches_is_not_blamed(self, monkeypatch) -> None:
        """``matches`` is the Python twin of the SQL the query ran, so a glob
        the store satisfies must fall through to the generic message."""
        result = self._search(
            monkeypatch, ["-n", "proj:*", "-t", "x", "hello"], namespaces=[("proj:a", 4)]
        )

        assert "matches none of the namespaces" not in result.stderr
        assert "This query included: --tag-filter 'x', --namespace 'proj:*'" in result.stderr

    def test_a_comma_list_with_one_live_member_is_not_blamed(self, monkeypatch) -> None:
        result = self._search(monkeypatch, ["-n", "gone,work", "hello"], namespaces=[("work", 1)])

        assert "matches none of the namespaces" not in result.stderr

    def test_an_explicit_system_namespace_is_matched_not_blamed(self, monkeypatch) -> None:
        """Naming ``archive:summary`` opts into it, so the default
        system-prefix exclusion is off and the namespace really is present."""
        result = self._search(
            monkeypatch, ["-n", "archive:summary", "hello"], namespaces=[("archive:summary", 3)]
        )

        assert "matches none of the namespaces" not in result.stderr

    def test_a_non_namespace_filter_is_named(self, monkeypatch) -> None:
        result = self._search(monkeypatch, ["-t", "nope", "hello"], namespaces=[("default", 7)])

        assert "This query included: --tag-filter 'nope'" in result.stderr
        assert HINT not in result.stderr

    def test_an_empty_store_still_blames_the_index_under_a_filter(self, monkeypatch) -> None:
        """The fallback is checked for every filtered query, not only
        namespaced ones: with nothing indexed, the index *is* the answer."""
        for argv in (["-t", "nope", "hello"], ["-n", "nosuch", "hello"]):
            result = self._search(monkeypatch, argv, namespaces=[])
            assert HINT in result.stderr, argv

    def test_an_empty_namespace_value_is_not_a_filter(self, monkeypatch) -> None:
        """``run_search`` normalizes ``-n ''`` away (``namespace or
        current_namespace``), so the query ran unfiltered and naming the
        namespace as the cause would be a lie."""
        result = self._search(monkeypatch, ["-n", "", "hello"], namespaces=[("default", 7)])

        # The namespace branch must not fire: the query ran without a
        # namespace filter, so calling it the cause would be a lie. It is
        # still reported as part of the command line, which is a fact.
        assert "matches none of the namespaces" not in result.stderr
        assert "This query included: --namespace ''" in result.stderr

    def test_the_namespace_list_is_truncated(self, monkeypatch) -> None:
        namespaces = [(f"ns{i}", i) for i in range(12)]
        result = self._search(monkeypatch, ["-n", "nosuch", "hello"], namespaces=namespaces)

        assert "ns7 (7)" in result.stderr
        assert "ns8 (8)" not in result.stderr
        assert "(+4 more)" in result.stderr

    @pytest.mark.parametrize("fmt", ["json", "context", "smart"])
    def test_formats_that_never_print_it_do_not_read_the_store(self, monkeypatch, fmt) -> None:
        """The diagnostic is only rendered for table/plain. Building it anyway
        would let a store read nothing displays fail the command — turning a
        silent ``[]`` into an error. A raising ``list_namespaces`` is the
        discriminator: a clean exit proves it was never called."""
        fake, _ = _raising_namespace_store()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["search", "--format", fmt, "-n", "nosuch", "hello"])

        assert result.exit_code == 0, result.output
        if fmt == "json":
            assert result.output.strip() == "[]"

    def test_recall_json_does_not_read_the_store_either(self, monkeypatch) -> None:
        """Same gate as search's, pinned on recall's own call site: a mock
        that merely succeeds would still pass if recall performed the read."""
        fake, _ = _raising_namespace_recall_store()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["recall", "--format", "json", "-n", "nosuch"])

        assert result.exit_code == 0, result.output
        assert result.output.strip() == "[]"

    def test_an_empty_scope_is_reported_as_typed(self, monkeypatch) -> None:
        """The diagnostic quotes the command line, not the searched value.

        ``mm search`` normalizes ``--scope ""`` to no filter now (#2193), so
        the empty scope is no longer what emptied the result — but someone
        reading the diagnostic is looking for the options they typed, and one
        that silently disappeared is one they cannot account for. Recall,
        which does not normalize, still has the filtering version of this:
        see ``test_recall_reports_an_empty_scope_too``.
        """
        result = self._search(monkeypatch, ["--scope", "", "hello"], namespaces=[("default", 7)])

        assert "This query included: --scope ''" in result.stderr
        assert HINT not in result.stderr

    def test_recall_reports_an_empty_scope_too(self, monkeypatch) -> None:
        result = self._recall(monkeypatch, ["--scope", ""], namespaces=[("default", 7)])

        assert "This query included: --scope ''" in result.stderr
        assert HINT not in result.stderr

    def test_recall_reports_an_empty_namespace(self, monkeypatch) -> None:
        """Recall does not go through ``run_search``, so it applies
        ``NamespaceFilter.parse("")`` verbatim — the empty namespace really is
        the filter that emptied the result, unlike on search."""
        result = self._recall(monkeypatch, ["-n", ""], namespaces=[("default", 7)])

        assert "--namespace '' matches none of the namespaces" in result.stderr
        assert HINT not in result.stderr

    @pytest.mark.parametrize(
        "argv",
        [["-t", ""], ["-t", ","], ["-s", ""], ["-n", "*"], ["--scope", "*"], ["-k", "0"]],
    )
    def test_a_non_empty_index_is_never_blamed(self, monkeypatch, argv) -> None:
        """The branch turns on what the store reports, not on judging which
        option narrowed the query. No-op filters, wildcards and ``-k 0`` all
        land here, and none of them may send the reader to ``mm status`` while
        the index demonstrably holds chunks (#2255)."""
        result = self._search(monkeypatch, [*argv, "hello"], namespaces=[("default", 7)])

        assert HINT not in result.stderr
        assert "The index has 7 chunks across 1 namespace" in result.stderr

    @pytest.mark.parametrize("argv", [["-t", ","], ["-s", ""], ["--since", ""], ["-l", "0"]])
    def test_recall_never_blames_a_non_empty_index(self, monkeypatch, argv) -> None:
        result = self._recall(monkeypatch, argv, namespaces=[("default", 7)])

        assert HINT not in result.stderr
        assert "The index has 7 chunks across 1 namespace" in result.stderr

    def test_no_filter_at_all_reports_the_inventory(self, monkeypatch) -> None:
        """Nothing was filtered and the index is not empty, so the honest
        answer states that much and no more. "nothing matched" would be a
        claim about retrieval that ``-k 0`` falsifies."""
        result = self._search(monkeypatch, ["hello"], namespaces=[("default", 7)])

        assert "so the index is not the empty one" in result.stderr
        assert "matched" not in result.stderr
        assert HINT not in result.stderr

    def test_supplied_options_are_reported_without_a_verdict(self, monkeypatch) -> None:
        """Listing what the command carried is a fact about the invocation.
        Asserting which option is responsible is what could not be kept
        honest across every option and value."""
        result = self._search(
            monkeypatch, ["-t", "x", "--scope", "user", "hello"], namespaces=[("default", 7)]
        )

        assert "This query included: --tag-filter 'x', --scope 'user'." in result.stderr
        assert "excluding matches" not in result.stderr
        assert "responsible" not in result.stderr

    @pytest.mark.parametrize("value", ["0", "00", "-1", "\u00b2", "+3", "9" * 5000])
    def test_no_count_suggestion_for_a_value_that_is_not_a_positive_count(
        self, monkeypatch, value: str
    ) -> None:
        """``\u00b2`` is ``isdigit()`` but not convertible, a 5000-digit string
        exceeds CPython's ``int()`` limit, and ``-1`` would propose
        ``LIMIT -1`` — which SQLite reads as no limit at all."""
        result = self._search(monkeypatch, ["-n", value, "hello"], namespaces=[("default", 7)])

        assert result.exit_code == 0, result.output
        assert "did you mean" not in result.stderr

    def test_the_suggestion_renders_the_normalized_value(self, monkeypatch) -> None:
        result = self._search(monkeypatch, ["-n", " 3 ", "hello"], namespaces=[("default", 7)])

        assert "did you mean `-k 3`?" in result.stderr
