"""Architectural guard: no supertype handler silently swallows a recovery state.

ADR-0030 §10 gave the gateway two new exceptions —
:class:`memtomem.context._dir_swap.SwapRecoveryError` (an ``OSError`` subclass,
with :class:`~memtomem.context._dir_swap.SwapForeignDestination` under it) and
:class:`memtomem.context.transfer.TransferRecoveryError` (a
``TransferCollisionError`` → ``click.ClickException`` subclass). Introducing a
subclass of a caught exception is a *sweep*, not an addition (the G4 design's
§2.1.2): every ``except`` handler whose caught type is any ANCESTOR of one of
these can intercept a recovery state and demote it to an ordinary collision
(``destination_exists`` / ``target_conflict``), a generic 500, or a bare
traceback. The ancestors are ``OSError`` / ``Exception`` / ``BaseException``
(swap) and ``TransferCollisionError`` / ``click.ClickException`` / ``Exception``
/ ``BaseException`` (transfer), plus the exception-group types that concurrency
can wrap either in.

**Why a guard and not a list.** Rounds 8–10 of the design gate each turned up
another live handler the prose census had missed (MCP transfer, MCP migrate, a
CLI wiki update, a batch ``except OSError``). Deriving scope from an enumeration
and then guarding with that same enumeration certifies itself (#1866). So
discovery measures the tree: every supertype ``except`` handler in scope must be
classified, with a written reason, and anything unclassified fails the build.

**What this guard is — and is not.** It is a regression tripwire in a test file,
the same altitude as ``test_context_c0_prelude_guard``. It is NOT a sound
whole-program static analyzer: bound-method type inference, inter-procedural
parameter-flow summaries, full reaching-definitions, and whole-program
exception-group propagation proofs are out of scope. The compensating rule is
uniform — **every point the analysis cannot resolve fails closed** (an
unresolved caught type is ``<dynamic>``; an unresolved callable in a
recovery-flow position is treated as recovery-capable; an unresolved
dynamic-import target is rejected). A limitation can therefore force a false
POSITIVE — a row a human must classify with a written reason — but not a silent
false negative. The live-surface matrix in
``test_context_swap_prelude_fanout`` and review discipline are the backstop.

Classifications (closed set):

* ``TRANSLATES_RECOVERY`` — the specific ``except SwapRecoveryError`` /
  ``except TransferRecoveryError`` clause itself; it maps the recovery state to
  a pinned boundary literal.
* ``PRECEDED_BY_SPECIFIC`` — a broad handler made safe because a specific clause
  precedes it in the same ``try`` (and, for a plain ``ast.Try``, because a group
  cannot reach it — see :func:`_groups_cannot_propagate`).
* ``RERAISES_RECOVERY`` — the handler re-raises the recovery state, either
  unconditionally or through the pinned ``if not _promote_race_conflict(exc):
  raise`` shape.
* ``UNREACHABLE_RECOVERY`` — no recovery state can reach the guarded region from
  a canonical-skill path; carries machine-checkable evidence.
* ``INTENTIONAL_TERMINAL`` — deliberately terminal, and the surfaced envelope
  preserves the recovery reason (pinned by a live-surface case).
* ``INFRASTRUCTURE`` — inside the recovery machinery itself, where a broad
  ``OSError`` IS the mechanism.

**Why registry rows do not pin a site digest.** The occurrence key means two
same-``(module, qualname, caught)`` siblings could in principle swap rows by
being reordered. That swap cannot go green silently: every classification that
differs between siblings is verified against POSITION or BODY —
``PRECEDED_BY_SPECIFIC`` re-checks what actually precedes, ``RERAISES`` re-checks
the body shape, ``no_recovery_callee``/``handled_upstream`` re-check the guarded
calls, and a ``TRANSLATES`` clause moved below a broad sibling trips the
dead-clause test — while siblings that share one classification (the
``INFRASTRUCTURE`` trios) are interchangeable by definition. Pinning 232 hashes
would re-key on every whitespace edit and buy nothing those checks do not.
"""

from __future__ import annotations

import ast
import builtins
import functools
import hashlib
import pathlib
from dataclasses import dataclass, field

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"

# ── The supertype universe ───────────────────────────────────────────────
#
# A handler intercepts a recovery state when its caught type is the exception
# itself or any ancestor. ``IOError``/``EnvironmentError`` are aliases of
# ``OSError``; the exception-group types are included because concurrent
# execution can wrap a recovery error in a group that a plain ``except
# Exception`` still catches.
_SWAP_ANCESTORS = frozenset(
    {"SwapRecoveryError", "OSError", "IOError", "EnvironmentError", "Exception", "BaseException"}
)
_TRANSFER_ANCESTORS = frozenset(
    {
        "TransferRecoveryError",
        "TransferCollisionError",
        "ClickException",
        "Exception",
        "BaseException",
    }
)
_GROUP_TYPES = frozenset({"ExceptionGroup", "BaseExceptionGroup"})
#: The specific clauses themselves — discovered too, so they anchor the
#: structural precedence check and get their own classification.
_SPECIFIC = frozenset({"SwapRecoveryError", "SwapForeignDestination", "TransferRecoveryError"})

#: Any of these names, as a caught type, makes a handler a site.
_INTERCEPTING = _SWAP_ANCESTORS | _TRANSFER_ANCESTORS | _GROUP_TYPES | _SPECIFIC

#: Sentinel for a caught expression the discovery cannot resolve to a name —
#: always a site, never silently dropped.
_DYNAMIC = "<dynamic>"
#: Sentinel for a bare ``except:``.
_BARE = "<bare>"

#: Builtin exception NAMES that are not intercepting — a caught ``Name`` that is
#: one of these provably cannot catch a recovery exception (``ValueError``,
#: ``KeyError``, ``FileNotFoundError`` — an OSError *subclass*, so a sibling of
#: SwapRecoveryError, never an ancestor). Computed, not hand-listed.
_BUILTIN_EXC = (
    frozenset(
        n
        for n in dir(builtins)
        if isinstance(getattr(builtins, n), type)
        and issubclass(getattr(builtins, n), BaseException)
    )
    - _INTERCEPTING
)

# ── Recovery-capable seed symbols (scope-rot + no_recovery_callee) ────────
#
# A callable is recovery-capable if it can propagate a recovery exception. The
# seeds are the two defining modules' public entry points; propagation grows the
# set (a function that references a recovery-capable symbol becomes one).
_RECOVERY_SEEDS = frozenset(
    {
        "SwapRecoveryError",
        "SwapForeignDestination",
        "TransferRecoveryError",
        "run_swap_prelude",
        "recover_pending_swaps",
        "_recover_and_reap_internal_dirs",
        "transfer_artifact",
    }
)
#: The dotted module paths whose dynamic import/access is rejected fail-closed.
_RECOVERY_MODULES = frozenset(
    {
        "memtomem.context._dir_swap",
        "memtomem.context.transfer",
    }
)

#: The RAW propagators for scope-rot: importing one of these means a module can
#: receive a raised recovery exception. The seeds plus the two public wrappers
#: that re-raise rather than convert (``copy_skill`` re-raises the prelude's
#: SwapRecoveryError; ``migrate_scope`` calls transfer_artifact). Deliberately
#: NOT the batch/tool functions (generate_all_skills, extract_skills_to_canonical,
#: the mem_context_* tools), which result-code recovery into a typed skip / string
#: and never raise it to a caller — importing those is safe.
_SCOPE_ROT_SYMBOLS = _RECOVERY_SEEDS | {"copy_skill", "migrate_scope"}

# ── Scope (Option B, user-confirmed) ─────────────────────────────────────
#
# context/ + the context web routes + server/tools/context.py + the two CLI
# entry points that orchestrate canonical-skill writes. A scope-rot self-test
# (below) proves nothing recovery-capable lives outside it.
_CONTEXT_WEB_ROUTES = frozenset(
    {
        "context_agents.py",
        "context_commands.py",
        "context_gateway.py",
        "context_mcp_servers.py",
        "context_mutations.py",
        "context_projects.py",
        "context_skills.py",
        "context_sync_all.py",
        "context_transfer.py",
        "context_versions.py",
        "_atomic_kind.py",
        "_sync_phase.py",
        "_wiki_common.py",
        "wiki.py",
        "wiki_mutations.py",
        "settings_sync.py",
    }
)


def _in_scope(rel: str) -> bool:
    parts = rel.split("/")
    if parts[0] == "context":
        return True
    if rel == "server/tools/context.py":
        return True
    if rel in ("cli/context_cmd.py", "cli/sync_doctor_cmd.py"):
        return True
    if parts[:2] == ["web", "routes"] and parts[-1] in _CONTEXT_WEB_ROUTES:
        return True
    return False


def _scan_files() -> list[pathlib.Path]:
    """Every production module in the package (the scope-rot pass needs all of them)."""
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


# ── qualname mapping (identical to the C0 guard) ─────────────────────────
def _qualname_index(tree: ast.AST) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                spans.append((child.lineno, getattr(child, "end_lineno", child.lineno), name))
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return spans


def _qualname_for(spans: list[tuple[int, int, str]], lineno: int) -> str:
    best = "<module>"
    best_start = -1
    for start, end, name in spans:
        if start <= lineno <= end and start > best_start:
            best, best_start = name, start
    return best


# ── Caught-type resolution, with a poisoning alias fixed point ────────────
def _module_exception_aliases(tree: ast.AST) -> dict[str, str | None]:
    """``local name → canonical exception name`` for module-level bindings.

    Covers ``import X as Y``, ``from m import ClickException`` and assignment
    aliases (``E = OSError``; ``RECOVERY = (OSError, ClickException)`` records
    the tuple's members). A name bound more than once, or bound conditionally /
    with ``del`` / to something unresolvable, is POISONED to ``None`` (which the
    caller renders as :data:`_DYNAMIC`) rather than trusting the last write —
    fail-closed, per the module's stated boundary.
    """
    resolved: dict[str, str | None] = {}
    poisoned: set[str] = set()

    def bind(name: str, value: str | None) -> None:
        if name in poisoned:
            return
        if name in resolved and resolved[name] != value:
            poisoned.add(name)
            resolved[name] = None
            return
        resolved[name] = value

    def canonical_of(node: ast.expr) -> str | None:
        # A Name/Attribute that already denotes an intercepting exception, or a
        # previously-bound alias of one.
        if isinstance(node, ast.Name):
            if node.id in _INTERCEPTING:
                return node.id
            return resolved.get(node.id)
        if isinstance(node, ast.Attribute):
            if node.attr in _INTERCEPTING:
                return node.attr
        return None

    # Top-level statements only for assignment aliases; imports may nest but the
    # tree does not alias exceptions inside functions today, and a nested alias
    # that we miss surfaces as an unresolved Name → _DYNAMIC downstream.
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                original = alias.name.rsplit(".", 1)[-1]
                local = alias.asname or original
                if original in _INTERCEPTING:
                    bind(local, original)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if value is None:  # a bare annotation ``E: type`` binds nothing
                continue
            member = canonical_of(value)
            is_tuple = isinstance(value, ast.Tuple)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id in _TUPLE_ALIASES.get(tree, {}):
                    # ANY rebind of a recorded tuple alias: EVICT the tuple and
                    # poison the name. :func:`_caught_names` consults the tuple
                    # map before the alias map, so a surviving stale entry would
                    # outrank the poison and misdescribe the runtime value
                    # (review follow-up F3) — fail closed to <dynamic> instead.
                    del _TUPLE_ALIASES[tree][target.id]
                    poisoned.add(target.id)
                    resolved[target.id] = None
                elif is_tuple:
                    members = [canonical_of(elt) for elt in value.elts]  # type: ignore[union-attr]
                    if any(m is not None for m in members) and len(targets) == 1:
                        if target.id in resolved:
                            # The symmetric crossing (single alias rebound to a
                            # tuple) poisons the same way.
                            poisoned.add(target.id)
                            resolved[target.id] = None
                        else:
                            _TUPLE_ALIASES.setdefault(tree, {})[target.id] = frozenset(
                                m for m in members if m is not None
                            )
                elif member is not None:
                    bind(target.id, member)
                elif target.id in resolved:
                    # A name that WAS an exception alias is now reassigned to
                    # something the analysis cannot resolve to an intercepting
                    # type — poison it fail-closed rather than trust either write.
                    poisoned.add(target.id)
                    resolved[target.id] = None
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    poisoned.add(target.id)
                    resolved[target.id] = None
                    _TUPLE_ALIASES.get(tree, {}).pop(target.id, None)

    return resolved


#: Per-tree tuple aliases (``RECOVERY = (OSError, ClickException)``), keyed by
#: the tree OBJECT (identity hash) so :func:`_module_exception_aliases` can
#: record them without widening its return type. All per-tree caches key on the
#: tree itself, never ``id(tree)``: an id-keyed entry outlives its GC'd tree and
#: CPython reuses the id, so a later parse could silently inherit another
#: module's maps — through :func:`_import_targets` that becomes a wrong
#: ``_runtime_intercepts`` verdict, i.e. a silently DROPPED site (fail-open,
#: violating this module's contract). The strong reference is deliberate.
_TUPLE_ALIASES: dict[ast.AST, dict[str, frozenset[str]]] = {}


def _caught_names(handler: ast.ExceptHandler, tree: ast.AST) -> tuple[str, ...]:
    """The canonical caught-type names of one ``except`` clause.

    ``None`` → ``(<bare>,)``. A ``Name`` resolves through the alias map (an
    intercepting builtin, an import alias, or an assignment alias / tuple alias);
    an ``Attribute`` takes the dotted chain's final attr (``click.ClickException``
    → ``ClickException``). An IMPORTED name or module attribute is resolved AT
    RUNTIME (this guard is a test — ``importlib`` + ``issubclass`` against the
    real recovery classes beats any static guess; G4a-3c re-review Blocker): a
    value that can catch a recovery exception (``from ext import ERROR_TYPES``
    hiding an ``OSError`` tuple, ``os.error``) is a ``<dynamic>`` site, one that
    provably cannot (``PrivacyScanError``, ``tomllib.TOMLDecodeError``) is
    unrelated. Anything unresolvable — a param, a local, a failed import — is
    :data:`_DYNAMIC`, fail-closed.
    """
    aliases = _alias_cache(tree)
    tuple_aliases = _TUPLE_ALIASES.get(tree, {})
    imports = _import_targets(tree)
    classdefs = _classdef_names(tree)

    def resolved(verdict: bool | None) -> tuple[str, ...]:
        if verdict is False:
            return ()  # provably cannot catch a recovery exception
        return (_DYNAMIC,)  # intercepts (True) or unresolvable (None): a site

    def one(node: ast.expr | None) -> tuple[str, ...]:
        if node is None:
            return (_BARE,)
        if isinstance(node, ast.Name):
            if node.id in _INTERCEPTING:
                return (node.id,)
            if node.id in tuple_aliases:
                return tuple(sorted(tuple_aliases[node.id]))
            if node.id in aliases:
                mapped = aliases[node.id]
                return (mapped,) if mapped is not None else (_DYNAMIC,)
            if node.id in _BUILTIN_EXC:
                return ()  # ValueError, KeyError, FileNotFoundError, … — siblings
            if node.id in classdefs:
                # A class DEFINED in this module is a new type; it cannot appear
                # in the recovery exceptions' MRO, so catching it is safe.
                return ()
            if node.id in imports:
                module_dotted, chain = imports[node.id]
                return resolved(_runtime_intercepts(module_dotted, chain))
            # A parameter, a local, a conditional/unresolvable binding — its
            # runtime type is unknown, so it might be OSError. Fail closed
            # (G4a-3c review Blocker): never silently drop it.
            return (_DYNAMIC,)
        if isinstance(node, ast.Attribute):
            if node.attr in _INTERCEPTING:
                return (node.attr,)
            chain: list[str] = []
            root: ast.expr = node
            while isinstance(root, ast.Attribute):
                chain.append(root.attr)
                root = root.value
            chain.reverse()
            if isinstance(root, ast.Name) and root.id in imports:
                module_dotted, prefix = imports[root.id]
                return resolved(_runtime_intercepts(module_dotted, prefix + tuple(chain)))
            return (_DYNAMIC,)
        if isinstance(node, ast.Tuple):
            out: list[str] = []
            for elt in node.elts:
                out.extend(one(elt))
            return tuple(out)
        return (_DYNAMIC,)

    names = one(handler.type)
    return names


#: Set by :func:`handlers_in_source` so relative imports can be resolved.
#: Tree-object keyed, like every per-tree cache here (see _TUPLE_ALIASES).
_MODULE_REL_BY_TREE: dict[ast.AST, str] = {}
_IMPORTS_BY_TREE: dict[ast.AST, dict[str, tuple[str, tuple[str, ...]]]] = {}
_CLASSDEFS_BY_TREE: dict[ast.AST, frozenset[str]] = {}

_PACKAGE_ROOT = "memtomem"


def _import_targets(tree: ast.AST) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``local name → (module dotted path, attribute chain)`` for every import,
    so a caught name can be resolved to the actual runtime object."""
    cached = _IMPORTS_BY_TREE.get(tree)
    if cached is not None:
        return cached
    rel = _MODULE_REL_BY_TREE.get(tree, "")
    targets: dict[str, tuple[str, tuple[str, ...]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                dotted = alias.name if alias.asname else alias.name.split(".")[0]
                targets[local] = (dotted, ())
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                # Resolve ``from ._dir_swap import X`` against this module's
                # package. A synthetic module has no package → unresolvable,
                # which the runtime check renders <dynamic> (fail-closed).
                if not rel:
                    continue
                pkg_parts = [_PACKAGE_ROOT] + rel.split("/")[:-1]
                if node.level > len(pkg_parts):
                    continue
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                module = ".".join(base + ([module] if module else []))
            for alias in node.names:
                if alias.name == "*":
                    continue
                targets[alias.asname or alias.name] = (module, (alias.name,))
    _IMPORTS_BY_TREE[tree] = targets
    return targets


def _classdef_names(tree: ast.AST) -> frozenset[str]:
    cached = _CLASSDEFS_BY_TREE.get(tree)
    if cached is None:
        cached = frozenset(node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef))
        _CLASSDEFS_BY_TREE[tree] = cached
    return cached


@functools.cache
def _runtime_intercepts(module_dotted: str, attr_chain: tuple[str, ...]) -> bool | None:
    """Whether the object at ``module.attr…`` can catch a recovery exception.

    ``True`` — a recovery exception (or a group wrapping one) is an instance of
    it; ``False`` — provably not (a sibling exception class); ``None`` — the
    import/attribute fails or the value is not an exception type, so nothing is
    proven (fail-closed to ``<dynamic>``).
    """
    import importlib

    from memtomem.context._dir_swap import SwapRecoveryError
    from memtomem.context.transfer import TransferRecoveryError

    try:
        obj: object = importlib.import_module(module_dotted)
        for attr in attr_chain:
            obj = getattr(obj, attr)
    except Exception:
        return None
    members = obj if isinstance(obj, tuple) else (obj,)
    if not members or not all(
        isinstance(m, type) and issubclass(m, BaseException) for m in members
    ):
        return None
    if any(
        issubclass(rec, m)
        for rec in (SwapRecoveryError, TransferRecoveryError)
        for m in members  # type: ignore[union-attr]
    ):
        return True
    # A group type (or alias of one) catches a group-wrapped recovery error.
    return any(issubclass(g, m) for g in (ExceptionGroup, BaseExceptionGroup) for m in members)  # type: ignore[union-attr]


def _alias_cache(tree: ast.AST) -> dict[str, str | None]:
    cached = _ALIAS_BY_TREE.get(tree)
    if cached is None:
        cached = _module_exception_aliases(tree)
        _ALIAS_BY_TREE[tree] = cached
    return cached


_ALIAS_BY_TREE: dict[ast.AST, dict[str, str | None]] = {}


# ── The discovered handler ───────────────────────────────────────────────
@dataclass(frozen=True)
class _Handler:
    """One discovered ``except`` clause that can intercept a recovery state.

    ``key`` is ``(module, qualname, caught, occurrence)`` — no line number
    (churns on unrelated edits), and the caught-type tuple is in the key so
    narrowing/widening a clause re-keys the row and forces re-review.
    """

    module: str
    qualname: str
    caught: tuple[str, ...]
    occurrence: int
    lineno: int
    node: ast.ExceptHandler = field(compare=False, repr=False)
    try_node: ast.Try | object = field(compare=False, repr=False)
    preceded_by: frozenset[str] = frozenset()
    followed_by_specific: bool = False
    site_digest: str = ""

    @property
    def key(self) -> tuple[str, str, tuple[str, ...], int]:
        return (self.module, self.qualname, self.caught, self.occurrence)


def _is_site(caught: tuple[str, ...]) -> bool:
    return any(c in _INTERCEPTING or c in (_DYNAMIC, _BARE) for c in caught)


def handlers_in_source(source: str, module: str) -> tuple[list[_Handler], ast.AST]:
    """Discover intercepting handlers in one module's *source*.

    Split out from the package walk so the guard's own detection can be run on
    synthetic modules (``feedback_pin_test_mutation_validation``).
    """
    tree = ast.parse(source)
    _MODULE_REL_BY_TREE[tree] = module  # for relative-import resolution
    _alias_cache(tree)  # prime alias + tuple-alias tables for this tree
    spans = _qualname_index(tree)
    parents = _parent_map(tree)

    # Collect first, THEN assign occurrence in SOURCE order — ``ast.walk`` is
    # breadth-first, so numbering during the walk would make the registry key
    # depend on traversal order rather than on where the handler sits (the C0
    # guard sorts by lineno for the same reason).
    raw: list[dict[str, object]] = []
    for try_node in ast.walk(tree):
        if not (isinstance(try_node, ast.Try) or (_TRYSTAR and isinstance(try_node, _TRYSTAR))):
            continue
        seen_specific: set[str] = set()
        handler_caught = [
            (h, _caught_names(h, tree))
            for h in try_node.handlers  # type: ignore[union-attr]
        ]
        for idx, (handler, caught) in enumerate(handler_caught):
            if not _is_site(caught):
                # No _SPECIFIC bookkeeping here: _SPECIFIC ⊆ _INTERCEPTING, so
                # a specific clause is always a site and takes the other path.
                continue
            later_specific = any(
                c in _SPECIFIC for _h, later in handler_caught[idx + 1 :] for c in later
            )
            raw.append(
                {
                    "handler": handler,
                    "caught": caught,
                    "lineno": handler.lineno,
                    "try_node": try_node,
                    "preceded_by": frozenset(seen_specific),
                    "followed_by_specific": later_specific,
                }
            )
            seen_specific |= {c for c in caught if c in _SPECIFIC}

    hits: list[_Handler] = []
    counters: dict[tuple[str, str, tuple[str, ...]], int] = {}
    for item in sorted(raw, key=lambda r: r["lineno"]):  # type: ignore[arg-type,return-value]
        handler = item["handler"]  # type: ignore[assignment]
        caught = item["caught"]  # type: ignore[assignment]
        lineno = item["lineno"]  # type: ignore[assignment]
        try_node = item["try_node"]  # type: ignore[assignment]
        qual = _qualname_for(spans, lineno)  # type: ignore[arg-type]
        ckey = (module, qual, caught)  # type: ignore[assignment]
        occ = counters.get(ckey, 0)
        counters[ckey] = occ + 1
        hits.append(
            _Handler(
                module=module,
                qualname=qual,
                caught=caught,  # type: ignore[arg-type]
                occurrence=occ,
                lineno=lineno,  # type: ignore[arg-type]
                node=handler,  # type: ignore[arg-type]
                try_node=try_node,  # type: ignore[arg-type]
                preceded_by=item["preceded_by"],  # type: ignore[arg-type]
                followed_by_specific=item["followed_by_specific"],  # type: ignore[arg-type]
                site_digest=_site_digest(  # type: ignore[arg-type]
                    try_node,
                    handler,
                    caught,
                    parents,
                    tree,  # type: ignore[arg-type]
                ),
            )
        )
    return hits, tree


# ``ast.TryStar`` exists on 3.11+; the repo targets py312 but keep the name lookup
# defensive so the module imports under any 3.12 build.
_TRYSTAR = getattr(ast, "TryStar", None)


def _parent_map(tree: ast.AST) -> dict[int, tuple[ast.AST, str, int]]:
    """``id(child) → (parent, field, index)`` for the root-to-site field path."""
    parents: dict[int, tuple[ast.AST, str, int]] = {}
    for node in ast.walk(tree):
        for fname, value in ast.iter_fields(node):
            if isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, ast.AST):
                        parents[id(item)] = (node, fname, i)
            elif isinstance(value, ast.AST):
                parents[id(value)] = (node, fname, -1)
    return parents


def _site_digest(
    try_node: ast.AST,
    handler: ast.ExceptHandler,
    caught: tuple[str, ...],
    parents: dict[int, tuple[ast.AST, str, int]],
    tree: ast.AST,
) -> str:
    """A location-free identity for a HANDLER site.

    Parts, so two structurally identical handler sites in different reachability
    contexts are distinct rows (round-5 gate): the normalized try subtree, WHICH
    handler this is (its caught tuple + index among the try's handlers — so two
    clauses of one try are separate sites), the complete root-to-site field path
    (``body``/``orelse``/handler index/etc. plus each enclosing statement's kind
    and salient test), and a fingerprint of the names the try references.
    ``ast.dump`` with ``include_attributes=False`` strips linenos.
    """
    subtree = ast.dump(try_node, include_attributes=False)
    handlers = list(getattr(try_node, "handlers", []))
    idx = next((i for i, h in enumerate(handlers) if h is handler), -1)
    who = f"{'|'.join(caught)}#{idx}"
    path_parts: list[str] = []
    cur: ast.AST = try_node
    while id(cur) in parents:
        parent, fname, index = parents[id(cur)]
        salient = _salient(parent)
        path_parts.append(f"{type(parent).__name__}.{fname}[{index}]{salient}")
        cur = parent
    binding_fp = _binding_fingerprint(try_node, tree)
    blob = subtree + "\n" + who + "\n" + "/".join(reversed(path_parts)) + "\n" + binding_fp
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _salient(node: ast.AST) -> str:
    """A normalized summary of a control node's discriminating fields."""
    if isinstance(node, (ast.If, ast.While)):
        return "(" + ast.dump(node.test, include_attributes=False) + ")"
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return "(" + ast.dump(node.iter, include_attributes=False) + ")"
    if isinstance(node, (ast.With, ast.AsyncWith)):
        return "(" + ";".join(ast.dump(i, include_attributes=False) for i in node.items) + ")"
    if isinstance(node, ast.ExceptHandler):
        return "(" + (ast.dump(node.type, include_attributes=False) if node.type else "") + ")"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return "(" + ast.dump(node.args, include_attributes=False) + ")"
    return ""


def _binding_fingerprint(try_node: ast.AST, tree: ast.AST) -> str:
    """Sorted (name → resolved-category) for every Name the try references."""
    aliases = _alias_cache(tree)
    names = sorted({n.id for n in ast.walk(try_node) if isinstance(n, ast.Name)})
    parts: list[str] = []
    for name in names:
        if name in aliases:
            parts.append(f"{name}=alias:{aliases[name]}")
        elif name in _RECOVERY_SEEDS:
            parts.append(f"{name}=recovery")
        else:
            parts.append(f"{name}=plain")
    return ",".join(parts)


@functools.cache
def _discover() -> tuple[tuple[_Handler, ...], dict[str, ast.AST]]:
    handlers: list[_Handler] = []
    trees: dict[str, ast.AST] = {}
    for path in _scan_files():
        rel = path.relative_to(_SRC).as_posix()
        if not _in_scope(rel):
            continue
        found, tree = handlers_in_source(path.read_text(encoding="utf-8"), rel)
        trees[rel] = tree
        handlers.extend(found)
    return tuple(handlers), trees


def discover_handlers() -> list[_Handler]:
    return list(_discover()[0])


# ── Recovery-capable symbol propagation (scope-rot + no_recovery_callee) ──
#
# A callable is recovery-capable if it can propagate a recovery exception. Seed
# with the two defining modules' primitives, then grow by fixed point: a
# function whose body references a recovery-capable name becomes one. This is
# the propagation the round-4 gate demanded — ``copy_skill`` / ``generate_all_
# skills`` are discovered, never hand-listed (#1866). A try body that references
# NONE of the resulting set cannot receive a recovery exception (``no_recovery_
# callee``); one that DOES either translates it or hands it to a callee that
# converts it to a typed skip (``handled_upstream``).
@functools.cache
def _recovery_capable_names() -> frozenset[str]:
    trees = _discover()[1]
    # Also parse the two defining modules even though they hold no in-scope
    # handlers of their own — their function bodies define the seed graph.
    defining = {}
    for rel in ("context/_dir_swap.py", "context/skills.py", "context/transfer.py"):
        path = _SRC / rel
        if rel not in trees and path.exists():
            defining[rel] = ast.parse(path.read_text(encoding="utf-8"))
    all_trees = {**trees, **defining}

    # (qualname-less) function name → set of names its body references, each
    # canonicalised through the module's import aliases so ``generate_all_skills
    # as gen`` still links the graph (G4a-3c review Major).
    fn_refs: dict[str, set[str]] = {}
    for tree in all_trees.values():
        amap = _import_alias_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                refs: set[str] = set()
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name):
                        refs.add(amap.get(sub.id, sub.id))
                    elif isinstance(sub, ast.Attribute):
                        refs.add(sub.attr)
                fn_refs.setdefault(node.name, set()).update(refs)

    capable = set(_RECOVERY_SEEDS)
    while True:
        grown = set(capable)
        for name, refs in fn_refs.items():
            if refs & capable:
                grown.add(name)
        if grown == capable:
            return frozenset(capable)
        capable = grown


def _try_body_nodes(try_node: ast.AST) -> list[ast.stmt]:
    return list(getattr(try_node, "body", []))


def _refs_in(nodes: list[ast.stmt]) -> set[str]:
    refs: set[str] = set()
    for stmt in nodes:
        for sub in ast.walk(stmt):
            if isinstance(sub, ast.Name):
                refs.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                refs.add(sub.attr)
    return refs


def _refs_in_try(handler: _Handler) -> set[str]:
    """The names a handler's guarded try body references, canonicalised through
    the module's import aliases (so an aliased recovery call still links)."""
    amap = _import_alias_map(
        _discover()[1].get(handler.module, ast.Module(body=[], type_ignores=[]))
    )
    raw = _refs_in(_try_body_nodes(handler.try_node))
    return {amap.get(n, n) for n in raw}


def _try_refs_recovery(try_node: ast.AST) -> bool:
    """Whether the guarded try body references any recovery-capable symbol."""
    return bool(_refs_in(_try_body_nodes(try_node)) & _recovery_capable_names())


# ── Classifications (closed set) ─────────────────────────────────────────
TRANSLATES_RECOVERY = "TRANSLATES_RECOVERY"
PRECEDED_BY_SPECIFIC = "PRECEDED_BY_SPECIFIC"
RERAISES_RECOVERY = "RERAISES_RECOVERY"
UNREACHABLE_RECOVERY = "UNREACHABLE_RECOVERY"
INTENTIONAL_TERMINAL = "INTENTIONAL_TERMINAL"
INFRASTRUCTURE = "INFRASTRUCTURE"

_CLASSIFICATIONS = frozenset(
    {
        TRANSLATES_RECOVERY,
        PRECEDED_BY_SPECIFIC,
        RERAISES_RECOVERY,
        UNREACHABLE_RECOVERY,
        INTENTIONAL_TERMINAL,
        INFRASTRUCTURE,
    }
)

#: ``evidence`` meaning per classification (machine-checked where noted):
#:  * PRECEDED_BY_SPECIFIC → ``"swap"`` / ``"transfer"`` — the specific clause
#:    that must precede in the same try (checked against ``preceded_by``).
#:  * RERAISES_RECOVERY → ``"bare"`` (handler ends in ``raise``) / ``"promote_race"``
#:    (the ``if not _promote_race_conflict(exc): raise`` shape) — checked.
#:  * UNREACHABLE_RECOVERY → ``"no_recovery_callee"`` (the guarded try
#:    references NO recovery-capable symbol by the conservative graph — machine
#:    checked, the strong anti-self-certification direction) / ``"handled_upstream"``
#:    (the try DOES reference a recovery-capable symbol per that graph, but no
#:    catchable recovery exception propagates HERE — the batch callee converts
#:    it to a typed skip / result code, OR the conservative graph over-
#:    approximated a non-skills / read-only path that shares a callee name with
#:    the skills path. The reason is argued in ``why``; the graph cannot settle
#:    it inter-procedurally, per the Non-goals). Both directions are checked: a
#:    no_recovery_callee row whose try DOES reach recovery fails, and a
#:    handled_upstream row whose try reaches nothing fails.
#:  * TRANSLATES_RECOVERY / INFRASTRUCTURE / INTENTIONAL_TERMINAL → ``""``.
_PRECEDED_MODES = frozenset({"swap", "transfer"})
_RERAISE_MODES = frozenset({"bare", "promote_race"})
_UNREACHABLE_MODES = frozenset({"no_recovery_callee", "handled_upstream"})

_Row = tuple[str, str, str]  # (classification, evidence, why)

# Compact aliases for the registry literal below.
_T = TRANSLATES_RECOVERY
_P = PRECEDED_BY_SPECIFIC
_R = RERAISES_RECOVERY
_U = UNREACHABLE_RECOVERY
_I = INFRASTRUCTURE

# Shared reasons (the bulk buckets), so each row states WHY without a novel
# sentence for every read probe.
_RO = "Read-only probe / non-mutating I/O; the guarded call references no "
_RO += "recovery-capable symbol, so no SwapRecoveryError/TransferRecoveryError "
_RO += "can reach it."
_NONSKILL = "Non-skills artifact domain (agents/commands/mcp_servers/versions/"
_NONSKILL += "settings/memory); the guarded call reaches no swap/transfer "
_NONSKILL += "recovery path."
_UPSTREAM = "The guarded batch callee converts an interrupted-swap state to a "
_UPSTREAM += "typed per-item skip / result code before it propagates (ADR-0030 "
_UPSTREAM += "§10), so this broad handler never receives a raised recovery "
_UPSTREAM += "exception."


INTERCEPT_SITES: dict[tuple[str, str, tuple[str, ...], int], _Row] = {
    # ── cli/context_cmd.py ───────────────────────────────────────────────
    ("cli/context_cmd.py", "_read_agent_file", ("OSError",), 0): (_U, "no_recovery_callee", _RO),
    ("cli/context_cmd.py", "_translate_to_click", ("<dynamic>",), 0): (
        _T,
        "",
        "The shared CLI error translator: it catches the caller-supplied "
        "error_types (a param, so <dynamic>) and dispatches on isinstance — a "
        "SwapRecoveryError becomes a one-line ClickException via swap_failure_"
        "text (the install/update swap boundary). Recovery is translated, not "
        "demoted.",
    ),
    ("cli/context_cmd.py", "_run_sync_all_projects", ("ClickException",), 0): (
        _U,
        "no_recovery_callee",
        "Sync-all leg runner: this try does not textually reach a swap/transfer "
        "recovery symbol (the legs' generate_all_skills sits behind "
        "_print_artifact_generate, which converts recovery to typed skips); a "
        "ClickException handler could not catch a SwapRecoveryError anyway.",
    ),
    ("cli/context_cmd.py", "_run_sync_all_projects", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Sync-all leg runner OSError arm: same try; no recovery symbol is "
        "referenced. Sibling batches _run_update_all/_run_install_all carry a "
        "typed SwapRecoveryError arm because THEY call the single-item install "
        "engine directly (which does propagate).",
    ),
    ("cli/context_cmd.py", "version_create_cmd._create_locked", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Guards working_file.read_bytes only; version create has no swap path.",
    ),
    ("cli/context_cmd.py", "version_enable_cmd", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "adopt_flat_to_dir is a plain os.replace; flat layout, never skills swap.",
    ),
    ("cli/context_cmd.py", "update_cmd", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Force-head advisory preflight (wiki head + lockfile read); the real "
        "update runs under a separate translator that carries the swap arm.",
    ),
    ("cli/context_cmd.py", "_run_update_all", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Batch update: typed swap_recovery row via swap_failure_text; precedes "
        "the broad OSError below.",
    ),
    ("cli/context_cmd.py", "_run_update_all", ("OSError",), 0): (
        _P,
        "swap",
        "Broad OSError after the SwapRecoveryError arm in the same try.",
    ),
    ("cli/context_cmd.py", "_run_status_all_projects", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "collect_project_status is read-only; " + _RO,
    ),
    ("cli/context_cmd.py", "_run_install_all", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Batch install: typed swap_recovery row; precedes the broad OSError.",
    ),
    ("cli/context_cmd.py", "_run_install_all", ("OSError",), 0): (
        _P,
        "swap",
        "Broad OSError after the SwapRecoveryError arm in the same try.",
    ),
    ("cli/context_cmd.py", "_is_within", ("OSError",), 0): (_U, "no_recovery_callee", _RO),
    ("cli/context_cmd.py", "_memory_migrate_run", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "Memory (L2) domain — SQLite chunk-scope update; " + _NONSKILL,
    ),
    ("cli/context_cmd.py", "_memory_migrate_run", ("Exception",), 1): (
        _U,
        "no_recovery_callee",
        "Compensating shutil.move revert of the memory migrate; " + _NONSKILL,
    ),
    ("cli/context_cmd.py", "rescan_cmd", ("OSError",), 0): (_U, "no_recovery_callee", _RO),
    ("cli/context_cmd.py", "seed_validation_cmd", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Seeder: translates a wedged swap to a one-line ClickException via swap_failure_text.",
    ),
    # ── cli/sync_doctor_cmd.py (all read-only doctor checks) ─────────────
    ("cli/sync_doctor_cmd.py", "_repo_top_level", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "git rev-parse subprocess probe; " + _RO,
    ),
    ("cli/sync_doctor_cmd.py", "_git_ls_files", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "git ls-files subprocess probe; " + _RO,
    ),
    ("cli/sync_doctor_cmd.py", "check_memory_dirs_under_home", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "expanduser/resolve probe; " + _RO,
    ),
    ("cli/sync_doctor_cmd.py", "check_claude_slug", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "cwd.resolve probe; " + _RO,
    ),
    ("cli/sync_doctor_cmd.py", "check_claude_slug", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "samefile probe; " + _RO,
    ),
    ("cli/sync_doctor_cmd.py", "_apply_memory_dirs_override_no_write", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "config.json read (no-write doctor); " + _RO,
    ),
    # ── context/_atomic.py (atomic-write primitive — INFRASTRUCTURE) ─────
    ("context/_atomic.py", "_file_lock", ("BaseException",), 0): (
        _I,
        "",
        "The lock primitive itself: on any failure it closes the fd and re-raises; "
        "a broad guard IS the mechanism.",
    ),
    ("context/_atomic.py", "async_file_lock", ("BaseException",), 0): (
        _I,
        "",
        "Async twin of the lock primitive; close-and-reraise.",
    ),
    ("context/_atomic.py", "_fsync_fd", ("OSError",), 0): (
        _I,
        "",
        "F_FULLFSYNC fallback inside the atomic-write primitive.",
    ),
    ("context/_atomic.py", "fsync_dir", ("OSError",), 0): (
        _I,
        "",
        "Directory fsync best-effort inside the atomic-write primitive.",
    ),
    ("context/_atomic.py", "fsync_dir", ("OSError",), 1): (
        _I,
        "",
        "Second fsync_dir guard (the os.fsync half).",
    ),
    ("context/_atomic.py", "atomic_write_bytes", ("BaseException",), 0): (
        _I,
        "",
        "THE atomic write: unlink the temp and re-raise on any failure.",
    ),
    ("context/_atomic.py", "link_or_copy_file", ("OSError",), 0): (
        _I,
        "",
        "The hardlink-or-copy primitive (G4b version carry): EXDEV-class errnos "
        "dispatch to the copy2 fallback, everything else re-raises — the errno "
        "dispatch IS the mechanism.",
    ),
    # ── context/_atomic_reverse.py (reverse/diff engine — read guards) ───
    ("context/_atomic_reverse.py", "import_passthrough_runtime", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Reverse-import read of a runtime file → typed PARSE skip; " + _RO,
    ),
    ("context/_atomic_reverse.py", "diff_atomic_artifact", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Diff read of canonical bytes; " + _RO,
    ),
    ("context/_atomic_reverse.py", "diff_atomic_artifact", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Diff read of an override; " + _RO,
    ),
    ("context/_atomic_reverse.py", "diff_atomic_artifact", ("OSError",), 2): (
        _U,
        "no_recovery_callee",
        "Diff read of a runtime target; " + _RO,
    ),
    ("context/_atomic_reverse.py", "diff_atomic_artifact", ("OSError",), 3): (
        _U,
        "no_recovery_callee",
        "Diff read of a runtime target (second site); " + _RO,
    ),
    ("context/_atomic_reverse.py", "diff_atomic_artifact", ("<dynamic>",), 0): (
        _U,
        "no_recovery_callee",
        "adapter.parse_error_type of a canonical parse (parse-error class, "
        "<dynamic> fail-closed), not a recovery type; a read/diff, no swap; " + _RO,
    ),
    # ── context/_canonical_txn.py (canonical txn machinery) ─────────────
    ("context/_canonical_txn.py", "write_canonical_locked", ("OSError",), 0): (
        _I,
        "",
        "Canonical FILE write transaction: a read failure becomes SnapshotError; "
        "no swap path (skills route through pull_apply).",
    ),
    ("context/_canonical_txn.py", "write_canonical_locked", ("OSError",), 1): (
        _I,
        "",
        "The create_version half of the same transaction → SnapshotError.",
    ),
    # ── context/_dir_swap.py (THE swap machinery — all INFRASTRUCTURE) ───
    ("context/_dir_swap.py", "marker_owns_transient", ("OSError",), 0): (
        _I,
        "",
        "Marker ownership probe inside the swap primitive.",
    ),
    ("context/_dir_swap.py", "_read_marker_bytes", ("OSError",), 0): (
        _I,
        "",
        "Marker read that PRODUCES a SwapRecoveryError on failure.",
    ),
    ("context/_dir_swap.py", "has_pending_swap", ("SwapRecoveryError",), 0): (
        _I,
        "",
        "The swap machinery's own read-only pending probe: catches _find_marker's "
        "fail-closed SwapRecoveryError and returns True.",
    ),
    ("context/_dir_swap.py", "has_pending_swap", ("OSError",), 0): (
        _I,
        "",
        "The non-recovery OSError arm of the same probe (unreadable root → False).",
    ),
    ("context/_dir_swap.py", "_barrier_unlink_marker", ("OSError",), 0): (
        _I,
        "",
        "Best-effort marker unlink inside the swap primitive.",
    ),
    ("context/_dir_swap.py", "_unwind_rename2", ("OSError",), 0): (
        _I,
        "",
        "Rename-2 unwind that PRODUCES a SwapRecoveryError retaining state.",
    ),
    ("context/_dir_swap.py", "_rmtree_quietly", ("OSError",), 0): (
        _I,
        "",
        "Best-effort rmtree inside the swap primitive.",
    ),
    ("context/_dir_swap.py", "_require_dir", ("OSError",), 0): (
        _I,
        "",
        "Pre-marker directory type gate (raises ValueError, not a recovery type).",
    ),
    ("context/_dir_swap.py", "swap_dir_tree", ("OSError",), 0): (
        _I,
        "",
        "Marker-write failure in the swap primitive: rmtree staging, re-raise.",
    ),
    ("context/_dir_swap.py", "swap_dir_tree", ("OSError",), 1): (
        _I,
        "",
        "Rename-1 failure: unwind, re-raise.",
    ),
    ("context/_dir_swap.py", "swap_dir_tree", ("OSError",), 2): (
        _I,
        "",
        "Rename-2 failure: unwind (may itself raise SwapRecoveryError), re-raise.",
    ),
    ("context/_dir_swap.py", "_present_dir", ("OSError",), 0): (
        _I,
        "",
        "Presence lstat that PRODUCES a SwapRecoveryError on an unexpected error.",
    ),
    ("context/_dir_swap.py", "_rename_recovery", ("OSError",), 0): (
        _I,
        "",
        "THE recovery rename: raises SwapForeignDestination / SwapRecoveryError.",
    ),
    # ── context/_sync_atomic.py (atomic-file forward sync — read guards) ─
    ("context/_sync_atomic.py", "sync_atomic_artifact", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Atomic FILE sync read → typed PARSE skip (no cross-process lock, no "
        "directory swap); " + _RO,
    ),
    ("context/_sync_atomic.py", "sync_atomic_artifact", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Override read of the same atomic-file sync; " + _RO,
    ),
    ("context/_sync_atomic.py", "sync_atomic_artifact", ("<dynamic>",), 0): (
        _U,
        "no_recovery_callee",
        "adapter.parse_error_type in the atomic-file sync → PARSE skip (parse-"
        "error class, <dynamic> fail-closed), not a recovery type; " + _RO,
    ),
    # ── context/commands.py ──────────────────────────────────────────────
    ("context/commands.py", "extract_commands_to_canonical", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Commands (not skills) reverse import → TOML parse skip; " + _NONSKILL,
    ),
    # ── context/dirty.py ─────────────────────────────────────────────────
    ("context/dirty.py", "is_asset_dirty", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "read_bytes drift probe; " + _RO,
    ),
    ("context/dirty.py", "is_asset_dirty", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Outer walk of the same drift probe; " + _RO,
    ),
    # ── context/error_redact.py ──────────────────────────────────────────
    ("context/error_redact.py", "_strip_project_roots", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "project_root.resolve() for redaction; " + _RO,
    ),
    # ── context/install.py (pre-lock read guards; swap runs under a later
    #    with-block, not these tries) ─────────────────────────────────────
    ("context/install.py", "_reconcile_removed_files", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "sha256 of read_bytes to prove a file untouched; " + _RO,
    ),
    ("context/install.py", "_reconcile_removed_files", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "parent.rmdir prune; " + _RO,
    ),
    ("context/install.py", "_adopt_asset", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "sha256 read for adopt; " + _RO,
    ),
    ("context/install.py", "_adopt_asset", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Adopt walk → AdoptMismatchError; adopt calls no swap; " + _RO,
    ),
    ("context/install.py", "_classify_for_all_update", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Lockfile read for classification; " + _RO,
    ),
    ("context/install.py", "_apply_pinned_install", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Pre-lock hash probe; run_swap_prelude runs under a LATER "
        "canonical_lock_shared_budget block, not this try; " + _RO,
    ),
    # ── context/lockfile.py ──────────────────────────────────────────────
    ("context/lockfile.py", "Lockfile.load", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "lock.json read → corrupt-or-default; " + _RO,
    ),
    # ── context/mcp_servers_copy.py (.mcp.json domain, own copy/stage) ───
    ("context/mcp_servers_copy.py", "_dst_mcp_json_notes", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        ".mcp.json read note; " + _NONSKILL,
    ),
    ("context/mcp_servers_copy.py", "_promote_no_clobber", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "os.link/os.replace of a .mcp.json (raises TransferCollisionError, never "
        "catches a recovery type); " + _NONSKILL,
    ),
    ("context/mcp_servers_copy.py", "copy_mcp_server", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        ".mcp.json read/decode → ClickException; " + _NONSKILL,
    ),
    ("context/mcp_servers_copy.py", "copy_mcp_server", ("BaseException",), 0): (
        _R,
        "bare",
        "Staging rollback for the mcp copy: remove staging and re-raise "
        "(_promote_no_clobber is mcp-local, never transfer_artifact).",
    ),
    # ── context/migrate.py (flat→dir migrate; migrate_scope's transfer is a
    #    different function, not these tries) ─────────────────────────────
    ("context/migrate.py", "migrate_one", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Migrate execute/cleanup (migrate-local os.rename staging, no swap); " + _RO,
    ),
    ("context/migrate.py", "_stage_move", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "os.rename with EXDEV copy fallback; migrate-local, no swap; " + _RO,
    ),
    ("context/migrate.py", "_stage_move", ("BaseException",), 0): (
        _R,
        "bare",
        "Copy-fallback rollback: clean staging and re-raise.",
    ),
    ("context/migrate.py", "_fanout_target_matches", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Override read for fan-out compare; " + _RO,
    ),
    ("context/migrate.py", "_fanout_target_matches", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "_skill_effective_equal compare; " + _RO,
    ),
    ("context/migrate.py", "_fanout_target_matches", ("OSError",), 2): (
        _U,
        "no_recovery_callee",
        "Runtime target read for fan-out compare; " + _RO,
    ),
    ("context/migrate.py", "_backup_fanout_target", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Snapshot copytree/copy2 best-effort; " + _RO,
    ),
    ("context/migrate.py", "_remove_runtime_fanout_for", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Parse canonical for fan-out removal; " + _RO,
    ),
    ("context/migrate.py", "_remove_runtime_fanout_for", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "rmtree/unlink of a fan-out target; " + _RO,
    ),
    # ── context/privacy_scan.py ──────────────────────────────────────────
    ("context/privacy_scan.py", "scan_artifact_tree", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Tree walk → PrivacyScanReadError; the scanner reads, never swaps; " + _RO,
    ),
    ("context/privacy_scan.py", "scan_artifact_tree", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Per-file read → fail-closed PrivacyScanReadError; " + _RO,
    ),
    # ── context/projects.py (read-only project registry / probes) ───────
    ("context/projects.py", "KnownProjectsStore._load_doc_with_report", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "known-projects doc read; " + _RO,
    ),
    ("context/projects.py", "_decode_claude_project_dirname.children", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "scandir child is_dir probe; " + _RO,
    ),
    ("context/projects.py", "_decode_claude_project_dirname.children", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "scandir iteration; " + _RO,
    ),
    ("context/projects.py", "_root_stale", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "marker is_dir probe; " + _RO,
    ),
    ("context/projects.py", "annotate_project_health", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "root.is_dir probe; " + _RO,
    ),
    ("context/projects.py", "discover_project_scopes_with_report._add", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "display.resolve probe; " + _RO,
    ),
    ("context/projects.py", "resolve_project_selector", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "candidate.resolve probe; " + _RO,
    ),
    # ── context/pull_apply.py ────────────────────────────────────────────
    ("context/pull_apply.py", "_commit_atomic", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Flat/atomic commit via write_canonical_locked (skills route to "
        "_commit_skills); write_canonical_locked has no swap path; " + _RO,
    ),
    ("context/pull_apply.py", "_commit_skills", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "payload_digest(iter_skill_payload_files(dst)) — a read of the Store copy; " + _RO,
    ),
    ("context/pull_apply.py", "_commit_skills", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "The G4a-3c fix: _stage_captured_tree raises SwapRecoveryError when a "
        "pending swap claims the staging path; typed swap_recovery_pending, "
        "inserted BEFORE the broad OSError so it is not demoted to write_failed.",
    ),
    ("context/pull_apply.py", "_commit_skills", ("OSError",), 1): (
        _P,
        "swap",
        "Broad OSError after the inserted SwapRecoveryError arm in the same try.",
    ),
    ("context/pull_apply.py", "_commit_skills", ("SwapRecoveryError",), 1): (
        _T,
        "",
        "The outer prelude refusal: _recover_and_reap_internal_dirs → swap_recovery_pending.",
    ),
    ("context/pull_apply.py", "_stage_captured_tree", ("BaseException",), 0): (
        _R,
        "bare",
        "Partial-tree cleanup: rmtree staging and re-raise.",
    ),
    # G4b overwrite path (#1916) — classified on the post-G4b rebase.
    ("context/pull_apply.py", "_overwrite_skill_tree", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Strict read-only preflight of the Store copy (lstat walk + carried-"
        "tree gate) → snapshot_failed; " + _RO,
    ),
    ("context/pull_apply.py", "_overwrite_skill_tree", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "iter_skill_payload_files read of the Store payload → snapshot_failed; " + _RO,
    ),
    ("context/pull_apply.py", "_overwrite_skill_tree", ("OSError",), 2): (
        _U,
        "no_recovery_callee",
        "create_tree_version pre-overwrite snapshot → snapshot_failed (version "
        "store, no swap path; TimeoutError re-raised by the preceding arm); " + _NONSKILL,
    ),
    ("context/pull_apply.py", "_overwrite_skill_tree", ("SwapRecoveryError",), 0): (
        _R,
        "bare",
        "Overwrite swap: discard unowned staging and re-raise — the caller's "
        "swap_recovery_pending arm translates it (G4b).",
    ),
    ("context/pull_apply.py", "_overwrite_skill_tree", ("OSError",), 3): (
        _P,
        "swap",
        "Broad OSError after the SwapRecoveryError arm in the same try → write_failed.",
    ),
    # ── context/pull_preview.py (read-only preview) ─────────────────────
    ("context/pull_preview.py", "_probe_present", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "os.stat presence probe; " + _RO,
    ),
    ("context/pull_preview.py", "_override_warning", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "override read; " + _RO,
    ),
    ("context/pull_preview.py", "_read_store", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "store stat; " + _RO,
    ),
    ("context/pull_preview.py", "_read_store", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "store payload iteration; " + _RO,
    ),
    ("context/pull_preview.py", "_read_store", ("OSError",), 2): (
        _U,
        "no_recovery_callee",
        "store file read; " + _RO,
    ),
    ("context/pull_preview.py", "_collect", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "_read_landing → landing_error candidate; " + _RO,
    ),
    ("context/pull_preview.py", "_drift_row", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "_collect → error drift row; " + _RO,
    ),
    # ── context/runtime_*.py (probes) ────────────────────────────────────
    ("context/runtime_coverage.py", "compute_runtime_coverage", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "probe_all_runtimes coverage; " + _RO,
    ),
    ("context/runtime_registry.py", "_probe_location", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "runtime config probe (toml/json load); " + _RO,
    ),
    # ── context/settings*.py (read/resolve) ─────────────────────────────
    ("context/settings.py", "_is_under_project_root", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "resolve containment check; " + _RO,
    ),
    ("context/settings.py", "_safe_load_json", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "settings json read; " + _NONSKILL,
    ),
    ("context/settings.py", "_read_settings_target", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "settings target read; " + _NONSKILL,
    ),
    ("context/settings_doctor.py", "_load_settings_dict", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "settings dict read; " + _NONSKILL,
    ),
    ("context/settings_doctor.py", "_resolved", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "path resolve; " + _RO,
    ),
    ("context/settings_migrate.py", "_safe_load_json_dict", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "settings json read; " + _NONSKILL,
    ),
    ("context/settings_migrate.py", "plan_migration", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "settings resolve; " + _NONSKILL,
    ),
    # ── context/skills.py ────────────────────────────────────────────────
    ("context/skills.py", "_stage_skill", ("BaseException",), 0): (
        _R,
        "bare",
        "Staging rollback: rmtree and re-raise (its own SwapRecoveryError raise "
        "is before this try).",
    ),
    ("context/skills.py", "_remove_internal_artifact", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "lstat dispatch of an internal dir; " + _RO,
    ),
    ("context/skills.py", "_remove_internal_artifact", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "unlink of an internal artifact (best-effort); " + _RO,
    ),
    ("context/skills.py", "_canonical_is_present", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "dst.lstat presence probe; " + _RO,
    ),
    ("context/skills.py", "_reap_move_aside", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Best-effort reap of .old-* trees (swallowed OSError); references only "
        "the internal-dir enumerator, no swap primitive; " + _RO,
    ),
    ("context/skills.py", "_promote_staging", ("BaseException",), 0): (
        _R,
        "bare",
        "Promote rollback (replace_existing=True): restore the parked tree and re-raise.",
    ),
    ("context/skills.py", "_promote_staging", ("BaseException",), 1): (
        _R,
        "bare",
        "Rollback-of-rollback: raise the ORIGINAL promote_exc from rollback_exc, "
        "preserving its type (and __cause__ for _promote_race_conflict).",
    ),
    ("context/skills.py", "copy_skill", ("BaseException",), 0): (
        _R,
        "bare",
        "Public copy rollback: rmtree and re-raise (prelude/_stage_skill raises "
        "are before this try).",
    ),
    ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "project_shared push: _recover_and_reap prelude → typed SWAP_RECOVERY_"
        "PENDING skip + blocked_dsts.",
    ),
    ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 1): (
        _T,
        "",
        "project_shared stage: _stage_skill → SWAP_RECOVERY_PENDING skip.",
    ),
    ("context/skills.py", "generate_all_skills", ("OSError",), 0): (
        _P,
        "swap",
        "Broad OSError after the stage-phase SwapRecoveryError arm.",
    ),
    ("context/skills.py", "generate_all_skills", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Override read in the stage loop → PARSE skip; " + _RO,
    ),
    ("context/skills.py", "generate_all_skills", ("OSError",), 2): (
        _R,
        "promote_race",
        "Promote loop: `if not _promote_race_conflict(exc): raise` re-raises a "
        "SwapRecoveryError (race → False), never demoting it to a skip.",
    ),
    ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 2): (
        _T,
        "",
        "per-destination push: _recover_and_reap prelude → SWAP_RECOVERY_PENDING.",
    ),
    ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 3): (
        _T,
        "",
        "per-destination stage: _stage_skill → SWAP_RECOVERY_PENDING.",
    ),
    ("context/skills.py", "generate_all_skills", ("OSError",), 3): (
        _P,
        "swap",
        "Broad OSError after the per-destination stage SwapRecoveryError arm.",
    ),
    ("context/skills.py", "generate_all_skills", ("OSError",), 4): (
        _U,
        "no_recovery_callee",
        "Override read in the per-destination loop → PARSE skip; " + _RO,
    ),
    ("context/skills.py", "generate_all_skills", ("OSError",), 5): (
        _R,
        "promote_race",
        "per-destination promote loop: same `if not _promote_race_conflict(exc): "
        "raise` re-raise shape.",
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Scan of skill files → unreadable; " + _RO,
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "src file read → unreadable; " + _RO,
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Reverse import prelude: _recover_and_reap → SWAP_RECOVERY_PENDING skip.",
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("SwapRecoveryError",), 1): (
        _T,
        "",
        "Reverse import stage: _stage_skill → SWAP_RECOVERY_PENDING skip.",
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("OSError",), 2): (
        _P,
        "swap",
        "Broad OSError after the reverse-import stage SwapRecoveryError arm.",
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("OSError",), 3): (
        _R,
        "promote_race",
        "Reverse import promote: same `if not _promote_race_conflict(exc): raise`.",
    ),
    ("context/skills.py", "extract_skills_to_canonical", ("BaseException",), 0): (
        _R,
        "bare",
        "Promote cleanup: rmtree staging and re-raise for non-OSError unwinds.",
    ),
    ("context/skills.py", "diff_skills", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Override read for drift; " + _RO,
    ),
    ("context/skills.py", "diff_skills", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "_skill_effective_equal compare for drift; " + _RO,
    ),
    # ── context/status.py (read-only status) ────────────────────────────
    ("context/status.py", "classify_status", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "wiki.current_commit degrade; " + _RO,
    ),
    ("context/status.py", "collect_project_status", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "diff_skills/list read → diff_errors; " + _RO,
    ),
    ("context/status.py", "collect_project_status", ("Exception",), 1): (
        _U,
        "no_recovery_callee",
        "diff_commands read → diff_errors; " + _RO,
    ),
    ("context/status.py", "collect_project_status", ("Exception",), 2): (
        _U,
        "no_recovery_callee",
        "diff_agents read → diff_errors; " + _RO,
    ),
    ("context/status.py", "collect_project_status", ("Exception",), 3): (
        _U,
        "no_recovery_callee",
        "diff_mcp_servers read → diff_errors; " + _RO,
    ),
    ("context/status.py", "collect_project_status", ("Exception",), 4): (
        _U,
        "no_recovery_callee",
        "diff_settings read → diff_errors; " + _RO,
    ),
    # ── context/transfer.py ──────────────────────────────────────────────
    ("context/transfer.py", "_classify_provenance_carry", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "is_asset_dirty provenance probe → skip; " + _RO,
    ),
    ("context/transfer.py", "_carry_provenance", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "installed-files read → not_carried; " + _RO,
    ),
    ("context/transfer.py", "_carry_provenance", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Lockfile.upsert_entry → not_carried; " + _RO,
    ),
    ("context/transfer.py", "_stage_copy", ("BaseException",), 0): (
        _R,
        "bare",
        "Copy staging rollback: remove staging and re-raise.",
    ),
    ("context/transfer.py", "transfer_artifact", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "The prelude's SwapRecoveryError is translated to TransferRecoveryError "
        "(the transfer-family recovery type) via swap_failure_text.",
    ),
    ("context/transfer.py", "transfer_artifact", ("BaseException",), 0): (
        _R,
        "bare",
        "Copy-branch rollback: remove staging and re-raise.",
    ),
    ("context/transfer.py", "transfer_artifact", ("BaseException",), 1): (
        _R,
        "bare",
        "Move-branch rollback: os.replace back, then re-raise.",
    ),
    ("context/transfer.py", "transfer_artifact", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Nested rollback os.replace(staging, src) — logs, preserves staging, the "
        "outer handler re-raises; " + _RO,
    ),
    ("context/transfer.py", "transfer_artifact", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "EXDEV source cleanup → MigratePartialError translation; " + _RO,
    ),
    ("context/transfer.py", "transfer_artifact", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "Post-move Lockfile.remove_entry cleanup (swallowed); " + _RO,
    ),
    # ── context/versioning.py (version store, no swap) ──────────────────
    ("context/versioning.py", "load_manifest", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "manifest read → VersionError/degrade; " + _NONSKILL,
    ),
    ("context/versioning.py", "create_version", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "version store write; no swap; " + _NONSKILL,
    ),
    ("context/versioning.py", "_refuse_case_colliding_store", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "case-collision dir listing probe; " + _RO,
    ),
    ("context/versioning.py", "_refuse_case_colliding_store", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "second case-collision probe; " + _RO,
    ),
    ("context/versioning.py", "create_tree_version", ("BaseException",), 0): (
        _R,
        "bare",
        "Version-store staging rollback: rmtree and re-raise (its own "
        "write_tree_payload/rename, not _promote_staging).",
    ),
    ("context/versioning.py", "_verify_manifest_spelling._entries", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "artifact_dir.iterdir → VersionError; " + _RO,
    ),
    ("context/versioning.py", "_verify_manifest_spelling", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "spelling os.replace → VersionError (version store, not swap); " + _NONSKILL,
    ),
    # ── server/tools/context.py ─────────────────────────────────────────
    ("server/tools/context.py", "mem_context_init", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "init skills block: extract_skills_to_canonical converts recovery to "
        "typed skips; the ClickException arm catches only the privacy block. " + _UPSTREAM,
    ),
    ("server/tools/context.py", "mem_context_init", ("ClickException",), 1): (
        _U,
        "no_recovery_callee",
        "init agents block: extract_agents_to_canonical is not a swap path; " + _NONSKILL,
    ),
    ("server/tools/context.py", "mem_context_init", ("ClickException",), 2): (
        _U,
        "no_recovery_callee",
        "init commands block: extract_commands_to_canonical is not a swap path; " + _NONSKILL,
    ),
    ("server/tools/context.py", "mem_context_memory_migrate", ("ClickException",), 0): (
        _U,
        "no_recovery_callee",
        "memory migrate source resolve; " + _NONSKILL,
    ),
    ("server/tools/context.py", "mem_context_memory_migrate", ("ClickException",), 1): (
        _U,
        "no_recovery_callee",
        "memory migrate run (shutil.move + DB); " + _NONSKILL,
    ),
    ("server/tools/context.py", "mem_context_artifact_migrate", ("TransferRecoveryError",), 0): (
        _T,
        "",
        "MCP migrate: migrate_scope → transfer engine → refused: "
        "swap_recovery_pending (redacted). Precedes the ClickException arm.",
    ),
    ("server/tools/context.py", "mem_context_artifact_migrate", ("ClickException",), 0): (
        _P,
        "transfer",
        "Broad ClickException after the TransferRecoveryError arm.",
    ),
    ("server/tools/context.py", "mem_context_artifact_transfer", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "Early tier-detect try: the conservative graph reaches a transfer symbol "
        "here, but _detect_source_scope is a read-only probe that raises no "
        "TransferRecoveryError; the real transfer runs in the later try whose "
        "TransferRecoveryError arm is classified TRANSLATES.",
    ),
    ("server/tools/context.py", "mem_context_artifact_transfer", ("TransferRecoveryError",), 0): (
        _T,
        "",
        "MCP transfer: transfer_artifact → refused: swap_recovery_pending "
        "(redacted). Precedes the collision/ClickException arms.",
    ),
    ("server/tools/context.py", "mem_context_artifact_transfer", ("TransferCollisionError",), 0): (
        _P,
        "transfer",
        "Collision arm after the TransferRecoveryError arm.",
    ),
    ("server/tools/context.py", "mem_context_artifact_transfer", ("ClickException",), 1): (
        _P,
        "transfer",
        "Broad ClickException catch-all after the TransferRecoveryError arm.",
    ),
    ("server/tools/context.py", "mem_context_version", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "adopt_flat_to_dir (os.replace, flat) — never skills swap; " + _NONSKILL,
    ),
    ("server/tools/context.py", "mem_context_version", ("OSError",), 1): (
        _U,
        "handled_upstream",
        "versioning_op_locked create → create_version has no swap path; the "
        "conservative graph reaches the lock wrapper (shared with skills-capable "
        "callers), but version create over agents/commands raises no "
        "SwapRecoveryError. " + _NONSKILL,
    ),
    ("server/tools/context.py", "mem_context_pull", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Gate-message path formatting (resolve/relative_to); prepare_pull/"
        "commit_pull run outside this try and are result-coded; " + _RO,
    ),
    # ── web/routes/_atomic_kind.py (agents/commands router) ─────────────
    ("web/routes/_atomic_kind.py", "user_sync_host_targets", ("OSError", "<dynamic>"), 0): (
        _U,
        "no_recovery_callee",
        "Host-target frontmatter read; the second caught type is the adapter's "
        "own parse-error class (a param attribute the analysis renders <dynamic> "
        "fail-closed), not a recovery type. " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "read_artifact", ("<dynamic>",), 0): (
        _U,
        "no_recovery_callee",
        "spec.parse_error of a canonical read (adapter parse-error class, "
        "<dynamic> fail-closed); a pure read, no swap. " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "rendered_artifact", ("<dynamic>",), 0): (
        _U,
        "no_recovery_callee",
        "spec.parse_error of a render read; " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "diff_artifact", ("<dynamic>",), 0): (
        _U,
        "no_recovery_callee",
        "spec.parse_error of a diff read; " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "sync_core", ("<dynamic>",), 0): (
        _U,
        "no_recovery_callee",
        "spec.parse_error in the agents/commands sync core (adapter parse-error "
        "class, not a swap path); " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "create_artifact._create_locked", ("BaseException",), 0): (
        _R,
        "bare",
        "Agents/commands create rollback: rmtree and re-raise.",
    ),
    ("web/routes/_atomic_kind.py", "delete_artifact._delete_locked", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Agents/commands unlink → skip; " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "delete_artifact._delete_locked", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Agents/commands cascade unlink → skip; " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "import_artifacts", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "Agents/commands batch import: the conservative graph reaches the shared "
        "extract dispatcher name, but this router targets non-skills kinds whose "
        "extractor has no swap path → privacy 422 only. " + _NONSKILL,
    ),
    ("web/routes/_atomic_kind.py", "import_artifact", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "Agents/commands single import: shares the extract dispatcher name with "
        "the skills path in the conservative graph, but targets non-skills kinds "
        "(no swap). " + _NONSKILL,
    ),
    # ── web/routes/context_gateway.py (all read-only status/overview) ───
    ("web/routes/context_gateway.py", "_safe_rel", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "project_root.resolve; " + _RO,
    ),
    ("web/routes/context_gateway.py", "read_text_lenient", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Diff-preview read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "expected_vs_runtime_row", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Override read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "expected_vs_runtime_row", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Runtime read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "_compute_last_synced_at._bump", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "path.stat mtime; " + _RO,
    ),
    ("web/routes/context_gateway.py", "_compute_last_synced_at", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "list_canonical_skills read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "_compute_last_synced_at", ("Exception",), 1): (
        _U,
        "no_recovery_callee",
        "list_canonical_commands read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "_compute_last_synced_at", ("Exception",), 2): (
        _U,
        "no_recovery_callee",
        "list_canonical_agents read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "_compute_last_synced_at", ("Exception",), 3): (
        _U,
        "no_recovery_callee",
        "list_canonical_mcp_servers read; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "Overview diff_skills summarize; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 1): (
        _U,
        "no_recovery_callee",
        "Overview diff_commands; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 2): (
        _U,
        "no_recovery_callee",
        "Overview diff_agents; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 3): (
        _U,
        "no_recovery_callee",
        "Overview diff_mcp_servers; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 4): (
        _U,
        "no_recovery_callee",
        "Overview diff_settings; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 5): (
        _U,
        "no_recovery_callee",
        "Overview classify_status; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 6): (
        _U,
        "no_recovery_callee",
        "Overview detected-runtimes; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_overview._collect", ("Exception",), 7): (
        _U,
        "no_recovery_callee",
        "Overview last-synced-at; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_runtimes", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "probe_all_runtimes; " + _RO,
    ),
    ("web/routes/context_gateway.py", "context_status_all", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "collect_project_status envelope; " + _RO,
    ),
    # ── web/routes/context_mcp_servers.py ───────────────────────────────
    ("web/routes/context_mcp_servers.py", "delete_mcp_server", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        ".mcp.json unlink → skip; " + _NONSKILL,
    ),
    ("web/routes/context_mcp_servers.py", "diff_mcp_server", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        ".mcp.json diff read; " + _NONSKILL,
    ),
    # ── web/routes/context_mutations.py (wiki install/update) ───────────
    ("web/routes/context_mutations.py", "install_asset", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Wiki install: install engine → run_swap_prelude → typed 409 swap_recovery_pending.",
    ),
    ("web/routes/context_mutations.py", "update_asset", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Wiki update: install engine → run_swap_prelude → typed 409 swap_recovery_pending.",
    ),
    # ── web/routes/context_projects.py (read-only counts) ───────────────
    ("web/routes/context_projects.py", "_counts_for", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "Inventory diff_skills count; " + _RO,
    ),
    ("web/routes/context_projects.py", "_counts_for", ("Exception",), 1): (
        _U,
        "no_recovery_callee",
        "Inventory diff_commands count; " + _RO,
    ),
    ("web/routes/context_projects.py", "_counts_for", ("Exception",), 2): (
        _U,
        "no_recovery_callee",
        "Inventory diff_agents count; " + _RO,
    ),
    ("web/routes/context_projects.py", "_counts_for", ("Exception",), 3): (
        _U,
        "no_recovery_callee",
        "Inventory diff_mcp_servers count; " + _RO,
    ),
    ("web/routes/context_projects.py", "_scope_to_dict", ("Exception",), 0): (
        _U,
        "no_recovery_callee",
        "compute_runtime_coverage probe; " + _RO,
    ),
    # ── web/routes/context_skills.py ────────────────────────────────────
    ("web/routes/context_skills.py", "create_skill._create_locked", ("BaseException",), 0): (
        _R,
        "bare",
        "Create rollback: rmtree the partial canonical and re-raise (the prelude "
        "SwapRecoveryError flows past to the outer arm).",
    ),
    ("web/routes/context_skills.py", "create_skill", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Web create: prelude → typed 409 swap_recovery_pending (redacted reason).",
    ),
    ("web/routes/context_skills.py", "update_skill", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Web update: prelude → typed 409 swap_recovery_pending.",
    ),
    ("web/routes/context_skills.py", "delete_skill._delete_locked", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Delete unlink (prelude runs outside this arm) → skip; " + _RO,
    ),
    ("web/routes/context_skills.py", "delete_skill._delete_locked", ("OSError",), 1): (
        _U,
        "no_recovery_callee",
        "Delete cascade rmtree → skip; " + _RO,
    ),
    ("web/routes/context_skills.py", "delete_skill", ("SwapRecoveryError",), 0): (
        _T,
        "",
        "Web delete: prelude → typed 409 swap_recovery_pending (whole-delete refusal).",
    ),
    ("web/routes/context_skills.py", "import_skills", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "Batch skills import: extract_skills_to_canonical converts recovery to "
        "typed skips; the ClickException arm catches only the privacy block. " + _UPSTREAM,
    ),
    ("web/routes/context_skills.py", "import_skill", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "Single skill import: extract_skills_to_canonical converts recovery to "
        "typed skips. " + _UPSTREAM,
    ),
    ("web/routes/context_skills.py", "import_skill_to_user", ("ClickException",), 0): (
        _U,
        "handled_upstream",
        "Import-to-user: extract_skills_to_canonical converts recovery to typed "
        "skips. " + _UPSTREAM,
    ),
    # ── web/routes/context_sync_all.py ──────────────────────────────────
    ("web/routes/context_sync_all.py", "_run_phase", ("Exception",), 0): (
        _U,
        "handled_upstream",
        "Sync-all phase runner: _sync_skills_core → generate_all_skills converts "
        "recovery to typed skips before it can propagate. " + _UPSTREAM,
    ),
    ("web/routes/context_sync_all.py", "sync_all_projects_context", ("Exception",), 0): (
        _U,
        "handled_upstream",
        "Outer sync-all loop: _run_phase consumes every phase exception "
        "internally, so no recovery state reaches this defensive guard. " + _UPSTREAM,
    ),
    # ── web/routes/context_transfer.py ──────────────────────────────────
    (
        "web/routes/context_transfer.py",
        "transfer_context_artifact",
        ("TransferRecoveryError",),
        0,
    ): (
        _T,
        "",
        "Web transfer: transfer_artifact → 409 swap_recovery_pending (redacted). "
        "Precedes the collision/ClickException/Exception arms.",
    ),
    (
        "web/routes/context_transfer.py",
        "transfer_context_artifact",
        ("TransferCollisionError",),
        0,
    ): (
        _P,
        "transfer",
        "Collision arm after the TransferRecoveryError arm.",
    ),
    ("web/routes/context_transfer.py", "transfer_context_artifact", ("ClickException",), 0): (
        _P,
        "transfer",
        "Validation arm after the TransferRecoveryError arm.",
    ),
    ("web/routes/context_transfer.py", "transfer_context_artifact", ("Exception",), 0): (
        _P,
        "transfer",
        "Classified 500 catch-all after the TransferRecoveryError arm; "
        "transfer_artifact raises no exception group.",
    ),
    # ── web/routes/context_versions.py ──────────────────────────────────
    ("web/routes/context_versions.py", "enable_artifact_versioning", ("OSError",), 0): (
        _U,
        "no_recovery_callee",
        "Version enable (skills refused pre-lock; not a swap path); " + _NONSKILL,
    ),
}

#: The pinned recovery boundaries — every one must be DISCOVERED and classified
#: TRANSLATES_RECOVERY. A floor, never the scope (#1866).
KNOWN_RECOVERY_BOUNDARIES: frozenset[tuple[str, str, tuple[str, ...], int]] = frozenset(
    {
        ("context/pull_apply.py", "_commit_skills", ("SwapRecoveryError",), 0),
        ("context/pull_apply.py", "_commit_skills", ("SwapRecoveryError",), 1),
        ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 0),
        ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 1),
        ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 2),
        ("context/skills.py", "generate_all_skills", ("SwapRecoveryError",), 3),
        ("context/skills.py", "extract_skills_to_canonical", ("SwapRecoveryError",), 0),
        ("context/skills.py", "extract_skills_to_canonical", ("SwapRecoveryError",), 1),
        ("context/transfer.py", "transfer_artifact", ("SwapRecoveryError",), 0),
        ("cli/context_cmd.py", "_run_update_all", ("SwapRecoveryError",), 0),
        ("cli/context_cmd.py", "_run_install_all", ("SwapRecoveryError",), 0),
        ("cli/context_cmd.py", "seed_validation_cmd", ("SwapRecoveryError",), 0),
        ("server/tools/context.py", "mem_context_artifact_migrate", ("TransferRecoveryError",), 0),
        ("server/tools/context.py", "mem_context_artifact_transfer", ("TransferRecoveryError",), 0),
        ("web/routes/context_mutations.py", "install_asset", ("SwapRecoveryError",), 0),
        ("web/routes/context_mutations.py", "update_asset", ("SwapRecoveryError",), 0),
        ("web/routes/context_skills.py", "create_skill", ("SwapRecoveryError",), 0),
        ("web/routes/context_skills.py", "update_skill", ("SwapRecoveryError",), 0),
        ("web/routes/context_skills.py", "delete_skill", ("SwapRecoveryError",), 0),
        (
            "web/routes/context_transfer.py",
            "transfer_context_artifact",
            ("TransferRecoveryError",),
            0,
        ),
    }
)


# ── Structural verification helpers ──────────────────────────────────────
_GROUP_PRODUCERS = frozenset(
    {"gather", "TaskGroup", "create_task_group", "ExceptionGroup", "BaseExceptionGroup"}
)


def _handler_reraises_bare(handler: ast.ExceptHandler) -> bool:
    """The handler's LAST statement is a ``raise`` (bare or ``raise X``).

    Tripwire-grade, not control-flow analysis: a conditional early ``return``
    before the trailing ``raise`` would still pass. Every current RERAISES
    handler is an unconditional cleanup-then-reraise, so the trailing-statement
    check pins the shape that matters without a CFG.
    """
    body = handler.body
    return bool(body) and isinstance(body[-1], ast.Raise)


def _handler_reraises_promote_race(handler: ast.ExceptHandler) -> bool:
    """The pinned ``if not _promote_race_conflict(<exc>): raise`` shape."""
    bound = handler.name
    for node in ast.walk(handler):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        call = test.operand
        if not (isinstance(call, ast.Call) and _callee_name(call.func) == "_promote_race_conflict"):
            continue
        if bound is not None and not (
            call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == bound
        ):
            continue
        if any(isinstance(s, ast.Raise) for s in node.body):
            return True
    return False


def _groups_cannot_propagate(try_node: ast.AST) -> bool:
    """No group-producing construct is referenced in the guarded try body."""
    return not (_refs_in(_try_body_nodes(try_node)) & _GROUP_PRODUCERS)


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# ── Tests ────────────────────────────────────────────────────────────────
def test_every_intercepting_handler_is_classified() -> None:
    """Discovery is the contract; the registry only explains what it found."""
    handlers = discover_handlers()
    unclassified = [
        f"{h.module}:{h.lineno} {h.qualname} caught={'|'.join(h.caught)} #{h.occurrence}"
        for h in handlers
        if h.key not in INTERCEPT_SITES
    ]
    assert not unclassified, (
        "unclassified supertype handler — a subclass of its caught type "
        "(SwapRecoveryError/TransferRecoveryError) could be intercepted and "
        "demoted. Classify it in INTERCEPT_SITES with a written reason. "
        "Paste-ready keys:\n  " + "\n  ".join(unclassified)
    )


def test_no_stale_registry_rows() -> None:
    stale = sorted(set(INTERCEPT_SITES) - {h.key for h in discover_handlers()})
    assert not stale, f"INTERCEPT_SITES rows no longer present in the tree: {stale}"


def test_registry_keys_are_unique() -> None:
    handlers = discover_handlers()
    assert len(handlers) == len({h.key for h in handlers}), "two handlers share one key"


def test_classifications_are_from_the_closed_set_and_explained() -> None:
    for key, (classification, evidence, why) in INTERCEPT_SITES.items():
        assert classification in _CLASSIFICATIONS, f"{key}: unknown classification"
        assert why.strip(), f"{key}: classification with no written reason"
        if classification == PRECEDED_BY_SPECIFIC:
            assert evidence in _PRECEDED_MODES, f"{key}: bad preceded mode {evidence!r}"
        elif classification == RERAISES_RECOVERY:
            assert evidence in _RERAISE_MODES, f"{key}: bad reraise mode {evidence!r}"
        elif classification == UNREACHABLE_RECOVERY:
            assert evidence in _UNREACHABLE_MODES, f"{key}: bad unreachable mode {evidence!r}"
        else:
            assert evidence == "", f"{key}: {classification} takes no evidence, got {evidence!r}"


def test_preceded_by_specific_is_structural() -> None:
    """A row claiming precedence must actually have the specific clause before it."""
    by_key = {h.key: h for h in discover_handlers()}
    offenders: list[str] = []
    for key, (classification, mode, _why) in INTERCEPT_SITES.items():
        if classification != PRECEDED_BY_SPECIFIC:
            continue
        handler = by_key[key]
        required = "SwapRecoveryError" if mode == "swap" else "TransferRecoveryError"
        if required not in handler.preceded_by:
            offenders.append(f"{handler.module}:{handler.lineno} lacks a preceding {required}")
        # Group-awareness: a plain Try needs the group not to reach this handler.
        if isinstance(handler.try_node, ast.Try) and not _groups_cannot_propagate(handler.try_node):
            offenders.append(
                f"{handler.module}:{handler.lineno} — a group producer in the try can wrap the "
                "recovery error past the specific clause"
            )
    assert not offenders, "PRECEDED_BY_SPECIFIC structural check failed:\n  " + "\n  ".join(
        offenders
    )


def test_reraises_recovery_is_structural() -> None:
    by_key = {h.key: h for h in discover_handlers()}
    offenders: list[str] = []
    for key, (classification, mode, _why) in INTERCEPT_SITES.items():
        if classification != RERAISES_RECOVERY:
            continue
        handler = by_key[key].node
        ok = (
            _handler_reraises_bare(handler)
            if mode == "bare"
            else _handler_reraises_promote_race(handler)
        )
        if not ok:
            offenders.append(f"{by_key[key].module}:{by_key[key].lineno} not a {mode} re-raise")
    assert not offenders, "RERAISES_RECOVERY structural check failed:\n  " + "\n  ".join(offenders)


def test_no_recovery_callee_rows_reference_no_recovery_symbol() -> None:
    """The strong anti-self-certification direction: a row that claims the try
    reaches no recovery path must be right about it."""
    by_key = {h.key: h for h in discover_handlers()}
    offenders: list[str] = []
    for key, (classification, mode, _why) in INTERCEPT_SITES.items():
        if classification != UNREACHABLE_RECOVERY or mode != "no_recovery_callee":
            continue
        if _refs_in_try(by_key[key]) & _recovery_capable_names():
            offenders.append(
                f"{by_key[key].module}:{by_key[key].lineno} claims no_recovery_callee but its "
                "try references a recovery-capable symbol — re-classify as handled_upstream "
                "(with the conversion argued) or a translating/re-raising handler"
            )
    assert not offenders, "no_recovery_callee misclassification:\n  " + "\n  ".join(offenders)


#: The RAW recovery propagators — a call to one of these raises a recovery
#: exception directly, so a broad handler over it cannot be ``handled_upstream``
#: (nothing converts it): it must translate, re-raise, or precede a specific
#: clause. Only the BATCH callees that turn recovery into a typed skip / result
#: code (generate_all_skills, extract_skills_to_canonical, the sync-core phase
#: functions, migrate_scope) legitimately carry the ``handled_upstream`` tag.
_DIRECT_PROPAGATORS = frozenset(
    {
        "transfer_artifact",
        "run_swap_prelude",
        "recover_pending_swaps",
        "_recover_and_reap_internal_dirs",
        "_stage_captured_tree",
        "_stage_skill",
        "commit_pull",
        "prepare_pull",
        # The public wrappers that RE-RAISE rather than convert (G4a-3c
        # re-review Major): copy_skill lets the prelude's SwapRecoveryError
        # propagate by contract; migrate_scope calls transfer_artifact.
        "copy_skill",
        "migrate_scope",
        # G4b: the overwrite engine re-raises the swap's SwapRecoveryError to
        # its caller's swap_recovery_pending arm.
        "_overwrite_skill_tree",
    }
)


def test_handled_upstream_rows_actually_reference_recovery() -> None:
    """The symmetric direction, hardened (G4a-3c review Major): an upstream row
    must genuinely reach a recovery callee (not just be tag-dodging the
    no_recovery_callee check) AND must NOT reach a RAW propagator directly — a
    try that calls ``transfer_artifact``/``run_swap_prelude`` receives the raw
    recovery exception, which nothing upstream converted, so a broad handler
    there is a real interception, not a handled one."""
    by_key = {h.key: h for h in discover_handlers()}
    offenders: list[str] = []
    for key, (classification, mode, _why) in INTERCEPT_SITES.items():
        if classification != UNREACHABLE_RECOVERY or mode != "handled_upstream":
            continue
        refs = _refs_in_try(by_key[key])
        if not (refs & _recovery_capable_names()):
            offenders.append(
                f"{by_key[key].module}:{by_key[key].lineno} claims handled_upstream but its try "
                "references no recovery-capable symbol — it is simply no_recovery_callee"
            )
        if refs & _DIRECT_PROPAGATORS:
            offenders.append(
                f"{by_key[key].module}:{by_key[key].lineno} claims handled_upstream but its try "
                f"calls a RAW propagator {sorted(refs & _DIRECT_PROPAGATORS)} — the recovery "
                "exception arrives un-converted; this is an interception, not a handled state"
            )
    assert not offenders, "handled_upstream misclassification:\n  " + "\n  ".join(offenders)


_RECOVERY_TOKENS = frozenset(
    {
        "SwapRecoveryError",
        "SwapForeignDestination",
        "TransferRecoveryError",
        "swap_failure_text",
        "SWAP_RECOVERY_PENDING",
        "swap_recovery_pending",
    }
)


def test_translates_recovery_rows_catch_a_specific_type() -> None:
    """A TRANSLATES row either catches a recovery-specific type outright, or (the
    shared CLI translator's shape) catches a caller-supplied <dynamic> type and
    dispatches on the recovery type in its body."""
    by_key = {h.key: h for h in discover_handlers()}
    for key, (classification, _mode, _why) in INTERCEPT_SITES.items():
        if classification != TRANSLATES_RECOVERY:
            continue
        caught = key[2]
        if set(caught) & _SPECIFIC:
            continue
        body_refs = _refs_in(list(by_key[key].node.body))
        assert body_refs & _RECOVERY_TOKENS, (
            f"{key}: TRANSLATES_RECOVERY but neither catches a recovery-specific "
            "type nor dispatches on one in its body"
        )


def test_no_specific_clause_is_dead() -> None:
    """A specific clause preceded by a same-family supertype in its own try is
    unreachable — Python would silently make it dead."""
    offenders = [
        f"{h.module}:{h.lineno} {h.qualname}"
        for h in discover_handlers()
        if set(h.caught) & _SPECIFIC and h.followed_by_specific is False and _clause_is_dead(h)
    ]
    assert not offenders, (
        "a specific recovery clause is dead (shadowed earlier):\n  " + "\n  ".join(offenders)
    )


def _raw_caught(handler: ast.ExceptHandler) -> set[str]:
    """The literal Name/Attribute ids in a handler's caught expression — enough
    for family membership (recovery ancestors appear as literal names)."""
    if handler.type is None:
        return {_BARE}
    names: set[str] = set()
    for sub in ast.walk(handler.type):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
    return names


def _clause_is_dead(handler: _Handler) -> bool:
    """Whether a same-or-broader-family clause precedes this specific clause in
    its own try (Python would make it silently unreachable).

    The family sets are the discovery's OWN ancestor sets (review follow-up:
    a hand-copied subset missed the ``IOError``/``EnvironmentError`` alias
    spellings and did not treat ``SwapForeignDestination`` as a specific
    clause), plus the bare ``except:``, which shadows everything.
    """
    if not isinstance(handler.try_node, ast.Try):
        return False
    specific = set(handler.caught) & _SPECIFIC
    swap_specific = specific & {"SwapRecoveryError", "SwapForeignDestination"}
    transfer_specific = specific & {"TransferRecoveryError"}
    for h in handler.try_node.handlers:
        if h.lineno == handler.lineno:
            return False  # reached our own clause with nothing shadowing it
        caught = _raw_caught(h)
        if swap_specific and (caught & (_SWAP_ANCESTORS | {_BARE})):
            return True
        if transfer_specific and (caught & (_TRANSFER_ANCESTORS | {_BARE})):
            return True
    return False


def test_recovery_ancestor_sets_match_the_real_mro() -> None:
    """The ancestor name sets are the one hand-list left (review follow-up F1).
    If the recovery hierarchy later gains an intermediate class, an imported
    catch of it is still resolved at runtime, but a handler in the DEFINING
    module hits the ``_classdef_names`` short-circuit and would be silently
    dropped — so pin the sets to the real MROs: every exception class an
    instance would satisfy must be spelled in ``_INTERCEPTING``."""
    from memtomem.context._dir_swap import SwapForeignDestination, SwapRecoveryError
    from memtomem.context.transfer import TransferRecoveryError

    for rec, ancestors in (
        (SwapRecoveryError, _SWAP_ANCESTORS),
        (SwapForeignDestination, _SWAP_ANCESTORS),
        (TransferRecoveryError, _TRANSFER_ANCESTORS),
    ):
        for klass in rec.__mro__:
            if not (isinstance(klass, type) and issubclass(klass, BaseException)):
                continue  # object
            assert klass.__name__ in _INTERCEPTING, (
                f"{klass.__name__} is in {rec.__name__}.__mro__ but not in _INTERCEPTING — "
                "a handler catching it in the defining module would be silently dropped"
            )
            if klass.__name__ not in _SPECIFIC:
                assert klass.__name__ in ancestors, (
                    f"{klass.__name__} missing from the {rec.__name__} family ancestor set"
                )


def test_known_recovery_boundaries_are_discovered_and_translate() -> None:
    handlers = {h.key for h in discover_handlers()}
    translates = {k for k, (c, _m, _w) in INTERCEPT_SITES.items() if c == TRANSLATES_RECOVERY}
    missing = sorted(KNOWN_RECOVERY_BOUNDARIES - handlers)
    assert not missing, f"a known recovery boundary is no longer discovered: {missing}"
    unt = sorted(KNOWN_RECOVERY_BOUNDARIES - translates)
    assert not unt, f"a known recovery boundary is no longer TRANSLATES_RECOVERY: {unt}"


def test_identical_site_digests_share_classification() -> None:
    """Two structurally identical sites (same subtree, ancestry, bindings) must
    carry the same classification and evidence — else the digest is meaningless."""
    import collections

    by_digest: dict[str, list[_Handler]] = collections.defaultdict(list)
    for h in discover_handlers():
        by_digest[h.site_digest].append(h)
    offenders: list[str] = []
    for digest, group in by_digest.items():
        rows = {INTERCEPT_SITES.get(h.key, ("?", "?", ""))[:2] for h in group}
        if len(rows) > 1:
            locs = ", ".join(f"{h.module}:{h.lineno}" for h in group)
            offenders.append(f"digest {digest[:8]} spans differing classifications: {locs}")
    assert not offenders, "identical site digests disagree:\n  " + "\n  ".join(offenders)


# ── Scope-rot: nothing recovery-capable lives outside _IN_SCOPE ──────────
def _import_alias_map(tree: ast.AST) -> dict[str, str]:
    """``local spelling → original name`` for every import, so a callable
    imported as ``generate_all_skills as gen`` is canonicalised back."""
    amap: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.asname:
                    amap[alias.asname] = alias.name.rsplit(".", 1)[-1]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    amap[alias.asname] = alias.name.split(".")[-1]
    return amap


def _imported_symbol_names(tree: ast.AST) -> set[str]:
    """The ORIGINAL names a module imports (``from m import X`` → ``X``). A
    module can only propagate a recovery exception from a function it actually
    imported — a local variable that merely shares a capable function's name
    (e.g. ``upgrade_cmd``'s local ``install_cmd``) is not a use of it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name.rsplit(".", 1)[-1])
    return names


@functools.cache
def _propagator_host_modules() -> frozenset[str]:
    """Dotted paths of the modules that DEFINE a raw propagator — computed from
    the tree, so ``import memtomem.context.skills as s; s.copy_skill()`` can be
    recognised without a hand list."""
    hosts: set[str] = set()
    for path in _scan_files():
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in _DIRECT_PROPAGATORS
            ):
                hosts.add(_PACKAGE_ROOT + "." + rel[: -len(".py")].replace("/", "."))
                break
    return frozenset(hosts)


def _scope_rot_offence(source: str, rel: str) -> str | None:
    """Why an out-of-scope module is a scope gap, or ``None``.

    Reach forms (G4a-3c re-review Major; review follow-up widened the second):
    ``from m import copy_skill`` (any source module — so a re-export CONSUMER is
    caught by name), and an attribute reference to a raw propagator whose dotted
    chain resolves through ANY imported module binding to a propagator-hosting
    module — ``import memtomem.context.skills as s``, ``from memtomem.context
    import skills``, or plain ``import memtomem.context.skills`` with the fully
    dotted call. Either is a gap only if the module ALSO catches a supertype — a
    pure re-export / registration module (server/__init__ wiring the MCP tools)
    catches nothing and is safe, and a local variable that merely shares a
    propagator's name (upgrade_cmd's local ``install_cmd``) is neither imported
    nor a module attribute."""
    tree = ast.parse(source)
    hit = _imported_symbol_names(tree) & _SCOPE_ROT_SYMBOLS
    if not hit:
        # Reuse the runtime-resolution import index (relative imports included):
        # every local import binding, flattened back to its dotted path.
        _MODULE_REL_BY_TREE[tree] = rel
        module_locals = {
            local: ".".join([module_dotted, *chain])
            for local, (module_dotted, chain) in _import_targets(tree).items()
        }
        hosts = _propagator_host_modules()
        attr_hits: set[str] = set()
        for sub in ast.walk(tree):
            if not (isinstance(sub, ast.Attribute) and sub.attr in _DIRECT_PROPAGATORS):
                continue
            # Walk the dotted chain down to its root Name, then substitute the
            # root's import binding: ``memtomem.context.skills.copy_skill`` and
            # ``skills.copy_skill`` (either import spelling) both resolve to the
            # hosting module's dotted path.
            parts: list[str] = []
            root: ast.expr = sub.value
            while isinstance(root, ast.Attribute):
                parts.append(root.attr)
                root = root.value
            if not (isinstance(root, ast.Name) and root.id in module_locals):
                continue
            if ".".join([module_locals[root.id], *reversed(parts)]) in hosts:
                attr_hits.add(sub.attr)
        if attr_hits:
            hit = attr_hits
    if not hit:
        return None
    if not handlers_in_source(source, rel)[0]:
        return None
    return f"{rel} reaches raw propagator {sorted(hit)} AND catches a supertype"


def test_scope_rot_no_recovery_symbol_referenced_outside_scope() -> None:
    """A new module that starts handling a recovery exception drags itself into
    scope — the fixed directory list cannot rot silently (round-4 gate)."""
    offenders: list[str] = []
    for path in _scan_files():
        rel = path.relative_to(_SRC).as_posix()
        if _in_scope(rel):
            continue
        offence = _scope_rot_offence(path.read_text(encoding="utf-8"), rel)
        if offence:
            offenders.append(offence)
    assert not offenders, (
        "an out-of-scope module reaches a raw recovery propagator and catches a "
        "supertype — add it to the guard's scope (its handlers need "
        "classification):\n  " + "\n  ".join(offenders)
    )


def test_scope_rot_detects_all_reach_forms() -> None:
    """Synthetic negatives for the helper: the from-import form, every
    module-object + attribute-call spelling (G4a-3c re-review Major; the
    from-module and fully dotted spellings were review follow-ups), and the safe
    shapes (no handlers; local name collision)."""
    from_import = (
        "from memtomem.context.skills import copy_skill\n"
        "def h():\n    try:\n        copy_skill(a, b)\n    except OSError:\n        pass\n"
    )
    assert _scope_rot_offence(from_import, "outside/x.py")

    module_attr = (
        "import memtomem.context.skills as skills\n"
        "def h():\n    try:\n        skills.copy_skill(a, b)\n    except Exception:\n        pass\n"
    )
    assert _scope_rot_offence(module_attr, "outside/y.py")

    from_module_import = (
        "from memtomem.context import skills\n"
        "def h():\n    try:\n        skills.copy_skill(a, b)\n    except OSError:\n        pass\n"
    )
    assert _scope_rot_offence(from_module_import, "outside/w.py")

    full_dotted = (
        "import memtomem.context.skills\n"
        "def h():\n    try:\n        memtomem.context.skills.copy_skill(a, b)\n"
        "    except Exception:\n        pass\n"
    )
    assert _scope_rot_offence(full_dotted, "outside/v.py")

    no_handlers = "from memtomem.context.skills import copy_skill\nX = [copy_skill]\n"
    assert _scope_rot_offence(no_handlers, "outside/reexport.py") is None

    local_collision = (
        "def h():\n    copy_skill = ['pip', 'install']\n"
        "    try:\n        run(copy_skill)\n    except OSError:\n        pass\n"
    )
    assert _scope_rot_offence(local_collision, "outside/z.py") is None


def test_no_dynamic_import_of_the_defining_modules() -> None:
    """importlib/__import__/getattr indirection of the swap/transfer modules is
    rejected fail-closed — a static reference scan cannot see through it."""
    dotted = {"memtomem.context._dir_swap", "memtomem.context.transfer"}
    offenders: list[str] = []
    for path in _scan_files():
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Constant-fold module-level string aliases so
        # ``M = "memtomem.context.transfer"; import_module(M)`` is caught.
        string_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            string_aliases[t.id] = node.value.value
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                callee = _callee_name(node.func)
                if callee in {"import_module", "__import__"}:
                    for arg in node.args:
                        val = None
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                            val = arg.value
                        elif isinstance(arg, ast.Name) and arg.id in string_aliases:
                            val = string_aliases[arg.id]
                        if val in dotted:
                            offenders.append(f"{rel}:{node.lineno} dynamically imports {val}")
    assert not offenders, "dynamic import of a defining module (fail-closed):\n  " + "\n  ".join(
        offenders
    )


# ── Synthetic negatives — proof the guard CATCHES a regression ───────────
#
# Asserting the guard's positive result on the tree cannot tell "no violations"
# from "detects nothing" (feedback_pin_test_mutation_validation). Each source
# below is fed to ``handlers_in_source`` and must be DISCOVERED (and, being a
# synthetic module, UNclassified — which is what fails the build).
_SYN_BROAD_UPSTREAM = """
def new_route(kind, name):
    try:
        transfer_artifact(kind, name)
    except Exception:
        return {"reason_code": "destination_exists"}
"""

_SYN_TUPLE = """
def h():
    try:
        f()
    except (ValueError, OSError):
        return None
"""

_SYN_ATTR = """
import click
def h():
    try:
        f()
    except click.ClickException:
        return None
"""

_SYN_ALIASED_IMPORT = """
from click import ClickException as CE
def h():
    try:
        f()
    except CE:
        return None
"""

_SYN_BARE = """
def h():
    try:
        f()
    except:
        return None
"""

_SYN_IOERROR = """
def h():
    try:
        f()
    except IOError:
        return None
"""

_SYN_ASSIGN_ALIAS = """
RECOVERY = (OSError, ValueError)
def h():
    try:
        f()
    except RECOVERY:
        return None
"""

_SYN_DYNAMIC = """
def h(errs):
    try:
        f()
    except errs():
        return None
"""

_SYN_POISON_CONFLICT = """
E = OSError
E = ValueError
def h():
    try:
        f()
    except E:
        return None
"""

_SYN_GROUP = """
def h():
    try:
        f()
    except ExceptionGroup:
        return None
"""

_SYN_DEAD_CLAUSE = """
def h():
    try:
        f()
    except OSError:
        return None
    except SwapRecoveryError:
        return "dead"
"""

# Alias-spelled shadow of a SUBCLASS specific — the review-F2 gap: ``IOError``
# is an ``OSError`` alias and ``SwapForeignDestination`` is a specific clause.
_SYN_DEAD_CLAUSE_ALIAS = """
def h():
    try:
        f()
    except IOError:
        return None
    except SwapForeignDestination:
        return "dead"
"""

# A recorded tuple alias later rebound — the review-F3 gap: the stale tuple
# members must not outrank the poison (fail-closed to <dynamic>).
_SYN_TUPLE_THEN_REBIND = """
RECOVERY = (OSError, ValueError)
RECOVERY = something_else
def h():
    try:
        f()
    except RECOVERY:
        return None
"""

# The fail-closed Blocker cases: a caught name whose runtime type the analysis
# cannot pin must become <dynamic>, never be silently dropped.
_SYN_PARAM_DEFAULT = """
def h(E=OSError):
    try:
        f()
    except E:
        return None
"""

_SYN_ANNOTATED_ALIAS = """
E: type = OSError
def h():
    try:
        f()
    except E:
        return None
"""

_SYN_UNRESOLVABLE_CALL = """
def h():
    try:
        f()
    except make_error_types():
        return None
"""


def _syn(source: str) -> list[_Handler]:
    return handlers_in_source(source, "synthetic.py")[0]


def test_broad_handler_upstream_of_a_boundary_is_discovered_unclassified() -> None:
    """The design's required synthetic: a broad ``except Exception`` guarding a
    recovery-raising call is a site, and (being unregistered) fails the build."""
    sites = _syn(_SYN_BROAD_UPSTREAM)
    assert sites and any("Exception" in s.caught for s in sites)
    assert all(s.key not in INTERCEPT_SITES for s in sites)


def test_discovery_covers_every_spelling() -> None:
    for label, source, expected in (
        ("tuple", _SYN_TUPLE, "OSError"),
        ("attribute", _SYN_ATTR, "ClickException"),
        ("aliased import", _SYN_ALIASED_IMPORT, "ClickException"),
        ("bare", _SYN_BARE, _BARE),
        ("IOError", _SYN_IOERROR, "IOError"),
        ("assignment-alias tuple", _SYN_ASSIGN_ALIAS, "OSError"),
        ("dynamic caught expr", _SYN_DYNAMIC, _DYNAMIC),
        ("conflicting-rebind poison", _SYN_POISON_CONFLICT, _DYNAMIC),
        ("tuple-then-rebind poison", _SYN_TUPLE_THEN_REBIND, _DYNAMIC),
        ("exception group", _SYN_GROUP, "ExceptionGroup"),
    ):
        sites = _syn(source)
        assert sites, f"discovery missed the {label} handler"
        caught = {c for s in sites for c in s.caught}
        assert expected in caught, f"{label}: expected {expected} in {caught}"
        assert all(s.key not in INTERCEPT_SITES for s in sites)


def test_unresolvable_caught_names_fail_closed_to_dynamic() -> None:
    """A caught name the analysis cannot pin to a concrete non-recovery type must
    be discovered as <dynamic> (a site to classify), never dropped — else a real
    OSError interception spelled through a param/call/import bypasses the
    registry (G4a-3c review Blocker, both rounds)."""
    for label, source in (
        ("param default", _SYN_PARAM_DEFAULT),
        ("unresolvable call result", _SYN_UNRESOLVABLE_CALL),
        (
            "unresolvable imported name",
            "from ext import ERROR_TYPES\ndef h():\n    try:\n        f()\n    except ERROR_TYPES:\n        pass\n",
        ),
        (
            "unresolvable imported attribute",
            "import ext\ndef h():\n    try:\n        f()\n    except ext.ERROR_TYPES:\n        pass\n",
        ),
        (
            "imported OSError alias (os.error)",
            "from os import error\ndef h():\n    try:\n        f()\n    except error:\n        pass\n",
        ),
    ):
        sites = _syn(source)
        assert sites, f"discovery dropped the {label} handler"
        assert any(_DYNAMIC in s.caught for s in sites), f"{label} was not <dynamic>"
        assert all(s.key not in INTERCEPT_SITES for s in sites)
    # A module-level annotated alias DOES resolve — it is a real OSError site.
    ann = _syn(_SYN_ANNOTATED_ALIAS)
    assert ann and any("OSError" in s.caught for s in ann)
    # And runtime resolution keeps precision: a genuinely-sibling imported
    # exception is provably safe and produces NO site (no registry flooding).
    safe = _syn(
        "from memtomem.context.privacy_scan import PrivacyScanError\n"
        "def h():\n    try:\n        f()\n    except PrivacyScanError:\n        pass\n"
    )
    assert safe == [], "a provably-sibling imported exception became a site"


def test_dead_specific_clause_is_flagged() -> None:
    """A ``SwapRecoveryError`` clause after an ``OSError`` clause in the same try
    is unreachable — the discovery must mark the OSError as followed_by_specific
    and _clause_is_dead must catch the shadowed specific."""
    sites = _syn(_SYN_DEAD_CLAUSE)
    oserr = next(s for s in sites if s.caught == ("OSError",))
    assert oserr.followed_by_specific is True
    specific = next(s for s in sites if s.caught == ("SwapRecoveryError",))
    assert _clause_is_dead(specific), "a shadowed specific clause was not detected as dead"

    # Review-F2 spellings: an IOError (OSError alias) shadow over the
    # SwapForeignDestination subclass specific must be flagged too.
    alias_sites = _syn(_SYN_DEAD_CLAUSE_ALIAS)
    foreign = next(s for s in alias_sites if s.caught == ("SwapForeignDestination",))
    assert _clause_is_dead(foreign), "an alias-spelled shadow of a subclass specific was missed"


def test_group_producer_defeats_preceded_by_specific() -> None:
    """A plain try whose body can raise an ExceptionGroup is not shielded by a
    plain specific clause — _groups_cannot_propagate must return False."""
    src = "def h():\n    try:\n        gather(a, b)\n    except SwapRecoveryError:\n        pass\n"
    tree = ast.parse(src)
    try_node = next(n for n in ast.walk(tree) if isinstance(n, ast.Try))
    assert not _groups_cannot_propagate(try_node)


def test_site_digest_distinguishes_reordered_and_branched_handlers() -> None:
    """The identical-digest test is vacuous unless the digest actually varies
    with handler position (all current sites are unique). Prove it does: the two
    clauses of one try, the same clause reordered, and identical trys in opposite
    branches of an ``if`` each get distinct digests, so an occurrence key cannot
    silently migrate to a same-signature neighbour (G4a-3c review Minor)."""
    two_clauses = _syn(
        "def h():\n"
        "    try:\n        f()\n"
        "    except SwapRecoveryError:\n        return 1\n"
        "    except OSError:\n        return 2\n"
    )
    assert len({h.site_digest for h in two_clauses}) == 2

    reordered = _syn(
        "def h():\n"
        "    try:\n        f()\n"
        "    except OSError:\n        return 2\n"
        "    except SwapRecoveryError:\n        return 1\n"
    )
    swap_a = next(h for h in two_clauses if h.caught == ("SwapRecoveryError",))
    swap_b = next(h for h in reordered if h.caught == ("SwapRecoveryError",))
    assert swap_a.site_digest != swap_b.site_digest, "reordering did not change the digest"

    branched = _syn(
        "def h(c):\n"
        "    if c:\n"
        "        try:\n            f()\n        except OSError:\n            return 1\n"
        "    else:\n"
        "        try:\n            f()\n        except OSError:\n            return 2\n"
    )
    assert len({h.site_digest for h in branched}) == 2, "opposite branches share a digest"


if __name__ == "__main__":  # pragma: no cover - developer aid / census dump
    import collections

    by_module: dict[str, list[_Handler]] = collections.defaultdict(list)
    for h in discover_handlers():
        by_module[h.module].append(h)
    total = 0
    for module in sorted(by_module):
        print(f"\n# {module}")
        for h in sorted(by_module[module], key=lambda x: x.lineno):
            total += 1
            print(
                f'    ("{h.module}", "{h.qualname}", {h.caught!r}, {h.occurrence}): '
                f"  # L{h.lineno} pre={sorted(h.preceded_by)} dead={h.followed_by_specific}"
            )
    print(f"\n# total in-scope intercepting handlers: {total}")
