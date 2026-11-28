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

**What counts as a gate.** A call to ``is_read_only_source``, or to one of the
helpers listed below, each pinned to the call that makes it a gate. The scan
takes every module with an acquiring function, whether or not it still names a
gate: when modules were chosen by the gate's name, deleting every use of that
name dropped the module from the scan.

The check is that such a call exists and where it stands, not that its result is
honoured.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"

#: L2 memory-file acquires. `async_file_lock` is the primitive;
#: `async_memory_file_lock` keys the sidecar itself from a data path, and so do
#: the two taken for a source outside every managed directory.
_ACQUIRE_NAMES = {
    "async_file_lock",
    "async_memory_file_lock",
    "async_detached_source_lock",
    "async_existing_memory_file_lock",
}
_GATE_ATTR = "is_read_only_source"

#: Helpers that make the refusal for their caller. Listed rather than inferred,
#: and counted only as a bare ``name(...)`` call: ``other.name(...)`` is some
#: other function that happens to share the name.
_SHARED_GATE_HELPERS = {"refuse_replace_target"}
#: A private helper is a gate only in the module that defines it.
_LOCAL_GATE_HELPERS = {"server/tools/hermes_mirror.py": {"_destination_refusal"}}

#: What each listed helper must itself call. Explicit edges, so a helper cannot
#: certify itself by calling itself or another helper in a circle.
_HELPER_EDGES = {
    ("source_provenance.py", "refuse_replace_target"): _GATE_ATTR,
    ("server/tools/hermes_mirror.py", "_destination_refusal"): "refuse_replace_target",
}

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
    ("cli/shell.py", "_cmd_add"),
    ("cli/review_cmd.py", "_decide"),
    ("integrations/langgraph.py", "add"),
    ("web/routes/system_indexing.py", "add_memory"),
    ("web/routes/scratch.py", "promote_scratch"),
    ("server/tools/hermes_mirror.py", "mem_hermes_mirror_apply"),
    ("server/tools/session.py", "_write_summary_archive"),
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


def _bare_calls(node: ast.AST, names: set[str]) -> list[int]:
    """Line numbers of ``name(...)`` calls, not ``obj.name(...)``."""
    return sorted(
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in names
    )


def _gate_calls(node: ast.AST, rel: str) -> list[int]:
    """Line numbers of the read-only gates under ``node`` in module ``rel``."""
    helpers = _SHARED_GATE_HELPERS | _LOCAL_GATE_HELPERS.get(rel, set())
    return sorted(_calls(node, attr=_GATE_ATTR) + _bare_calls(node, helpers))


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
    return _offending_source(path.read_text(encoding="utf-8"), path.relative_to(SRC).as_posix())


def _offending_source(
    source: str, rel: str, must_gate: set[tuple[str, str]] = _MUST_GATE
) -> list[str]:
    """:func:`_offending` for source text judged as module ``rel``."""
    tree = ast.parse(source)
    out: list[str] = []
    for fn in _functions(tree):
        acquires = _calls(fn, names=_ACQUIRE_NAMES)
        gates = _gate_calls(fn, rel)
        if not acquires:
            continue
        if not gates:
            if (rel, fn.name) in must_gate:
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
            in_loop_gates = _gate_calls(loop, rel)
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


def _acquiring_in(source: str) -> list[str]:
    """Names of the functions in ``source`` that acquire an L2 lock."""
    return [fn.name for fn in _functions(ast.parse(source)) if _calls(fn, names=_ACQUIRE_NAMES)]


def _is_scanned(source: str) -> bool:
    return bool(_acquiring_in(source))


def _scanned_modules() -> list[Path]:
    return [p for p in sorted(SRC.rglob("*.py")) if _is_scanned(p.read_text(encoding="utf-8"))]


def _acquiring_functions() -> set[tuple[str, str]]:
    return {
        (path.relative_to(SRC).as_posix(), name)
        for path in sorted(SRC.rglob("*.py"))
        for name in _acquiring_in(path.read_text(encoding="utf-8"))
    }


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
    """Self-check: a module that is not scanned gets no ordering check from the
    parametrized test below."""
    rels = {p.relative_to(SRC).as_posix() for p in _scanned_modules()}
    assert rels == {rel for rel, _ in _acquiring_functions()}
    missing = {rel for rel, _ in _MUST_GATE} - rels
    assert not missing, f"registered modules that are not scanned: {sorted(missing)}"


@pytest.mark.parametrize("module", _scanned_modules(), ids=lambda p: p.relative_to(SRC).as_posix())
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


_HELPER_GOOD = """
async def f(app, target):
    if refuse_replace_target(target, app.index_engine):
        return
    async with async_file_lock(target):
        pass
"""

_HELPER_LATE = """
async def f(app, target):
    async with async_file_lock(target):
        if refuse_replace_target(target, app.index_engine):
            return
"""

_HELPER_GOOD_LOOP = """
async def f(app, target):
    for _ in range(3):
        if refuse_replace_target(target, app.index_engine):
            return
        async with async_file_lock(target):
            target = other()
"""

_HELPER_BAD_LOOP = """
async def f(app, target):
    if refuse_replace_target(target, app.index_engine):
        return
    for _ in range(3):
        async with async_file_lock(target):
            target = other()
"""

_RECHECK_IN_A_LOOP = """
async def f(app, target):
    if refuse_replace_target(target, app.index_engine):
        return
    for _ in range(3):
        async with async_file_lock(target):
            if refuse_replace_target(target, app.index_engine):
                return
"""

_HELPER_THEN_LITERAL = """
async def f(app, target):
    if refuse_replace_target(target, app.index_engine):
        return
    async with async_file_lock(target):
        if app.index_engine.is_read_only_source(target):
            return
"""

_LOCAL_HELPER = """
async def f(app, target):
    if _destination_refusal(app, target):
        return
    async with async_file_lock(target):
        pass
"""

_QUALIFIED_SHARED_HELPER = """
async def f(app, target):
    if other.refuse_replace_target(target, app.index_engine):
        return
    async with async_file_lock(target):
        pass
"""

_QUALIFIED_LOCAL_HELPER = """
async def f(app, target):
    if other._destination_refusal(app, target):
        return
    async with async_file_lock(target):
        pass
"""

_NO_GATE = "no read-only gate at all"
_LATE = "comes after the lock acquire"
_NOT_IN_LOOP = "no read-only gate before it *in that loop*"

_PROBE = "_synthetic_guard_probe.py"
_HERMES = "server/tools/hermes_mirror.py"


@pytest.mark.parametrize(
    "source,rel,expected",
    [
        pytest.param(_GOOD_ORDER, _PROBE, None, id="gate before acquire"),
        pytest.param(_BAD_ORDER, _PROBE, _LATE, id="gate inside the lock body"),
        pytest.param(_BAD_LOOP, _PROBE, _NOT_IN_LOOP, id="gate outside a re-keying loop"),
        pytest.param(_GOOD_LOOP, _PROBE, None, id="gate inside the loop"),
        pytest.param(_HELPER_GOOD, _PROBE, None, id="helper before acquire"),
        pytest.param(_HELPER_LATE, _PROBE, _LATE, id="helper inside the lock body"),
        pytest.param(_HELPER_GOOD_LOOP, _PROBE, None, id="helper inside the loop"),
        pytest.param(_HELPER_BAD_LOOP, _PROBE, _NOT_IN_LOOP, id="helper outside the loop"),
        pytest.param(
            _RECHECK_IN_A_LOOP, _PROBE, _NOT_IN_LOOP, id="only the recheck is in the loop"
        ),
        pytest.param(_HELPER_THEN_LITERAL, _PROBE, None, id="helper, then a literal recheck"),
        pytest.param(_LOCAL_HELPER, _PROBE, _NO_GATE, id="private helper of another module"),
        pytest.param(_QUALIFIED_SHARED_HELPER, _PROBE, _NO_GATE, id="qualified shared name"),
        pytest.param(_QUALIFIED_LOCAL_HELPER, _HERMES, _NO_GATE, id="qualified private name"),
    ],
)
def test_the_matcher_judges_synthetic_shapes(source, rel, expected):
    offences = _offending_source(source, rel, must_gate={(rel, "f")})
    if expected is None:
        assert not offences, offences
    else:
        assert len(offences) == 1 and expected in offences[0], offences


# --- the listed helpers ------------------------------------------------------

_EDGES = [(rel, helper, callee) for (rel, helper), callee in sorted(_HELPER_EDGES.items())]


def _helper_makes_its_call(source: str, helper: str, callee: str) -> bool:
    (fn,) = [f for f in _functions(ast.parse(source)) if f.name == helper]
    if callee == _GATE_ATTR:
        return bool(_calls(fn, attr=callee))
    return bool(_bare_calls(fn, {callee}))


def test_every_listed_helper_has_an_edge():
    listed = _SHARED_GATE_HELPERS.union(*_LOCAL_GATE_HELPERS.values())
    assert listed == {helper for _, helper in _HELPER_EDGES}


@pytest.mark.parametrize("rel,helper,callee", _EDGES)
def test_each_gate_helper_makes_the_call_it_is_trusted_for(rel, helper, callee):
    source = (SRC / rel).read_text(encoding="utf-8")
    assert _helper_makes_its_call(source, helper, callee), (
        f"{rel}:{helper} no longer calls {callee}, so a call to it is not a read-only gate"
    )


@pytest.mark.parametrize("rel,helper,callee", _EDGES)
def test_a_helper_that_calls_itself_instead_fails_its_edge(rel, helper, callee):
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    (fn,) = [f for f in _functions(tree) if f.name == helper]
    replaced = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n) == callee]
    assert replaced, f"{rel}:{helper}: nothing to replace"
    for call in replaced:
        call.func = ast.Name(id=helper, ctx=ast.Load())
    source = ast.unparse(ast.fix_missing_locations(tree))
    assert not _helper_makes_its_call(source, helper, callee)


# --- mutations of the real sources -------------------------------------------
#
# A registry entry is worth something only if breaking that function fails. Each
# entry's real source is broken here and judged the way the suite judges it.

#: What the mutator removes. Its own list, not the matcher's, so that a matcher
#: which forgot a helper still has that helper taken out from under it.
_MUTATOR_GATE_NAMES = {"is_read_only_source", "refuse_replace_target", "_destination_refusal"}


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _mutate(rel: str, fn_name: str, *, sink: bool = False, only_before_lock: bool = False) -> str:
    """``rel``'s source with ``fn_name``'s gate calls renamed away.

    ``sink`` puts a copy of the first one back as the function's last statement;
    ``only_before_lock`` leaves the calls at or after the first acquire.
    """
    tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
    (fn,) = [f for f in _functions(tree) if f.name == fn_name]
    gates = sorted(
        (n for n in ast.walk(fn) if isinstance(n, ast.Call) and _callee(n) in _MUTATOR_GATE_NAMES),
        key=lambda n: n.lineno,
    )
    if only_before_lock:
        first_acquire = min(_calls(fn, names=_ACQUIRE_NAMES))
        gates = [g for g in gates if g.lineno < first_acquire]
    assert gates, f"{rel}:{fn_name}: nothing to remove, so the mutation would prove nothing"
    kept = copy.deepcopy(gates[0])
    for gate in gates:
        gate.func = ast.Name(id="_removed_gate", ctx=ast.Load())
    if sink:
        fn.body.append(ast.Expr(value=kept))
    return ast.unparse(ast.fix_missing_locations(tree))


def _offences_after(rel: str, fn_name: str, **how: bool) -> list[str]:
    source = _mutate(rel, fn_name, **how)
    assert _is_scanned(source), f"{rel}: the mutated module is no longer scanned"
    return [o for o in _offending_source(source, rel) if o.startswith(f"{rel}:{fn_name}")]


@pytest.mark.parametrize("rel,fn_name", sorted(_MUST_GATE))
def test_removing_a_registered_gate_is_reported(rel, fn_name):
    offences = _offences_after(rel, fn_name)
    assert len(offences) == 1 and _NO_GATE in offences[0], offences


@pytest.mark.parametrize("rel,fn_name", sorted(_MUST_GATE))
def test_moving_a_registered_gate_below_its_lock_is_reported(rel, fn_name):
    offences = _offences_after(rel, fn_name, sink=True)
    assert len(offences) == 1 and _LATE in offences[0], offences


def test_removing_only_the_gate_before_the_lock_is_reported():
    """The mirror apply gates again under the lock, and that recheck must not stand in
    for the gate before it. The guard does not require the recheck, so either report
    will do: late with the recheck there, no gate at all without it."""
    offences = _offences_after(_HERMES, "mem_hermes_mirror_apply", only_before_lock=True)
    assert len(offences) == 1 and (_LATE in offences[0] or _NO_GATE in offences[0]), offences
