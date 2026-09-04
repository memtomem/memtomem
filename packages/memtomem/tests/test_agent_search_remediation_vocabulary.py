"""Everything ``mm agent search`` tells you to do names a command that takes it.

Two review findings on #2195 were the same defect at two depths. The first: the
empty-result explainer reported the merged namespace as ``--namespace
'agent-runtime:planner,shared'``, an option this verb rejects. The second, found
only after the first was fixed: the hidden-rows hint ends in ``pass
namespace="agent-runtime:*"``, a working query on ``mem_search`` and not a thing
this verb takes at all — and a glob reaching *every* agent's private scope
rather than the one being asked about.

Fixing instances one at a time was the wrong shape of work. This verb assembles
its query out of options nobody typed, then borrows several layers of prose
written for surfaces whose vocabulary is different: the shared empty-result
explainer, the shared search service's hints, the shared error translator, and
its own notes. "Which of those strings names an option we do not have" is not
answerable by reading any one of them, which is why reading them one at a time
produced one finding per round.

So this stops adjudicating and observes: drive the verb across its option matrix
against every store shape that changes what it says, then read the remediation
*actually emitted* and check it against the option list taken off the Click
command object. A new prose layer is covered without anybody remembering to come
back here, and a renamed flag updates the expected set on its own.

The rule is one idea in two spellings. Remediation either **attributes** itself
to a command — a backticked command line (```mm embedding-reset --mode
apply-current```) or an MCP tool call (``mem_embedding_reset(mode="status")``) —
or it is spoken in this command's own voice, in which case every flag it names
must be one this command accepts and no bare ``foo=`` suggestion is allowed at
all. That distinction is exactly what separated the hint that was fine from the
hint that was not: ``mem_embedding_reset(mode="status")`` says whose ``mode``
it is, and ``pass namespace="…"`` does not.

**What this guard does not promise.** It establishes *lexical ownership* only:
that the reader is never told to use an option that does not exist here. It
cannot tell you that an accepted flag was suggested with a workable value, that
the advice helps in the state the caller is actually in, that two notes do not
contradict each other, or that remediation was emitted at all when it was
needed. Those are behavioral claims and are pinned individually in
``test_agent_cmd.py``. A scan finding nothing is not evidence of a good message,
which is why every cell below also asserts that the producer it exists to
exercise actually spoke.

It reads stderr only, and that is a scope decision rather than an oversight.
Result bodies go to stdout and are indexed memory content — user text, which
this verb quotes rather than authors, and which would produce findings about
somebody's notes. ``--help`` is also stdout and is the option list itself, not
advice about what to do next; a flag named there is accepted by construction.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.search.pipeline import RetrievalStats

from test_agent_cmd import _patched_cli_components, _search_components

#: Dash characters a human or a formatter can substitute for ASCII ``-``. An
#: en-dash in ``--namespace`` makes the token invisible to a naive scan while
#: staying perfectly readable as advice, so normalize before extracting. Prose
#: em-dashes normalize too and then fall out below, having no alphanumerics.
_DASHES = str.maketrans(dict.fromkeys("‐‑‒–—―−", "-"))

#: Punctuation that can hug a flag in a sentence — ``--agent-id,`` or
#: ``(--agent-id)`` — without being part of it.
_EDGE_PUNCTUATION = ".,;:!?()[]{}<>'\"`“”‘’"

#: A keyword suggestion as prose spells one, with or without spaces around the
#: ``=``. The lookbehind keeps ``--format=json`` out: that is a flag, scanned as
#: a flag, and its own rule already covers it.
_KWARG = re.compile(r"(?<![\w.\-])([a-z_][a-z0-9_]*)\s*=\s*")

#: Remediation that says whose command it is. A backticked line beginning with
#: this CLI's own name, or an MCP tool call. Deliberately *not* "any identifier
#: followed by parentheses": ``bogus(namespace=x)`` attributes nothing, and
#: exempting it would let the guard be talked out of the very finding it exists
#: to catch. A bare ```--namespace``` in backticks is not attributed either —
#: backticks are formatting, not a citation.
_ATTRIBUTED = re.compile(
    r"`\s*(?:mm|memtomem)\s[^`]*`"  # `mm embedding-reset --mode apply-current`
    r"|\bmem_[a-z0-9_]+\s*\([^)]*\)"  # mem_embedding_reset(mode="status")
)


def _own_voice(text: str) -> str:
    """``text`` with every attributed span removed.

    What remains is what the command said about itself, which is the only part
    its own option list has any authority over.
    """

    return _ATTRIBUTED.sub(" ", text)


def _flags_named(text: str) -> set[str]:
    """Every option-shaped token, compared whole.

    Whole-token comparison is the point: a regex that captures the longest
    *valid-looking* prefix reads ``--agent-id_extra`` as the accepted
    ``--agent-id`` and passes something Click rejects. Splitting on whitespace
    and trimming sentence punctuation keeps the token intact, so a suffix that
    makes it invalid keeps it out of the accepted set.
    """

    found: set[str] = set()
    for raw in text.translate(_DASHES).split():
        token = raw.strip(_EDGE_PUNCTUATION)
        if not token.startswith("-"):
            continue
        # A prose dash, or ``--`` on its own: option-shaped only by accident.
        if not any(char.isalnum() for char in token):
            continue
        found.add(token)
    return found


def _keywords_suggested(text: str) -> set[str]:
    """Every bare ``foo=`` in the command's own voice.

    No accepted set to compare against, on purpose. ``param.name`` is callback
    vocabulary, not command-line syntax — Click takes ``--format``, never
    ``fmt=json`` — so treating those names as permitted keywords would bless
    spellings the parser refuses. In this command's own voice a keyword
    suggestion is always foreign; the way to offer one is to name the command
    that takes it.
    """

    return set(_KWARG.findall(text))


def _accepted_flags() -> frozenset[str]:
    """The flags this command actually accepts.

    Read off the Click command rather than listed here, so renaming a flag or
    adding one cannot leave this guard asserting yesterday's vocabulary. The
    group is walked by name rather than importing the callback, because the
    thing being guarded is the command as the CLI exposes it.
    """

    search = cli.commands["agent"].commands["search"]
    flags: set[str] = set()
    for param in search.params:
        flags.update(getattr(param, "opts", []) or [])
        flags.update(getattr(param, "secondary_opts", []) or [])
    # The help option belongs to the framework, not to this verb, and is not
    # always in ``params``. Naming it is never a wrong thing to tell a reader.
    flags.update({"-h", "--help"})
    return frozenset(flags)


def _run(monkeypatch, argv: list[str], *, comp, session_ns: str | None):
    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
    monkeypatch.setattr(
        "memtomem.cli._session_state.resolve_session_write_namespace",
        AsyncMock(return_value=session_ns),
    )
    return CliRunner().invoke(cli, argv)


_MISMATCH = {
    "dimension_mismatch": True,
    "stored": {"provider": "none", "model": "", "dimension": 0},
    "configured": {"provider": "onnx", "model": "bge-small-en-v1.5", "dimension": 384},
}

#: The message every cell produces no matter which branch it lands on. Used as
#: the witness wherever a cell's headline producer is out of reach, so that
#: "this cell said something" is still asserted.
_ALWAYS = "No results found"

#: Store and retrieval shapes, each reaching a producer the others do not.
#: ``witness`` is the substring proving that producer spoke, and ``origins``
#: names the agent origins that can reach it — some producers are gated on the
#: query being unpinned, which only an unresolved agent achieves. The witness is
#: what keeps a cell from passing vacuously: a producer that stops emitting
#: fails here instead of quietly shrinking the guard's coverage to nothing.
#:
#: The rerank hint (``search_service.py``) has no cell because it has no
#: reachable path: ``mm agent search`` never passes ``rerank``, so the producer's
#: ``rerank is True`` test cannot hold. A cell for it would assert nothing.
_STORES: dict[str, tuple[list[tuple[str, int]], RetrievalStats, str, frozenset[str] | None]] = {
    "empty-index": ([], RetrievalStats(), _ALWAYS, None),
    "no-namespace-matches": ([("default", 9)], RetrievalStats(), _ALWAYS, None),
    "agent-namespace-matches": (
        [("agent-runtime:planner", 3), ("agent-runtime:coder", 2), ("shared", 1), ("default", 9)],
        RetrievalStats(),
        _ALWAYS,
        None,
    ),
    "numeric-looking-namespace": (
        [("3", 4), ("default", 9)],
        RetrievalStats(),
        _ALWAYS,
        None,
    ),
    # Gated on the query being unpinned: with an agent resolved the search
    # carries a namespace, the hiding never engages, and there is nothing
    # hidden to report. Asserting the hint in those cells would be asserting a
    # bug.
    "rows-hidden-behind-system-prefixes": (
        [("default", 9)],
        RetrievalStats(hidden_system_ns=3, hidden_by_prefix={"agent-runtime:": 2, "archive:": 1}),
        "hidden in system namespaces",
        frozenset({"unresolved"}),
    ),
    "dense-leg-dropped-out": (
        [("default", 9)],
        RetrievalStats(
            bm25_candidates=1,
            dense_candidates=0,
            dense_suppressed_mismatch=True,
            mismatch_detail=_MISMATCH,
        ),
        "dense retrieval did not contribute",
        None,
    ),
}

#: Every way the three scoping options can be combined, plus the agent origins
#: the command distinguishes. ``None`` session means unresolved.
#:
#: One pair does not run a search at all: ``--no-include-shared`` with no agent
#: selects no bucket and is refused (#2296). Those cells stay in the matrix
#: because a refusal is remediation — it tells the reader what to do instead,
#: in two flag names — and dropping them would exempt the message most likely
#: to name one.
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


#: The ``shared`` spellings that drop the shared bucket. With ``origin`` also
#: unresolved there is nothing left to search, and the verb refuses.
_DROPS_SHARED = frozenset({"no-include-shared", "no-include-shared-and-repointed"})

#: What the refusal says. Its own witness: a refusal that stopped explaining
#: itself would leave those cells scanning an empty string.
_REFUSAL = "--no-include-shared needs an agent to scope to"


def _emit(monkeypatch, origin: str, shared: str, store: str):
    """Run one cell and hand back its stderr, having checked it is not empty.

    The witness assertion lives here rather than in each test so both scans see
    the same guarantee: a silent cell cannot satisfy either rule by having
    nothing to read.
    """

    namespaces, stats, witness, origins = _STORES[store]
    # A refused cell never reaches retrieval, so no store-shaped producer can
    # speak in it and the store axis collapses. The refusal is what it says
    # instead, and it is the same for every store.
    refused = origin == "unresolved" and shared in _DROPS_SHARED
    if refused:
        witness = _REFUSAL
    elif origins is not None and origin not in origins:
        witness = _ALWAYS
    agent_argv, session_ns = _AGENT_ORIGINS[origin]

    comp = _search_components(namespaces=namespaces)
    comp.search_pipeline.search = AsyncMock(return_value=([], stats))
    result = _run(
        monkeypatch,
        ["agent", "search", "deploy", *agent_argv, *_SHARED_OPTIONS[shared]],
        comp=comp,
        session_ns=session_ns,
    )

    # Pinned in both directions: a cell that starts refusing when it should
    # not is as much a defect as one that stops.
    assert (result.exit_code != 0) is refused, (
        f"{origin}/{shared}/{store} exited {result.exit_code}, "
        f"{'expected a refusal' if refused else 'expected it to run'}.\n"
        f"stderr: {result.stderr}"
    )
    assert witness in result.stderr, (
        f"{origin}/{shared}/{store} produced nothing from the producer this cell "
        f"exists to exercise ({witness!r}); the scans below would pass on silence.\n"
        f"stderr: {result.stderr}"
    )
    return result.stderr


@pytest.mark.parametrize("store", sorted(_STORES))
@pytest.mark.parametrize("shared", sorted(_SHARED_OPTIONS))
@pytest.mark.parametrize("origin", sorted(_AGENT_ORIGINS))
def test_every_flag_it_names_in_its_own_voice_is_one_it_accepts(
    monkeypatch, origin, shared, store
) -> None:
    """No cell may tell the reader to use an option this command rejects.

    Flags inside an attributed command line are somebody else's and are left
    alone — ``mm embedding-reset --mode apply-current`` is good advice that
    happens to name a flag this verb has never heard of.
    """
    stderr = _emit(monkeypatch, origin, shared, store)

    unknown = _flags_named(_own_voice(stderr)) - _accepted_flags()
    assert not unknown, (
        f"{origin}/{shared}/{store} told the reader to use {sorted(unknown)}, "
        f"which `mm agent search` does not accept and which no command claimed."
        f"\nstderr: {stderr}"
    )


@pytest.mark.parametrize("store", sorted(_STORES))
@pytest.mark.parametrize("shared", sorted(_SHARED_OPTIONS))
@pytest.mark.parametrize("origin", sorted(_AGENT_ORIGINS))
def test_no_cell_suggests_an_unattributed_keyword(monkeypatch, origin, shared, store) -> None:
    """A bare ``foo="bar"`` claims to be an argument of the command you just
    ran. This command takes none, so it has to say whose it is."""
    stderr = _emit(monkeypatch, origin, shared, store)

    suggested = _keywords_suggested(_own_voice(stderr))
    assert not suggested, (
        f"{origin}/{shared}/{store} suggested {sorted(suggested)} as though it were "
        f"an argument of this command, without naming the command that takes it."
        f"\nstderr: {stderr}"
    )


class TestTheScannerCatchesWhatItClaimsTo:
    """A guard that cannot fail is a guard that proves nothing.

    Every case here is a spelling that reached a real reviewer's attention or
    that a previous version of this scanner silently accepted. Keeping them as
    fixtures means the scanner's reach is pinned independently of whether the
    matrix above currently happens to produce any of them.
    """

    def test_the_original_finding(self) -> None:
        leaked = (
            "No results found: --namespace 'agent-runtime:planner,shared' matches none "
            "of the namespaces this index has."
        )

        assert _flags_named(_own_voice(leaked)) - _accepted_flags() == {"--namespace"}

    def test_the_second_finding_one_prose_layer_out(self) -> None:
        leaked = (
            "3 result(s) hidden in system namespaces: 2 in agent-runtime:* (pass "
            'namespace="agent-runtime:*" to include them).'
        )

        assert _keywords_suggested(_own_voice(leaked)) == {"namespace"}

    def test_a_short_flag_with_an_attached_value(self) -> None:
        """``-n3`` is how someone actually types it, and Click rejects it."""
        assert _flags_named(_own_voice("did you mean `-n3`?")) - _accepted_flags() == {"-n3"}

    def test_a_unicode_dash_does_not_hide_the_flag(self) -> None:
        """An en-dash reads as a flag and parses as nothing."""
        leaked = "pass –namespace to include them"

        assert _flags_named(_own_voice(leaked)) - _accepted_flags() == {"-namespace"}

    def test_a_valid_prefix_with_an_invalid_suffix(self) -> None:
        """Longest-prefix matching read this as the accepted ``--agent-id``."""
        leaked = "pass --agent-id_extra here"

        assert _flags_named(_own_voice(leaked)) - _accepted_flags() == {"--agent-id_extra"}

    def test_a_prose_em_dash_is_not_a_flag(self) -> None:
        """Normalizing dashes must not turn punctuation into findings — this
        sentence contains one and the codebase's prose is full of them."""
        ordinary = "no agent resolved — searching unpinned. Pass --agent-id."

        assert not _flags_named(_own_voice(ordinary)) - _accepted_flags()

    def test_spaced_out_keyword_assignment(self) -> None:
        assert _keywords_suggested(_own_voice('pass namespace = "archive:*"')) == {"namespace"}

    def test_an_unknown_callable_attributes_nothing(self) -> None:
        """Exempting any identifier-with-parentheses would let the guard be
        talked out of its own founding finding by adding a wrapper."""
        leaked = 'try bogus(namespace="archive:*")'

        assert _keywords_suggested(_own_voice(leaked)) == {"namespace"}

    def test_callback_parameter_names_are_not_accepted_spellings(self) -> None:
        """``fmt`` is the callback's word for ``--format``. Click rejects
        ``fmt=json``, so the guard must not bless it."""
        assert _keywords_suggested(_own_voice("pass fmt=json")) == {"fmt"}

    def test_an_attributed_mcp_call_is_left_alone(self) -> None:
        """``mem_embedding_reset(mode="status")`` names the command that takes
        ``mode``, so it is legible and must not be flagged — otherwise the
        guard pushes real advice into vaguer wording for nothing."""
        attributed = 'run mem_embedding_reset(mode="status") for the reset options.'

        assert not _keywords_suggested(_own_voice(attributed))

    def test_an_attributed_command_line_keeps_its_own_flags(self) -> None:
        """``--mode`` is not ours, and telling somebody to run another command
        with it is correct advice, not a leak."""
        attributed = "`mm embedding-reset --mode apply-current` repairs it."

        assert not _flags_named(_own_voice(attributed)) - _accepted_flags()

    def test_backticks_alone_do_not_attribute(self) -> None:
        """Formatting is not a citation. Only a command line names a command."""
        leaked = "pass `--namespace` to include them"

        assert _flags_named(_own_voice(leaked)) - _accepted_flags() == {"--namespace"}

    def test_the_accepted_set_is_read_from_the_command(self) -> None:
        """If this ever comes back empty the matrix passes vacuously, so the
        source of the expected vocabulary is pinned too."""
        accepted = _accepted_flags()

        assert {"-a", "--agent-id", "--no-include-shared", "--shared-namespace"} <= accepted
        assert "--namespace" not in accepted
        assert "--mode" not in accepted


#: Failures whose translated message carries remediation. ``raise_cli_error``
#: maps a failure class to a one-line hint, and ``mm agent search`` routes every
#: unexpected exception through it — a path the matrix above never takes,
#: because every cell there succeeds.
_FAILURES: dict[str, tuple[str, str]] = {
    "embedding-dimension-mismatch": (
        "EmbeddingDimensionMismatchError",
        "embedding-reset",
    ),
    "schema-downgrade": ("SchemaDowngradeError", "upgrade"),
    "embedding-backend": ("EmbeddingError", "embedding"),
    "config": ("ConfigError", "configuration"),
}


@pytest.mark.parametrize("failure", sorted(_FAILURES))
def test_a_translated_failure_attributes_the_flags_it_names(monkeypatch, failure: str) -> None:
    """The error translator's advice is the other place remediation comes from.

    Its hints name other commands on purpose — ``mm embedding-reset --mode
    apply-current`` is the right thing to tell somebody whose embeddings
    disagree — so this is as much a check that the guard does *not* fire on
    correct cross-command advice as that it fires on a leak. An earlier version
    of the flag rule would have reported ``--mode`` here.
    """
    import memtomem.errors as errors

    exc_name, witness = _FAILURES[failure]
    exc_type = getattr(errors, exc_name)

    comp = _search_components(namespaces=[("default", 9)])
    comp.search_pipeline.search = AsyncMock(side_effect=exc_type("boom"))
    result = _run(
        monkeypatch,
        ["agent", "search", "deploy", "-a", "planner"],
        comp=comp,
        session_ns=None,
    )

    assert result.exit_code != 0
    assert "Hint:" in result.stderr, f"no remediation to check.\nstderr: {result.stderr}"
    assert witness in result.stderr, f"unexpected hint.\nstderr: {result.stderr}"

    own = _own_voice(result.stderr)
    assert not _flags_named(own) - _accepted_flags(), (
        f"{failure} named a flag this command does not accept and no command "
        f"claimed.\nstderr: {result.stderr}"
    )
    assert not _keywords_suggested(own), (
        f"{failure} suggested an unattributed keyword.\nstderr: {result.stderr}"
    )
