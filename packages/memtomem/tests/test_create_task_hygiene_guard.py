"""Every background task under ``server/`` and ``web/`` has an owner (#2185).

``asyncio.create_task`` holds only a weak reference to the task it returns. A
task nobody else holds can be garbage-collected mid-await, and the failure is
invisible: no traceback, no log, just work that silently did not happen. The
same call with the result assigned and forgotten is the other half — an
exception ending the coroutine stays parked on the task object until someone
awaits it, which for a fire-and-forget task is never.

Four sites in #2185 had one of those shapes, and each was found by reading
rather than by anything failing. This guard is what keeps the sweep from
decaying: a new ``create_task`` here must either hand the task to something
that keeps it (``track_task``, a set, an attribute, a return) or attach a
done-callback that logs.

Both spellings of the factory are covered — ``asyncio.create_task``,
``asyncio.ensure_future``, and the bare names a ``from asyncio import ...``
binds, alias included — because a guard that knew only one of them would hand
the next author a one-word way around itself. Ownership also has to be
*durable*: an attribute (``self._tasks``, ``app.state.x``) or a container the
function did not build in its own frame. A list created beside the task dies
with the frame, taking the only reference with it, and an ``.add`` inside a
nested ``def`` that may never run proves nothing — both are rejected.

**Known limit.** The rules are syntactic and single-function: the guard reads
the statement that creates the task and the rest of its enclosing function's
own scope. A handle passed one hop into a helper that stores it reads as owned
even if that helper drops it. That is deliberate — the bug shape this exists to
stop is the reference nobody keeps, and a taint analysis able to follow the
handle anywhere would be more code than the invariant it protects. Reviewers
still own the hop.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import memtomem

_SRC = Path(memtomem.__file__).parent
_SCANNED_DIRS = ("server", "web")

#: Names that count as handing the task to an owner when the task is passed to
#: them as an argument. ``track_task`` adds it to a set and attaches the logging
#: callback; that is the shape this guard is steering people towards.
_OWNING_CALLS = frozenset({"track_task"})

#: Attaching one of these to the task satisfies the "someone reports the
#: failure" half of the contract.
_LOGGING_CALLBACKS = frozenset(
    {"loop_task_error_cb", "bg_task_error_cb", "webhook_error_cb", "_bg_task_error_cb"}
)

#: Call sites exempt from the rules below. Empty on purpose: every site in
#: ``server/`` and ``web/`` satisfies one of the owned shapes as of #2185. An
#: addition here must name why the task genuinely has no owner — not merely
#: that adding one looked awkward.
EXEMPT: dict[tuple[str, int], str] = {}


class Violation(NamedTuple):
    path: str
    line: int
    reason: str

    def __str__(self) -> str:  # pragma: no cover - failure output only
        return f"{self.path}:{self.line} — {self.reason}"


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for name in _SCANNED_DIRS:
        files.extend((_SRC / name).rglob("*.py"))
    return sorted(files)


#: The event-loop entry points that spawn a task. ``ensure_future`` is here
#: because it is ``create_task`` with a different name for the case that
#: matters — a coroutine argument — and a guard that knew only one word would
#: hand the next author a one-line way around itself.
_TASK_FACTORIES = frozenset({"create_task", "ensure_future"})


def _factory_aliases(tree: ast.Module) -> frozenset[str]:
    """Local names bound to a task factory by ``from asyncio import ...``.

    ``asyncio.create_task(...)`` is an attribute call, but
    ``from asyncio import create_task`` (with or without ``as``) makes the same
    call a bare name. Both spellings have to reach the same rules.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _TASK_FACTORIES:
                    aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _is_create_task(node: ast.AST, aliases: frozenset[str] = frozenset()) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in _TASK_FACTORIES
    if isinstance(node.func, ast.Name):
        return node.func.id in aliases
    return False


def _attr_chain(node: ast.AST) -> str | None:
    """``a.b.c`` -> ``"a.b.c"``; anything else -> ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _assigned_in(func: ast.AST) -> set[str]:
    """Plain local names this function assigns."""
    names: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _same_scope_nodes(func: ast.AST):
    """Walk ``func`` without descending into nested function bodies.

    A nested ``def`` is a separate scope that may never run; counting an
    ``.add(task)`` inside one as ownership would accept a handle the outer
    function still drops.
    """
    for child in ast.iter_child_nodes(func):
        yield from _walk_skipping_functions(child)


def _walk_skipping_functions(node: ast.AST):
    yield node
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        return
    for child in ast.iter_child_nodes(node):
        yield from _walk_skipping_functions(child)


def _is_durable_container(receiver: ast.AST, func: ast.AST) -> bool:
    """Whether ``receiver.add(task)`` actually outlives the call.

    An attribute chain (``self._tasks``, ``app.state.jobs``) or a name the
    function does not itself create — a module-level set — keeps the task
    alive. A list built inside the same function does not: it dies with the
    frame, taking the only reference with it. Durability is decided by the
    *root* of the chain, so ``holder = make_holder()`` followed by
    ``holder.tasks.append(task)`` is frame-local too.
    """
    chain = _attr_chain(receiver) if isinstance(receiver, (ast.Attribute, ast.Name)) else None
    if chain is None:
        return False
    return chain.split(".", 1)[0] not in _assigned_in(func)


def _awaited(name: str, func: ast.AST, after_line: int) -> bool:
    """Whether the task is awaited — directly or by a settlement helper.

    ``future = ensure_future(...)`` followed by
    ``await settle_shielded_value(future, ...)`` is not fire-and-forget: the
    frame holds the reference until the await returns, which is the ownership
    this guard is asking for.
    """
    for node in _same_scope_nodes(func):
        if not isinstance(node, ast.Await) or getattr(node, "lineno", 0) < after_line:
            continue
        if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node)):
            return True
    return False


def _owned_in_function(name: str, func: ast.AST, after_line: int = 0) -> bool:
    """Whether ``name`` (a local holding a task) is handed to a durable owner."""
    if _awaited(name, func, after_line):
        return True
    for node in _same_scope_nodes(func):
        if getattr(node, "lineno", 0) < after_line:
            continue
        if isinstance(node, ast.Call):
            # ``task.add_done_callback(cb)`` / ``self._tasks.add(task)`` /
            # ``track_task(task, ...)``
            if isinstance(node.func, ast.Attribute):
                target = _attr_chain(node.func.value)
                if node.func.attr == "add_done_callback" and target == name:
                    if any(_callback_logs(arg) for arg in node.args):
                        return True
                if node.func.attr in {"add", "append"} and any(
                    isinstance(a, ast.Name) and a.id == name for a in node.args
                ):
                    if _is_durable_container(node.func.value, func):
                        return True
            if isinstance(node.func, ast.Name) and node.func.id in _OWNING_CALLS:
                if any(isinstance(a, ast.Name) and a.id == name for a in node.args):
                    return True
        # ``self._task = task`` / ``app.state.x = task`` / ``return task``
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            if node.value.id == name and any(
                isinstance(t, (ast.Attribute, ast.Subscript)) for t in node.targets
            ):
                return True
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
            if node.value.id == name:
                return True
    return False


def _callback_logs(arg: ast.AST) -> bool:
    if isinstance(arg, ast.Name):
        return arg.id in _LOGGING_CALLBACKS
    chain = _attr_chain(arg) if isinstance(arg, ast.Attribute) else None
    if chain is None:
        return False
    # ``self._pending_tasks.discard`` is bookkeeping, not reporting; a chain
    # only counts when its final name is one of the logging callbacks.
    return chain.rsplit(".", 1)[-1] in _LOGGING_CALLBACKS


def _enclosing_functions(tree: ast.Module) -> list[ast.AST]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _check_file(path: Path) -> list[Violation]:
    return _check_source(str(path.relative_to(_SRC)), path.read_text(encoding="utf-8"))


def _check_source(rel: str, source: str) -> list[Violation]:
    tree = ast.parse(source)
    aliases = _factory_aliases(tree)
    violations: list[Violation] = []

    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node

    def _nearest(node: ast.AST, kinds) -> ast.AST | None:
        current = parents.get(id(node))
        while current is not None:
            if isinstance(current, kinds):
                return current
            current = parents.get(id(current))
        return None

    found: list[tuple[int, ast.stmt, ast.AST, ast.Call]] = []
    for node in ast.walk(tree):
        if not _is_create_task(node, aliases):
            continue
        stmt = _nearest(node, ast.stmt)
        func = _nearest(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if stmt is None or func is None:
            continue
        found.append((node.lineno, stmt, func, node))

    for lineno, stmt, func, call in sorted(found, key=lambda item: item[0]):
        if (rel, lineno) in EXEMPT:
            continue

        # Discarded outright: the result is the whole statement.
        if isinstance(stmt, ast.Expr) and stmt.value is call:
            violations.append(
                Violation(rel, lineno, "create_task result is discarded — no strong reference")
            )
            continue

        # Handed straight to an owner, returned, or stored on an attribute /
        # subscript in the same statement.
        if isinstance(stmt, ast.Return):
            continue
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, (ast.Attribute, ast.Subscript)) for t in stmt.targets
        ):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            outer = stmt.value
            if isinstance(outer.func, ast.Name) and outer.func.id in _OWNING_CALLS:
                continue
            if (
                isinstance(outer.func, ast.Attribute)
                and outer.func.attr in {"add", "append"}
                and _is_durable_container(outer.func.value, func)
            ):
                continue

        # Bound to a local name: the rest of the function has to hand it on.
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if isinstance(target, ast.Name):
                if _owned_in_function(target.id, func, after_line=lineno):
                    continue
                violations.append(
                    Violation(
                        rel,
                        lineno,
                        f"task bound to local {target.id!r} but never stored, returned, or "
                        "given a logging done-callback",
                    )
                )
                continue

        violations.append(Violation(rel, lineno, "create_task result has no recognisable owner"))
    return violations


def test_scope_is_not_empty():
    """A path typo would otherwise make this guard pass by scanning nothing."""
    files = _scanned_files()
    assert len(files) > 20, files
    assert any("create_task" in f.read_text(encoding="utf-8") for f in files)


def test_every_background_task_has_an_owner():
    violations = [v for path in _scanned_files() for v in _check_file(path)]
    assert not violations, "Background tasks with no owner:\n" + "\n".join(
        str(v) for v in violations
    )


def test_guard_rejects_a_discarded_task():
    """The guard's own teeth: the bug shape must fail it."""
    violations = _check_source(
        "sample.py",
        "import asyncio\nasync def go():\n    asyncio.create_task(work())\n",
    )
    assert len(violations) == 1
    assert "discarded" in violations[0].reason


def test_guard_rejects_a_forgotten_handle():
    """Assigning the result and never using it is the other half of the bug."""
    violations = _check_source(
        "sample.py",
        "import asyncio\nasync def go():\n    task = asyncio.create_task(work())\n",
    )
    assert len(violations) == 1
    assert "never stored" in violations[0].reason


def test_guard_sees_a_directly_imported_factory():
    """``from asyncio import create_task`` is the same call, spelled shorter."""
    violations = _check_source(
        "sample.py",
        "from asyncio import create_task\nasync def go():\n    create_task(work())\n",
    )
    assert len(violations) == 1 and "discarded" in violations[0].reason


def test_guard_sees_an_aliased_factory():
    violations = _check_source(
        "sample.py",
        "from asyncio import create_task as spawn\nasync def go():\n    spawn(work())\n",
    )
    assert len(violations) == 1


def test_guard_sees_ensure_future():
    violations = _check_source(
        "sample.py",
        "import asyncio\nasync def go():\n    asyncio.ensure_future(work())\n",
    )
    assert len(violations) == 1


def test_guard_rejects_a_frame_local_collection():
    """A list built in the same frame dies with it — that is not ownership."""
    violations = _check_source(
        "sample.py",
        "import asyncio\n"
        "async def go():\n"
        "    tasks = []\n"
        "    task = asyncio.create_task(work())\n"
        "    tasks.append(task)\n",
    )
    assert len(violations) == 1 and "never stored" in violations[0].reason


def test_guard_rejects_ownership_from_a_nested_scope():
    """A nested ``def`` may never run; its ``.add`` proves nothing."""
    violations = _check_source(
        "sample.py",
        "import asyncio\n"
        "async def go(self):\n"
        "    task = asyncio.create_task(work())\n"
        "    def later():\n"
        "        self._tasks.add(task)\n",
    )
    assert len(violations) == 1


def test_guard_rejects_a_frame_local_holder():
    """Durability follows the root of the chain, not the dot count."""
    violations = _check_source(
        "sample.py",
        "import asyncio\n"
        "async def go():\n"
        "    holder = make_holder()\n"
        "    task = asyncio.create_task(work())\n"
        "    holder.tasks.append(task)\n",
    )
    assert len(violations) == 1


def test_guard_accepts_a_durable_collection():
    violations = _check_source(
        "sample.py",
        "import asyncio\n"
        "async def go(self):\n"
        "    task = asyncio.create_task(work())\n"
        "    self._tasks.add(task)\n",
    )
    assert violations == []


def test_guard_accepts_an_awaited_future():
    """``ensure_future`` handed to a settlement helper is not fire-and-forget."""
    violations = _check_source(
        "sample.py",
        "import asyncio\n"
        "async def go():\n"
        "    future = asyncio.ensure_future(work())\n"
        "    result = await settle(future)\n",
    )
    assert violations == []


def test_guard_accepts_a_stored_handle():
    violations = _check_source(
        "sample.py",
        "import asyncio\nasync def go(self):\n    self._task = asyncio.create_task(work())\n",
    )
    assert violations == []


def test_guard_rejects_an_untracked_local():
    """A named-but-forgotten handle is not ownership."""
    tree = ast.parse(
        "import asyncio\n"
        "async def go():\n"
        "    task = asyncio.create_task(work())\n"
        "    task.add_done_callback(lambda t: None)\n"
    )
    func = _enclosing_functions(tree)[0]
    assert not _owned_in_function("task", func)


def test_guard_accepts_the_tracked_shapes():
    tree = ast.parse(
        "import asyncio\n"
        "async def go(self):\n"
        "    task = asyncio.create_task(work())\n"
        "    task.add_done_callback(bg_task_error_cb)\n"
        "    self._tasks.add(task)\n"
    )
    func = _enclosing_functions(tree)[0]
    assert _owned_in_function("task", func)
