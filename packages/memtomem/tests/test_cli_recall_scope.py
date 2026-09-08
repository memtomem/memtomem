"""``mm recall --scope`` refuses what ``mm search --scope`` refuses (#2295).

#2291 put the closed ADR-0011 tier vocabulary in front of ``mm search``;
``mm recall`` kept parsing the value directly, so ``--scope User`` was an
error on one command and a successful, empty result on the other — with
the store opened and closed first. These mirror ``test_cli_search_hints``'s
scope tests: the refusal names the flag and carries the validator's own
reason, it happens before components open, and the value that reaches
storage is the normalized one while the diagnostic still quotes the
command line.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


def _mock_components():
    storage = SimpleNamespace(
        recall_chunks=AsyncMock(return_value=[]),
        list_namespaces=AsyncMock(return_value=[("default", 7)]),
    )
    config = SimpleNamespace(
        search=SimpleNamespace(system_namespace_prefixes=()),
        indexing=SimpleNamespace(project_memory_dirs=[]),
    )
    comp = SimpleNamespace(storage=storage, config=config)

    @asynccontextmanager
    async def fake():
        yield comp

    fake.storage = storage
    return fake


def _invoke(monkeypatch, *args):
    fake = _mock_components()
    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    result = CliRunner().invoke(cli, ["recall", *args])
    return result, fake.storage


def _invoke_with_opened_probe(monkeypatch, *args):
    opened = False

    @asynccontextmanager
    async def fake():
        nonlocal opened
        opened = True
        yield SimpleNamespace()

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    result = CliRunner().invoke(cli, ["recall", *args])
    return result, opened


class TestRecallScopeVocabulary:
    def test_a_scope_that_is_not_a_tier_is_refused_with_its_own_reason(self, monkeypatch) -> None:
        result, storage = _invoke(monkeypatch, "--scope", "User")

        assert result.exit_code != 0
        assert "invalid --scope value 'User'" in result.stderr
        assert "is not a scope tier" in result.stderr
        assert "comma list" not in result.stderr
        storage.recall_chunks.assert_not_awaited()

    def test_a_glob_that_names_no_tier_is_refused(self, monkeypatch) -> None:
        result, _ = _invoke(monkeypatch, "--scope", "projet_*")

        assert result.exit_code != 0
        assert "invalid --scope value 'projet_*'" in result.stderr
        assert "matches no scope tier" in result.stderr

    @pytest.mark.parametrize("value", ["projet_local", "project_*,user"])
    def test_rejected_before_components_open(self, monkeypatch, value: str) -> None:
        """Parity with ``mm search``: a value the CLI cannot honor must not
        pay for opening the store. The comma/glob mix used to be caught
        *inside* the component block, by the generic CLI error path."""
        result, opened = _invoke_with_opened_probe(monkeypatch, "--scope", value)

        assert result.exit_code != 0
        assert f"invalid --scope value '{value}'" in result.stderr
        assert opened is False

    @pytest.mark.parametrize("value", ["user", "user,project_local", "project_*", "proj*"])
    def test_each_spelling_on_its_own_still_runs(self, monkeypatch, value: str) -> None:
        result, storage = _invoke(monkeypatch, "--scope", value)

        assert result.exit_code == 0, result.stderr
        storage.recall_chunks.assert_awaited_once()

    @pytest.mark.parametrize(
        ("typed", "scopes"), [("  user  ", ("user",)), ("", None), ("   ", None)]
    )
    def test_the_normalized_scope_is_what_gets_recalled(
        self, monkeypatch, typed: str, scopes: tuple[str, ...] | None
    ) -> None:
        """Exit code 0 does not prove the right value was recalled.

        ``--scope ""`` is unset and ``--scope "  user  "`` is ``user``; parsing
        either as typed puts ``scope IN ('')`` or ``scope IN (' user ')`` into
        the SQL and answers an empty result set with no error at all.
        """
        result, storage = _invoke(monkeypatch, "--scope", typed)

        assert result.exit_code == 0, result.stderr
        scope_filter = storage.recall_chunks.await_args.kwargs["scope_filter"]
        assert (scope_filter.scopes if scope_filter is not None else None) == scopes

    def test_the_diagnostic_still_quotes_the_scope_as_typed(self, monkeypatch) -> None:
        """The recalled value and the reported one come from different
        places: storage gets ``user``, the reader sees the option on their
        screen."""
        result, _ = _invoke(monkeypatch, "--scope", "  user  ")

        assert result.exit_code == 0
        assert "This query included: --scope '  user  '" in result.stderr

    def test_json_output_is_refused_on_stderr_too(self, monkeypatch) -> None:
        """Machine consumers pipe stdout; the refusal must not land there as
        a bare ``[]`` that reads like a valid empty recall."""
        result, _ = _invoke(monkeypatch, "--scope", "User", "--format", "json")

        assert result.exit_code != 0
        assert "is not a scope tier" in result.stderr
        assert result.stdout.strip() == ""
