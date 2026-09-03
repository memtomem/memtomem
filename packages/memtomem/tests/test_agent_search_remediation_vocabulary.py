"""Everything ``mm agent search`` tells you to do must be a thing you can do.

Two separate review findings on #2195 were the same defect at two depths. The
first: the empty-result explainer reported the merged namespace as
``--namespace 'agent-runtime:planner,shared'``, an option this verb rejects.
The second, found only after the first was fixed: the hidden-rows hint ends in
``pass namespace="agent-runtime:*"``, which is a working query on ``mem_search``
and not a thing this verb takes at all — and the glob reaches *every* agent's
private scope rather than the one being asked about.

Fixing instances one at a time was the wrong shape of work. This verb assembles
its query out of options nobody typed, then borrows three layers of prose
written for surfaces whose vocabulary is different — the shared empty-result
explainer, the shared search service's hints, and its own notes. "Which of
those strings names an option we do not have" is not answerable by reading any
one of them, which is why reading them one at a time produced one finding per
round.

So this stops adjudicating and starts observing: drive the verb over its option
matrix against every store shape that changes what it says, then read the
remediation *actually emitted* and check it against the option list taken from
the command object. A fourth prose layer added tomorrow is covered without
anybody remembering to come back here, and a renamed flag updates the expected
set on its own.

The rule has two halves, because remediation comes in two spellings:

* a CLI flag (``--agent-id``) must be one this command accepts;
* a keyword suggestion (``namespace="…"``) must either name one of this
  command's own parameters or be attributed to a command that takes it —
  ``mem_embedding_reset(mode="status")`` is attributed and legible, a bare
  ``pass namespace="…"`` is not, and that is exactly the difference between
  the hint that was fine and the hint that was not.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.search.pipeline import RetrievalStats

from test_agent_cmd import _patched_cli_components, _search_components

#: Option-shaped tokens: ``--agent-id``, ``-k``. Trailing punctuation is left
#: out of the capture so ``--agent-id,`` and ``--agent-id.`` both read as the
#: flag. A bare ``--`` is not a flag and never matches.
_FLAG = re.compile(r"--[a-z][a-z0-9-]*|(?<![\w-])-[a-z](?![\w-])")

#: A keyword suggestion, as the prose spells one: ``namespace="archive:*"``.
_KWARG = re.compile(r"(?<![\w.])([a-z_][a-z0-9_]*)=")

#: An attributed call — ``mem_embedding_reset(mode="status")``. Spans matching
#: this are removed before the kwarg scan, because the command that takes the
#: keyword is named right there.
_ATTRIBUTED_CALL = re.compile(r"\b[a-z_][a-z0-9_]*\([^)]*\)")


def _accepted_vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    """The flags and parameter names this command actually accepts.

    Read off the Click command rather than listed here, so renaming a flag or
    adding one cannot leave this guard asserting yesterday's vocabulary. The
    group is walked by name to avoid importing the callback directly — the
    thing being guarded is the command as the CLI exposes it.
    """

    agent = cli.commands["agent"]
    search = agent.commands["search"]
    flags: set[str] = set()
    names: set[str] = set()
    for param in search.params:
        flags.update(getattr(param, "opts", []) or [])
        flags.update(getattr(param, "secondary_opts", []) or [])
        names.add(param.name)
    # Click adds the help option to the rendered command, not to ``params``
    # on every version; include both spellings so the guard never fails on a
    # flag the framework owns rather than this verb.
    flags.update({"-h", "--help"})
    return frozenset(flags), frozenset(names)


def _run(monkeypatch, argv: list[str], *, comp, session_ns: str | None):
    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
    monkeypatch.setattr(
        "memtomem.cli._session_state.resolve_session_write_namespace",
        AsyncMock(return_value=session_ns),
    )
    return CliRunner().invoke(cli, argv)


#: Store shapes that change what the verb says. Each one lands on a different
#: branch of the explainer, and the last one is the only way to reach the
#: hidden-rows hint.
_STORES: dict[str, tuple[list[tuple[str, int]], RetrievalStats]] = {
    "empty-index": ([], RetrievalStats()),
    "no-namespace-matches": ([("default", 9)], RetrievalStats()),
    "agent-namespace-matches": (
        [("agent-runtime:planner", 3), ("agent-runtime:coder", 2), ("shared", 1), ("default", 9)],
        RetrievalStats(),
    ),
    "numeric-looking-namespace": ([("3", 4), ("default", 9)], RetrievalStats()),
    "rows-hidden-behind-system-prefixes": (
        [("default", 9)],
        RetrievalStats(hidden_system_ns=3, hidden_by_prefix={"agent-runtime:": 2, "archive:": 1}),
    ),
}

#: Every way the three scoping options can be combined, plus the two agent
#: origins the command distinguishes. ``None`` session means unresolved.
_AGENT_ORIGINS: dict[str, tuple[list[str], str | None]] = {
    "explicit-flag": (["-a", "planner"], None),
    "session-binding": ([], "agent-runtime:coder"),
    "unresolved": ([], None),
}
_SHARED_OPTIONS: dict[str, list[str]] = {
    "default": [],
    "no-include-shared": ["--no-include-shared"],
    "repointed-shared": ["--shared-namespace", "shared:myproj"],
    "no-include-shared-and-repointed": [
        "--no-include-shared",
        "--shared-namespace",
        "shared:myproj",
    ],
}


@pytest.mark.parametrize("store", sorted(_STORES))
@pytest.mark.parametrize("shared", sorted(_SHARED_OPTIONS))
@pytest.mark.parametrize("origin", sorted(_AGENT_ORIGINS))
def test_every_flag_it_names_is_a_flag_it_accepts(monkeypatch, origin, shared, store) -> None:
    """No cell of the matrix may name an option this command rejects."""
    accepted_flags, _ = _accepted_vocabulary()
    namespaces, stats = _STORES[store]
    agent_argv, session_ns = _AGENT_ORIGINS[origin]

    comp = _search_components(namespaces=namespaces)
    comp.search_pipeline.search = AsyncMock(return_value=([], stats))
    result = _run(
        monkeypatch,
        ["agent", "search", "deploy", *agent_argv, *_SHARED_OPTIONS[shared]],
        comp=comp,
        session_ns=session_ns,
    )

    assert result.exit_code == 0, result.output
    named = set(_FLAG.findall(result.stderr))
    unknown = named - accepted_flags
    assert not unknown, (
        f"{origin}/{shared}/{store} told the reader to use {sorted(unknown)}, "
        f"which `mm agent search` does not accept.\nstderr: {result.stderr}"
    )


@pytest.mark.parametrize("store", sorted(_STORES))
@pytest.mark.parametrize("shared", sorted(_SHARED_OPTIONS))
@pytest.mark.parametrize("origin", sorted(_AGENT_ORIGINS))
def test_every_keyword_it_suggests_is_attributed_or_its_own(
    monkeypatch, origin, shared, store
) -> None:
    """A bare ``foo="bar"`` claims to be an argument of the command you just
    ran. It has to be one, or it has to say whose it is."""
    _, accepted_names = _accepted_vocabulary()
    namespaces, stats = _STORES[store]
    agent_argv, session_ns = _AGENT_ORIGINS[origin]

    comp = _search_components(namespaces=namespaces)
    comp.search_pipeline.search = AsyncMock(return_value=([], stats))
    result = _run(
        monkeypatch,
        ["agent", "search", "deploy", *agent_argv, *_SHARED_OPTIONS[shared]],
        comp=comp,
        session_ns=session_ns,
    )

    assert result.exit_code == 0, result.output
    unattributed = _ATTRIBUTED_CALL.sub(" ", result.stderr)
    suggested = set(_KWARG.findall(unattributed))
    unknown = suggested - accepted_names
    assert not unknown, (
        f"{origin}/{shared}/{store} suggested {sorted(unknown)} as though it were an "
        f"argument of this command, without naming the command that takes it.\n"
        f"stderr: {result.stderr}"
    )


class TestTheGuardItself:
    """A guard that cannot fail is a guard that proves nothing.

    Both halves are self-tested against the two real findings that motivated
    them, so the matrix above is known to be reading the strings it claims to
    read rather than passing on an empty scan.
    """

    def test_the_flag_scan_catches_the_original_finding(self) -> None:
        """The first review finding, verbatim."""
        accepted_flags, _ = _accepted_vocabulary()
        leaked = (
            "No results found: --namespace 'agent-runtime:planner,shared' matches none "
            "of the namespaces this index has."
        )

        assert set(_FLAG.findall(leaked)) - accepted_flags == {"--namespace"}

    def test_the_keyword_scan_catches_the_second_finding(self) -> None:
        """The second, one prose layer further out."""
        _, accepted_names = _accepted_vocabulary()
        leaked = (
            "3 result(s) hidden in system namespaces: 2 in agent-runtime:* (pass "
            'namespace="agent-runtime:*" to include them).'
        )
        unattributed = _ATTRIBUTED_CALL.sub(" ", leaked)

        assert set(_KWARG.findall(unattributed)) - accepted_names == {"namespace"}

    def test_an_attributed_keyword_is_not_flagged(self) -> None:
        """``mem_embedding_reset(mode="status")`` names the command that takes
        ``mode``, so it is legible and must not trip the scan — otherwise the
        guard would push the degradation hint into vaguer wording for nothing."""
        _, accepted_names = _accepted_vocabulary()
        attributed = (
            "Fix: run `mm embedding-reset` (CLI) or "
            'mem_embedding_reset(mode="status") (MCP) for the reset options.'
        )
        unattributed = _ATTRIBUTED_CALL.sub(" ", attributed)

        assert not set(_KWARG.findall(unattributed)) - accepted_names

    def test_the_accepted_set_is_read_from_the_command(self) -> None:
        """If this ever comes back empty the matrix above passes vacuously, so
        the source of the expected vocabulary is pinned too."""
        accepted_flags, accepted_names = _accepted_vocabulary()

        assert {"-a", "--agent-id", "--no-include-shared", "--shared-namespace"} <= accepted_flags
        assert {"agent_id", "include_shared", "shared_namespace"} <= accepted_names
        assert "--namespace" not in accepted_flags
        assert "namespace" not in accepted_names
