"""No storage mixin ends a transaction it does not own.

``SqliteBackend.transaction()`` composes writers by ambient task-affine state:
a writer that runs inside it must suppress its own commit, or it ends the
owner's transaction from the inside and strands a partial write. Nothing about
a bare ``db.commit()`` looks wrong, and the failure is silent — the owner sees
a clean ``async with`` and a successful return over half-durable work.

Every guard before #2162 was added reactively, when a caller finally needed
one: ``upsert_entities`` for #2155, ``add_relation`` and
``update_importance_scores`` for #2158. That is 43 of 51 call sites relying on
nobody ever composing them. This guard is what makes the sweep hold: a writer
that reaches the connection's ``commit``/``rollback`` directly, instead of
through ``_commit_if_standalone``/``_rollback_if_standalone``, fails here
unless it is registered as owning its own transaction.

Scope is ``storage/mixins/`` only. ``sqlite_backend.py`` implements
``transaction()`` and the helpers — it is the authority, not a participant.
``sqlite_namespace.py`` composes through an injected ``_in_transaction``
callable and its own borrow-or-refuse ``_begin_namespace_write``, covered by
``test_namespace_writer_transactions.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import memtomem

_MIXINS = Path(memtomem.__file__).parent / "storage" / "mixins"

# The connection methods that end a transaction. Reaching either one directly
# is what this guard is about; the ownership-aware helpers are the way through.
_TRANSACTION_ENDING = frozenset({"commit", "rollback"})

# The same thing said in SQL. ``db.execute("COMMIT")`` ends the transaction
# exactly as ``db.commit()`` does, and a guard that only knew about the method
# would hand any future writer a one-word way around itself.
_TRANSACTION_ENDING_SQL = frozenset({"COMMIT", "ROLLBACK", "END", "END TRANSACTION"})

# ``REFUSES`` — runs its own BEGIN and raises if a caller already holds a
# transaction, so its bare commit can only ever be its own.
REFUSES = "refuses"
# ``BORROWS`` — joins a caller's transaction when there is one and takes its
# own BEGIN otherwise, with every commit/rollback gated on that same flag.
BORROWS = "borrows"

# Writers that manage a transaction themselves instead of joining one through
# ``_commit_if_standalone``. Each entry records which shape it is, because the
# two are checked differently: a ``REFUSES`` entry must contain a refusal, and
# a ``BORROWS`` entry must gate every transaction-ending statement on its
# ownership flag. Add an entry ONLY with the shape and the reason.
OWNED_TRANSACTION_WRITERS: dict[tuple[str, str], tuple[str, str]] = {
    ("history.py", "HistoryMixin.save_search_feedback"): (
        REFUSES,
        "Owns a BEGIN IMMEDIATE around a read-modify-write of the feedback row.",
    ),
    ("eval_cases.py", "EvalCaseMixin.promote_search_run"): (
        REFUSES,
        "Promotion owns its full BEGIN/commit/rollback contract.",
    ),
    ("eval_cases.py", "EvalCaseMixin.import_eval_cases"): (
        REFUSES,
        "An import is all-or-nothing across many cases and owns its transaction.",
    ),
    ("formation.py", "FormationMixin.recover_stale_memory_candidates"): (
        REFUSES,
        "Serializes claim recovery across processes under its own BEGIN IMMEDIATE.",
    ),
    ("sessions.py", "SessionMixin.end_session"): (
        BORROWS,
        "Read-modify-write that needs a transaction but composes into one: "
        "takes its own BEGIN IMMEDIATE only when not already inside a "
        "caller's, and gates commit/rollback on that ``owns_transaction`` flag.",
    ),
    ("sessions.py", "SessionMixin.finalize_session_summary"): (
        BORROWS,
        "Borrow-or-own, as end_session.",
    ),
    ("sessions.py", "SessionMixin.update_session_metadata"): (
        BORROWS,
        "Borrow-or-own, as end_session.",
    ),
}

# The two names a function can consult to learn it is inside someone else's
# transaction. Naming one is not itself a refusal or a gate — what the function
# then *does* with it is, which is why ``_refuses_outer_transaction`` and
# ``_ungated_transaction_enders`` both read control flow rather than stopping
# here. ``getattr(self, "_in_transaction", False)`` is the second dialect of
# the first and is matched through the string constant.
_REFUSAL_NAMES = frozenset({"_in_transaction", "_require_transaction_idle"})


def _qualified_functions(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """``(qualname, node)`` for every function, class-qualified.

    Qualified because a bare function name would let one mixin's registered
    exemption silently cover an identically named method on another.
    """
    out: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{prefix}{child.name}", child))
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return out


def _own_body(fn: ast.AST):
    """Nodes belonging to ``fn`` itself, stopping at nested ``def`` boundaries.

    A commit parked inside a nested ``def`` belongs to that function, not to
    this one — ``_qualified_functions`` yields it separately, so it is still
    checked. ``ast.walk`` cannot express the boundary (it keeps descending past
    the nested ``def``), so this is an explicit non-descending traversal.

    Lambdas are the exception: they are *not* a boundary here. Nothing yields
    a lambda body separately, so skipping one would make
    ``lambda: db.commit()`` invisible to the whole guard.
    """
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        # Stop *at* the boundary: a nested function is neither yielded nor
        # descended into. Filtering children instead of the node itself is the
        # subtle version of this bug — the nested ``def`` gets skipped while
        # its body is still walked.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _transaction_ending_nodes(fn: ast.AST) -> list[ast.AST]:
    """Every place this function ends a transaction itself.

    Two spellings count. The method — matched as an attribute *reference*, not
    only as a call, because a ``db.commit`` handed to ``asyncio.to_thread`` or
    stashed in a callback ends the transaction just the same, and on any
    receiver name so that renaming the local from ``db`` to ``conn`` does not
    walk a writer out of this guard. And the SQL — ``db.execute("COMMIT")``,
    which no attribute check would ever see.
    """
    found: list[ast.AST] = []
    for node in _own_body(fn):
        if isinstance(node, ast.Attribute) and node.attr in _TRANSACTION_ENDING:
            found.append(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"execute", "executescript"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and node.args[0].value.strip().upper().rstrip(";") in _TRANSACTION_ENDING_SQL
        ):
            found.append(node)
    return found


def _ends_transaction_directly(fn: ast.AST) -> bool:
    return bool(_transaction_ending_nodes(fn))


def _is_ownership_read(node: ast.AST) -> bool:
    """Whether ``node`` is exactly a read of "am I inside a transaction".

    Exactly, not "mentions": ``self._in_transaction and enabled`` is a
    different question with a different answer, and treating it as this one is
    how a gate stops meaning what its name says.
    """
    if isinstance(node, ast.Attribute) and node.attr in _REFUSAL_NAMES:
        return True
    # ``getattr(self, "_in_transaction", False)`` — the second dialect.
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in _REFUSAL_NAMES
    )


def _ungated_transaction_enders(fn: ast.AST) -> list[int]:
    """Line numbers of transaction-ending statements not under an ownership test.

    A ``BORROWS`` writer is safe only while every one of its commits sits
    inside ``if owns_transaction:``. Dropping that ``if`` is a one-line edit
    that reintroduces the bug, and the mere presence of ``_in_transaction``
    somewhere in the function would still read as compliance.
    """
    # Step 1: find the ownership flag, by its definition rather than its name.
    # Only the exact ``<flag> = not self._in_transaction`` counts: a variable
    # that merely has "transaction" in its name (``transaction_failed``) is not
    # one, and neither is a compound like ``not (self._in_transaction and x)``,
    # whose truth no longer means "this writer owns the transaction".
    flags: set[str] = set()
    rebound: set[str] = set()
    for node in _own_body(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if (
            isinstance(value, ast.UnaryOp)
            and isinstance(value.op, ast.Not)
            and _is_ownership_read(value.operand)
        ):
            flags.add(target.id)
        else:
            # Any other assignment to the same name — ``owns = True`` later on
            # — means the gate no longer tracks ownership at the point it is
            # read, so the name stops counting as a flag at all.
            rebound.add(target.id)
    flags -= rebound

    # Step 2: only the branch that runs when the flag is TRUE is a gate. An
    # ``else:`` or an ``if not owns_transaction:`` body is where the writer is
    # inside someone else's transaction — committing there is the bug.
    gated: set[int] = set()
    for node in _own_body(fn):
        if not isinstance(node, ast.If):
            continue
        if isinstance(node.test, ast.Name) and node.test.id in flags:
            branch = node.body
        elif (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id in flags
        ):
            branch = node.orelse
        else:
            continue
        for stmt in branch:
            for sub in ast.walk(stmt):
                gated.add(getattr(sub, "lineno", -1))
    return sorted(
        {node.lineno for node in _transaction_ending_nodes(fn) if node.lineno not in gated}
    )


def _refuses_outer_transaction(fn: ast.AST) -> bool:
    """Whether this function actually *raises* when a caller holds a transaction.

    Merely mentioning ``_in_transaction`` is not a refusal — a borrow-or-own
    writer opens with ``owns_transaction = not self._in_transaction`` and goes
    on to participate. Only two shapes count: a call to the backend's
    ``_require_transaction_idle``, which raises on that condition, or an
    ``if self._in_transaction:`` whose branch raises. Polarity matters — the
    inverted ``if not self._in_transaction: raise`` refuses the *standalone*
    case and sails straight into a caller's transaction.

    And it has to come first. A refusal below the commit it is supposed to
    prevent guards nothing, so anything found after the earliest
    transaction-ending statement does not count.
    """
    enders = _transaction_ending_nodes(fn)
    first_ender = min((node.lineno for node in enders), default=None)

    def in_time(node: ast.AST) -> bool:
        return first_ender is None or node.lineno < first_ender

    for node in _own_body(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_require_transaction_idle"
            and in_time(node)
        ):
            return True
        if not isinstance(node, ast.If) or not in_time(node):
            continue
        if _is_ownership_read(node.test) and any(
            isinstance(sub, ast.Raise) for stmt in node.body for sub in ast.walk(stmt)
        ):
            return True
    return False


# ---- #2167: a failed writer closes its own transaction ----------------------

# The helper whose ``with`` block is the protected region, and the commit
# helper that must sit inside one.
_ROLLBACK_CM = "_rolls_back_if_standalone"
_COMMIT_HELPER = "_commit_if_standalone"

# Statements that provably do not write. Classification is deliberately
# inverted — anything that is not recognisably one of these counts as a write —
# because the failure modes are not symmetric: a misjudged read costs a
# needlessly wide region, a misjudged write is the #2167 bug shipped under a
# green guard. ``WITH`` is absent on purpose: a CTE can end in either an INSERT
# or a SELECT, and the guard should not have to parse it to find out.
_READ_ONLY_SQL = ("SELECT", "PRAGMA", "EXPLAIN", "ANALYZE", "VALUES")


def _sql_leading_keyword(node: ast.AST) -> str | None:
    """The first SQL keyword of an ``execute`` argument, when it is knowable.

    ``None`` means the guard cannot tell — a variable, a ``.join``, a name — and
    the caller must treat that as a write. An f-string is readable up to its
    first placeholder, which is where the leading keyword always is: the
    interpolation in this codebase builds ``WHERE`` clauses and placeholder
    lists, never the verb.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value
    elif isinstance(node, ast.JoinedStr):
        head = node.values[0] if node.values else None
        if not (isinstance(head, ast.Constant) and isinstance(head.value, str)):
            return None
        text = head.value
    else:
        return None
    stripped = text.strip().lstrip("(").lstrip()
    return stripped.split(None, 1)[0].upper() if stripped else None


def _module_sql_constants(tree: ast.AST) -> dict[str, str]:
    """Leading keywords of module-level ``NAME = "SELECT ..."`` statements.

    Readers here hoist their longer statements into a module constant, so a
    resolver that only looked inside the function would call every one of them
    unknowable — and therefore a write.
    """
    consts: dict[str, str] = {}
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        keyword = _sql_leading_keyword(node.value)
        if isinstance(target, ast.Name) and keyword is not None:
            consts[target.id] = keyword
    return consts


def _resolve_local_sql(fn: ast.AST, name: str) -> str | None:
    """The leading keyword of a statement built up in a local variable.

    ``query = "SELECT ..."`` followed by ``query += " WHERE ..."`` is how the
    filtering readers here are written, and the verb is fixed by the first
    assignment. Only that first binding is read; if it is not a knowable
    string, or the name is rebound to something unknowable, the answer is
    ``None`` and the caller falls back to assuming a write.
    """
    first: str | None = None
    for node in _own_body(fn):
        if isinstance(node, ast.AugAssign):
            # ``query += " WHERE ..."`` appends a clause; it cannot change the
            # verb the first assignment fixed.
            continue
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id != name:
            continue
        keyword = _sql_leading_keyword(node.value)
        if keyword is None:
            return None
        if first is None:
            first = keyword
        elif first != keyword:
            # Two bindings disagreeing on the verb: which one reaches the
            # execute is a control-flow question this guard will not answer.
            return None
    return first


def _is_writing_sql(
    node: ast.AST, fn: ast.AST | None = None, consts: dict[str, str] | None = None
) -> bool:
    keyword = _sql_leading_keyword(node)
    if keyword is None and isinstance(node, ast.Name):
        if fn is not None:
            keyword = _resolve_local_sql(fn, node.id)
        if keyword is None and consts is not None:
            keyword = consts.get(node.id)
    if keyword is None:
        # Unknowable: assume it writes. Widening a region is cheap; missing a
        # write is the whole bug.
        return True
    if keyword in _TRANSACTION_ENDING_SQL:
        # Handled by the ownership half of this guard, not here.
        return False
    return keyword not in _READ_ONLY_SQL


def _db_argument(call: ast.Call) -> str | None:
    """The connection a call operates on, positional or ``db=``.

    Reading only ``args[0]`` would let ``self._commit_if_standalone(db=db)``
    slip past every containment check while looking identical in review.
    """
    if call.args and isinstance(call.args[0], ast.Name):
        return call.args[0].id
    for kw in call.keywords:
        if kw.arg == "db" and isinstance(kw.value, ast.Name):
            return kw.value.id
    return None


# Methods that write but never commit: they run on a ``db`` handed to them and
# leave the transaction to their caller. Two things follow, and the tests below
# enforce both — a call to one counts as a write at the call site, and a new
# write-only helper cannot appear without being classified here. Without the
# first, ``self._purge_expired(db)`` above a region hides a pending DELETE
# behind a call that shows no SQL.
#
# Keyed by ``(file, qualname)`` for the same reason ``OWNED_TRANSACTION_WRITERS``
# is: a bare method name would let one mixin's classification cover an
# identically named method on another.
_PARTICIPANT_HELPERS: dict[tuple[str, str], str] = {
    ("idempotency.py", "IdempotencyMixin._purge_expired"): "DELETEs expired ledger rows.",
    ("maintenance_runs.py", "MaintenanceRunMixin._prune_maintenance_runs"): (
        "DELETEs run rows past the retention window."
    ),
    ("formation.py", "FormationMixin._record_candidate_transition"): (
        "INSERTs the audit row for a state change its caller is making."
    ),
    ("eval_cases.py", "EvalCaseMixin._import_one_case"): (
        "DELETE + INSERTs one case inside import_eval_cases' own BEGIN."
    ),
}

#: Bare method names of the above, for matching a call site.
_PARTICIPANT_HELPER_NAMES = frozenset(q.rsplit(".", 1)[-1] for _, q in _PARTICIPANT_HELPERS)


def _protected_regions(fn: ast.AST) -> list[tuple[str, set[int]]]:
    """``(db-name, linenos)`` for every ``with self._rolls_back_if_standalone(db):``.

    The db name is carried because a region entered on one connection protects
    nothing on another: ``with self._rolls_back_if_standalone(other):`` around
    a commit on ``db`` reads as compliance and rolls back the wrong thing.
    """
    regions: list[tuple[str, set[int]]] = []
    for node in _own_body(fn):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            call = item.context_expr
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == _ROLLBACK_CM
                and _db_argument(call) is not None
            ):
                continue
            region_db = _db_argument(call)
            assert region_db is not None  # narrowed by the check above
            covered: set[int] = set()
            for stmt in node.body:
                # ``_own_body``-style boundary: a commit inside a nested ``def``
                # belongs to that function, which is checked on its own, so the
                # enclosing region must not vouch for it.
                stack = [stmt]
                while stack:
                    sub = stack.pop()
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    if hasattr(sub, "lineno"):
                        covered.add(sub.lineno)
                    stack.extend(ast.iter_child_nodes(sub))
            regions.append((region_db, covered))
    return regions


def _write_nodes(fn: ast.AST, consts: dict[str, str] | None = None) -> list[tuple[str, ast.AST]]:
    """``(db-name, node)`` for everything in ``fn`` that writes or commits.

    Both halves have to be inside the region. Guarding only the commit lets a
    write sit above the ``with`` where no rollback reaches it, which is the
    stranded transaction wearing a compliant shape.
    """
    found: list[tuple[str, ast.AST]] = []
    # A full walk, not ``_own_body``: a commit parked in a nested ``def`` inside
    # a region runs after that region has exited, so the region cannot vouch for
    # it, and nothing else would — ``_qualified_functions`` does not descend into
    # compound statements, so such a def is never yielded on its own.
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr in {_COMMIT_HELPER} | _PARTICIPANT_HELPER_NAMES:
            db_name = _db_argument(node)
            if db_name is not None:
                found.append((db_name, node))
        elif (
            attr in {"execute", "executemany", "executescript"}
            and isinstance(node.func.value, ast.Name)
            and node.args
            and _is_writing_sql(node.args[0], fn, consts)
        ):
            found.append((node.func.value.id, node))
    return found


def _region_exits(fn: ast.AST) -> list[int]:
    """``return``/``break``/``continue`` inside a protected region.

    Leaving a region by any of these exits the context manager *normally*: no
    exception, so no rollback, and the commit below never runs either. The
    statements already issued stay pending on the shared connection while the
    caller is told the write succeeded — the #2167 failure wearing a shape this
    guard's containment check would otherwise call compliant.
    """
    regions = _protected_regions(fn)
    if not regions:
        return []
    covered: set[int] = set().union(*(lines for _, lines in regions))
    return sorted(
        {
            node.lineno
            for node in _own_body(fn)
            if isinstance(node, (ast.Return, ast.Break, ast.Continue)) and node.lineno in covered
        }
    )


def _unprotected_writes(fn: ast.AST, consts: dict[str, str] | None = None) -> list[int]:
    """Line numbers of writes and commits outside a matching protected region."""
    regions = _protected_regions(fn)
    out: list[int] = []
    for db_name, node in _write_nodes(fn, consts):
        if not any(db_name == name and node.lineno in covered for name, covered in regions):
            out.append(node.lineno)
    return sorted(set(out))


def _awaits_inside_regions(fn: ast.AST) -> list[int]:
    """``await`` line numbers inside a protected region.

    The region is only as task-affine as it is uninterrupted: suspend inside
    one and another task can reach the shared writer connection and commit
    the half-written work this region exists to be able to discard.
    """
    regions = _protected_regions(fn)
    if not regions:
        return []
    covered: set[int] = set().union(*(lines for _, lines in regions))
    return sorted(
        {
            node.lineno
            for node in _own_body(fn)
            if isinstance(node, ast.Await) and node.lineno in covered
        }
    )


def _swallowing_handlers(fn: ast.AST) -> list[int]:
    """Handlers inside a protected region that can finish without re-raising.

    Catching inside the region and returning normally commits nothing and
    rolls back nothing — the pending statements stay on the connection, and
    the caller is told the write succeeded.

    The raise has to be unconditional. A handler whose ``raise`` sits under an
    ``if`` re-raises on one path and swallows on the other, and "contains a
    Raise somewhere" would call that compliant.
    """
    regions = _protected_regions(fn)
    if not regions:
        return []
    covered: set[int] = set().union(*(lines for _, lines in regions))
    out: list[int] = []
    for node in _own_body(fn):
        if not isinstance(node, ast.ExceptHandler) or node.lineno not in covered:
            continue
        if not any(isinstance(stmt, ast.Raise) for stmt in node.body):
            out.append(node.lineno)
    return sorted(out)


def _commits_standalone(fn: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr == _COMMIT_HELPER for node in ast.walk(fn)
    )


def _writes_without_committing(fn: ast.AST, consts: dict[str, str] | None = None) -> bool:
    """A participant: it writes, but leaves the transaction to its caller.

    Writing counts transitively — a helper that only calls ``_purge_expired``
    leaves exactly the same DELETE pending as one that spells it out, and
    classifying by visible SQL alone would let the delegating version stay
    unclassified and therefore invisible at its own call sites.
    """
    if _commits_standalone(fn) or _ends_transaction_directly(fn):
        return False
    for node in _own_body(fn):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr in _PARTICIPANT_HELPER_NAMES:
            return True
        if (
            node.func.attr in {"execute", "executemany", "executescript"}
            and node.args
            and _is_writing_sql(node.args[0], fn, consts)
        ):
            return True
    return False


def _failure_cleanup_lineno(fn: ast.AST) -> int | None:
    """Where this function discards its own work on the failure path.

    An owned-transaction writer is not covered by the region containment check
    — it runs its own ``BEGIN`` and ends the transaction itself — so what has
    to be verified instead is that some handler rolls back. Both spellings
    count, the method and the SQL, and it has to be inside an ``except``:
    a rollback on the success path is a different statement entirely.
    """
    for node in _own_body(fn):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Attribute) and sub.attr in {"rollback", _ROLLBACK_CM}:
                    return node.lineno
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr in {"execute", "executescript"}
                    and sub.args
                    and _sql_leading_keyword(sub.args[0]) == "ROLLBACK"
                ):
                    return node.lineno
    return None


def _owner_qualnames(filename: str) -> set[str]:
    return {q for f, q in OWNED_TRANSACTION_WRITERS if f == filename}


def _unprotected_writers() -> dict[tuple[str, str], list[int]]:
    """Every standalone writer with a write or commit outside a protected region.

    Only writers that end their own transaction through ``_commit_if_standalone``
    are checked here. A function that writes without committing is a participant
    on someone else's transaction; it is checked by
    ``test_every_write_only_helper_is_classified`` instead, which forces it into
    ``_PARTICIPANT_HELPERS`` so its *call sites* are the thing that must sit in
    a region.
    """
    found: dict[tuple[str, str], list[int]] = {}
    for path in sorted(_MIXINS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_sql_constants(tree)
        owners = _owner_qualnames(path.name)
        for qualname, fn in _qualified_functions(tree):
            if qualname in owners:
                # Owns its BEGIN/commit/rollback outright; checked by the
                # REFUSES/BORROWS tests above, not by region containment.
                continue
            if not _commits_standalone(fn):
                continue
            lines = _unprotected_writes(fn, consts)
            if lines:
                found[(path.name, qualname)] = lines
    return found


def _unclassified_write_only_helpers() -> dict[tuple[str, str], str]:
    """Write-only functions not declared in ``_PARTICIPANT_HELPERS``."""
    found: dict[tuple[str, str], str] = {}
    for path in sorted(_MIXINS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_sql_constants(tree)
        owners = _owner_qualnames(path.name)
        for qualname, fn in _qualified_functions(tree):
            if qualname in owners or not _writes_without_committing(fn, consts):
                continue
            if (path.name, qualname) not in _PARTICIPANT_HELPERS:
                found[(path.name, qualname)] = "writes without committing"
    return found


def _direct_transaction_enders() -> dict[tuple[str, str], ast.AST]:
    """Every mixin function that ends a transaction without the helpers."""
    found: dict[tuple[str, str], ast.AST] = {}
    for path in sorted(_MIXINS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qualname, fn in _qualified_functions(tree):
            if _ends_transaction_directly(fn):
                found[(path.name, qualname)] = fn
    return found


def test_mixin_directory_is_where_this_guard_thinks_it_is() -> None:
    """A move or rename must fail here rather than scan an empty directory."""
    assert _MIXINS.is_dir()
    names = {p.name for p in _MIXINS.glob("*.py")}
    assert {"sessions.py", "formation.py", "history.py", "relations.py"} <= names


class TestEveryMixinWriterCommitsThroughOwnership:
    def test_no_unregistered_direct_commit_or_rollback(self) -> None:
        """The sweep, kept swept.

        A new writer that commits directly is the #2162 bug arriving again,
        and it will not look wrong in review.
        """
        unregistered = sorted(set(_direct_transaction_enders()) - set(OWNED_TRANSACTION_WRITERS))
        assert not unregistered, (
            "These mixin functions reach the connection's commit/rollback "
            "directly. A writer that joins a caller's transaction must go "
            "through self._commit_if_standalone(db) / "
            "self._rollback_if_standalone(db) instead, or — if it genuinely "
            "runs its own BEGIN and refuses in-transaction callers — be added "
            f"to OWNED_TRANSACTION_WRITERS with that reason: {unregistered}"
        )

    def test_registry_has_no_stale_entries(self) -> None:
        """An exemption for code that no longer commits directly certifies
        nothing, and would keep covering the next writer to take that name."""
        stale = sorted(set(OWNED_TRANSACTION_WRITERS) - set(_direct_transaction_enders()))
        assert not stale, f"registered owners no longer commit/rollback directly: {stale}"

    def test_refusing_writers_still_refuse_an_outer_transaction(self) -> None:
        """The exemption's premise, not just its presence.

        A ``REFUSES`` entry is bare-commit-safe *because* it cannot run inside
        someone else's transaction. Delete the refusal and the entry silently
        becomes a licence for exactly the bug this guards.
        """
        sites = _direct_transaction_enders()
        unrefusing = [
            key
            for key, (mode, _) in OWNED_TRANSACTION_WRITERS.items()
            if mode == REFUSES and key in sites and not _refuses_outer_transaction(sites[key])
        ]
        assert not unrefusing, (
            "registered owned-transaction writers with no in-transaction "
            f"refusal left: {unrefusing}"
        )

    def test_borrowing_writers_gate_every_transaction_ending_statement(self) -> None:
        """A ``BORROWS`` entry has no refusal to check — its safety is the gate.

        These run inside a caller's transaction on purpose, so what makes them
        safe is that every commit and rollback sits under the same
        ``owns_transaction`` test. One ungated statement is the #2162 bug.
        """
        sites = _direct_transaction_enders()
        ungated = {
            key: _ungated_transaction_enders(sites[key])
            for key, (mode, _) in OWNED_TRANSACTION_WRITERS.items()
            if mode == BORROWS and key in sites and _ungated_transaction_enders(sites[key])
        }
        assert not ungated, (
            "borrow-or-own writers end a transaction without gating it on "
            f"their ownership flag, at these lines: {ungated}"
        )

    def test_every_registry_entry_declares_a_known_mode(self) -> None:
        """A typo'd mode would silently skip both checks above."""
        bad = {
            key: mode
            for key, (mode, _) in OWNED_TRANSACTION_WRITERS.items()
            if mode not in {REFUSES, BORROWS}
        }
        assert not bad, f"registry entries with an unknown mode: {bad}"


class TestEveryMixinWriterClosesItsFailedTransaction:
    """#2167: a writer that raises mid-write must not leave the transaction open.

    ``OWNED_TRANSACTION_WRITERS`` covers the writers that run their own
    ``BEGIN``; everything else reaches the connection through
    ``_commit_if_standalone`` and has to sit inside a
    ``with self._rolls_back_if_standalone(db):`` region together with its
    writes. There is deliberately no exemption registry: the sweep left no
    site needing one, and adding an empty one now would only invite the first
    entry to be added without a reason.
    """

    def test_every_write_and_commit_is_inside_a_protected_region(self) -> None:
        offenders = _unprotected_writers()
        assert not offenders, (
            "These mixin functions write or commit outside a "
            "`with self._rolls_back_if_standalone(db):` region, so a failure "
            "between the statement and the commit leaves the transaction open "
            "on the shared writer connection for the next unrelated commit to "
            f"flush (#2167, #1572): {offenders}"
        )

    def test_every_write_only_helper_is_classified(self) -> None:
        """A helper that writes on a caller's ``db`` and never commits is the
        one shape the containment check above cannot see, because there is no
        commit in it to anchor on. Declaring it in ``_PARTICIPANT_HELPERS`` is
        what makes its *call sites* count as writes."""
        unclassified = _unclassified_write_only_helpers()
        assert not unclassified, (
            "These mixin functions write without ending a transaction. Add each "
            "to _PARTICIPANT_HELPERS so calls to it are treated as writes at "
            f"the call site: {sorted(unclassified)}"
        )

    def test_participant_helper_registry_has_no_stale_entries(self) -> None:
        """An entry that no longer refers to a write-only helper stops
        certifying anything and would keep covering the next function to take
        that name."""
        live: set[tuple[str, str]] = set()
        for path in sorted(_MIXINS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            consts = _module_sql_constants(tree)
            live |= {
                (path.name, qualname)
                for qualname, fn in _qualified_functions(tree)
                if _writes_without_committing(fn, consts)
            }
        stale = sorted(set(_PARTICIPANT_HELPERS) - live)
        assert not stale, f"_PARTICIPANT_HELPERS entries that no longer write: {stale}"

    def test_no_region_is_left_by_a_normal_exit(self) -> None:
        """``return`` inside a region leaves it without an exception, so the
        rollback never runs — and neither does the commit below it."""
        offenders = {}
        for path in sorted(_MIXINS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for qualname, fn in _qualified_functions(tree):
                lines = _region_exits(fn)
                if lines:
                    offenders[(path.name, qualname)] = lines
        assert not offenders, (
            "return/break/continue inside a rollback-protected region: move the "
            f"exit below the region so the commit is always reached: {offenders}"
        )

    def test_owned_transaction_writers_still_clean_up_on_failure(self) -> None:
        """The registry's other half of the #2167 contract.

        Owned writers are exempt from region containment because they run their
        own ``BEGIN`` — but exempt from containment is not exempt from closing a
        failed transaction. Deleting the ``except`` that rolls back leaves the
        refusal and the gating intact, so every other check here stays green.
        """
        sites = _direct_transaction_enders()
        uncleaned = [
            key
            for key in OWNED_TRANSACTION_WRITERS
            if key in sites and _failure_cleanup_lineno(sites[key]) is None
        ]
        assert not uncleaned, (
            "registered owned-transaction writers with no rollback on their "
            f"failure path: {uncleaned}"
        )

    def test_no_await_inside_a_protected_region(self) -> None:
        """A suspension point hands the shared connection to another task
        while this writer's statements are pending — the region can no longer
        promise that what it rolls back is only its own work."""
        offenders = {}
        for path in sorted(_MIXINS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for qualname, fn in _qualified_functions(tree):
                lines = _awaits_inside_regions(fn)
                if lines:
                    offenders[(path.name, qualname)] = lines
        assert not offenders, (
            "`await` inside a rollback-protected region: move it out, or the "
            f"region stops being task-affine: {offenders}"
        )

    def test_no_handler_inside_a_region_swallows_its_failure(self) -> None:
        """Catching inside the region without re-raising skips both the commit
        and the rollback, and reports success over a pending write."""
        offenders = {}
        for path in sorted(_MIXINS.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for qualname, fn in _qualified_functions(tree):
                lines = _swallowing_handlers(fn)
                if lines:
                    offenders[(path.name, qualname)] = lines
        assert not offenders, (
            f"exception handlers inside a protected region that never re-raise: {offenders}"
        )

    def test_the_rollback_helper_still_exists_under_that_name(self) -> None:
        """The whole check is spelled against one method name; a rename that
        misses this file would turn every assertion above green and empty."""
        import inspect

        from memtomem.storage.sqlite_backend import SqliteBackend

        assert hasattr(SqliteBackend, _ROLLBACK_CM)
        assert hasattr(SqliteBackend, _COMMIT_HELPER)
        source = inspect.getsource(getattr(SqliteBackend, _ROLLBACK_CM))
        assert "rollback" in source, f"{_ROLLBACK_CM} no longer rolls anything back"


class TestGuardHelpersRejectFakeCompliance:
    """The guard's own unit tests.

    Each case is a way a future edit could satisfy a laxer version of this
    check while still ending someone else's transaction. Verified against the
    helpers directly, so the cases stay readable and need no matching edit in
    production code.
    """

    @staticmethod
    def _fn(source: str) -> ast.AST:
        return _qualified_functions(ast.parse(source))[0][1]

    def test_bare_commit_is_flagged(self) -> None:
        assert _ends_transaction_directly(
            self._fn("async def w(self):\n    db = self._get_db()\n    db.commit()\n")
        )

    def test_helper_call_is_not_flagged(self) -> None:
        assert not _ends_transaction_directly(
            self._fn(
                "async def w(self):\n    db = self._get_db()\n    self._commit_if_standalone(db)\n"
            )
        )

    def test_renamed_receiver_is_still_flagged(self) -> None:
        """The guard keys on the method, not on the local being called ``db``."""
        assert _ends_transaction_directly(
            self._fn("async def w(self):\n    conn = self._get_db()\n    conn.rollback()\n")
        )

    def test_uncalled_attribute_reference_is_flagged(self) -> None:
        """Handing the bound method somewhere else ends the transaction too."""
        assert _ends_transaction_directly(
            self._fn(
                "async def w(self):\n"
                "    db = self._get_db()\n"
                "    await asyncio.to_thread(db.commit)\n"
            )
        )

    def test_commit_spelled_as_sql_is_flagged(self) -> None:
        """``db.execute("COMMIT")`` ends the transaction as surely as
        ``db.commit()``, and is the obvious way around an attribute-only check."""
        for statement in ('"COMMIT"', '"rollback"', '"END TRANSACTION"', '"COMMIT;"'):
            assert _ends_transaction_directly(
                self._fn(
                    f"async def w(self):\n    db = self._get_db()\n    db.execute({statement})\n"
                )
            ), statement

    def test_begin_and_ordinary_sql_are_not_flagged(self) -> None:
        """Opening a transaction is not ending one, and a DML statement that
        merely mentions a keyword must not trip the guard."""
        assert not _ends_transaction_directly(
            self._fn('async def w(self):\n    db.execute("BEGIN IMMEDIATE")\n')
        )
        assert not _ends_transaction_directly(
            self._fn("async def w(self):\n    db.execute(\"UPDATE runs SET status='end'\")\n")
        )

    def test_commit_inside_a_lambda_is_flagged(self) -> None:
        """Nothing else visits a lambda body, so skipping it would hide the call
        entirely rather than attribute it elsewhere."""
        assert _ends_transaction_directly(
            self._fn("async def w(self):\n    run(lambda: db.commit())\n")
        )

    def test_commit_in_a_nested_def_belongs_to_the_nested_def(self) -> None:
        """Otherwise a helper's commit would be attributed to its enclosing
        method, which both exempts the real site and flags an innocent one."""
        source = "async def outer(self):\n    def inner():\n        db.commit()\n    return inner\n"
        by_name = dict(_qualified_functions(ast.parse(source)))
        assert not _ends_transaction_directly(by_name["outer"])
        assert _ends_transaction_directly(by_name["outer.inner"])

    def test_refusal_detected_in_both_dialects_and_via_require_idle(self) -> None:
        assert _refuses_outer_transaction(
            self._fn(
                "async def w(self):\n"
                '    if getattr(self, "_in_transaction", False):\n'
                "        raise Err()\n"
            )
        )
        assert _refuses_outer_transaction(
            self._fn("async def w(self):\n    if self._in_transaction:\n        raise Err('no')\n")
        )
        assert _refuses_outer_transaction(
            self._fn('async def w(self):\n    self._require_transaction_idle("w")\n')
        )

    def test_writer_with_no_refusal_is_not_treated_as_refusing(self) -> None:
        assert not _refuses_outer_transaction(
            self._fn("async def w(self):\n    db = self._get_db()\n    db.commit()\n")
        )

    def test_borrowing_is_not_mistaken_for_refusing(self) -> None:
        """The distinction the two registry modes rest on.

        A borrow-or-own writer opens by *reading* ``_in_transaction`` and then
        participates. Counting that as a refusal would let a REFUSES entry keep
        its exemption while quietly joining a caller's transaction.
        """
        assert not _refuses_outer_transaction(
            self._fn("async def w(self):\n    owns = not self._in_transaction\n")
        )

    def test_an_inverted_refusal_is_not_a_refusal(self) -> None:
        """``if not self._in_transaction: raise`` refuses the standalone case
        and sails straight into a caller's transaction — the exact inverse of
        the contract, and a one-character edit away from the real thing."""
        assert not _refuses_outer_transaction(
            self._fn("async def w(self):\n    if not self._in_transaction:\n        raise Err()\n")
        )

    def test_a_refusal_after_the_commit_is_not_a_refusal(self) -> None:
        """Order is the whole point: a check below the commit it should have
        prevented runs only once the damage is done."""
        assert not _refuses_outer_transaction(
            self._fn(
                'async def w(self):\n    db.commit()\n    self._require_transaction_idle("w")\n'
            )
        )

    def test_a_check_that_does_not_raise_is_not_a_refusal(self) -> None:
        """Turning the raise into an early return is not refusing — it is
        silently doing nothing, and the caller still believes the write ran."""
        assert not _refuses_outer_transaction(
            self._fn("async def w(self):\n    if self._in_transaction:\n        return None\n")
        )

    def test_ungated_commit_is_caught_even_when_the_flag_exists(self) -> None:
        """The borrow check must read control flow, not just look for the word.

        Dropping the ``if`` around one commit while the ownership flag stays
        assigned above it is the realistic regression here, and a
        mentions-``_in_transaction`` check would call it compliant.
        """
        gated = self._fn(
            "async def w(self):\n"
            "    owns_transaction = not self._in_transaction\n"
            "    if owns_transaction:\n"
            '        db.execute("COMMIT")\n'
        )
        assert _ungated_transaction_enders(gated) == []

        ungated = self._fn(
            "async def w(self):\n"
            "    owns_transaction = not self._in_transaction\n"
            '    db.execute("COMMIT")\n'
        )
        assert _ungated_transaction_enders(ungated) == [3]

    def test_only_the_ownership_positive_branch_counts_as_a_gate(self) -> None:
        """``if owns_transaction:`` is the gate; its negation is the bug.

        The branch that runs when the writer does NOT own the transaction is
        precisely where it is inside someone else's — committing there is what
        #2162 is about, so an inverted test or an ``else`` must not read as
        gated.
        """
        inverted = self._fn(
            "async def w(self):\n"
            "    owns_transaction = not self._in_transaction\n"
            "    if not owns_transaction:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(inverted) == [4]

        else_branch = self._fn(
            "async def w(self):\n"
            "    owns_transaction = not self._in_transaction\n"
            "    if owns_transaction:\n"
            "        pass\n"
            "    else:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(else_branch) == [6]

    def test_a_compound_condition_is_not_an_ownership_flag(self) -> None:
        """``not (self._in_transaction and enabled)`` is true in cases where
        the writer does not own the transaction, so it cannot gate a commit."""
        compound = self._fn(
            "async def w(self):\n"
            "    owns = not (self._in_transaction and enabled)\n"
            "    if owns:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(compound) == [4]

    def test_a_rebound_flag_stops_counting_as_a_gate(self) -> None:
        """Reassigning the flag decouples it from ownership; by the time it is
        read it is answering a different question."""
        rebound = self._fn(
            "async def w(self):\n"
            "    owns = not self._in_transaction\n"
            "    owns = True\n"
            "    if owns:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(rebound) == [5]

    def test_a_lookalike_flag_is_not_an_ownership_gate(self) -> None:
        """The flag is recognised by how it is defined, not by its name — an
        unrelated variable that happens to say "transaction" proves nothing."""
        lookalike = self._fn(
            "async def w(self):\n"
            "    transaction_failed = check()\n"
            "    if transaction_failed:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(lookalike) == [4]


class TestRollbackRegionHelpersRejectFakeCompliance:
    """Unit tests for the #2167 half of the guard.

    Each case is a shape that would satisfy a laxer containment check while
    still leaving a failed writer's transaction open.
    """

    @staticmethod
    def _fn(source: str) -> ast.AST:
        return _qualified_functions(ast.parse(source))[0][1]

    _WRAPPED = (
        "async def w(self):\n"
        "    db = self._get_db()\n"
        "    with self._rolls_back_if_standalone(db):\n"
        '        db.execute("INSERT INTO t VALUES (?)", (x,))\n'
        "        self._commit_if_standalone(db)\n"
    )

    def test_the_canonical_shape_passes(self) -> None:
        assert _unprotected_writes(self._fn(self._WRAPPED)) == []

    def test_a_write_before_the_region_is_caught(self) -> None:
        """The reason containment is checked on writes and not just on the
        commit: this shape rolls back nothing the INSERT did."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.execute("INSERT INTO t VALUES (?)", (x,))\n'
            "    with self._rolls_back_if_standalone(db):\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == [3]

    def test_a_write_after_the_region_is_caught(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        self._commit_if_standalone(db)\n"
            '    db.execute("UPDATE t SET a=1")\n'
        )
        assert _unprotected_writes(fn) == [5]

    def test_a_commit_outside_any_region_is_caught(self) -> None:
        fn = self._fn(
            "async def w(self):\n    db = self._get_db()\n    self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == [3]

    def test_a_lookalike_context_manager_is_not_a_region(self) -> None:
        """Any ``with`` would do if the check only counted indentation."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._lock:\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == [4]

    def test_a_region_on_a_different_connection_does_not_count(self) -> None:
        """Rolling back ``other`` leaves ``db``'s statements exactly as pending
        as they were, while the shape reads as compliant."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(other):\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == [4]

    def test_a_commit_in_a_nested_def_is_not_covered_by_the_outer_region(self) -> None:
        """The nested function runs on its own later, once the enclosing
        ``with`` has exited, so the region cannot vouch for it — and nothing
        else would either, since ``_qualified_functions`` does not yield a def
        buried inside a compound statement."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        def later():\n"
            "            self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == [5]

    def test_a_writing_helper_call_counts_as_a_write(self) -> None:
        """``_purge_expired(db)`` shows no SQL at the call site but leaves a
        DELETE pending exactly as an inline one would."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    self._purge_expired(db)\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == [3]

    def test_a_read_outside_the_region_is_not_a_write(self) -> None:
        """Reads legitimately live outside — flagging them would push callers
        to widen regions for no gain."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    row = db.execute("SELECT 1").fetchone()\n'
            "    with self._rolls_back_if_standalone(db):\n"
            '        db.execute("UPDATE t SET a=1")\n'
            "        self._commit_if_standalone(db)\n"
        )
        assert _unprotected_writes(fn) == []

    def test_an_await_inside_the_region_is_caught(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        await self.something()\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _awaits_inside_regions(fn) == [4]

    def test_an_await_after_the_region_is_fine(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        self._commit_if_standalone(db)\n"
            "    return await self.reload()\n"
        )
        assert _awaits_inside_regions(fn) == []

    def test_a_handler_inside_the_region_that_swallows_is_caught(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        try:\n"
            '            db.execute("INSERT INTO t VALUES (1)")\n'
            "        except Exception:\n"
            "            pass\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _swallowing_handlers(fn) == [6]

    def test_a_handler_inside_the_region_that_reraises_is_fine(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        try:\n"
            '            db.execute("INSERT INTO t VALUES (1)")\n'
            "        except Exception as exc:\n"
            "            raise StorageError(str(exc)) from exc\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _swallowing_handlers(fn) == []

    def test_a_keyword_connection_argument_is_still_a_commit(self) -> None:
        """``_commit_if_standalone(db=db)`` is the same call; reading only
        ``args[0]`` would make it invisible to every containment check."""
        fn = self._fn(
            "async def w(self):\n    db = self._get_db()\n    self._commit_if_standalone(db=db)\n"
        )
        assert _unprotected_writes(fn) == [3]

    def test_a_cte_write_is_not_assumed_to_be_a_read(self) -> None:
        """A statement whose verb the guard cannot see counts as a write: a
        ``WITH`` can end in an INSERT, and guessing wrong ships the bug."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.execute("WITH stale AS (SELECT id FROM t) DELETE FROM t WHERE id IN stale")\n'
            "    with self._rolls_back_if_standalone(db):\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert 3 in _unprotected_writes(fn)

    def test_unknowable_sql_counts_as_a_write(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    db.execute(build_statement())\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert 3 in _unprotected_writes(fn)

    def test_a_select_assembled_in_a_local_is_still_a_read(self) -> None:
        """The filtering readers build their statement in a variable; treating
        those as writes would force pointless regions around pure reads."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    query = "SELECT a FROM t"\n'
            '    query += " WHERE b = ?"\n'
            "    rows = db.execute(query, params).fetchall()\n"
        )
        assert _unprotected_writes(fn) == []

    def test_a_write_assembled_in_a_local_is_caught(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    stmt = "DELETE FROM t"\n'
            '    stmt += " WHERE b = ?"\n'
            "    db.execute(stmt, params)\n"
        )
        assert _unprotected_writes(fn) == [5]

    def test_a_return_inside_the_region_is_caught(self) -> None:
        """Neither commit nor rollback runs on this path, and the statements
        already issued stay pending while the caller sees success."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            '        cur = db.execute("DELETE FROM t WHERE id=?", (i,))\n'
            "        if not cur.rowcount:\n"
            "            return False\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _region_exits(fn) == [6]

    def test_a_return_after_the_region_is_fine(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            '        cur = db.execute("DELETE FROM t")\n'
            "        self._commit_if_standalone(db)\n"
            "    return cur.rowcount > 0\n"
        )
        assert _region_exits(fn) == []

    def test_a_conditionally_reraising_handler_is_still_swallowing(self) -> None:
        """One path re-raises, the other returns normally — "contains a Raise"
        would call that compliant."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            "        try:\n"
            '            db.execute("INSERT INTO t VALUES (1)")\n'
            "        except Exception:\n"
            "            if strict:\n"
            "                raise\n"
            "        self._commit_if_standalone(db)\n"
        )
        assert _swallowing_handlers(fn) == [6]

    def test_an_owned_writer_without_a_rollback_handler_is_caught(self) -> None:
        """The owned-transaction half: refusal and BEGIN intact, cleanup gone."""
        without = self._fn(
            "async def w(self):\n"
            '    self._require_transaction_idle("w")\n'
            '    db.execute("BEGIN IMMEDIATE")\n'
            '    db.execute("INSERT INTO t VALUES (1)")\n'
            "    db.commit()\n"
        )
        assert _failure_cleanup_lineno(without) is None

    def test_an_owned_writer_with_a_rollback_handler_passes(self) -> None:
        with_cleanup = self._fn(
            "async def w(self):\n"
            '    self._require_transaction_idle("w")\n'
            '    db.execute("BEGIN IMMEDIATE")\n'
            "    try:\n"
            '        db.execute("INSERT INTO t VALUES (1)")\n'
            "        db.commit()\n"
            "    except Exception:\n"
            "        db.rollback()\n"
            "        raise\n"
        )
        assert _failure_cleanup_lineno(with_cleanup) == 7

    def test_a_rollback_on_the_success_path_is_not_failure_cleanup(self) -> None:
        """Only a handler counts: a rollback the happy path runs says nothing
        about what happens when the write raises."""
        fn = self._fn(
            "async def w(self):\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
            "    if dry_run:\n"
            "        db.rollback()\n"
            "    db.commit()\n"
        )
        assert _failure_cleanup_lineno(fn) is None

    def test_a_delegating_helper_is_classified_transitively(self) -> None:
        """A helper that only calls another participant leaves the same rows
        pending, and shows no SQL of its own to be spotted by."""
        fn = self._fn("def helper(self, db):\n    self._purge_expired(db)\n")
        assert _writes_without_committing(fn) is True
