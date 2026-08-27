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

Scope is ``storage/mixins/*.py`` plus ``sqlite_backend.py`` and
``sqlite_meta.py`` (#2182). The backend was outside the sweep until #2175
found six of these exact shapes living there, none of them visible to a guard
scoped at ``storage/mixins/`` — so the scope now follows the contract rather
than the directory. What the backend adds is that not every writer there can
go through the helpers, which is why exemptions here name a *kind* and a
reason, never just a path:

* **authority** (``TRANSACTION_AUTHORITY``) — ``transaction()`` and the
  ``_commit_if_standalone`` / ``_rollback_if_standalone`` helpers *are* the
  machinery every other writer is required to use, so they reach ``commit``
  and ``rollback`` directly by definition. Their entries are checked against
  their own premise, not merely against still-committing: an unconditional
  ``_commit_if_standalone`` would keep the exemption valid while breaking
  every participant that trusts it.
* **owns-transaction** (``OWNED_TRANSACTION_WRITERS``) — four modes, each
  checked differently: ``REFUSES``, ``BORROWS``, ``GATED`` (participates by
  gating each ender on ``self._in_transaction`` inline, with its writes inside
  a try that rolls back), and ``PRIVATE`` (commits a connection it opened
  itself, which no caller can be inside).
* **standalone-default** (``STANDALONE_DEFAULTS``) — ``sqlite_meta.py``'s
  module-level ``_standalone_commit`` / ``_standalone_write_guard``, the
  ownership-blind defaults a non-backend construction gets. The backend
  injects its own ownership-aware pair over them, which is pinned here; a
  writer calling the defaults directly is rejected.

Two rules are backend-scope on purpose. The blind-``BEGIN`` check (a ``BEGIN``
needs a refusal of a pending transaction this task does not own, #2167) is not
applied to the mixins, whose borrow-or-own writers gate ``BEGIN`` on the
ownership flag alone — extending it there is follow-up. And ``sqlite_meta.py``
is the only file where ``_commit`` / ``_write_guard`` name the helpers; those
are generic enough that accepting them everywhere would let any method
impersonate the contract.

``sqlite_namespace.py`` stays out: it composes through an injected
``_in_transaction`` callable and its own borrow-or-refuse
``_begin_namespace_write``, covered by
``test_namespace_writer_transactions.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import memtomem

_STORAGE = Path(memtomem.__file__).parent / "storage"
_MIXINS = _STORAGE / "mixins"

#: The two non-mixin files under this guard (#2182). Named rather than globbed:
#: ``sqlite_namespace.py`` and ``sqlite_schema.py`` live beside them and are
#: deliberately out of scope, so a glob would quietly adopt them.
_BACKEND_FILES = ("sqlite_backend.py", "sqlite_meta.py")


def _scanned_files() -> list[Path]:
    """Every file this guard reads, in a stable order."""
    return sorted(_MIXINS.glob("*.py")) + [_STORAGE / name for name in _BACKEND_FILES]


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
# ``GATED`` — a participant that spells the helpers out inline: every ender sits
# under ``if not self._in_transaction:`` and every write sits inside a try whose
# handler rolls back. Same contract as ``_commit_if_standalone`` +
# ``_rolls_back_if_standalone``, written by hand on the hot write paths.
GATED = "gated"
# ``PRIVATE`` — commits a connection it opened itself. No caller can be inside
# a transaction on a connection that does not exist until this function runs.
PRIVATE = "private"

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
    ("sqlite_backend.py", "SqliteBackend.reset_embedding_meta"): (
        REFUSES,
        "Rewrites schema and in-memory embedding state together under its own "
        "BEGIN IMMEDIATE; an owner's rollback would restore the old schema "
        "while the in-memory fields kept pointing at the new config.",
    ),
    ("sqlite_backend.py", "SqliteBackend.reset_all"): (
        BORROWS,
        "Wipes every table, which a caller may want inside a wider "
        "transaction: takes its own BEGIN IMMEDIATE only when standalone and "
        "gates commit/rollback on that ``owns_txn`` flag.",
    ),
    ("sqlite_backend.py", "SqliteBackend.update_chunks_scope_for_source"): (
        BORROWS,
        "Borrow-or-own: needs BEGIN IMMEDIATE around its SELECT-then-UPDATE "
        "pair so a concurrent indexer cannot duplicate chunks at the "
        "destination, but composes into a caller's migration transaction.",
    ),
    ("sqlite_backend.py", "SqliteBackend.upsert_chunks"): (
        GATED,
        "Hot write path: gates its commit/rollback on self._in_transaction "
        "inline instead of calling the helpers.",
    ),
    ("sqlite_backend.py", "SqliteBackend.update_chunk_line_ranges"): (
        GATED,
        "Inline-gated participant, as upsert_chunks.",
    ),
    ("sqlite_backend.py", "SqliteBackend.update_chunk_metadata"): (
        GATED,
        "Inline-gated participant, as upsert_chunks.",
    ),
    ("sqlite_backend.py", "SqliteBackend.delete_chunks"): (
        GATED,
        "Inline-gated participant, as upsert_chunks.",
    ),
    ("sqlite_backend.py", "SqliteBackend.delete_by_source"): (
        GATED,
        "Inline-gated participant on its non-empty branch; the empty branch "
        "goes through the helpers, and both are covered by the containment "
        "check (#2175).",
    ),
    ("sqlite_backend.py", "SqliteBackend.rebuild_fts._run"): (
        PRIVATE,
        "Runs in a worker thread on its own sqlite3.connect against the same "
        "file; the shared writer connection is never touched, and dispatch is "
        "refused while this backend owns a transaction.",
    ),
}

# kind: authority — the ownership machinery itself. These reach commit and
# rollback directly because they are what every other writer is required to
# reach them *through*. Registered separately from the owns-transaction writers
# because the thing to re-check is different: an owned writer must still refuse
# or gate, while these must still be the gate (see
# ``test_the_authority_helpers_still_gate_on_ownership``).
TRANSACTION_AUTHORITY: dict[tuple[str, str], str] = {
    ("sqlite_backend.py", "SqliteBackend.transaction"): (
        "The owner: takes the BEGIN IMMEDIATE, commits on clean exit and rolls "
        "back on failure, and refuses a connection that already has one open."
    ),
    ("sqlite_backend.py", "SqliteBackend._commit_if_standalone"): (
        "The sanctioned commit. Ends the transaction only when this task does not own one."
    ),
    ("sqlite_backend.py", "SqliteBackend._rollback_if_standalone"): (
        "The sanctioned rollback, with the same ownership test."
    ),
}

# kind: standalone-default — ``sqlite_meta.py``'s ownership-blind defaults, for
# a MetaManager built without a backend (schema helpers, tests). The backend
# injects ``_commit_if_standalone`` / ``_rolls_back_if_standalone`` over them,
# which ``test_the_backend_injects_its_ownership_aware_meta_helpers`` pins; a
# scanned writer that calls these by name instead is rejected.
STANDALONE_DEFAULTS: dict[tuple[str, str], str] = {
    ("sqlite_meta.py", "_standalone_commit"): (
        "Default ``commit`` for a MetaManager with no backend to defer to."
    ),
    ("sqlite_meta.py", "_standalone_write_guard"): (
        "Default ``write_guard``: rolls back a failed standalone meta write "
        "instead of stranding its transaction."
    ),
}

#: Every exemption, by key — for the "registered somewhere, exactly once" checks.
_ALL_EXEMPTIONS: tuple[tuple[str, dict[tuple[str, str], object]], ...] = (
    ("OWNED_TRANSACTION_WRITERS", OWNED_TRANSACTION_WRITERS),  # type: ignore[arg-type]
    ("TRANSACTION_AUTHORITY", TRANSACTION_AUTHORITY),  # type: ignore[arg-type]
    ("STANDALONE_DEFAULTS", STANDALONE_DEFAULTS),  # type: ignore[arg-type]
)


def _exempt_keys() -> set[tuple[str, str]]:
    """Every ``(file, qualname)`` excused from the direct-ender sweep."""
    return set(OWNED_TRANSACTION_WRITERS) | set(TRANSACTION_AUTHORITY) | set(STANDALONE_DEFAULTS)


def _exempt_qualnames(filename: str) -> set[str]:
    return {q for f, q in _exempt_keys() if f == filename}


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

    Compound statements are descended through, so a ``def`` under an ``if`` or
    inside a ``try`` is yielded like any other. It is a real place to put a
    commit — ``_own_body`` stops at the nested ``def``, so a version of this
    that stopped at the ``if`` would leave that function checked by nothing at
    all.
    """
    out: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{prefix}{child.name}", child))
                walk(child, f"{prefix}{child.name}.")
            elif not isinstance(child, (ast.Lambda, ast.expr)):
                # A compound statement (if/try/with/for/while) is not a naming
                # scope: a def inside one keeps the enclosing qualname.
                walk(child, prefix)

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

    Four spellings count. The method — matched as an attribute *reference*, not
    only as a call, because a ``db.commit`` handed to ``asyncio.to_thread`` or
    stashed in a callback ends the transaction just the same, and on any
    receiver name so that renaming the local from ``db`` to ``conn`` does not
    walk a writer out of this guard. The SQL — ``db.execute("COMMIT")``, which
    no attribute check would ever see.

    And two that are neither, because sqlite3 ends the transaction for you.
    ``executescript`` issues a COMMIT before it runs whatever it was handed,
    and ``with db:`` commits on a clean exit — both on the shared writer
    connection, both while an owner's work is pending, and neither with the
    word "commit" anywhere in the writer. Measured on this backend's
    connections (created with the module default ``isolation_level``): the
    owner's rows go durable and its later ``rollback()`` is a no-op. That is
    #2162 exactly, in a spelling the sweep could not see. No writer here uses
    either construct today; this keeps it that way.
    """
    found: list[ast.AST] = []
    for node in _own_body(fn):
        if isinstance(node, ast.Attribute) and node.attr in _TRANSACTION_ENDING:
            found.append(node)
        elif isinstance(node, (ast.With, ast.AsyncWith)) and any(
            isinstance(item.context_expr, ast.Name) for item in node.items
        ):
            # ``with db:`` — the connection as its own context manager.
            found.append(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "executescript":
                found.append(node)
            elif (
                node.func.attr == "execute"
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


def _is_self_ownership_read(node: ast.AST) -> bool:
    """Whether ``node`` is exactly ``self._in_transaction`` (or its getattr form).

    Stricter than ``_is_ownership_read`` on two counts, because this one is
    read as a *gate* rather than as evidence that ownership was considered:
    the receiver must be ``self`` — ``other._in_transaction`` answers a
    question about a different object — and ``_require_transaction_idle`` does
    not count, since it is a method that raises, and a bare reference to it is
    always truthy.
    """
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "_in_transaction"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        )
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "self"
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "_in_transaction"
    )


def _ownership_flags(fn: ast.AST) -> set[str]:
    """Local names that provably mean "this writer owns the transaction".

    By definition rather than by name. Only the exact
    ``<flag> = not self._in_transaction`` counts: a variable that merely has
    "transaction" in its name (``transaction_failed``) is not one, neither is a
    compound like ``not (self._in_transaction and x)``, whose truth no longer
    means what the flag is read as meaning, and neither is
    ``not other._in_transaction``, which is a question about a different
    backend.

    Any *other* binding of the same name disqualifies it — ``owns = True``
    later on, an ``owns += 1``, a ``for owns in ...``, a walrus. The flag is
    read as a fact about ownership at the point of the commit, and only a name
    that is never reassigned can carry that fact there.
    """
    flags: set[str] = set()
    canonical: set[int] = set()
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
            and _is_self_ownership_read(value.operand)
        ):
            flags.add(target.id)
            canonical.add(id(target))
    rebound = {
        node.id
        for node in _own_body(fn)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and id(node) not in canonical
    }
    return flags - rebound


def _ungated_transaction_enders(fn: ast.AST) -> list[int]:
    """Line numbers of transaction-ending statements not under an ownership test.

    A ``BORROWS`` writer is safe only while every one of its commits sits
    inside ``if owns_transaction:``. Dropping that ``if`` is a one-line edit
    that reintroduces the bug, and the mere presence of ``_in_transaction``
    somewhere in the function would still read as compliance.
    """
    flags = _ownership_flags(fn)

    # Step 2: only the branch that runs when the flag is TRUE is a gate. An
    # ``else:`` or an ``if not owns_transaction:`` body is where the writer is
    # inside someone else's transaction — committing there is the bug.
    #
    # The ownership test can also be spelled inline, without a flag, which is
    # how the backend's hot write paths are written: there the *inverted*
    # branch is the gate, because ``if not self._in_transaction:`` is true
    # exactly when this writer is standalone. Polarity is the whole check
    # either way, so the two spellings are kept side by side rather than
    # collapsed.
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
        elif (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and _is_self_ownership_read(node.test.operand)
        ):
            branch = node.body
        elif _is_self_ownership_read(node.test):
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

    On ``self``, and on the path. ``other._in_transaction`` asks a different
    object whether *it* is busy, which says nothing about the transaction this
    writer is about to end, and a refusal parked inside ``if strict:`` is not
    reached by the paths that skip that branch. So the refusal must be a
    statement of the function's own body — a sibling of the commit rather than
    something nested in a branch beside it — and it has to come first: a
    refusal below the commit it is supposed to prevent guards nothing.
    """
    enders = _transaction_ending_nodes(fn)
    first_ender = min((node.lineno for node in enders), default=None)

    def in_time(node: ast.AST) -> bool:
        return first_ender is None or node.lineno < first_ender

    # Top level only: ``_own_body`` walks into every branch, and a refusal one
    # branch over does not dominate anything.
    for node in fn.body:  # type: ignore[attr-defined]
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "_require_transaction_idle"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "self"
                and in_time(call)
            ):
                return True
        if not isinstance(node, ast.If) or not in_time(node):
            continue
        # The raise has to be unconditional, in the same sense the swallowing
        # check means it: ``if self._in_transaction: if strict: raise`` refuses
        # on one path and walks into the caller's transaction on the other, and
        # "contains a Raise somewhere" would call that a refusal.
        if _is_self_ownership_read(node.test) and any(
            isinstance(stmt, ast.Raise) for stmt in node.body
        ):
            return True
    return False


# ---- #2167: a failed writer closes its own transaction ----------------------

# The helper whose ``with`` block is the protected region, and the commit
# helper that must sit inside one.
_ROLLBACK_CM = "_rolls_back_if_standalone"
_COMMIT_HELPER = "_commit_if_standalone"


class _Helpers(NamedTuple):
    """The names that count as the ownership helpers in one file."""

    rollback_cms: frozenset[str]
    commit_helpers: frozenset[str]


_DEFAULT_HELPERS = _Helpers(frozenset({_ROLLBACK_CM}), frozenset({_COMMIT_HELPER}))

# ``sqlite_meta.py`` reaches the same two helpers through injected attributes
# (``self._write_guard`` / ``self._commit``, bound in ``MetaManager.__init__``),
# so the contract holds there under different names. The alias is per-file and
# deliberately not global: ``_commit`` is generic enough that accepting it
# everywhere would let any method with that name pass as the sanctioned commit.
_FILE_HELPERS: dict[str, _Helpers] = {
    "sqlite_meta.py": _Helpers(
        frozenset({_ROLLBACK_CM, "_write_guard"}),
        frozenset({_COMMIT_HELPER, "_commit"}),
    ),
}


def _helpers_for(filename: str) -> _Helpers:
    return _FILE_HELPERS.get(filename, _DEFAULT_HELPERS)


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
    ("sqlite_backend.py", "SqliteBackend._reset_unknown_virtual_table"): (
        "Best-effort DROP/DELETE of a vtab whose module is unavailable, inside "
        "reset_all's transaction; it never ends one."
    ),
}

#: Bare method names of the above, for matching a call site.
_PARTICIPANT_HELPER_NAMES = frozenset(q.rsplit(".", 1)[-1] for _, q in _PARTICIPANT_HELPERS)


def _covered_linenos(stmts: list[ast.stmt]) -> set[int]:
    """Line numbers of ``stmts``, stopping at nested ``def`` boundaries.

    The boundary is the same one ``_own_body`` draws: a commit inside a nested
    ``def`` runs after the enclosing region has exited, so the region must not
    vouch for it. ``_qualified_functions`` yields that def separately.
    """
    covered: set[int] = set()
    stack: list[ast.AST] = list(stmts)
    while stack:
        sub = stack.pop()
        if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if hasattr(sub, "lineno"):
            covered.add(sub.lineno)
        stack.extend(ast.iter_child_nodes(sub))
    return covered


def _protected_regions(
    fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS
) -> list[tuple[str, set[int]]]:
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
                and call.func.attr in helpers.rollback_cms
                and _db_argument(call) is not None
            ):
                continue
            region_db = _db_argument(call)
            assert region_db is not None  # narrowed by the check above
            regions.append((region_db, _covered_linenos(node.body)))
    return regions


def _write_nodes(
    fn: ast.AST,
    consts: dict[str, str] | None = None,
    helpers: _Helpers = _DEFAULT_HELPERS,
) -> list[tuple[str, ast.AST]]:
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
        if attr in helpers.commit_helpers | _PARTICIPANT_HELPER_NAMES:
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


def _region_exits(fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS) -> list[int]:
    """``return``/``break``/``continue`` inside a protected region.

    Leaving a region by any of these exits the context manager *normally*: no
    exception, so no rollback, and the commit below never runs either. The
    statements already issued stay pending on the shared connection while the
    caller is told the write succeeded — the #2167 failure wearing a shape this
    guard's containment check would otherwise call compliant.
    """
    regions = _protected_regions(fn, helpers)
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


def _unprotected_writes(
    fn: ast.AST,
    consts: dict[str, str] | None = None,
    helpers: _Helpers = _DEFAULT_HELPERS,
) -> list[int]:
    """Line numbers of writes and commits outside a matching protected region."""
    regions = _protected_regions(fn, helpers)
    out: list[int] = []
    for db_name, node in _write_nodes(fn, consts, helpers):
        if not any(db_name == name and node.lineno in covered for name, covered in regions):
            out.append(node.lineno)
    return sorted(set(out))


def _awaits_inside_regions(fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS) -> list[int]:
    """``await`` line numbers inside a protected region.

    The region is only as task-affine as it is uninterrupted: suspend inside
    one and another task can reach the shared writer connection and commit
    the half-written work this region exists to be able to discard.
    """
    regions = _protected_regions(fn, helpers)
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


def _swallowing_handlers(fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS) -> list[int]:
    """Handlers inside a protected region that can finish without re-raising.

    Catching inside the region and returning normally commits nothing and
    rolls back nothing — the pending statements stay on the connection, and
    the caller is told the write succeeded.

    The raise has to be unconditional. A handler whose ``raise`` sits under an
    ``if`` re-raises on one path and swallows on the other, and "contains a
    Raise somewhere" would call that compliant.
    """
    regions = _protected_regions(fn, helpers)
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


def _commits_standalone(fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr in helpers.commit_helpers
        for node in ast.walk(fn)
    )


def _writes_without_committing(
    fn: ast.AST,
    consts: dict[str, str] | None = None,
    helpers: _Helpers = _DEFAULT_HELPERS,
) -> bool:
    """A participant: it writes, but leaves the transaction to its caller.

    Writing counts transitively — a helper that only calls ``_purge_expired``
    leaves exactly the same DELETE pending as one that spells it out, and
    classifying by visible SQL alone would let the delegating version stay
    unclassified and therefore invisible at its own call sites.
    """
    if _commits_standalone(fn, helpers) or _ends_transaction_directly(fn):
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


def _failure_cleanup_lineno(fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS) -> int | None:
    """Where this function discards its own work on the failure path.

    An owned-transaction writer is not covered by the region containment check
    — it runs its own ``BEGIN`` and ends the transaction itself — so what has
    to be verified instead is that some handler rolls back. Both spellings
    count, the method and the SQL, and it has to be inside an ``except``:
    a rollback on the success path is a different statement entirely.

    It has to be a *call*, and on a connection this writer actually ends a
    transaction on. ``cleanup = db.rollback`` stored for later and
    ``other.rollback()`` both read as cleanup to a check that only looks for
    the name, and neither closes this writer's transaction.

    And the ``try`` it belongs to has to be the one protecting the transaction:
    an unrelated ``try/finally`` further down the function never runs when the
    commit above it raises, so reading any handler anywhere as cleanup lets a
    writer be certified by a block its failure path never reaches.
    """
    # The connection this writer *commits* — deliberately not every ender, or
    # the stray ``other.rollback()`` being judged would enter the set it is
    # judged against and vouch for itself.
    #
    # A commit whose receiver is not a plain name (``self._get_db().commit()``)
    # leaves nothing to match cleanup against. That is not "no constraint": it
    # is a shape this check cannot verify, so it reports no cleanup rather than
    # accepting a rollback on any connection at all.
    commits = [node for node in _transaction_ending_nodes(fn) if _is_commit_ender(node)]
    if any(_ender_receiver(node) is None for node in commits):
        return None
    owned = {name for name in (_ender_receiver(node) for node in commits) if name is not None}
    flags = frozenset(_ownership_flags(fn))
    commit_lines = {node.lineno for node in commits}
    for node in _own_body(fn):
        if not isinstance(node, ast.Try):
            continue
        # The try has to be the one the commit runs inside, or its handlers are
        # not on that commit's failure path at all.
        if not commit_lines & _covered_linenos(node.body):
            continue
        # An ``except`` that rolls back, or a ``finally`` that does — the latter
        # runs on every path out, the failure path included, so it closes the
        # transaction just as surely.
        blocks = [handler.body for handler in node.handlers]
        if node.finalbody:
            blocks.append(node.finalbody)
        for block in blocks:
            if _rolled_back_connections(block, helpers, flags) & owned:
                return node.lineno
    return None


# ---- #2182: the backend's inline-gated, own-BEGIN and private writers -------

#: The plain rollback helper. Not a context manager, so it is not a region —
#: but calling it *is* cleanup, which is what the handler checks look for.
_ROLLBACK_HELPER = "_rollback_if_standalone"


def _ender_receiver(node: ast.AST) -> str | None:
    """The connection name a transaction-ending node acts on, when it is a name."""
    if isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if isinstance(item.context_expr, ast.Name):
                return item.context_expr.id
        return None
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    ):
        return node.func.value.id
    return None


def _is_commit_ender(node: ast.AST) -> bool:
    """Whether a transaction-ending node *commits* rather than rolls back.

    Only commits have to sit inside the protected region: a rollback is the
    cleanup the region exists to perform, and requiring it to be contained
    would reject every handler that does the right thing.
    """
    if isinstance(node, ast.Attribute):
        return node.attr == "commit"
    if isinstance(node, (ast.With, ast.AsyncWith)):
        # ``with db:`` commits on a clean exit (and rolls back on a failing
        # one, but it is the commit that ends someone else's transaction).
        return True
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr == "executescript":
            return True  # issues a COMMIT before running what it was handed
        keyword = _sql_leading_keyword(node.args[0]) if node.args else None
        return keyword in {"COMMIT", "END"}
    return False


def _rolled_back_connections(
    stmts: list[ast.stmt], helpers: _Helpers, flags: frozenset[str] = frozenset()
) -> set[str]:
    """Connection names these statements actually roll back.

    A *call*, not a reference: ``db.rollback`` mentioned but never called is
    the shape a containment check should refuse to read as cleanup. The
    rollback context manager is not cleanup here at all — it rolls back only
    when an exception passes *through* its body, so
    ``with self._rolls_back_if_standalone(db): pass`` inside a handler enters,
    sees nothing raised, and rolls nothing back. In a handler the direct
    ``_rollback_if_standalone`` is the tool; the context manager belongs around
    the writes, where ``_protected_regions`` reads it.

    Only statements that run unconditionally are read. A rollback under an
    ``if cleanup_enabled:`` runs on one path and not the other, and cleanup
    that happens sometimes is what the #2167 stranding looks like. Two
    conditions are not that: the ownership gate, which is the condition the
    whole contract is written in terms of (on the other branch the owner
    decides what the failure means), and ``if <db>.in_transaction:``, which
    only skips a rollback when there is nothing left to roll back — for that
    same ``<db>``, since asking a different connection whether it is busy
    reinstates exactly the skipped-cleanup path.
    """
    names: set[str] = set()

    def scan(block: list[ast.stmt], only: str | None = None) -> None:
        """``only`` restricts what counts to one connection, when a condition
        above has bound the cleanup to it."""

        def take(name: str) -> None:
            if only is None or name == only:
                names.add(name)

        for stmt in block:
            if isinstance(stmt, ast.If):
                body_keys, orelse_keys = _ownership_gate_keys(stmt.test, flags)
                refusal = _pending_transaction_refusal(stmt.test, flags)
                if body_keys:
                    scan(stmt.body, only)
                elif refusal is not None:
                    scan(stmt.body, refusal[0] if only is None else only)
                elif orelse_keys:
                    scan(stmt.orelse, only)
                continue
            if isinstance(stmt, ast.Try):
                # ``try: db.rollback() except: log`` — the preservation shape the
                # helpers use; the rollback itself still runs unconditionally.
                # ``finally`` runs on every path out, so it is cleanup too.
                scan(stmt.body, only)
                scan(stmt.finalbody, only)
                continue
            if isinstance(stmt, (ast.With, ast.AsyncWith)):
                scan(stmt.body, only)
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # The call has to be the statement, not something reachable from
            # inside its expression. ``cleanup_enabled and db.rollback()`` and
            # ``db.rollback() if retry else None`` both run on one path only,
            # and a ``lambda: db.rollback()`` runs on none of them — a walk
            # cannot tell any of those from a rollback that simply happens.
            if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
                continue
            for sub in (stmt.value,):
                if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Attribute):
                    continue
                attr = sub.func.attr
                if attr == "rollback" and isinstance(sub.func.value, ast.Name):
                    take(sub.func.value.id)
                elif attr == _ROLLBACK_HELPER:
                    db_name = _db_argument(sub)
                    if db_name is not None:
                        take(db_name)
                elif (
                    attr in {"execute", "executescript"}
                    and sub.args
                    and _sql_leading_keyword(sub.args[0]) == "ROLLBACK"
                    and isinstance(sub.func.value, ast.Name)
                ):
                    take(sub.func.value.id)

    scan(list(stmts))
    return names


def _rollback_try_regions(
    fn: ast.AST, helpers: _Helpers = _DEFAULT_HELPERS
) -> list[tuple[str, set[int]]]:
    """``(db-name, linenos)`` for every ``try`` whose handlers roll that db back.

    The hand-written equivalent of a ``_rolls_back_if_standalone`` region, and
    checked the same way — by connection name, so a ``try`` that writes ``db``
    and rolls back ``other`` protects nothing, and across *every* handler, so a
    second ``except`` that returns without rolling back cannot leave one path
    stranded while the first path looks compliant.

    The handlers are held to the context manager's other promise too: it
    re-raises. A handler that rolls back and then ``return``s swallows the
    failure and reports success, and it cannot be caught by the swallowing
    check that covers the *body* of a region — the handler is what defines
    this region, so it has to be checked right here.
    """
    regions: list[tuple[str, set[int]]] = []
    flags = frozenset(_ownership_flags(fn))
    for node in _own_body(fn):
        if not isinstance(node, ast.Try) or not node.handlers:
            continue
        cleaned: set[str] | None = None
        for handler in node.handlers:
            if not any(isinstance(stmt, ast.Raise) for stmt in handler.body) or any(
                isinstance(sub, (ast.Return, ast.Break, ast.Continue))
                for stmt in handler.body
                for sub in ast.walk(stmt)
            ):
                # Swallows, or leaves by a normal exit: not a region at all.
                cleaned = set()
                break
            names = _rolled_back_connections(handler.body, helpers, flags)
            cleaned = names if cleaned is None else (cleaned & names)
        if not cleaned:
            continue
        covered = _covered_linenos(node.body)
        for db_name in sorted(cleaned):
            regions.append((db_name, covered))
    return regions


def _gated_writer_violations(
    fn: ast.AST,
    consts: dict[str, str] | None = None,
    helpers: _Helpers = _DEFAULT_HELPERS,
) -> dict[str, list[int]]:
    """What a ``GATED`` writer got wrong, by category.

    Same #2167 contract the region containment check enforces, verified against
    a region the writer spells out itself. The ancillary invariants (no normal
    exit, no ``await``, no swallowing handler) start at the writer's first write
    rather than at the top of the region: everything above that point is still
    reading, and an early ``return`` there strands nothing — which is exactly
    how ``update_chunk_line_ranges`` and ``update_chunk_metadata`` bail out
    when a SELECT shows there is nothing to change.
    """
    regions = _protected_regions(fn, helpers) + _rollback_try_regions(fn, helpers)
    writes = _write_nodes(fn, consts, helpers)
    commits = [
        (_ender_receiver(node), node)
        for node in _transaction_ending_nodes(fn)
        if _is_commit_ender(node)
    ]

    def contained(db_name: str | None, node: ast.AST) -> bool:
        return any(
            db_name == name and getattr(node, "lineno", -1) in covered for name, covered in regions
        )

    unprotected = sorted(
        {node.lineno for db_name, node in writes if not contained(db_name, node)}
        | {node.lineno for db_name, node in commits if not contained(db_name, node)}
    )

    covered_lines: set[int] = set().union(*(lines for _, lines in regions)) if regions else set()
    first_write = min((node.lineno for _, node in writes), default=None)
    live = {line for line in covered_lines if first_write is not None and line >= first_write}

    exits = sorted(
        {
            node.lineno
            for node in _own_body(fn)
            if isinstance(node, (ast.Return, ast.Break, ast.Continue)) and node.lineno in live
        }
    )
    awaits = sorted(
        {
            node.lineno
            for node in _own_body(fn)
            if isinstance(node, ast.Await) and node.lineno in live
        }
    )
    swallows = sorted(
        {
            node.lineno
            for node in _own_body(fn)
            if isinstance(node, ast.ExceptHandler)
            and node.lineno in live
            and not any(isinstance(stmt, ast.Raise) for stmt in node.body)
        }
    )
    return {
        key: value
        for key, value in (
            ("writes or commits outside a rollback-protected region", unprotected),
            ("normal exits from the region after a write", exits),
            ("await inside the region after a write", awaits),
            ("handlers inside the region that never re-raise", swallows),
        )
        if value
    }


def _child_blocks(stmt: ast.AST) -> list[list[ast.stmt]]:
    """The statement lists nested inside a compound statement."""
    blocks: list[list[ast.stmt]] = []
    for field in ("body", "orelse", "finalbody"):
        block = getattr(stmt, field, None)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
            blocks.append(block)
    for handler in getattr(stmt, "handlers", None) or []:
        blocks.append(handler.body)
    return blocks


def _ownership_gate_keys(test: ast.AST, flags: frozenset[str]) -> tuple[set[str], set[str]]:
    """``(keys in effect in the body, keys in effect in the orelse)`` for an ``if``.

    A key names the ownership predicate a branch runs under: an ownership flag
    by name, or ``"self"`` for the inline ``self._in_transaction`` spelling.
    Both are recorded in the polarity that means *this writer is standalone*,
    which is the only branch where taking a BEGIN is this writer's business.
    """
    if isinstance(test, ast.Name) and test.id in flags:
        return ({test.id}, set())
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        operand = test.operand
        if isinstance(operand, ast.Name) and operand.id in flags:
            return (set(), {operand.id})
        if _is_self_ownership_read(operand):
            return ({"self"}, set())
    if _is_self_ownership_read(test):
        return (set(), {"self"})
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        body: set[str] = set()
        for value in test.values:
            body |= _ownership_gate_keys(value, flags)[0]
        return (body, set())
    return (set(), set())


def _pending_transaction_refusal(
    test: ast.AST, flags: frozenset[str] = frozenset()
) -> tuple[str, frozenset[str]] | None:
    """The connection an ``if`` test asks "is a transaction already open?" about.

    Returns the connection and the ownership keys the refusal runs under — an
    empty set meaning it runs unconditionally.

    ``and``-ed operands count, but only ownership ones, and only in the
    standalone polarity: ``if owns_txn and db.in_transaction:`` asks the same
    question on the only path that takes the BEGIN, while
    ``if fast_path and db.in_transaction:`` narrows the refusal to an unrelated
    condition and ``if self._in_transaction and db.in_transaction:`` narrows it
    to the borrowed path — leaving the standalone one, the path that actually
    runs a BEGIN, refusing nothing. Anything under a ``not`` is not a refusal
    either: ``if not db.in_transaction:`` is the opposite question, and its
    body is where the BEGIN goes.
    """
    if (
        isinstance(test, ast.Attribute)
        and test.attr == "in_transaction"
        and isinstance(test.value, ast.Name)
    ):
        return (test.value.id, frozenset())
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        conns: set[str] = set()
        keys: set[str] = set()
        for value in test.values:
            nested = _pending_transaction_refusal(value, flags)
            if nested is not None:
                conns.add(nested[0])
                keys |= nested[1]
                continue
            narrowing = _ownership_gate_keys(value, flags)[0]
            if not narrowing:
                # Narrowed by something that is not a standalone-path ownership
                # test, so the refusal no longer covers every path to the BEGIN.
                return None
            keys |= narrowing
        if len(conns) != 1:
            # Two connections ``and``-ed together refuse only when *both* have a
            # transaction open, which proves nothing about either one alone.
            return None
        return (conns.pop(), frozenset(keys))
    return None


def _may_bind_begin(fn: ast.AST, name: str) -> bool:
    """Whether any binding of ``name`` in ``fn`` is a ``BEGIN`` statement.

    Read when the resolver cannot name one keyword for the local. "Which
    binding reaches the execute" is a control-flow question this guard does not
    answer, so a local that is a BEGIN on *any* path is treated as one.
    """
    for node in _own_body(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            if _sql_leading_keyword(node.value) == "BEGIN":
                return True
    return False


def _blind_begins(fn: ast.AST, consts: dict[str, str] | None = None) -> list[int]:
    """``BEGIN`` statements with no refusal of an unowned pending transaction.

    Ownership is task-affine (``self._in_transaction``), which says nothing
    about a transaction some other writer left open on the shared connection.
    A ``BEGIN`` there raises "cannot start a transaction within a transaction"
    from outside the writer's own ``try``, stranding it a second time — or,
    worse, is skipped and the writer silently adopts and then commits a
    stranger's transaction (#2167, and the ``reset_all`` case #2182 found).

    The refusal has to *dominate* the ``BEGIN``: a preceding sibling in the
    same block, or in a block enclosing it. A refusal inside some other branch
    is not on the path that reaches the ``BEGIN``, and one below it is too
    late.

    A refusal narrowed by an ownership predicate covers only the BEGINs that
    run under that same predicate, which is why the branch keys are carried
    down rather than collapsed into "a refusal appeared somewhere above".
    """
    out: list[int] = []
    flags = frozenset(_ownership_flags(fn))

    def begins(stmt: ast.stmt) -> list[ast.Call]:
        found: list[ast.Call] = []
        for node in ast.walk(stmt):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "executescript"}
                and node.args
            ):
                continue
            keyword = _sql_leading_keyword(node.args[0])
            if keyword is None and isinstance(node.args[0], ast.Name):
                # Hoisted into a local or a module constant: the same statement,
                # one indirection away from a literal-only check. When the local
                # is bound more than once and the bindings disagree,
                # ``_resolve_local_sql`` answers ``None`` — one of those paths
                # may still run a BEGIN, so the ambiguity is read as one.
                name = node.args[0].id
                keyword = _resolve_local_sql(fn, name) or (consts or {}).get(name)
                if keyword is None and _may_bind_begin(fn, name):
                    keyword = "BEGIN"
            if keyword == "BEGIN":
                found.append(node)
        return found

    def visit(
        stmts: list[ast.stmt],
        refused: dict[str, frozenset[str]],
        active: frozenset[str],
    ) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue  # its own function; checked on its own
            blocks = _child_blocks(stmt)
            if blocks:
                body_keys, orelse_keys = (
                    _ownership_gate_keys(stmt.test, flags)
                    if isinstance(stmt, ast.If)
                    else (set(), set())
                )
                for block in blocks:
                    if isinstance(stmt, ast.If) and block is stmt.body:
                        visit(block, refused, active | frozenset(body_keys))
                    elif isinstance(stmt, ast.If) and block is stmt.orelse:
                        visit(block, refused, active | frozenset(orelse_keys))
                    else:
                        visit(block, refused, active)
            else:
                for node in begins(stmt):
                    conn = _ender_receiver(node)
                    keys = refused.get(conn) if conn is not None else None
                    if keys is None or not keys <= active:
                        out.append(node.lineno)
            if isinstance(stmt, ast.If) and any(isinstance(sub, ast.Raise) for sub in stmt.body):
                refusal = _pending_transaction_refusal(stmt.test, flags)
                if refusal is not None:
                    refused_conn, refused_keys = refusal
                    refused = {**refused, refused_conn: refused_keys}

    visit(list(fn.body), {}, frozenset())  # type: ignore[attr-defined]
    return sorted(set(out))


def _private_connection_violations(
    fn: ast.AST,
    consts: dict[str, str] | None = None,
    helpers: _Helpers = _DEFAULT_HELPERS,
) -> list[int]:
    """Writes or enders in a ``PRIVATE`` writer that touch a connection it did
    not open.

    The exemption's whole premise is that no caller can be inside a transaction
    on this connection, because it did not exist until the function ran. A
    ``sqlite3.connect`` left unused while the commit lands on ``self._get_db()``
    keeps the premise's *shape* and loses its meaning.
    """
    own: set[str] = set()
    canonical: set[int] = set()
    for node in _own_body(fn):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        if not isinstance(target, ast.Name) or not isinstance(value, ast.Call):
            continue
        func = value.func
        # ``sqlite3.connect(...)`` specifically: any method named ``connect``
        # could hand back the shared connection under a private-looking name.
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "connect"
            and isinstance(func.value, ast.Name)
            and func.value.id == "sqlite3"
        ):
            own.add(target.id)
            canonical.add(id(target))
    # Rebinding the name to anything else — ``conn = self._get_db()`` a line
    # later — makes every use below it a use of that other connection.
    own -= {
        node.id
        for node in _own_body(fn)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and id(node) not in canonical
    }
    if not own:
        return [getattr(fn, "lineno", -1)]
    offending = {
        node.lineno for db_name, node in _write_nodes(fn, consts, helpers) if db_name not in own
    }
    offending |= {
        node.lineno for node in _transaction_ending_nodes(fn) if _ender_receiver(node) not in own
    }
    return sorted(offending)


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
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_sql_constants(tree)
        helpers = _helpers_for(path.name)
        exempt = _exempt_qualnames(path.name)
        for qualname, fn in _qualified_functions(tree):
            if qualname in exempt:
                # Owns its BEGIN/commit/rollback outright, or is the machinery
                # itself; checked by the mode-specific tests above, not by
                # region containment.
                continue
            if not _commits_standalone(fn, helpers):
                continue
            lines = _unprotected_writes(fn, consts, helpers)
            if lines:
                found[(path.name, qualname)] = lines
    return found


def _unclassified_write_only_helpers() -> dict[tuple[str, str], str]:
    """Write-only functions not declared in ``_PARTICIPANT_HELPERS``."""
    found: dict[tuple[str, str], str] = {}
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_sql_constants(tree)
        helpers = _helpers_for(path.name)
        exempt = _exempt_qualnames(path.name)
        for qualname, fn in _qualified_functions(tree):
            if qualname in exempt or not _writes_without_committing(fn, consts, helpers):
                continue
            if (path.name, qualname) not in _PARTICIPANT_HELPERS:
                found[(path.name, qualname)] = "writes without committing"
    return found


def _direct_transaction_enders() -> dict[tuple[str, str], ast.AST]:
    """Every scanned function that ends a transaction without the helpers."""
    found: dict[tuple[str, str], ast.AST] = {}
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qualname, fn in _qualified_functions(tree):
            if _ends_transaction_directly(fn):
                found[(path.name, qualname)] = fn
    return found


def _scanned_functions() -> dict[tuple[str, str], tuple[ast.AST, dict[str, str], _Helpers]]:
    """``(file, qualname) -> (node, module SQL constants, helper names)``."""
    found: dict[tuple[str, str], tuple[ast.AST, dict[str, str], _Helpers]] = {}
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_sql_constants(tree)
        helpers = _helpers_for(path.name)
        for qualname, fn in _qualified_functions(tree):
            found[(path.name, qualname)] = (fn, consts, helpers)
    return found


def test_the_guard_scans_the_files_it_thinks_it_does() -> None:
    """A move or rename must fail here rather than scan an empty directory."""
    assert _MIXINS.is_dir()
    names = {p.name for p in _MIXINS.glob("*.py")}
    assert {"sessions.py", "formation.py", "history.py", "relations.py"} <= names
    missing = [path for path in _scanned_files() if not path.is_file()]
    assert not missing, f"scanned files that no longer exist: {missing}"
    scanned = {path.name for path in _scanned_files()}
    assert set(_BACKEND_FILES) <= scanned


class TestEveryMixinWriterCommitsThroughOwnership:
    def test_no_unregistered_direct_commit_or_rollback(self) -> None:
        """The sweep, kept swept.

        A new writer that commits directly is the #2162 bug arriving again,
        and it will not look wrong in review.
        """
        unregistered = sorted(set(_direct_transaction_enders()) - _exempt_keys())
        assert not unregistered, (
            "These functions reach the connection's commit/rollback "
            "directly. A writer that joins a caller's transaction must go "
            "through self._commit_if_standalone(db) / "
            "self._rollback_if_standalone(db) instead, or — if it genuinely "
            "runs its own BEGIN and refuses in-transaction callers — be added "
            f"to OWNED_TRANSACTION_WRITERS with that reason: {unregistered}"
        )

    def test_registry_has_no_stale_entries(self) -> None:
        """An exemption for code that no longer commits directly certifies
        nothing, and would keep covering the next writer to take that name."""
        stale = sorted(_exempt_keys() - set(_direct_transaction_enders()))
        assert not stale, f"registered exemptions no longer commit/rollback directly: {stale}"

    def test_no_key_is_registered_in_two_kinds(self) -> None:
        """One function, one kind. Two entries would mean two premises, and
        only whichever test ran first would be checking anything."""
        seen: dict[tuple[str, str], list[str]] = {}
        for name, registry in _ALL_EXEMPTIONS:
            for key in registry:
                seen.setdefault(key, []).append(name)
        doubled = {key: names for key, names in seen.items() if len(names) > 1}
        assert not doubled, f"exemptions registered under more than one kind: {doubled}"

    def test_every_exemption_states_a_reason(self) -> None:
        """The kind says how it is checked; the reason says why it is allowed.
        An empty one turns the registry back into a list of names."""
        blank = []
        for name, registry in _ALL_EXEMPTIONS:
            for key, value in registry.items():
                reason = value[1] if isinstance(value, tuple) else value
                if not isinstance(reason, str) or not reason.strip():
                    blank.append((name, key))
        assert not blank, f"exemptions with no stated reason: {blank}"

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

    def test_gated_writers_gate_and_contain_every_statement(self) -> None:
        """A ``GATED`` entry writes the helpers out by hand, so both halves of
        what the helpers do are checked here: the ownership gate on each ender,
        and a rollback-carrying region around the writes and the commit."""
        sites = _direct_transaction_enders()
        scanned = _scanned_functions()
        offenders: dict[tuple[str, str], dict[str, list[int]]] = {}
        for key, (mode, _) in OWNED_TRANSACTION_WRITERS.items():
            if mode != GATED or key not in sites:
                continue
            fn, consts, helpers = scanned[key]
            problems = dict(_gated_writer_violations(fn, consts, helpers))
            ungated = _ungated_transaction_enders(fn)
            if ungated:
                problems["enders not gated on self._in_transaction"] = ungated
            if problems:
                offenders[key] = problems
        assert not offenders, (
            f"inline-gated writers that no longer meet the contract they stand in for: {offenders}"
        )

    def test_private_writers_only_touch_the_connection_they_opened(self) -> None:
        """A ``PRIVATE`` entry is safe because its connection is its own. Commit
        the shared one instead and the exemption keeps its shape and loses its
        meaning."""
        scanned = _scanned_functions()
        offenders = {}
        for key, (mode, _) in OWNED_TRANSACTION_WRITERS.items():
            if mode != PRIVATE or key not in scanned:
                continue
            fn, consts, helpers = scanned[key]
            lines = _private_connection_violations(fn, consts, helpers)
            if lines:
                offenders[key] = lines
        assert not offenders, (
            "writers registered as owning a private connection that write or "
            f"commit on one they did not open: {offenders}"
        )

    def test_no_begin_without_refusing_an_unowned_pending_transaction(self) -> None:
        """``self._in_transaction`` is task ownership, not connection state.

        A BEGIN taken without asking whether the connection already has a
        transaction open either raises from outside the writer's own try, or —
        when the BEGIN is skipped instead — adopts a stranger's transaction and
        commits it (#2167, #2182). Backend scope: the mixins' borrow-or-own
        writers gate BEGIN on their ownership flag alone, which is follow-up.
        """
        offenders = {}
        for (filename, qualname), (fn, consts, _) in _scanned_functions().items():
            if filename not in _BACKEND_FILES:
                continue
            lines = _blind_begins(fn, consts)
            if lines:
                offenders[(filename, qualname)] = lines
        assert not offenders, (
            f"BEGIN with no `if <db>.in_transaction: raise` refusal dominating it: {offenders}"
        )

    def test_the_authority_helpers_still_gate_on_ownership(self) -> None:
        """The authority exemption's premise, not just its presence.

        ``_commit_if_standalone`` is allowed a bare ``db.commit()`` *because*
        it only reaches it when this task owns nothing. Drop that ``if`` and
        every participant that routes through it commits inside its caller's
        transaction — with this registry entry still reading as compliant.
        """
        sites = _direct_transaction_enders()
        ungated = {}
        for key in (
            ("sqlite_backend.py", "SqliteBackend._commit_if_standalone"),
            ("sqlite_backend.py", "SqliteBackend._rollback_if_standalone"),
        ):
            assert key in TRANSACTION_AUTHORITY, f"{key} is no longer registered as authority"
            assert key in sites, f"{key} no longer ends a transaction; the helper contract moved"
            lines = _ungated_transaction_enders(sites[key])
            if lines:
                ungated[key] = lines
        assert not ungated, f"the ownership helpers end a transaction unconditionally: {ungated}"

    def test_standalone_defaults_are_never_called_by_a_scanned_writer(self) -> None:
        """The meta defaults are ownership-blind on purpose — they exist for a
        MetaManager with no backend. A writer that calls one by name instead of
        going through its injected helper commits inside a caller's
        transaction, with the exemption covering the callee."""
        default_names = {q.rsplit(".", 1)[-1] for _, q in STANDALONE_DEFAULTS}
        callers = {}
        for key, (fn, _, _) in _scanned_functions().items():
            if key in STANDALONE_DEFAULTS:
                continue
            lines = sorted(
                {
                    node.lineno
                    for node in _own_body(fn)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in default_names
                }
            )
            if lines:
                callers[key] = lines
        assert not callers, f"scanned writers calling sqlite_meta's standalone defaults: {callers}"

    def test_standalone_defaults_live_where_the_registry_says(self) -> None:
        """Module-level in ``sqlite_meta.py``: a method by the same name is a
        different thing, and this entry must not cover it."""
        scanned = _scanned_functions()
        for filename, qualname in STANDALONE_DEFAULTS:
            assert filename == "sqlite_meta.py", (
                f"standalone-default exemptions belong to sqlite_meta.py: {filename}"
            )
            assert "." not in qualname, f"{qualname} is not a module-level function"
            assert (filename, qualname) in scanned, f"{qualname} no longer exists"

    def test_the_backend_injects_its_ownership_aware_meta_helpers(self) -> None:
        """The one thing this guard cannot see from either file alone.

        ``MetaManager``'s writes are compliant only because the backend passes
        its own ``_commit_if_standalone`` / ``_rolls_back_if_standalone`` in.
        Dropping those keywords restores the ownership-blind defaults — every
        AST check here stays green while ``set_meta`` commits inside its
        caller's transaction again (#2175, fix 1).
        """
        source = (_STORAGE / "sqlite_backend.py").read_text(encoding="utf-8")
        constructions: list[tuple[int, set[str]]] = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "MetaManager":
                continue
            # The receiver matters as much as the method: another backend's
            # ``_commit_if_standalone`` consults *its* ownership state, so it
            # would happily commit this backend's owned transaction.
            injected = {
                f"{kw.arg}={kw.value.value.id}.{kw.value.attr}"
                for kw in node.keywords
                if kw.arg in {"commit", "write_guard"}
                and isinstance(kw.value, ast.Attribute)
                and isinstance(kw.value.value, ast.Name)
            }
            if node.args and isinstance(node.args[0], ast.Attribute):
                first = node.args[0]
                if isinstance(first.value, ast.Name):
                    injected.add(f"get_db={first.value.id}.{first.attr}")
            constructions.append((node.lineno, injected))
        assert constructions, "no MetaManager construction found in sqlite_backend.py"
        # Every construction, not the union of them: a second, uninjected
        # MetaManager would otherwise ride on the first one's keywords while
        # committing through sqlite_meta's ownership-blind default.
        expected = {
            "get_db=self._get_db",
            f"commit=self.{_COMMIT_HELPER}",
            f"write_guard=self.{_ROLLBACK_CM}",
        }
        wrong = [lineno for lineno, injected in constructions if injected != expected]
        assert not wrong, (
            "MetaManager built without this backend's own ownership-aware "
            f"triple at sqlite_backend.py lines {wrong}"
        )

    def test_the_meta_manager_still_binds_the_injected_helpers(self) -> None:
        """``sqlite_meta.py``'s helper aliases are matched by attribute name, so
        a rename in ``__init__`` would empty every check in that file."""
        from memtomem.storage.sqlite_meta import MetaManager

        manager = MetaManager(lambda: None)  # type: ignore[arg-type,return-value]
        meta_helpers = _helpers_for("sqlite_meta.py")
        for attr in ("_commit", "_write_guard"):
            assert hasattr(manager, attr), f"MetaManager no longer binds {attr}"
            assert attr in meta_helpers.commit_helpers | meta_helpers.rollback_cms, (
                f"{attr} is bound but no longer recognised as a helper"
            )

    def test_every_registry_entry_declares_a_known_mode(self) -> None:
        """A typo'd mode would silently skip every check above."""
        bad = {
            key: mode
            for key, (mode, _) in OWNED_TRANSACTION_WRITERS.items()
            if mode not in {REFUSES, BORROWS, GATED, PRIVATE}
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
        live = {
            key
            for key, (fn, consts, helpers) in _scanned_functions().items()
            if _writes_without_committing(fn, consts, helpers)
        }
        stale = sorted(set(_PARTICIPANT_HELPERS) - live)
        assert not stale, f"_PARTICIPANT_HELPERS entries that no longer write: {stale}"

    def test_no_region_is_left_by_a_normal_exit(self) -> None:
        """``return`` inside a region leaves it without an exception, so the
        rollback never runs — and neither does the commit below it."""
        offenders = {}
        for key, (fn, _, helpers) in _scanned_functions().items():
            lines = _region_exits(fn, helpers)
            if lines:
                offenders[key] = lines
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
        scanned = _scanned_functions()
        sites = _direct_transaction_enders()
        uncleaned = [
            key
            for key in OWNED_TRANSACTION_WRITERS
            if key in sites and _failure_cleanup_lineno(sites[key], scanned[key][2]) is None
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
        for key, (fn, _, helpers) in _scanned_functions().items():
            lines = _awaits_inside_regions(fn, helpers)
            if lines:
                offenders[key] = lines
        assert not offenders, (
            "`await` inside a rollback-protected region: move it out, or the "
            f"region stops being task-affine: {offenders}"
        )

    def test_no_handler_inside_a_region_swallows_its_failure(self) -> None:
        """Catching inside the region without re-raising skips both the commit
        and the rollback, and reports success over a pending write."""
        offenders = {}
        for key, (fn, _, helpers) in _scanned_functions().items():
            lines = _swallowing_handlers(fn, helpers)
            if lines:
                offenders[key] = lines
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
        # The protecting ``try``, not the handler: what is being certified is
        # that the commit runs inside a block whose failure path cleans up.
        assert _failure_cleanup_lineno(with_cleanup) == 4

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


class TestBackendGuardHelpersRejectFakeCompliance:
    """The #2182 rules' own unit tests.

    Same shape as the two classes above: each case is a way the extended guard
    could be satisfied by something that still ends a transaction it does not
    own, checked against the helper rather than against production code.
    """

    @staticmethod
    def _fn(source: str) -> ast.AST:
        return _qualified_functions(ast.parse(source))[0][1]

    # ---- inline ownership gates ---------------------------------------------

    def test_inline_gate_counts_as_a_gate(self) -> None:
        """The backend's spelling of ``if owns_transaction:``, with no flag."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if not self._in_transaction:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(fn) == []

    def test_inverted_inline_gate_is_not_a_gate(self) -> None:
        """``if self._in_transaction: db.commit()`` commits *because* someone
        else owns the transaction — the bug, wearing the gate's shape."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if self._in_transaction:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(fn) == [4]

    def test_a_gate_on_another_object_is_not_a_gate(self) -> None:
        """``other._in_transaction`` answers a question about another backend."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if not other._in_transaction:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(fn) == [4]

    def test_the_idle_check_is_not_a_boolean_gate(self) -> None:
        """``_require_transaction_idle`` is a method that raises; a reference to
        it is always truthy, so reading it as a gate inverts nothing."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if not self._require_transaction_idle:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(fn) == [4]

    # ---- rollback-carrying try regions --------------------------------------

    _GATED = (
        "async def w(self):\n"
        "    db = self._get_db()\n"
        "    try:\n"
        '        db.execute("DELETE FROM t")\n'
        "        if not self._in_transaction:\n"
        "            db.commit()\n"
        "    except Exception:\n"
        "        if not self._in_transaction:\n"
        "            db.rollback()\n"
        "        raise\n"
    )

    def test_a_rollback_carrying_try_is_a_region(self) -> None:
        assert _gated_writer_violations(self._fn(self._GATED)) == {}

    def test_a_try_whose_handler_does_not_roll_back_is_not_a_region(self) -> None:
        source = self._GATED.replace("            db.rollback()\n", "            pass\n")
        assert _rollback_try_regions(self._fn(source)) == []

    def test_a_handler_rolling_back_another_connection_is_not_a_region(self) -> None:
        """Cleanup on the wrong connection reads as compliance and discards
        someone else's work while leaving this writer's pending."""
        source = self._GATED.replace("db.rollback()", "other.rollback()")
        fn = self._fn(source)
        # The try *is* a region — for ``other``, whose name is carried — so what
        # has to hold is that it vouches for nothing written on ``db``.
        assert [name for name, _ in _rollback_try_regions(fn)] == ["other"]
        assert list(_gated_writer_violations(fn)) == [
            "writes or commits outside a rollback-protected region"
        ]

    def test_a_referenced_but_uncalled_rollback_is_not_cleanup(self) -> None:
        source = self._GATED.replace("db.rollback()", "cleanup = db.rollback")
        assert _rollback_try_regions(self._fn(source)) == []

    def test_every_handler_has_to_clean_up(self) -> None:
        """A second ``except`` that returns without rolling back leaves one
        path stranded while the first path still looks compliant."""
        source = (
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    try:\n"
            '        db.execute("DELETE FROM t")\n'
            "        db.commit()\n"
            "    except ValueError:\n"
            "        return 0\n"
            "    except Exception:\n"
            "        db.rollback()\n"
            "        raise\n"
        )
        assert _rollback_try_regions(self._fn(source)) == []

    def test_a_write_above_the_try_is_not_contained(self) -> None:
        source = self._GATED.replace(
            "    try:\n", '    db.execute("DELETE FROM other")\n    try:\n'
        )
        problems = _gated_writer_violations(self._fn(source))
        assert list(problems) == ["writes or commits outside a rollback-protected region"]

    def test_an_exit_after_the_first_write_is_caught(self) -> None:
        source = self._GATED.replace(
            '        db.execute("DELETE FROM t")\n',
            '        db.execute("DELETE FROM t")\n        if done:\n            return 0\n',
        )
        assert "normal exits from the region after a write" in _gated_writer_violations(
            self._fn(source)
        )

    def test_an_exit_before_the_first_write_is_allowed(self) -> None:
        """The SELECT-then-bail shape: nothing is pending yet, so leaving the
        try normally strands nothing."""
        source = self._GATED.replace(
            "    try:\n",
            "    try:\n"
            '        rows = db.execute("SELECT 1").fetchall()\n'
            "        if not rows:\n"
            "            return 0\n",
        )
        assert _gated_writer_violations(self._fn(source)) == {}

    def test_an_await_after_the_first_write_is_caught(self) -> None:
        source = self._GATED.replace(
            '        db.execute("DELETE FROM t")\n',
            '        db.execute("DELETE FROM t")\n        await flush()\n',
        )
        assert "await inside the region after a write" in _gated_writer_violations(self._fn(source))

    # ---- blind BEGIN --------------------------------------------------------

    def test_a_bare_begin_is_flagged(self) -> None:
        fn = self._fn(
            'async def w(self):\n    db = self._get_db()\n    db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [3]

    def test_a_dominating_refusal_clears_the_begin(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            "    try:\n"
            '        db.execute("BEGIN IMMEDIATE")\n'
            "    except Exception:\n"
            "        raise\n"
        )
        assert _blind_begins(fn) == []

    def test_a_refusal_below_the_begin_is_too_late(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
            "    if db.in_transaction:\n"
            "        raise StorageError('refused')\n"
        )
        assert _blind_begins(fn) == [3]

    def test_a_refusal_on_another_connection_does_not_count(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if other.in_transaction:\n"
            "        raise StorageError('refused')\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [5]

    def test_a_refusal_inside_an_unrelated_branch_does_not_escape_it(self) -> None:
        """It only refuses on the path through that branch; the BEGIN below
        runs on every other one."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if strict:\n"
            "        if db.in_transaction:\n"
            "            raise StorageError('refused')\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [6]

    def test_an_ownership_flag_may_narrow_the_refusal(self) -> None:
        """``if owns_txn and db.in_transaction:`` is the same question asked on
        the only path that takes the BEGIN."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    owns_txn = not self._in_transaction\n"
            "    if owns_txn and db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            "    if owns_txn:\n"
            '        db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == []

    def test_a_non_ownership_condition_may_not_narrow_the_refusal(self) -> None:
        """Anything else leaves a path to the BEGIN with no refusal on it."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if fast_path and db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [5]

    def test_an_inverted_test_is_not_a_refusal(self) -> None:
        """``if not db.in_transaction:`` is where the BEGIN goes, not where the
        refusal goes — a writer that only skips is the reset_all bug (#2182)."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if not db.in_transaction:\n"
            '        db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [4]

    # ---- private connections ------------------------------------------------

    def test_a_private_connection_writer_passes(self) -> None:
        fn = self._fn(
            "def _run():\n"
            "    conn = sqlite3.connect(db_path)\n"
            '    conn.execute("DELETE FROM t")\n'
            "    conn.commit()\n"
        )
        assert _private_connection_violations(fn) == []

    def test_committing_the_shared_connection_is_not_private(self) -> None:
        """The unused ``connect`` keeps the premise's shape and loses it."""
        fn = self._fn(
            "def _run(self):\n"
            "    conn = sqlite3.connect(db_path)\n"
            "    db = self._get_db()\n"
            '    db.execute("DELETE FROM t")\n'
            "    db.commit()\n"
        )
        assert _private_connection_violations(fn) == [4, 5]

    def test_a_writer_that_opens_nothing_is_not_private(self) -> None:
        fn = self._fn("def _run(self, db):\n    db.commit()\n")
        assert _private_connection_violations(fn) == [1]

    # ---- scope and naming ---------------------------------------------------

    def test_a_def_under_a_conditional_is_still_swept(self) -> None:
        """``_own_body`` stops at the nested def, so a discovery pass that
        stopped at the ``if`` would leave it checked by nothing at all."""
        found = dict(
            _qualified_functions(
                ast.parse(
                    "class C:\n"
                    "    def outer(self):\n"
                    "        if flag:\n"
                    "            def later():\n"
                    "                db.commit()\n"
                )
            )
        )
        assert "C.outer.later" in found
        assert _ends_transaction_directly(found["C.outer.later"])

    # ---- shapes a review pass found accepted (round two) --------------------

    def test_a_flag_from_another_object_is_not_an_ownership_flag(self) -> None:
        """``owns = not other._in_transaction`` reads like the real thing and
        answers a question about a different backend."""
        fn = self._fn(
            "async def w(self):\n"
            "    owns = not other._in_transaction\n"
            "    if owns:\n"
            "        db.commit()\n"
        )
        assert _ungated_transaction_enders(fn) == [4]

    def test_a_flag_rebound_by_any_form_stops_counting(self) -> None:
        """Not just ``owns = True``: a walrus, a loop target or an augmented
        assignment leaves the same name meaning something else at the commit."""
        for rebinding in ("    owns += 1\n", "    for owns in things:\n        pass\n"):
            fn = self._fn(
                "async def w(self):\n"
                "    owns = not self._in_transaction\n" + rebinding + "    if owns:\n"
                "        db.commit()\n"
            )
            assert _ungated_transaction_enders(fn), rebinding

    def test_a_handler_that_returns_instead_of_reraising_is_not_a_region(self) -> None:
        """The handler defines the region, so the swallowing check that covers
        a region's *body* can never reach it — it is checked here instead."""
        source = self._GATED.replace("        raise\n", "        return 0\n")
        fn = self._fn(source)
        assert _rollback_try_regions(fn) == []
        assert "writes or commits outside a rollback-protected region" in (
            _gated_writer_violations(fn)
        )

    def test_an_uncalled_rollback_is_not_failure_cleanup(self) -> None:
        """``cleanup = db.rollback`` stashes the method; nothing rolls back."""
        fn = self._fn(
            "async def w(self):\n"
            "    try:\n"
            "        db.commit()\n"
            "    except Exception:\n"
            "        cleanup = db.rollback\n"
            "        raise\n"
        )
        assert _failure_cleanup_lineno(fn) is None

    def test_cleanup_on_another_connection_is_not_failure_cleanup(self) -> None:
        fn = self._fn(
            "async def w(self):\n"
            "    try:\n"
            "        db.commit()\n"
            "    except Exception:\n"
            "        other.rollback()\n"
            "        raise\n"
        )
        assert _failure_cleanup_lineno(fn) is None

    def test_a_refusal_narrowed_to_the_borrowed_path_clears_nothing(self) -> None:
        """``if self._in_transaction and db.in_transaction: raise`` refuses on
        the path that never takes a BEGIN, and the standalone path below it —
        the one that does — refuses nothing."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if self._in_transaction and db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            "    if not self._in_transaction:\n"
            '        db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [6]

    def test_a_narrowed_refusal_covers_only_its_own_predicate(self) -> None:
        """The flag-narrowed refusal clears the BEGIN under that same flag, and
        an ungated BEGIN elsewhere in the function still counts."""
        cleared = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    owns = not self._in_transaction\n"
            "    if owns and db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            "    if owns:\n"
            '        db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(cleared) == []
        elsewhere = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    owns = not self._in_transaction\n"
            "    if owns and db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(elsewhere) == [6]

    def test_a_begin_hoisted_into_a_constant_is_still_a_begin(self) -> None:
        local = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    sql = "BEGIN IMMEDIATE"\n'
            "    db.execute(sql)\n"
        )
        assert _blind_begins(local) == [4]
        module = self._fn("async def w(self):\n    db = self._get_db()\n    db.execute(_BEGIN)\n")
        assert _blind_begins(module, {"_BEGIN": "BEGIN"}) == [3]

    def test_a_private_connection_rebound_to_the_shared_one_is_caught(self) -> None:
        fn = self._fn(
            "def _run(self):\n"
            "    conn = sqlite3.connect(p)\n"
            "    conn = self._get_db()\n"
            '    conn.execute("DELETE FROM t")\n'
            "    conn.commit()\n"
        )
        assert _private_connection_violations(fn) == [1]

    def test_a_connect_that_is_not_sqlite3s_is_not_a_private_connection(self) -> None:
        """Any method named ``connect`` could hand back the shared connection."""
        fn = self._fn(
            "def _run(self):\n"
            "    conn = self._pool.connect()\n"
            '    conn.execute("DELETE FROM t")\n'
            "    conn.commit()\n"
        )
        assert _private_connection_violations(fn) == [1]

    # ---- shapes a review pass found accepted (round three) ------------------

    def test_a_commit_on_an_unresolvable_receiver_admits_no_cleanup(self) -> None:
        """``self._get_db().commit()`` leaves no name for cleanup to be matched
        against. That is a shape this check cannot verify, not a shape with no
        constraint — reading it as the latter let any rollback certify it."""
        fn = self._fn(
            "async def w(self):\n"
            "    try:\n"
            "        self._get_db().commit()\n"
            "    except Exception:\n"
            "        other.rollback()\n"
            "        raise\n"
        )
        assert _failure_cleanup_lineno(fn) is None

    def test_two_connections_anded_together_refuse_neither(self) -> None:
        """``if other.in_transaction and db.in_transaction: raise`` fires only
        when both are busy, which proves nothing about ``db`` alone."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    if other.in_transaction and db.in_transaction:\n"
            "        raise StorageError('refused')\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
        )
        assert _blind_begins(fn) == [5]

    def test_cleanup_under_an_unrelated_condition_does_not_count(self) -> None:
        """Cleanup that happens sometimes is what the stranding looks like."""
        source = self._GATED.replace(
            "        if not self._in_transaction:\n            db.rollback()\n",
            "        if cleanup_enabled:\n            db.rollback()\n",
        )
        assert _rollback_try_regions(self._fn(source)) == []

    def test_cleanup_under_the_ownership_gate_still_counts(self) -> None:
        """The gate the whole contract is written in terms of, in both of its
        spellings — the inline read and a proven ownership flag."""
        assert _rollback_try_regions(self._fn(self._GATED)) != []
        by_flag = (
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    owns = not self._in_transaction\n"
            "    try:\n"
            '        db.execute("DELETE FROM t")\n'
            "        if owns:\n"
            "            db.commit()\n"
            "    except Exception:\n"
            "        if owns:\n"
            "            db.rollback()\n"
            "        raise\n"
        )
        assert _rollback_try_regions(self._fn(by_flag)) != []

    def test_cleanup_skipped_only_when_there_is_nothing_to_roll_back_counts(self) -> None:
        """``if db.in_transaction:`` guards a no-op, not a path."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
            "    try:\n"
            "        db.commit()\n"
            "    except Exception:\n"
            "        if db.in_transaction:\n"
            "            db.rollback()\n"
            "        raise\n"
        )
        assert _failure_cleanup_lineno(fn) == 4

    def test_the_rollback_context_manager_must_be_entered(self) -> None:
        """Called without a ``with``, the factory builds a generator and rolls
        nothing back — while reading exactly like the sanctioned cleanup."""
        source = self._GATED.replace(
            "        if not self._in_transaction:\n            db.rollback()\n",
            "        self._rolls_back_if_standalone(db)\n",
        )
        assert _rollback_try_regions(self._fn(source)) == []

    def test_an_ambiguous_local_begin_is_read_as_a_begin(self) -> None:
        """Which binding reaches the execute is a control-flow question this
        guard does not answer, so a local that is a BEGIN on any path is one."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    sql = "BEGIN IMMEDIATE"\n'
            "    if rewrite:\n"
            "        sql = choose_sql()\n"
            "    db.execute(sql)\n"
        )
        assert _blind_begins(fn) == [6]

    # ---- shapes a review pass found accepted (round four) -------------------

    def test_a_refusal_on_another_object_does_not_refuse(self) -> None:
        """``other._in_transaction`` asks a different object whether *it* is
        busy, which says nothing about the transaction about to be ended."""
        fn = self._fn(
            "async def w(self):\n"
            "    if other._in_transaction:\n"
            "        raise StorageError('refused')\n"
            "    db.commit()\n"
        )
        assert _refuses_outer_transaction(fn) is False

    def test_a_refusal_nested_in_another_branch_does_not_dominate(self) -> None:
        """Parked inside ``if strict:``, it is not reached by the paths that
        skip that branch — and those paths still reach the commit."""
        fn = self._fn(
            "async def w(self):\n"
            "    if strict:\n"
            "        if self._in_transaction:\n"
            "            raise StorageError('refused')\n"
            "    db.commit()\n"
        )
        assert _refuses_outer_transaction(fn) is False

    def test_the_top_level_refusal_shapes_still_pass(self) -> None:
        """Both sanctioned spellings, so the tightening above cannot pass by
        refusing everything."""
        for refusal in (
            '    self._require_transaction_idle("w")\n',
            "    if self._in_transaction:\n        raise StorageError('refused')\n",
        ):
            fn = self._fn("async def w(self):\n" + refusal + "    db.commit()\n")
            assert _refuses_outer_transaction(fn) is True, refusal

    def test_an_entered_context_manager_is_not_handler_cleanup(self) -> None:
        """It rolls back only when an exception passes *through* its body, so
        entering one inside a handler and doing nothing rolls nothing back."""
        source = self._GATED.replace(
            "        if not self._in_transaction:\n            db.rollback()\n",
            "        with self._rolls_back_if_standalone(db):\n            pass\n",
        )
        fn = self._fn(source)
        assert _rollback_try_regions(fn) == []
        assert "writes or commits outside a rollback-protected region" in (
            _gated_writer_violations(fn)
        )

    def test_the_in_transaction_exemption_binds_its_own_connection(self) -> None:
        """``if other.in_transaction: db.rollback()`` skips cleanup whenever the
        other connection is idle — the skipped-cleanup path, reinstated."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
            "    try:\n"
            "        db.commit()\n"
            "    except Exception:\n"
            "        if other.in_transaction:\n"
            "            db.rollback()\n"
            "        raise\n"
        )
        assert _failure_cleanup_lineno(fn) is None

    def test_a_finally_block_is_guaranteed_cleanup(self) -> None:
        """``finally`` runs on every path out, the failure path included."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    try:\n"
            "        db.commit()\n"
            "    finally:\n"
            "        db.rollback()\n"
        )
        assert _failure_cleanup_lineno(fn) == 3

    # ---- shapes a review pass found accepted (round five) -------------------

    def test_executescript_is_a_transaction_ender(self) -> None:
        """sqlite3 issues a COMMIT before running what it was handed, so this
        ends an owner's transaction without the word appearing anywhere."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.executescript("INSERT INTO t VALUES (1);")\n'
        )
        assert _ends_transaction_directly(fn)
        assert _ender_receiver(_transaction_ending_nodes(fn)[0]) == "db"

    def test_the_connection_as_a_context_manager_is_a_transaction_ender(self) -> None:
        """``with db:`` commits on a clean exit — the same #2162 bug, spelled
        as a block rather than a call."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with db:\n"
            '        db.execute("INSERT INTO t VALUES (1)")\n'
        )
        assert _ends_transaction_directly(fn)
        assert _ender_receiver(_transaction_ending_nodes(fn)[0]) == "db"

    def test_the_rollback_helper_region_is_not_read_as_a_bare_with(self) -> None:
        """``with self._rolls_back_if_standalone(db):`` is a call, not a bare
        name, so the sanctioned region is not swept up as an implicit commit."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            "    with self._rolls_back_if_standalone(db):\n"
            '        db.execute("DELETE FROM t")\n'
            "        self._commit_if_standalone(db)\n"
        )
        assert not _ends_transaction_directly(fn)

    def test_a_conditional_raise_inside_the_ownership_branch_is_no_refusal(self) -> None:
        """The inner nesting: refuses on one path, walks into the caller's
        transaction on the other."""
        fn = self._fn(
            "async def w(self):\n"
            "    if self._in_transaction:\n"
            "        if strict:\n"
            "            raise StorageError('refused')\n"
            "    db.commit()\n"
        )
        assert _refuses_outer_transaction(fn) is False

    def test_a_short_circuited_rollback_is_not_cleanup(self) -> None:
        """``cleanup_enabled and db.rollback()`` runs on one path; a lazy
        ``lambda: db.rollback()`` on none. Neither is a rollback that happens."""
        for expression in (
            "cleanup_enabled and db.rollback()",
            "db.rollback() if retry else None",
            "later(lambda: db.rollback())",
        ):
            block = ast.parse(f"def _(self):\n    {expression}\n").body[0].body
            assert _rolled_back_connections(block, _DEFAULT_HELPERS) == set(), expression

    def test_cleanup_must_belong_to_the_try_the_commit_runs_in(self) -> None:
        """An unrelated ``try/finally`` further down never runs when the commit
        above it raises, so it certifies nothing."""
        fn = self._fn(
            "async def w(self):\n"
            "    db = self._get_db()\n"
            '    db.execute("BEGIN IMMEDIATE")\n'
            '    db.execute("INSERT INTO t VALUES (1)")\n'
            "    db.commit()\n"
            "    try:\n"
            "        unrelated()\n"
            "    finally:\n"
            "        db.rollback()\n"
        )
        assert _failure_cleanup_lineno(fn) is None

    def test_the_meta_helper_aliases_are_scoped_to_that_file(self) -> None:
        """``_commit`` is generic enough that a mixin method by that name must
        not read as the sanctioned commit."""
        fn = self._fn("def w(self):\n    db = self._get_db()\n    self._commit(db)\n")
        assert _commits_standalone(fn, _helpers_for("relations.py")) is False
        assert _commits_standalone(fn, _helpers_for("sqlite_meta.py")) is True
