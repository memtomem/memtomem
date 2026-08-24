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
