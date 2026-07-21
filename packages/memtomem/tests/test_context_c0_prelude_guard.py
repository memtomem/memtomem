"""Architectural guard: every C0 holder is classified, and the skills ones recover.

ADR-0030 §6 gives every first-party canonical artifact one name-keyed lock —
C0, ``<root>/.{name}.lock`` — and §10 adds a second rule on top of it: a writer
that holds C0 over a *skills* canonical must run the recovery prelude
(:func:`memtomem.context.skills.run_swap_prelude`) FIRST, so an interrupted
directory swap is resolved before anything reads or writes that tree.

The prelude's own call sites are not the same set as "every first-party
canonical-skill writer" — PR-G4a-3b found ten holders that took C0 and never
recovered, each a concrete data-loss path (a crash state plus a create/install
materializes a destination, after which recovery reads a different row and
deletes the original). This guard exists so an eleventh cannot arrive quietly.

**Why a guard and not a list.** The design note's hand-written census of gap
sites was written by surface (web CRUD, install, transfer, seeder) and missed
:func:`memtomem.context.skills.copy_skill`, a library entry point that does not
sit on any of those axes. Deriving scope from an enumeration and then guarding
with that same enumeration certifies itself (#1866). So discovery here measures
the tree, and :data:`C0_SITES` must be a SUPERSET of the known table — never
the other way round.

Discovery covers **two acquisition forms**, and neither alone is enough:

1. Approved wrappers (:data:`_WRAPPERS`) — matched as *references*, not just as
   call callees, because a wrapper handed to an executor
   (``asyncio.to_thread(versioning_op_locked, …)``) is a real acquisition that
   a Call-only scan walks straight past. That is the same lesson
   ``test_context_atomic_write_guard`` records for ``.write_text``.
2. Raw derived primitives — ``_file_lock`` / ``async_file_lock`` whose path
   argument derives from ``_lock_path_for`` / ``canonical_lock_path``, however
   indirected (nested call, intermediate variable, comprehension). Matching
   only the literal nested form misses the multi-destination push, which builds
   the path set into a variable first; matching *every* raw ``_file_lock``
   sweeps in unrelated domains (memory CRUD, config, projects).

Every discovered site carries a classification and a written justification;
anything unclassified fails the build.
"""

from __future__ import annotations

import ast
import functools
import pathlib
from dataclasses import dataclass

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "memtomem"

#: The four approved C0 wrappers (``context/_canonical_txn.py``). A reference to
#: one of these IS an acquisition — including a bare reference passed to an
#: executor.
_WRAPPERS = frozenset(
    {
        "canonical_sidecar_lock",
        "canonical_lock_shared_budget",
        "acquire_canonical_locks",
        "versioning_op_locked",
    }
)

#: The raw lock primitives. Only an acquisition when the path argument derives
#: from a canonical-lock path builder (below).
_PRIMITIVES = frozenset({"_file_lock", "async_file_lock"})

#: Builders whose result is a canonical (or canonical-shaped) sidecar path.
_LOCK_PATH_BUILDERS = frozenset({"_lock_path_for", "canonical_lock_path"})

#: Names that satisfy §10 when they dominate a C0 body.
_PRELUDES = frozenset({"run_swap_prelude", "_recover_and_reap_internal_dirs"})

#: Mutating calls used by the ``before_mutators`` dominance mode only (see
#: :class:`_Site`). Deliberately small and deliberately NOT the membership
#: rule: a mutator missing here weakens dominance at the two sites that use
#: that mode, and nothing else.
_MUTATORS = frozenset(
    {
        "_stage_skill",
        "_promote_staging",
        "_stage_copy",
        "_stage_move",
        "_promote_move",
        "create_tree_version",
        "create_version",
        "atomic_write_text",
        "atomic_write_bytes",
        "copy_tree_atomic",
        "copy_asset_at_commit",
        "rmtree",
        "replace",
        "mkdir",
        "unlink",
        "_write_lf",
    }
)

# Classifications.
RUNS_SWAP_PRELUDE = "RUNS_SWAP_PRELUDE"  # C0 over a skills-capable root
EXEMPT = "EXEMPT"  # C0, but never over skills
NON_C0 = "NON_C0"  # a sidecar lock that is not a canonical NAME lock
INFRASTRUCTURE = "INFRASTRUCTURE"  # the lock-composition helpers themselves

_CLASSIFICATIONS = frozenset({RUNS_SWAP_PRELUDE, EXEMPT, NON_C0, INFRASTRUCTURE})


@dataclass(frozen=True)
class _Site:
    """One discovered acquisition.

    ``key`` is ``(module, qualname, occurrence, callee)`` — deliberately not a
    line number (churns on every unrelated edit) and deliberately not the callee
    alone (``server/tools/context.py`` takes ``versioning_op_locked`` twice in
    one function, and a callee-only key would collapse the two into one
    registry row).
    """

    module: str
    qualname: str
    occurrence: int
    callee: str
    lineno: int
    node: ast.AST

    @property
    def key(self) -> tuple[str, str, int, str]:
        return (self.module, self.qualname, self.occurrence, self.callee)


def _scan_files() -> list[pathlib.Path]:
    """Every production module in the package — the census is package-wide.

    Narrowing to ``context/**`` + the gateway surfaces would make the guard's
    own scope a hand-written list, which is the failure mode it exists to
    prevent: a C0 lock taken in a new domain would simply not be seen.
    """
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _qualname_index(tree: ast.AST) -> list[tuple[int, int, str]]:
    """``(start, end, qualname)`` for every function/class body, outermost first."""
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


def _bound_names(target: ast.AST) -> set[str]:
    return {sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name)}


def _canonical_path_vars(fn: ast.AST) -> set[str]:
    """Names in *fn* that carry a canonical-lock path, transitively.

    Four binding shapes, and every one of them appears in the tree today:

    * ``lock = _lock_path_for(dst)`` — assignment;
    * ``for lock_path in sorted({_lock_path_for(d) for d in …})`` — the loop
      variable of the multi-destination push and both settings copiers, which
      is the shape gate R8 called out: an intra-statement match sees only the
      comprehension, never the ``_file_lock(lock_path)`` two lines down;
    * ``[…for p in {_lock_path_for(x)}]`` — comprehension generators;
    * ``ordered = sorted([lock_a, lock_b])`` — one hop removed, so the analysis
      iterates to a fixed point rather than looking one level deep.
    """
    names: set[str] = set()
    while True:
        known = _LOCK_PATH_BUILDERS | names
        grown = set(names)
        for node in ast.walk(fn):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                value = node.value
                if value is None or not _mentions(value, known):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    grown |= _bound_names(target)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                if _mentions(node.iter, known):
                    grown |= _bound_names(node.target)
            elif isinstance(node, ast.comprehension):
                if _mentions(node.iter, known):
                    grown |= _bound_names(node.target)
        if grown == names:
            return names
        names = grown


def _mentions(node: ast.AST, names: frozenset[str] | set[str]) -> bool:
    """Whether *node*'s subtree references any of *names*."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
        if isinstance(sub, ast.Attribute) and sub.attr in names:
            return True
    return False


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def sites_in_source(source: str, module: str) -> tuple[list[_Site], ast.AST]:
    """Discover acquisitions in one module's *source*.

    Split out from the package walk so the guard's own detection can be
    exercised on synthetic modules — a guard that is only ever run against a
    tree it already passes on proves nothing about what it would catch
    (``feedback_pin_test_mutation_validation``).
    """
    tree = ast.parse(source)
    spans = _qualname_index(tree)
    # Function-scoped variable analysis for the derived-primitive form.
    fn_nodes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    var_cache = {id(n): _canonical_path_vars(n) for n in fn_nodes}

    def vars_at(lineno: int) -> set[str]:
        found: set[str] = set()
        for fn in fn_nodes:
            if fn.lineno <= lineno <= getattr(fn, "end_lineno", fn.lineno):
                found |= var_cache[id(fn)]
        return found

    hits: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(tree):
        # Form 1 — a reference to an approved wrapper. The wrappers' own
        # definitions are not matched (a ``def`` is a FunctionDef, not a Name)
        # and neither are imports (an alias) or docstring mentions (a string).
        if isinstance(node, ast.Name) and node.id in _WRAPPERS:
            hits.append((node.lineno, node.id, node))
            continue
        if isinstance(node, ast.Attribute) and node.attr in _WRAPPERS:
            hits.append((node.lineno, node.attr, node))
            continue
        # Form 2 — a raw primitive whose path argument is canonical-derived.
        if isinstance(node, ast.Call):
            callee = _callee_name(node.func)
            if callee in _PRIMITIVES and node.args:
                arg = node.args[0]
                if _mentions(arg, _LOCK_PATH_BUILDERS) or _mentions(arg, vars_at(node.lineno)):
                    hits.append((node.lineno, callee, node))

    sites: list[_Site] = []
    counters: dict[tuple[str, str], int] = {}
    for lineno, callee, node in sorted(hits, key=lambda t: t[0]):
        qual = _qualname_for(spans, lineno)
        idx = counters.get((qual, callee), 0)
        counters[(qual, callee)] = idx + 1
        sites.append(_Site(module, qual, idx, callee, lineno, node))
    return sites, tree


@functools.cache
def _discover() -> tuple[tuple[_Site, ...], dict[str, ast.AST]]:
    """Sites plus the exact trees they were found in.

    Cached, and the trees are handed back rather than re-parsed per caller:
    ``_locked_body`` matches the acquisition node by IDENTITY, so a second
    ``ast.parse`` of the same file produces nodes that are equal in shape and
    match nothing.
    """
    sites: list[_Site] = []
    trees: dict[str, ast.AST] = {}
    for path in _scan_files():
        rel = path.relative_to(_SRC).as_posix()
        found, tree = sites_in_source(path.read_text(encoding="utf-8"), rel)
        trees[rel] = tree
        sites.extend(found)
    return tuple(sites), trees


def discover_sites() -> list[_Site]:
    """Every C0 acquisition in the package, measured from the tree."""
    return list(_discover()[0])


#: ``key → (classification, dominance_mode, why)``. ``dominance_mode`` is read
#: only for :data:`RUNS_SWAP_PRELUDE`: ``"first"`` (the prelude is the first
#: executable statement of the locked body) or ``"before_mutators"`` (the two
#: batch sites whose lock acquisition is itself a loop — see the module note on
#: dominance). Every row states WHY in its own words; a row with no reason is
#: how an unexamined site sneaks in wearing a classification.
C0_SITES: dict[tuple[str, str, int, str], tuple[str, str, str]] = {
    # ── RUNS_SWAP_PRELUDE — C0 over a skills canonical ──────────────────
    ("context/pull_apply.py", "_commit_skills", 0, "canonical_sidecar_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Pull commit writes the skills canonical; wired in G4a-3a.",
    ),
    ("context/skills.py", "copy_skill", 0, "_file_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Public copy entry point; stages+promotes into a skills canonical.",
    ),
    ("context/skills.py", "generate_all_skills", 0, "_file_lock"): (
        RUNS_SWAP_PRELUDE,
        "before_mutators",
        "project_shared push: acquires N destination locks in an ExitStack loop, "
        "then recovers per destination, so no single statement can be first — "
        "the batch cannot recover a destination before it holds every lock "
        "(#1229 all-or-nothing).",
    ),
    ("context/skills.py", "generate_all_skills", 1, "_file_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Non-shared push: one destination lock, prelude leads the locked body.",
    ),
    ("context/skills.py", "extract_skills_to_canonical", 0, "_file_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Reverse import writes the canonical; wired in G4a-3a.",
    ),
    ("context/transfer.py", "transfer_artifact", 0, "acquire_canonical_locks"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Cross-scope transfer MOVES the tree; recovers both roots (G4a-3b).",
    ),
    ("context/install.py", "_install_asset", 0, "canonical_lock_shared_budget"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Wiki install materializes the canonical; kind-gated (G4a-3b).",
    ),
    ("context/install.py", "_apply_update", 0, "canonical_lock_shared_budget"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Wiki update overwrites the canonical; kind-gated (G4a-3b).",
    ),
    ("context/install.py", "_apply_pinned_install", 0, "canonical_lock_shared_budget"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Pinned install writes the canonical; kind-gated (G4a-3b).",
    ),
    (
        "context/_validation_seed.py",
        "seed_adr0026_validation_states",
        0,
        "canonical_sidecar_lock",
    ): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Seeder writes a canonical skill (can target a live project with --force).",
    ),
    (
        "context/_validation_seed.py",
        "seed_adr0026_validation_states",
        1,
        "canonical_sidecar_lock",
    ): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Second seeded skill; same reasoning as the first.",
    ),
    ("web/routes/context_skills.py", "create_skill._create_locked", 0, "canonical_sidecar_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Web create materializes the canonical directory (G4a-3b).",
    ),
    ("web/routes/context_skills.py", "update_skill._update_locked", 0, "canonical_sidecar_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Web update rewrites SKILL.md inside the canonical (G4a-3b).",
    ),
    ("web/routes/context_skills.py", "delete_skill._delete_locked", 0, "canonical_sidecar_lock"): (
        RUNS_SWAP_PRELUDE,
        "first",
        "Web delete removes the canonical tree (G4a-3b).",
    ),
    # ── EXEMPT — a canonical name lock that never covers a skills tree ───
    ("context/_canonical_txn.py", "write_canonical_locked", 0, "canonical_sidecar_lock"): (
        EXEMPT,
        "",
        "Single canonical FILE writer (the agents/commands shape). Its own "
        "docstring records that the skills tree precondition belongs to "
        "pull_apply under pull_apply's lock, and skills overwrite is refused "
        "here. Deliberately NOT INFRASTRUCTURE: it is a writer, not a lock "
        "primitive, so a future skills path through it must show up as a "
        "reclassification in review.",
    ),
    ("context/migrate.py", "migrate_one", 0, "canonical_sidecar_lock"): (
        EXEMPT,
        "",
        "flat→dir layout migration. Skills have no flat layout (see "
        "_detect_source_scope), so this never covers a skills canonical.",
    ),
    ("context/migrate.py", "adopt_flat_to_dir", 0, "canonical_sidecar_lock"): (
        EXEMPT,
        "",
        "The adopt half of the same flat→dir path; flat-only, so never skills.",
    ),
    ("context/mcp_servers.py", "generate_all_mcp_servers", 0, "_file_lock"): (
        EXEMPT,
        "",
        "mcp_servers domain — a .mcp.json target, no directory artifact and no swap protocol.",
    ),
    ("context/migrate.py", "_acquire_pair_lock", 0, "_file_lock"): (
        EXEMPT,
        "",
        "Path-keyed pair lock, now used only by mcp_servers_copy (skills and "
        "the artifact kinds moved to name-keyed acquire_canonical_locks in "
        "PR-B2a). Same-path degenerate branch.",
    ),
    ("context/migrate.py", "_acquire_pair_lock", 1, "_file_lock"): (
        EXEMPT,
        "",
        "Two-path branch of the same mcp_servers_copy pair lock.",
    ),
    ("context/migrate.py", "_acquire_pair_lock", 2, "_file_lock"): (
        EXEMPT,
        "",
        "Second lock of that same two-path branch.",
    ),
    ("web/routes/_atomic_kind.py", "create_artifact._create_locked", 0, "canonical_sidecar_lock"): (
        EXEMPT,
        "",
        "Parametrized agents/commands CRUD (skills have their own router).",
    ),
    ("web/routes/_atomic_kind.py", "update_artifact._update_locked", 0, "canonical_sidecar_lock"): (
        EXEMPT,
        "",
        "Same agents/commands router.",
    ),
    ("web/routes/_atomic_kind.py", "delete_artifact._delete_locked", 0, "canonical_sidecar_lock"): (
        EXEMPT,
        "",
        "Same agents/commands router.",
    ),
    ("web/routes/context_versions.py", "create_artifact_version", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "Version WRITES are refused for skills before the lock (ADR-0030 §10 "
        "read-only surface, PR-G3), so this C0 only ever covers agents/commands.",
    ),
    ("web/routes/context_versions.py", "promote_artifact_label", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "Same read-only-for-skills gate as create.",
    ),
    ("web/routes/context_versions.py", "delete_artifact_label", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "Same read-only-for-skills gate as create.",
    ),
    ("cli/context_cmd.py", "version_create_cmd", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "CLI twin of the version write surface; skills refused before the lock.",
    ),
    ("cli/context_cmd.py", "version_promote_cmd", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "Same; skills refused before the lock.",
    ),
    ("cli/context_cmd.py", "version_delete_label_cmd", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "Same; skills refused before the lock.",
    ),
    ("server/tools/context.py", "mem_context_version", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "MCP twin of the version write surface; skills refused before the lock.",
    ),
    ("server/tools/context.py", "mem_context_promote", 0, "versioning_op_locked"): (
        EXEMPT,
        "",
        "MCP promote; skills refused before the lock.",
    ),
    ("server/tools/context.py", "mem_context_promote", 1, "versioning_op_locked"): (
        EXEMPT,
        "",
        "MCP delete-label, which shares the promote handler; same gate.",
    ),
    # ── NON_C0 — a sidecar lock, but not a canonical NAME lock ───────────
    ("context/versioning.py", "create_version", 0, "_file_lock"): (
        NON_C0,
        "",
        "C1: the versions.json sidecar, taken UNDER C0 by ADR-0030 §6 order.",
    ),
    ("context/versioning.py", "create_tree_version", 0, "_file_lock"): (
        NON_C0,
        "",
        "C1 versions.json for the tree store; the caller already holds C0.",
    ),
    ("context/versioning.py", "promote_label", 0, "_file_lock"): (NON_C0, "", "C1 versions.json."),
    ("context/versioning.py", "delete_label", 0, "_file_lock"): (NON_C0, "", "C1 versions.json."),
    ("context/lockfile.py", "Lockfile.upsert_entry", 0, "_file_lock"): (
        NON_C0,
        "",
        "The project lock.json, one file per project — not per artifact name.",
    ),
    ("context/lockfile.py", "Lockfile.remove_entry", 0, "_file_lock"): (
        NON_C0,
        "",
        "Same project lock.json.",
    ),
    ("context/projects.py", "KnownProjectsStore.add_with_status", 0, "_file_lock"): (
        NON_C0,
        "",
        "The known-projects registry file.",
    ),
    ("context/projects.py", "KnownProjectsStore.remove_by_scope_id", 0, "_file_lock"): (
        NON_C0,
        "",
        "Same registry file.",
    ),
    ("context/projects.py", "KnownProjectsStore.update_entry_by_scope_id", 0, "_file_lock"): (
        NON_C0,
        "",
        "Same registry file.",
    ),
    ("context/settings.py", "generate_all_settings", 0, "_file_lock"): (
        NON_C0,
        "",
        "Runtime settings.json targets — settings are not canonical artifacts.",
    ),
    ("context/settings_copy.py", "apply_hook_copy", 0, "_file_lock"): (
        NON_C0,
        "",
        "Hook-copy settings targets (same settings domain).",
    ),
    ("context/settings_migrate.py", "apply_migration", 0, "_file_lock"): (
        NON_C0,
        "",
        "Settings tier migration (same settings domain).",
    ),
    ("web/routes/settings_sync.py", "_locked_cas_write", 0, "_file_lock"): (
        NON_C0,
        "",
        "Settings CAS write on the web side.",
    ),
    ("config.py", "_config_write_lock", 0, "_file_lock"): (
        NON_C0,
        "",
        "The user config.json.",
    ),
    ("cli/memory.py", "_add", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory FILE lock (L2) — the memory domain, not context artifacts.",
    ),
    ("cli/memory_doctor_cmd.py", "_apply_fix", 0, "_file_lock"): (
        NON_C0,
        "",
        "Memory index file.",
    ),
    ("cli/review_cmd.py", "_decide", 0, "async_file_lock"): (NON_C0, "", "Memory file (L2)."),
    ("indexing/engine.py", "IndexEngine._index_file_locked", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Per-memory-file indexing lock.",
    ),
    ("server/tools/memory_crud.py", "_locked_chunk", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory file (L2).",
    ),
    ("server/tools/memory_crud.py", "_mem_add_core", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory file (L2).",
    ),
    ("server/tools/memory_crud.py", "mem_delete", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory file (L2).",
    ),
    ("server/tools/memory_crud.py", "mem_batch_add", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory file (L2).",
    ),
    ("tools/memory_mutation.py", "locked_source_chunk", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory file (L2).",
    ),
    ("web/routes/system.py", "add_memory", 0, "async_file_lock"): (
        NON_C0,
        "",
        "Memory file (L2) on the web side.",
    ),
    # ── INFRASTRUCTURE — the lock-composition helpers themselves ─────────
    ("context/_canonical_txn.py", "canonical_sidecar_lock", 0, "_file_lock"): (
        INFRASTRUCTURE,
        "",
        "THE primitive every wrapper funnels through. An intra-function "
        "analysis here cannot know whether a given caller targets skills, so "
        "treating this one line as 'the' acquisition site would make every "
        "wrapper-based writer invisible.",
    ),
    ("context/_canonical_txn.py", "acquire_canonical_locks", 0, "_file_lock"): (
        INFRASTRUCTURE,
        "",
        "The multi-lock loop inside the same module.",
    ),
    ("context/_canonical_txn.py", "canonical_lock_shared_budget", 0, "canonical_sidecar_lock"): (
        INFRASTRUCTURE,
        "",
        "Wrapper-to-wrapper delegation.",
    ),
    ("context/_canonical_txn.py", "versioning_op_locked", 0, "canonical_sidecar_lock"): (
        INFRASTRUCTURE,
        "",
        "Wrapper-to-wrapper delegation.",
    ),
}

#: The ten sites PR-G4a-3b was written to fix, plus the four G4a-3a already
#: wired. Discovery must be a SUPERSET of this — the list is a floor, never the
#: scope (#1866). Its own history is the argument: the design note's by-surface
#: census omitted ``copy_skill`` for three revisions.
KNOWN_SKILLS_C0_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("web/routes/context_skills.py", "create_skill._create_locked"),
        ("web/routes/context_skills.py", "update_skill._update_locked"),
        ("web/routes/context_skills.py", "delete_skill._delete_locked"),
        ("context/install.py", "_install_asset"),
        ("context/install.py", "_apply_update"),
        ("context/install.py", "_apply_pinned_install"),
        ("context/transfer.py", "transfer_artifact"),
        ("context/_validation_seed.py", "seed_adr0026_validation_states"),
        ("context/skills.py", "copy_skill"),
        ("context/skills.py", "generate_all_skills"),
        ("context/skills.py", "extract_skills_to_canonical"),
        ("context/pull_apply.py", "_commit_skills"),
    }
)


def _enter_context_stack(module_tree: ast.AST, site: _Site) -> str | None:
    """``<stack>`` when the acquisition is ``<stack>.enter_context(<acquisition>)``."""
    for node in ast.walk(module_tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "enter_context"
            and isinstance(node.func.value, ast.Name)
            and any(sub is site.node for arg in node.args for sub in ast.walk(arg))
        ):
            return node.func.value.id
    return None


def _locked_body(module_tree: ast.AST, site: _Site) -> list[ast.stmt] | None:
    """The body this acquisition guards.

    Two shapes, because the tree has two:

    * ``with <acquisition>:`` — the ``with`` body, the common case.
    * ``<stack>.enter_context(<acquisition>)`` — an ``ExitStack``, where the
      guarded region is whatever block the stack is scoped to. Preference order
      is ``with <stack>:`` (the push/import paths build the stack, then enter
      it) and otherwise ``with ExitStack() as <stack>:`` (the batch push, where
      the stack IS the ``with``). Resolving this rather than declaring the shape
      unsupported matters: those three sites are the ones that hold N locks at
      once, i.e. the ones whose ordering is hardest to eyeball.
    """
    for node in ast.walk(module_tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            for sub in ast.walk(item.context_expr):
                if sub is site.node:
                    return node.body

    stack_name = _enter_context_stack(module_tree, site)
    if stack_name is None:
        return None
    binder: list[ast.stmt] | None = None
    for node in ast.walk(module_tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        for item in node.items:
            if isinstance(item.context_expr, ast.Name) and item.context_expr.id == stack_name:
                return node.body
            if (
                isinstance(item.optional_vars, ast.Name)
                and item.optional_vars.id == stack_name
                and any(sub is site.node for sub in ast.walk(node))
            ):
                binder = node.body
    return binder


def _leading_calls(body: list[ast.stmt]) -> set[str]:
    """Callee names reachable as the FIRST executable step of *body*.

    A ``try:`` or a ``for``/``async for`` that leads the body is descended
    into: both are how the shipping recovery sites spell "run the prelude
    first" — the import path wraps it to convert the refusal into a typed skip,
    and the per-destination paths loop over their destinations. Anything else
    (an assignment, an ``if``, a probe) ends the walk, which is the point: those
    are exactly the pre-recovery reads §10 forbids.
    """
    names: set[str] = set()
    while body:
        head = body[0]
        if isinstance(head, ast.Expr):
            name = _callee_name(head.value.func) if isinstance(head.value, ast.Call) else None
            if name:
                names.add(name)
            return names
        if isinstance(head, ast.Try):
            body = head.body
            continue
        if isinstance(head, (ast.For, ast.AsyncFor)):
            body = head.body
            continue
        return names
    return names


def _prelude_precedes_mutators(body: list[ast.stmt]) -> bool:
    """Whether a prelude call comes before any mutator in *body*'s statement order."""
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node.func)
        if name in _PRELUDES:
            return True
        if name in _MUTATORS:
            return False
    return False


def test_every_c0_site_is_classified() -> None:
    """Discovery is the contract; the registry only explains what it found."""
    sites = discover_sites()
    unclassified = [
        f"{s.module}:{s.lineno} {s.qualname} #{s.occurrence} {s.callee}"
        for s in sites
        if s.key not in C0_SITES
    ]
    assert not unclassified, (
        "unclassified canonical-lock acquisition — classify it in C0_SITES with a "
        "written reason. A skills-capable C0 must ALSO run "
        "skills.run_swap_prelude first (ADR-0030 §10):\n  " + "\n  ".join(unclassified)
    )
    stale = sorted(set(C0_SITES) - {s.key for s in sites})
    assert not stale, f"C0_SITES rows no longer present in the tree: {stale}"


def test_classifications_are_from_the_closed_set_and_explained() -> None:
    for key, (classification, mode, why) in C0_SITES.items():
        assert classification in _CLASSIFICATIONS, f"{key}: unknown classification"
        assert why.strip(), f"{key}: classification with no written reason"
        if classification == RUNS_SWAP_PRELUDE:
            assert mode in {"first", "before_mutators"}, f"{key}: bad dominance mode {mode!r}"
        else:
            assert mode == "", f"{key}: dominance mode only applies to {RUNS_SWAP_PRELUDE}"


def test_registry_keys_are_unique() -> None:
    """A dict cannot hold duplicates — this pins that the KEY can distinguish.

    ``server/tools/context.py::mem_context_promote`` takes ``versioning_op_locked``
    twice; a callee-only or qualname-only key would silently merge them and
    classify one site with the other's reasoning.
    """
    sites = discover_sites()
    assert len(sites) == len({s.key for s in sites}), "two acquisitions share one registry key"


def test_skills_c0_sites_run_the_prelude_first() -> None:
    """The §10 ordering rule, enforced on the sites the registry calls skills-capable."""
    sites, trees = _discover()
    offenders: list[str] = []
    for site in sites:
        classification, mode, _why = C0_SITES.get(site.key, (None, "", ""))
        if classification != RUNS_SWAP_PRELUDE:
            continue
        body = _locked_body(trees[site.module], site)
        if body is None:
            offenders.append(f"{site.module}:{site.lineno} — acquisition is not a `with` item")
            continue
        if mode == "first":
            if not (_leading_calls(body) & _PRELUDES):
                offenders.append(
                    f"{site.module}:{site.lineno} ({site.qualname}) — the locked body does "
                    "not START with a prelude call"
                )
        elif not _prelude_precedes_mutators(body):
            offenders.append(
                f"{site.module}:{site.lineno} ({site.qualname}) — a mutator runs before the prelude"
            )
    assert not offenders, (
        "ADR-0030 §10: a C0 holder over a skills canonical must run "
        "skills.run_swap_prelude BEFORE any in-lock re-check or write — a probe that "
        "reads the pre-recovery tree can refuse or overwrite an artifact recovery was "
        "about to restore:\n  " + "\n  ".join(offenders)
    )


def test_known_gap_sites_are_all_covered() -> None:
    """Discovery must be a superset of the hand-written table, not equal to it."""
    found = {
        (s.module, s.qualname)
        for s in discover_sites()
        if C0_SITES.get(s.key, ("", "", ""))[0] == RUNS_SWAP_PRELUDE
    }
    missing = sorted(KNOWN_SKILLS_C0_SITES - found)
    assert not missing, (
        "a known skills C0 holder is no longer discovered or no longer classified "
        f"RUNS_SWAP_PRELUDE — the guard has gone blind to it: {missing}"
    )


# ── Injected-failure pins: what the guard would CATCH ────────────────────
#
# Every one of these is a shape that shipped, or nearly shipped, as a real
# defect: the callable-offload acquisition (the wrapper handed to an executor),
# the loop-variable lock path, a prelude that was never called, and a prelude
# demoted below an in-lock re-check. Asserting the guard's positive result on
# the tree cannot distinguish "no violations" from "detects nothing".

_SYNTHETIC_CALL_FORM = """
def new_writer(root, name):
    with canonical_sidecar_lock(root, name):
        write(root / name)
"""

_SYNTHETIC_OFFLOAD_FORM = """
async def new_route(root, name):
    await asyncio.to_thread(versioning_op_locked, root / name, op=_do)
"""

_SYNTHETIC_LOOP_VAR_FORM = """
def new_batch(dsts):
    with ExitStack() as stack:
        for lock_path in sorted({_lock_path_for(d) for d in dsts}):
            stack.enter_context(_file_lock(lock_path))
        promote(dsts)
"""

_SYNTHETIC_NO_PRELUDE = """
def writer(root, name):
    with canonical_sidecar_lock(root, name):
        _promote_staging(stage, root / name)
"""

_SYNTHETIC_PRELUDE_AFTER_RECHECK = """
def writer(root, name):
    with canonical_sidecar_lock(root, name):
        if (root / name).exists():
            return "already exists"
        run_swap_prelude(root, name, kind="skills")
        _promote_staging(stage, root / name)
"""


def test_guard_detects_every_acquisition_form() -> None:
    for label, source in (
        ("wrapper call", _SYNTHETIC_CALL_FORM),
        ("wrapper passed to an executor", _SYNTHETIC_OFFLOAD_FORM),
        ("raw primitive on a loop-variable path", _SYNTHETIC_LOOP_VAR_FORM),
    ):
        sites, _tree = sites_in_source(source, "synthetic.py")
        assert sites, f"discovery missed a {label} acquisition"
        assert all(s.key not in C0_SITES for s in sites), (
            "the synthetic module must be UNclassified — that is what makes a new "
            "acquisition fail the build"
        )


def test_dominance_rejects_a_missing_or_demoted_prelude() -> None:
    """`first` mode fails both when the prelude is absent and when it is merely late."""
    for label, source in (
        ("no prelude at all", _SYNTHETIC_NO_PRELUDE),
        ("prelude below an in-lock re-check", _SYNTHETIC_PRELUDE_AFTER_RECHECK),
    ):
        sites, tree = sites_in_source(source, "synthetic.py")
        body = _locked_body(tree, sites[0])
        assert body is not None
        assert not (_leading_calls(body) & _PRELUDES), f"dominance accepted: {label}"

    # …and passes the shape the real sites use.
    sites, tree = sites_in_source(
        _SYNTHETIC_PRELUDE_AFTER_RECHECK.replace(
            '        if (root / name).exists():\n            return "already exists"\n', ""
        ),
        "synthetic.py",
    )
    body = _locked_body(tree, sites[0])
    assert body is not None and (_leading_calls(body) & _PRELUDES)


if __name__ == "__main__":  # pragma: no cover - developer aid
    for site in discover_sites():
        print(f"{site.module}:{site.lineno} {site.qualname} #{site.occurrence} {site.callee}")
