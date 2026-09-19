"""Every write-target gate asks both questions, enforced structurally.

The reason this file exists rather than another list of behavioural tests: the
set of places memtomem writes a memory file is large (a dozen surfaces across
MCP, web, CLI and the LangGraph integration) and it grew twice while this
feature was being reviewed. Enumerating it by hand once protects today and
rots tomorrow — the next surface to be added will copy the ``is_excluded``
gate from its neighbour and silently not copy the read-only one.

So the enumeration is derived from the source instead. ``is_excluded`` already
marks every write-target site: it was put there by #2488, which had to find the
same set. This asserts the two gates appear as a *pair* at every one of those
sites, so adding a write surface with only half the protection fails here
rather than in someone's vault.

A parity guard is only worth as much as its own coverage, so the matcher is
pinned in both directions: it must find the sites that exist, and it must fail
when a site loses its read-only half.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"
TESTS = Path(__file__).resolve().parent
THIS_FILE = Path(__file__).name

#: Modules whose ``is_excluded`` calls are about *reading* or *reporting*, not
#: about refusing a write, so they carry no read-only obligation. Kept as an
#: explicit allowlist — a module added here is a decision someone can review,
#: whereas a matcher that silently skipped them would hide real gaps.
_NOT_WRITE_GATES = {
    # The engine defines the predicates and uses them to decide what to index.
    "indexing/engine.py",
    # Retrieval-time filtering: excluded sources are hidden from results.
    "search/pipeline.py",
    # Read-only audits/reports over already-stored rows.
    "cli/purge_cmd.py",
    "indexing/budget_audit.py",
}

#: Sites that reach the shared ``refuse_replace_target`` helper instead of
#: spelling both gates inline. The helper asks both questions itself, which is
#: the whole point of it taking a ``WriteTargetGuard``.
#: Note what is deliberately *not* here: ``tools/memory_mutation.py`` and
#: ``web/upload_quarantine.py`` spell both gates inline even though they are
#: near the helper, so they take the ordinary parity check below. Listing them
#: here would have exempted them from the only check that covers them — which
#: is exactly what the first draft of this file did, and what
#: ``test_an_exempted_module_actually_uses_the_shared_helper`` caught.
_VIA_SHARED_HELPER = {
    "indexing/importers.py",
    "indexing/url_fetcher.py",
    "server/tools/session.py",
}


def _module_calls(path: Path) -> tuple[Counter[str], Counter[str]]:
    """Count the gate calls in ``path``, keyed by the argument expression.

    Matching on the call's attribute name rather than on a source substring, so
    a mention in a comment or docstring cannot satisfy the parity check.

    Counted rather than collected into sets, which the first version did: one
    module can hold several write-target sites that all name their destination
    ``target`` (``memory_crud`` has two — ``mem_add`` and batch add). With sets,
    removing one site's read-only gate left the argument present via its
    sibling and the guard stayed green. Mutation testing found that blind spot;
    the counts are what close it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    excluded: Counter[str] = Counter()
    read_only: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        arg = ast.unparse(node.args[0]) if node.args else "?"
        if node.func.attr == "is_excluded":
            excluded[arg] += 1
        elif node.func.attr == "is_read_only_source":
            read_only[arg] += 1
    return excluded, read_only


def _write_gate_modules() -> list[Path]:
    out = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in _NOT_WRITE_GATES or rel in _VIA_SHARED_HELPER:
            continue
        excluded, _ = _module_calls(path)
        if excluded:
            out.append(path)
    return out


def test_the_matcher_finds_the_write_gate_modules():
    """Self-check: an empty or near-empty set would make every assertion below
    vacuous, so the guard states how many sites it is actually guarding."""
    modules = _write_gate_modules()
    rels = {p.relative_to(SRC).as_posix() for p in modules}
    assert len(rels) >= 8, f"matcher found only {sorted(rels)} — it has stopped seeing the sites"
    # A few the guard must always see, named so a refactor that moves them
    # shows up here instead of quietly shrinking the guarded set.
    for expected in (
        "server/tools/memory_crud.py",
        "web/routes/system.py",
        "web/routes/scratch.py",
        "cli/memory.py",
        "integrations/langgraph.py",
    ):
        assert expected in rels, f"{expected} no longer looks like a write-target gate"


@pytest.mark.parametrize(
    "module", _write_gate_modules(), ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_each_write_target_gate_also_checks_read_only(module: Path):
    """``is_excluded`` and ``is_read_only_source`` must be asked about the same
    targets in the same module.

    The argument expressions are compared, not just the counts: a module that
    checked exclusion on ``target`` and read-only on some other path would
    otherwise pass while leaving ``target`` writable.
    """
    excluded, read_only = _module_calls(module)
    # Counter subtraction drops non-positive results, so this is exactly "an
    # argument guarded against exclusion more times than against read-only".
    missing = excluded - read_only
    assert not missing, (
        f"{module.relative_to(SRC).as_posix()} refuses an excluded write target but not a "
        f"read-only one, for: {sorted(missing.elements())}. Add the sibling "
        "``is_read_only_source`` gate next to it (or route the write through "
        "``refuse_replace_target``), so a read-only index root is not writable "
        "through this surface."
    )


def test_the_shared_helper_really_asks_both():
    """The modules exempted above are exempt *because* the helper asks for them.

    Without this, adding a module to ``_VIA_SHARED_HELPER`` would be a way to
    switch the guard off for it.
    """
    excluded, read_only = _module_calls(SRC / "source_provenance.py")
    assert excluded["target"] >= 1 and read_only["target"] >= 1


def _doubles_configuring(attr: str) -> set[str]:
    """Test files that give a fake index engine ``attr``, by any spelling.

    Covers both shapes the suite uses: a keyword in a ``SimpleNamespace(...)``
    call and an assignment onto a ``Mock``/``AsyncMock``.
    """
    found: set[str] = set()
    for path in sorted(TESTS.glob("test_*.py")):
        if path.name == THIS_FILE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == attr:
                found.add(path.name)
            elif isinstance(node, ast.Attribute) and node.attr == attr:
                parent_assign = getattr(node, "_is_target", False)
                if parent_assign:
                    found.add(path.name)
        # ast does not link parents; catch assignments in a second pass.
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Attribute) and target.attr == attr:
                        found.add(path.name)
    return found


def test_every_fake_index_engine_answers_both_predicates():
    """A test double that knows ``is_excluded`` must know its sibling too.

    This is a test-suite rule rather than a production one, and it earns its
    place: the obligation was missed three separate times while this feature was
    built. The failure mode is not loud in the useful direction — an
    ``AsyncMock`` returns a *coroutine* for a sync predicate, which is truthy, so
    the double silently refuses every write and the tests that break are ones
    with nothing to do with read-only roots (the web fixture already carries a
    comment about exactly this for ``is_excluded``, from #2488).
    """
    excluded = _doubles_configuring("is_excluded")
    read_only = _doubles_configuring("is_read_only_source")
    missing = excluded - read_only
    assert not missing, (
        "these test files configure a fake index engine's ``is_excluded`` but not "
        f"``is_read_only_source``: {sorted(missing)}. Add "
        "``is_read_only_source`` to the double — a sync predicate missing from an "
        "AsyncMock returns a truthy coroutine and refuses every write."
    )


@pytest.mark.parametrize("module_rel", sorted(_VIA_SHARED_HELPER))
def test_an_exempted_module_actually_uses_the_shared_helper(module_rel: str):
    """Each exemption is checked against the source, so the allowlist cannot
    drift into covering a module that stopped routing through the helper."""
    source = (SRC / module_rel).read_text(encoding="utf-8")
    assert "refuse_replace_target" in source, (
        f"{module_rel} is exempted from the inline parity check because it uses "
        "refuse_replace_target, but it no longer mentions it"
    )
