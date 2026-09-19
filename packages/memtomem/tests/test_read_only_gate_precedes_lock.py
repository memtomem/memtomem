"""A read-only gate must precede the lock it protects — and share its loop.

Acquiring an L2 memory-file sidecar *creates* `.<name>.lock` next to the source
with ``O_RDWR | O_CREAT``, and the file is not removed on release. So a refusal
decided after the acquire has already written into the directory a read-only
root exists to keep memtomem out of.

This rule was got wrong three times in one change — in `mem_add`'s retarget
loop, in `_locked_chunk`'s move-retry loop, and in the web routes, which gated
their own earlier fetch while `locked_source_chunk` chose and locked a different
path. Each was fixed by hand and the next one repeated it, which is the signal
that the rule belongs in a test rather than in reviewers' heads.

**Line order alone is not the rule.** The loop cases all had the gate on an
*earlier line* than the acquire and were still wrong: the loop re-keys the path
and comes back round to the acquire without passing the gate again. Hence the
second clause.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"

#: L2 memory-file acquires. `async_file_lock` is the primitive;
#: `async_memory_file_lock` keys the sidecar itself from a data path.
_ACQUIRE_NAMES = {"async_file_lock", "async_memory_file_lock"}
_GATE_ATTR = "is_read_only_source"

#: Functions that acquire an L2 lock on a path a *memory write* is about to
#: touch, and therefore must refuse a read-only source first. Listed rather than
#: inferred because plenty of L2 acquires are not memory writes — the artifact
#: Store, namespace coordination and indexing all lock legitimately without a
#: read-only gate. An acquire outside both this set and the exempt set below
#: fails the registry test, so a new one has to be classified by a human.
_MUST_GATE = {
    ("server/tools/memory_crud.py", "_locked_chunk"),
    ("server/tools/memory_crud.py", "_mem_add_core"),
    ("server/tools/memory_crud.py", "mem_batch_add"),
    ("tools/memory_mutation.py", "locked_source_chunk"),
    ("cli/memory.py", "_add"),
    ("cli/agent_cmd.py", "_run_share"),
    ("cli/review_cmd.py", "_decide"),
    ("integrations/langgraph.py", "add"),
    ("web/routes/system.py", "add_memory"),
}

#: Acquires that are deliberately ungated, each with the reason.
_EXEMPT = {
    # Indexing a protected root is supposed to happen; its sidecar is #2501.
    ("indexing/engine.py", "_locked_index"),
    # The artifact Store and namespace coordination are separate domains with
    # their own roots — read-only memory roots do not govern them.
    ("cli/context_cmd.py", "_memory_migrate_run"),
    ("services/namespace_management.py", "_coordinated_mutation"),
    # Deletes index rows; its own gates live in the tool body above the lock.
    ("server/tools/memory_crud.py", "mem_delete"),
}


def _calls(node: ast.AST, *, names: set[str] | None = None, attr: str | None = None) -> list[int]:
    """Line numbers of matching calls anywhere under ``node``."""
    out: list[int] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if names is not None and isinstance(f, ast.Name) and f.id in names:
            out.append(n.lineno)
        elif names is not None and isinstance(f, ast.Attribute) and f.attr in names:
            out.append(n.lineno)
        elif attr is not None and isinstance(f, ast.Attribute) and f.attr == attr:
            out.append(n.lineno)
    return sorted(out)


def _functions(tree: ast.AST):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def _loops(node: ast.AST):
    for n in ast.walk(node):
        if isinstance(n, (ast.For, ast.AsyncFor, ast.While)):
            yield n


def _offending(path: Path) -> list[str]:
    """Return one message per violation in ``path``; empty when clean."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(SRC).as_posix()
    out: list[str] = []
    for fn in _functions(tree):
        acquires = _calls(fn, names=_ACQUIRE_NAMES)
        gates = _calls(fn, attr=_GATE_ATTR)
        if not acquires:
            continue
        if not gates:
            if (rel, fn.name) in _MUST_GATE:
                out.append(
                    f"{rel}:{fn.name} acquires an L2 lock on a memory-write path but has "
                    "no read-only gate at all. Deleting the gate is the failure this "
                    "registry exists to catch."
                )
            continue

        # Clause 1 — the first gate must come before the first acquire.
        if min(gates) > min(acquires):
            out.append(
                f"{rel}:{fn.name}: the read-only gate (line {min(gates)}) comes after the "
                f"lock acquire (line {min(acquires)}); the sidecar is created before the "
                "refusal."
            )
            continue

        # Clause 2 — an acquire inside a loop needs a gate inside that same
        # loop. A loop that re-keys the path returns to the acquire without
        # passing a gate placed outside it, which is how this was got wrong
        # twice even though the line numbers looked right.
        for loop in _loops(fn):
            in_loop_acquires = _calls(loop, names=_ACQUIRE_NAMES)
            if not in_loop_acquires:
                continue
            in_loop_gates = _calls(loop, attr=_GATE_ATTR)
            # Presence is not enough: an in-lock gate also lives inside the loop,
            # and counting it would let the pre-lock one be hoisted out.
            if not any(g < min(in_loop_acquires) for g in in_loop_gates):
                out.append(
                    f"{rel}:{fn.name}: a lock is acquired inside a loop (line "
                    f"{min(in_loop_acquires)}) with no read-only gate before it *in that "
                    "loop*. A gate before the loop is not enough — the loop can re-key "
                    "the path and acquire again without passing it."
                )
    return out


def _modules_with_both() -> list[Path]:
    out = []
    for p in sorted(SRC.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        if _GATE_ATTR in text and any(n in text for n in _ACQUIRE_NAMES):
            out.append(p)
    return out


def _acquiring_functions() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - source is always parseable
            continue
        for fn in _functions(tree):
            if _calls(fn, names=_ACQUIRE_NAMES):
                found.add((rel, fn.name))
    return found


def test_every_registry_entry_names_a_real_function():
    """A misspelled entry silently guards nothing.

    Four of the first nine were wrong — guessed instead of read — and the file
    was green throughout, which is exactly the shape this registry exists to
    stop elsewhere.
    """
    real = _acquiring_functions()
    missing = (_MUST_GATE | _EXEMPT) - real
    assert not missing, f"registry names no longer present in the source: {sorted(missing)}"


def test_every_acquiring_function_is_classified():
    """An unclassified acquire fails rather than defaulting to "no gate needed".

    This is what makes the registry hold its value: adding a new L2 acquire
    forces a decision here instead of quietly joining the ungated majority.
    """
    unclassified = _acquiring_functions() - _MUST_GATE - _EXEMPT
    assert not unclassified, (
        "these functions acquire an L2 lock and are in neither _MUST_GATE nor _EXEMPT: "
        f"{sorted(unclassified)}. Classify each one."
    )


def test_the_matcher_sees_the_modules_it_is_meant_to_guard():
    """Self-check: if the matcher stops recognising these, every assertion below
    becomes vacuous and the file still reports green."""
    rels = {p.relative_to(SRC).as_posix() for p in _modules_with_both()}
    for expected in (
        "server/tools/memory_crud.py",
        "tools/memory_mutation.py",
        "cli/memory.py",
        "integrations/langgraph.py",
    ):
        assert expected in rels, f"{expected} no longer looks like a gated-lock module"


@pytest.mark.parametrize(
    "module", _modules_with_both(), ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_the_gate_precedes_the_lock_it_protects(module: Path):
    offences = _offending(module)
    assert not offences, "\n".join(offences)


# --- the matcher's own behaviour, on synthetic sources -----------------------
#
# Driving the checker with hand-written shapes is what keeps it from passing
# because it stopped matching. Production mutation proves it catches a real
# regression; these prove it rejects *fake* compliance.

_GOOD_ORDER = """
async def f(app, target):
    if app.index_engine.is_read_only_source(target):
        return
    async with async_file_lock(target):
        pass
"""

_BAD_ORDER = """
async def f(app, target):
    async with async_file_lock(target):
        if app.index_engine.is_read_only_source(target):
            return
"""

_BAD_LOOP = """
async def f(app, target):
    if app.index_engine.is_read_only_source(target):
        return
    for _ in range(3):
        async with async_file_lock(target):
            target = other()
"""

_GOOD_LOOP = """
async def f(app, target):
    for _ in range(3):
        if app.index_engine.is_read_only_source(target):
            return
        async with async_file_lock(target):
            target = other()
"""


def _offences_for(source: str, tmp_path: Path) -> list[str]:
    p = SRC / "_synthetic_guard_probe.py"
    try:
        p.write_text(source, encoding="utf-8")
        return _offending(p)
    finally:
        p.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "source,should_flag,why",
    [
        (_GOOD_ORDER, False, "gate before acquire"),
        (_BAD_ORDER, True, "gate inside the lock body"),
        (_BAD_LOOP, True, "gate outside a loop that re-keys and re-acquires"),
        (_GOOD_LOOP, False, "gate inside the loop"),
    ],
)
def test_the_matcher_judges_synthetic_shapes(tmp_path, source, should_flag, why):
    offences = _offences_for(source, tmp_path)
    assert bool(offences) is should_flag, f"{why}: {offences}"
