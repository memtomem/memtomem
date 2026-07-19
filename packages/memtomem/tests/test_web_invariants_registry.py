"""AST-walking registry for web write-boundary invariants.

Pins two cross-cutting invariants per RFC #787 stage 1:

1. **CSRF / Origin / Host coverage.** Every unsafe-method handler
   (``POST``/``PATCH``/``PUT``/``DELETE``) under ``web/routes/`` must be
   classified into ``_CSRF_PROTECTED`` (the middleware-covered surface,
   which is the default for new routes) or ``_CSRF_EXEMPT`` (with a
   one-line justification). Unclassified routes fail. The registry is
   the AST contract — adding a new unsafe-method handler without
   updating this file fails the test, which forces the reviewer to
   acknowledge the new surface.

2. **Redaction guard coverage.** Every web-route handler that writes
   user-supplied content to LTM must scan it (``enforce_write_guard``
   directly, an engine wrapper like ``scan_mcp_server_text`` that self-
   refuses, or the ``scan_text_content`` wrapper *and* a refusal on the
   result — the guard also follows one level of local-module delegation;
   see ``_function_scans_and_refuses``), or be in
   ``_REDACTION_EXEMPT``. The list of handlers requiring redaction is
   explicit (``_REDACTION_PROTECTED``) — drift is caught by the
   classification test, which fails when an unsafe-method route is
   added but not classified for redaction either.

Pattern lineage: ``feedback_ast_architectural_guard_pattern.md``
(unclassified-fails registry). The test mirrors the
``tests/test_web_csp_vendor.py`` shape — small, AST-only, no app boot.
"""

from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path

import pytest

from .helpers import CTX_GATEWAY_JS_FILES

ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "routes"
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"

_UNSAFE_DECORATOR_METHODS = frozenset({"post", "patch", "put", "delete"})


# --- CSRF registry ---------------------------------------------------------
#
# Every entry is ``"<module>.<function>"`` matching the AST qualified name.
# CSRF middleware covers the entire ``/api/*`` unsafe-method surface
# uniformly, so the *default* classification for a new handler is
# ``_CSRF_PROTECTED``. ``_CSRF_EXEMPT`` is reserved for handlers that
# intentionally bypass the token gate (e.g. the bootstrap endpoint
# itself, which has its own carve-out in the middleware).
#
# When you add a new ``@router.{post,patch,put,delete}`` handler, append
# its qualified name to ``_CSRF_PROTECTED`` (or ``_CSRF_EXEMPT`` with a
# justification comment). The test fails noisily until you do.

_CSRF_PROTECTED: frozenset[str] = frozenset(
    {
        "chunks.delete_chunk",
        "chunks.edit_chunk",
        "chunks.update_chunk_tags",
        "context_agents.create_agent",
        "context_agents.delete_agent",
        "context_agents.import_agent",
        "context_agents.import_agents",
        "context_agents.sync_agents",
        "context_agents.update_agent",
        "context_commands.create_command",
        "context_commands.delete_command",
        "context_commands.import_command",
        "context_commands.import_commands",
        "context_commands.sync_commands",
        "context_commands.update_command",
        "context_gateway.context_pull_apply",
        "context_mcp_servers.create_mcp_server",
        "context_mutations.install_asset",
        "context_mutations.update_asset",
        "context_mcp_servers.delete_mcp_server",
        "context_mcp_servers.patch_mcp_server",
        "context_mcp_servers.sync_mcp_servers",
        "context_mcp_servers.update_mcp_server",
        "context_projects.add_known_project",
        "context_projects.delete_known_project",
        "context_projects.update_known_project",
        "context_skills.create_skill",
        "context_skills.delete_skill",
        "context_skills.import_skill",
        "context_skills.import_skill_to_user",
        "context_skills.import_skills",
        "context_skills.sync_skills",
        "context_skills.update_skill",
        "context_sync_all.sync_all_context",
        "context_sync_all.sync_all_projects_context",
        "context_transfer.transfer_context_artifact",
        "context_versions.create_artifact_version",
        "context_versions.delete_artifact_label",
        "context_versions.enable_artifact_versioning",
        "context_versions.promote_artifact_label",
        "decay.expire_old_chunks",
        "dedup.merge_duplicates",
        "export.import_memories",
        "quality.promote_quality_case",
        "quality.run_quality_replay",
        "scratch.delete_scratch",
        "scratch.promote_scratch",
        "scratch.set_scratch",
        "search_runs.save_search_feedback",
        "settings_sync.apply_settings_sync",
        "settings_sync.copy_hook_to_project",
        "settings_sync.delete_target_rule",
        "settings_sync.promote_target_rule",
        "settings_sync.resolve_conflict",
        "sources.delete_source",
        "sources.regenerate_summaries",
        "system.add_memory",
        "system.add_memory_dir",
        "system.embed_text",
        "system.health_active",
        "system.index_stream",
        "system.open_memory_dir",
        "system.patch_config",
        "system.preview_namespace",
        "system.rebuild_fts",
        "system.reindex_all",
        "system.remove_memory_dir",
        "system.reset_all",
        "system.reset_embedding",
        "system.save_config",
        "system.trigger_index",
        "system.upload_files",
        "tags.delete_tag",
        "tags.merge_tags",
        "tags.rename_tag",
        "tags.run_auto_tag",
        "watchdog.watchdog_run_now",
        "wiki_mutations.commit_wiki",
        "wiki_mutations.edit_wiki_canonical",
        "wiki_mutations.edit_wiki_override",
        "wiki_mutations.seed_wiki_override",
    }
)

_CSRF_EXEMPT: dict[str, str] = {
    # No unsafe-method exemptions yet. The token-bootstrap endpoint
    # ``GET /api/session`` is exempt at the middleware layer (see
    # ``CSRFGuardMiddleware._TOKEN_BOOTSTRAP_PATH``) but is a GET so it
    # never appears in the AST walk. Future entries belong here with a
    # one-line justification — keep them rare, the gate is uniform on
    # purpose.
}


# --- Redaction registry ----------------------------------------------------
#
# Handlers that ingest user-supplied content and persist it to LTM. Each
# *must* both scan (``enforce_write_guard`` / ``scan_text_content``) and
# refuse on a hit (``raise_if_privacy_blocked`` / ``raise_or_collect`` /
# an inline ``.decision`` check that raises) in its body — see
# ``_function_scans_and_refuses``. New content-writing handlers either
# join this list (and the body must scan-and-refuse) or join
# ``_REDACTION_EXEMPT`` with a justification.

_REDACTION_PROTECTED: frozenset[str] = frozenset(
    {
        "chunks.edit_chunk",
        # The canonical editors write free-form user bytes into the
        # git-tracked project_shared canonical — write-time Gate A scan
        # in-route, before atomic_write_text (#1509).
        "context_agents.create_agent",
        "context_agents.update_agent",
        "context_commands.create_command",
        "context_commands.update_command",
        "context_skills.create_skill",
        "context_skills.update_skill",
        # The mcp-servers editors write free-form user bytes into the
        # git-tracked project_shared canonical (.mcp.json fan-out). Write-time
        # Gate A scan in-route via ``scan_mcp_server_text`` (create in-body;
        # update/patch through the ``_update_mcp_server_impl`` delegate) — the
        # AST guard recognizes the wrapper + one-level delegation (#1579).
        "context_mcp_servers.create_mcp_server",
        "context_mcp_servers.patch_mcp_server",
        "context_mcp_servers.update_mcp_server",
        "scratch.promote_scratch",
        # Promote appends a private-tier hook rule (free-form command
        # strings + a free-string event key) into the git-tracked shared
        # canonical — Gate A fragment scan in-route (#1247).
        "settings_sync.promote_target_rule",
        "system.add_memory",
        "system.upload_files",
    }
)

_REDACTION_EXEMPT: dict[str, str] = {
    "system.health_active": "active dependency checks only; no user content is persisted",
    "system.index_stream": (
        "path/options only; discovered file content is guarded by the indexing engine"
    ),
    "system.preview_namespace": ("read-only path/options preview; no file content is persisted"),
    # Delete-only / no body content.
    "chunks.delete_chunk": "delete-only, no payload",
    "context_agents.delete_agent": "delete-only, no payload",
    "context_commands.delete_command": "delete-only, no payload",
    "context_projects.delete_known_project": "delete-only, no payload",
    "context_skills.delete_skill": "delete-only, no payload",
    "scratch.delete_scratch": "delete-only, no payload",
    "search_runs.save_search_feedback": (
        "body is IDs echoed from prior server output plus a closed-vocabulary "
        "judgment validated in storage; no free-text content is persisted"
    ),
    # Quality Lab (#1802 PR-5). Replay is read-only (no persistence at all).
    # Promote copies query_text + labels from an already-observed run in
    # query_history (validated at search time); the request body carries only a
    # run_id (ID), an optional short name label (like a tag/namespace name), and
    # a bool — no free-text content ingress.
    "quality.run_quality_replay": "read-only replay; no content is persisted",
    "quality.promote_quality_case": (
        "body is a run_id plus an optional short name label and a bool; the "
        "persisted case content is copied from an already-validated observed run, "
        "not free text from this request"
    ),
    "sources.delete_source": "delete-only, no payload",
    "sources.regenerate_summaries": (
        "no user-supplied content — body is empty; trigger only "
        "rewrites cached LLM summaries from chunks already validated "
        "at index time"
    ),
    # Structured artifact import/sync (skills/commands/agents) — redaction
    # lives in the ``memtomem.context`` ingest path (import Gate A / sync
    # tree scan); the HTTP layer validates the schema only. The create/update
    # editors moved to ``_REDACTION_PROTECTED`` with an in-route Gate A scan
    # (#1509).
    "context_agents.import_agent": "structured artifact import; redaction at context-ingest layer",
    "context_agents.import_agents": "bulk structured artifact import; see above",
    "context_agents.sync_agents": "filesystem-driven sync; redaction "
    "happens at file-write time inside the indexer",
    "context_commands.import_command": "structured artifact import; see above",
    "context_commands.import_commands": "bulk structured artifact import",
    "context_commands.sync_commands": "filesystem-driven sync; see above",
    # The mcp-servers create/update/patch editors moved to _REDACTION_PROTECTED
    # (#1579): they scan in-route via ``scan_mcp_server_text`` and the AST guard
    # now recognizes the wrapper + one-level ``_update_mcp_server_impl``
    # delegation. Only the payload-free operations stay EXEMPT here.
    "context_mcp_servers.delete_mcp_server": "delete-only, no payload",
    "context_mcp_servers.sync_mcp_servers": (
        "filesystem-driven fan-out of already-canonical bytes; Gate A runs "
        "in-engine on the canonical tree, no HTTP-layer content payload"
    ),
    # Wiki install/update (ADR-0008 PR-E E-3): snapshots existing wiki canonical
    # bytes into <project>/.memtomem/; no user payload (update body is just
    # ``force``). Gate A runs in-engine on the wiki source tree before the copy.
    "context_mutations.install_asset": (
        "no content payload — snapshots existing wiki canonical into the project; "
        "Gate A runs in-engine on the wiki source tree before copy"
    ),
    "context_mutations.update_asset": (
        "no free-form content payload (body is just ``force``) — refreshes the "
        "project snapshot from wiki canonical; Gate A runs in-engine pre-copy"
    ),
    "context_skills.import_skill": "structured artifact import",
    "context_skills.import_skill_to_user": "structured artifact import (project runtime → user library)",
    "context_skills.import_skills": "bulk structured artifact import",
    "context_skills.sync_skills": "filesystem-driven sync",
    "context_sync_all.sync_all_context": "filesystem-driven sync (per-type "
    "cores under one lock); no content payload",
    "context_sync_all.sync_all_projects_context": "filesystem-driven sync "
    "(same cores, one lock window per project); no content payload",
    "context_transfer.transfer_context_artifact": (
        "no content payload — moves/copies existing canonical bytes between "
        "stores; project_shared landings run Gate A in-engine on the staged tree"
    ),
    "context_gateway.context_pull_apply": (
        "no content payload — pulls existing runtime bytes into the Store; Gate A "
        "runs in-engine (prepare_pull over the captured tree), project_shared "
        "hard-refuses, and every surfaced reason is display-sanitized"
    ),
    "context_projects.add_known_project": "path/label only, no prose",
    "context_projects.update_known_project": "label/enabled update, no prose",
    # Versioning: the snapshot bytes are the already-canonical working file,
    # re-scanned at deploy time on the frozen versions/vN.md (ADR-0022 Gate A);
    # promote/delete only move a label pointer in versions.json. No LTM-bound
    # prose ingress at this layer.
    "context_versions.create_artifact_version": (
        "freezes already-canonical bytes; redaction at sync-time Gate A on the frozen version"
    ),
    "context_versions.promote_artifact_label": "moves a label pointer in versions.json; no prose",
    "context_versions.delete_artifact_label": "drops a label pointer in versions.json; no prose",
    "context_versions.enable_artifact_versioning": (
        "byte-identical flat→dir rename of an already-canonical file; no new "
        "content ingress (Gate A still applies at sync-time on frozen versions)"
    ),
    # Wiki override-seed renders the existing wiki canonical into
    # overrides/<vendor>.<ext>; no user payload. Gate A guards the wiki→project
    # install direction (ADR-0008 Inv 1), not wiki→wiki seeding.
    "wiki_mutations.seed_wiki_override": (
        "no content payload — renders existing wiki canonical into overrides/; "
        "Gate A guards wiki→project install, not wiki→wiki seeding"
    ),
    # Wiki override editor (ADR-0027 Editor-A) DOES take a user payload, but its
    # privacy posture is a soft, non-blocking warning (D-E): privacy.scan runs
    # and returns a count, the write is never refused. _REDACTION_PROTECTED means
    # "refuse on hit" (the AST guard requires those handlers to call
    # enforce_write_guard); a non-refusing handler there would be a coverage
    # false-pass. The wiki is a single-curator host-global store with no push
    # remote, so a hard gate is over-reach — see ADR-0027 §8 / §D-E.
    "wiki_mutations.edit_wiki_override": (
        "user payload, but privacy is a soft non-blocking warning (ADR-0027 §D-E) — "
        "the write is never refused, so it is not _REDACTION_PROTECTED (which means "
        "refuse-on-hit); single-curator host-global store, no push remote"
    ),
    # Wiki canonical editor (ADR-0027 Editor-B): same posture as the override
    # editor above — a user payload with a soft, non-blocking privacy.scan
    # warning (D-E). The write is never refused, so it is _REDACTION_EXEMPT, not
    # _REDACTION_PROTECTED (which means refuse-on-hit and would be a coverage
    # false-pass for a deliberately non-refusing handler).
    "wiki_mutations.edit_wiki_canonical": (
        "user payload, but privacy is a soft non-blocking warning (ADR-0027 §D-E) — "
        "the write is never refused, so it is not _REDACTION_PROTECTED (which means "
        "refuse-on-hit); single-curator host-global store, no push remote"
    ),
    # Wiki commit affordance (ADR-0027 §3): commits already-saved+scanned artifact
    # bytes; the only new user text is the commit *message*, which gets the same
    # soft, non-blocking privacy.scan (a returned count, never a refusal). A
    # non-refusing handler in _REDACTION_PROTECTED would be a coverage false-pass
    # (that set means refuse-on-hit). Valve, not gate — ADR-0027 §8 / §D-E.
    "wiki_mutations.commit_wiki": (
        "control-plane git commit of already-saved+scanned bytes; the commit "
        "message gets a soft non-blocking privacy.scan (ADR-0027 §D-E) but the "
        "commit is never refused, so it is not _REDACTION_PROTECTED (refuse-on-hit)"
    ),
    # Tag mutations: short labels, separate validation at ingest.
    "chunks.update_chunk_tags": "tags are short labels; redaction not applicable to tag strings",
    "tags.run_auto_tag": "auto-tag operates on already-stored chunks; "
    "redaction already applied at original write time",
    "tags.rename_tag": "tags are short labels; rewrites existing chunk metadata only",
    "tags.delete_tag": "tags are short labels; rewrites existing chunk metadata only",
    "tags.merge_tags": "tags are short labels; rewrites existing chunk metadata only",
    # Maintenance / control plane: no user-supplied content.
    "decay.expire_old_chunks": "control plane; operates on existing chunks",
    "dedup.merge_duplicates": "operates on existing chunks; redaction "
    "already applied at original write time",
    "scratch.set_scratch": "scratch is local-only ephemeral note storage "
    "outside the LTM trust boundary; redaction not applicable",
    "settings_sync.apply_settings_sync": "structured settings merge; no free-form prose",
    "settings_sync.copy_hook_to_project": (
        "no free-form content payload — copies an existing canonical hook "
        "entry between projects; the engine's Gate A fragment scan runs for "
        "every destination tier (the canonical leg is always git-tracked)"
    ),
    "settings_sync.delete_target_rule": "structured settings hook rule deletion; no free-form prose",
    "settings_sync.resolve_conflict": "structured settings conflict resolution; no free-form prose",
    "system.embed_text": "ephemeral compute; no persistence",
    "system.add_memory_dir": "path-only payload, no prose",
    "system.remove_memory_dir": "path-only payload, no prose",
    "system.open_memory_dir": "path-only; opens local FS, no LTM write",
    "system.patch_config": "structured config payload, no free-form prose",
    "system.save_config": "structured config payload, no free-form prose",
    "system.reset_embedding": "control plane; no content payload",
    "system.reset_all": "control plane; destructive reset, no payload",
    "system.rebuild_fts": "control plane; no content payload",
    "system.trigger_index": "control plane; operates on existing files; "
    "redaction happens at file-ingest time inside the indexer",
    "system.reindex_all": "control plane; operates on existing files",
    # Bundle import is redaction-gated in the shared ``tools/export_import.py::
    # import_chunks`` path, not in this route handler body — so the AST walk
    # (which inspects handler bodies only) correctly leaves it exempt here.
    # ADR-0006 Axis F.3: a self-export carrying a valid local-provenance marker
    # round-trips unchanged (re-scan skipped); a foreign/unverified bundle is
    # scanned per-record (``privacy.enforce_write_guard``) and rejected on a
    # secret hit unless ``force_unsafe`` is passed.
    "export.import_memories": (
        "redaction enforced in the shared import_chunks() path (ADR-0006 F.3): "
        "self-exports with a valid provenance marker round-trip unchanged, foreign "
        "bundles are gated per-record; the guard is not in this handler body"
    ),
    "watchdog.watchdog_run_now": "control plane; no payload",
}


# --- AST walk --------------------------------------------------------------


def _iter_unsafe_route_handlers() -> list[tuple[str, str, str]]:
    """Return ``(module, function, http_method)`` for every unsafe handler.

    "Unsafe" here is the CSRF / RFC sense: ``POST``/``PATCH``/``PUT``/
    ``DELETE``. The walk is purely AST-based so the test never imports
    the FastAPI app — that keeps it fast and decouples it from runtime
    component wiring (storage, embedder, etc.). A function is reported
    once even if it has multiple ``@router`` decorators (e.g. legacy +
    new path), since the registry classifies the *handler*, not the URL.
    """
    handlers: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(ROUTES_DIR.glob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        module = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            for deco in node.decorator_list:
                if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
                    continue
                func = deco.func
                if not (
                    isinstance(func.value, ast.Name)
                    and func.value.id == "router"
                    and func.attr in _UNSAFE_DECORATOR_METHODS
                ):
                    continue
                key = (module, node.name)
                if key in seen:
                    continue
                seen.add(key)
                handlers.append((module, node.name, func.attr.upper()))
    return handlers


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _walk_own_scope(node: ast.AST):
    """Yield descendants of ``node`` in its OWN lexical scope.

    Unlike ``ast.walk``, this does not descend into nested
    function / lambda / class bodies — code inside an uncalled nested
    ``def`` must not count as the handler's scan or refusal (#1509
    re-review — Codex, dead nested-function reconstruction).
    """
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, _NESTED_SCOPES):
            yield from _walk_own_scope(child)


def _call_name(call: ast.Call) -> str | None:
    """The called function's bare or attribute name, or None."""
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _arg_names(call: ast.Call) -> set[str]:
    """Names passed as positional or keyword-value args of ``call``."""
    names: set[str] = set()
    for arg in call.args:
        if isinstance(arg, ast.Name):
            names.add(arg.id)
    for kw in call.keywords:
        if isinstance(kw.value, ast.Name):
            names.add(kw.value.id)
    return names


# The decision strings that mean "blocked" — the ``if`` test of an inline
# refusal must compare the scan's ``.decision`` against one of these, so an
# inverted ``if scan.decision == "allowed": raise`` is not mistaken for a
# refusal (#1509 re-review — Codex).
_BLOCKED_DECISIONS = frozenset({"blocked", "blocked_project_shared"})


# Engine scan wrappers that internally call ``enforce_write_guard`` and
# self-refuse (raise on a blocked decision) — recognized as a chokepoint,
# like a direct ``enforce_write_guard`` call. Explicit allow-list so an
# unrelated helper name cannot silently satisfy the invariant (#1579). A call
# to one of these names is trusted only when the name is soundly bound to the
# engine import (``_trusted_engine_wrappers``), never a rebinding / shadow.
_ENGINE_SCAN_WRAPPERS = frozenset({"scan_mcp_server_text"})
_ENGINE_WRAPPER_MODULE = "memtomem.context.mcp_servers"

# Local-module delegation targets the guard follows exactly one level deep
# (``update``/``patch`` → ``_update_mcp_server_impl``). An explicit allow-list,
# NOT "any sibling function": following any sibling that happens to scan would
# let a handler delegate the scan to unrelated content and write its own body
# unscanned, yet still pass (#1579 review — Codex). A new delegating handler
# must add its delegate here.
_DELEGATE_TARGETS = frozenset({"_update_mcp_server_impl"})


def _trusted_engine_wrappers(tree: ast.Module) -> set[str]:
    """``_ENGINE_SCAN_WRAPPERS`` names soundly bound to the trusted engine import.

    A wrapper name is trusted as the self-refusing chokepoint only when the
    module imports it *from* ``memtomem.context.mcp_servers`` (no alias) and
    nothing else binds it *at module scope*. Trust is the single canonical
    import; ANY other module-scope binding of the name drops it — a ``def`` /
    ``class`` (even nested in a top-level ``if`` / ``try`` / ``with``), an
    import from a different module, a ``from other import *`` wildcard that
    could shadow it, a ``match`` capture, or any Store (plain / annotated /
    augmented assignment, tuple/list destructuring, ``for`` / ``with`` target,
    walrus). We walk the module's OWN scope (``_walk_own_scope``, not
    ``ast.walk``) so a binding inside a top-level compound statement counts but
    one inside a nested function / class body does not — that is a different
    scope, whose trust boundary is code review + the runtime 422 tests.
    Name-only matching would let a shadow / rebinding masquerade as the trusted
    wrapper (#1579 review — Codex).
    """
    imported: set[str] = set()
    rebound: set[str] = set()
    for node in _walk_own_scope(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    # A wildcard from a non-canonical module could bind any
                    # wrapper name — we cannot prove it does not; fail closed.
                    if node.module != _ENGINE_WRAPPER_MODULE:
                        rebound |= set(_ENGINE_SCAN_WRAPPERS)
                    continue
                bound = alias.asname or alias.name
                if bound not in _ENGINE_SCAN_WRAPPERS:
                    continue
                if node.module == _ENGINE_WRAPPER_MODULE and alias.asname is None:
                    imported.add(bound)
                else:
                    rebound.add(bound)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound in _ENGINE_SCAN_WRAPPERS:
                    rebound.add(bound)
        elif isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef):
            if node.name in _ENGINE_SCAN_WRAPPERS:
                rebound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            if node.id in _ENGINE_SCAN_WRAPPERS:
                rebound.add(node.id)
        elif isinstance(node, ast.MatchAs | ast.MatchStar):
            if node.name in _ENGINE_SCAN_WRAPPERS:
                rebound.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest in _ENGINE_SCAN_WRAPPERS:
                rebound.add(node.rest)
        elif isinstance(node, ast.ExceptHandler):
            # ``except ... as scan_mcp_server_text`` binds the name (the binder
            # is ``ExceptHandler.name``, a str — not a ``Name`` Store node).
            if node.name in _ENGINE_SCAN_WRAPPERS:
                rebound.add(node.name)
    return imported - rebound


def _is_scan_decision(node: ast.AST, scan_vars: set[str]) -> bool:
    """True iff ``node`` is ``<scan_var>.decision`` for a tracked scan var."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "decision"
        and isinstance(node.value, ast.Name)
        and node.value.id in scan_vars
    )


def _is_blocked_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in _BLOCKED_DECISIONS


def _test_checks_blocked_decision(test: ast.AST, scan_vars: set[str]) -> bool:
    """True iff ``test`` is a *positive* blocked-decision comparison whose
    truthiness means "the scan is blocked".

    Only ``<scan_var>.decision == <blocked literal>`` (``Eq``) or
    ``<scan_var>.decision in (<...blocked...>)`` (``In``) qualify. Negated
    polarity (``!=`` / ``not in`` / ``not (...)``) is rejected: those raise on
    the *allowed* case and let blocked content fall through to the write
    (#1509 re-review — Codex). Matches the single real inline handler,
    ``settings_sync.promote_target_rule``; any fancier shape fails closed.
    """
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not _is_scan_decision(test.left, scan_vars):
        return False
    op, comparator = test.ops[0], test.comparators[0]
    if isinstance(op, ast.Eq):
        return _is_blocked_const(comparator)
    if isinstance(op, ast.In):
        return isinstance(comparator, ast.Tuple | ast.List | ast.Set) and any(
            _is_blocked_const(elt) for elt in comparator.elts
        )
    return False


def _contains_own_scope_raise(body: list[ast.stmt]) -> bool:
    """True iff ``body`` raises in its own scope (not inside a nested def)."""
    for stmt in body:
        if isinstance(stmt, ast.Raise):
            return True
        if not isinstance(stmt, _NESTED_SCOPES):
            if any(isinstance(s, ast.Raise) for s in _walk_own_scope(stmt)):
                return True
    return False


def _wrapper_scan_refused(func: ast.AST, scan_vars: set[str]) -> bool:
    """True iff ``func`` refuses on a ``scan_text_content`` result, in its own
    scope.

    The refusal must be *tied to the scan variable* AND reachable in the
    handler's own scope — an unrelated ``.decision`` check, an unrelated
    validation ``raise``, a refusal buried in an uncalled nested ``def``, or
    an inverted/else-branch shape does not count (#1509 re-review — Codex).
    Accepts either:

    * a call to ``raise_if_privacy_blocked`` / ``raise_or_collect`` whose
      args include a tracked scan var; or
    * an ``if`` whose test compares ``<scan_var>.decision`` against a blocked
      decision literal and whose *true branch* raises in its own scope — the
      ``settings_sync.promote_target_rule`` shape. (The blocked-literal and
      true-branch requirements reject ``if scan.decision == "allowed": raise``
      and a ``raise`` that only lives in the ``else``.)
    """
    for sub in _walk_own_scope(func):
        if isinstance(sub, ast.Call):
            if _call_name(sub) in ("raise_if_privacy_blocked", "raise_or_collect"):
                if _arg_names(sub) & scan_vars:
                    return True
        elif isinstance(sub, ast.If) and _test_checks_blocked_decision(sub.test, scan_vars):
            if _contains_own_scope_raise(sub.body):
                return True
    return False


def _sole_awaited_delegate(
    node: ast.AsyncFunctionDef | ast.FunctionDef, module_funcs: set[str]
) -> str | None:
    """The allow-listed delegate a pure trampoline handler forwards to, or None.

    Only a handler whose body is exactly ``return await <delegate>(...)`` (a
    leading docstring aside) trampolines — the real ``update_mcp_server`` /
    ``patch_mcp_server`` shape. A handler that does anything else in its own
    scope (an own write before/around the ``await``, an un-awaited call) is not
    a trampoline: its own-scope write could bypass the delegate's scan, so the
    delegate is not followed (#1579 review — Codex). The delegate must be a
    bare-name call to an ``_DELEGATE_TARGETS`` sibling defined in this module.
    """
    body = node.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # skip a docstring
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    value = body[0].value
    if not isinstance(value, ast.Await) or not isinstance(value.value, ast.Call):
        return None
    call = value.value
    name = _call_name(call)
    if isinstance(call.func, ast.Name) and name in _DELEGATE_TARGETS and name in module_funcs:
        return name
    return None


def _function_scans_and_refuses(module: str, function_name: str) -> bool:
    """Wrapper over :func:`_source_scans_and_refuses` reading a route module."""
    source = (ROUTES_DIR / f"{module}.py").read_text(encoding="utf-8")
    return _source_scans_and_refuses(source, function_name)


def _source_scans_and_refuses(
    source: str, function_name: str, *, _follow_delegation: bool = True
) -> bool:
    """True iff ``function_name`` in ``source`` scans user content — and, when
    it scans through the thin ``scan_text_content`` wrapper, refuses on the
    result *bound to that scan*.

    Two accepted scan shapes with different strengths:

    * ``privacy.enforce_write_guard(...)`` — the audited chokepoint. The call
      itself records the blocked outcome (``record_outcome=True``), so its
      presence is the meaningful boundary event; the handler's own refusal
      may legitimately be a non-raising skip (``system.upload_files`` records
      a per-file error and ``continue``s past the write). We do not second-
      guess the refusal shape for direct chokepoint callers.
    * ``scan_text_content(...)`` — a thin ``context.privacy_scan`` wrapper that
      only *returns* a ``FileScan``. Dropping the paired refusal keeps the scan
      but writes the blocked bytes anyway. So a wrapper-scanning handler must
      also refuse *on the scan variable* (``_wrapper_scan_refused``); an
      unrelated ``.decision`` / ``raise`` no longer satisfies the invariant
      (#1509 re-review — Codex).

    Scan detection, scan-variable tracking, and the refusal check all walk the
    handler's OWN scope only (``_walk_own_scope``) so an uncalled nested
    ``def`` cannot supply a phantom scan or refusal. **Enforced convention:**
    the scan result must be bound by a plain ``scan = scan_text_content(...)``
    assignment — tuple-unpacking / annotated / walrus targets are deliberately
    not tracked (no handler uses them; a future one that does fails this test
    closed, which forces either the simple-assignment convention or a guard
    update).
    """
    tree = ast.parse(source)
    # Sibling module-level functions — delegation targets we may follow one
    # level deep (Module.body only; nested defs and imported symbols excluded).
    module_funcs = {
        n.name for n in tree.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    # Engine wrapper names soundly bound to the trusted engine import (not a
    # shadow / rebinding) — the only bare names trusted as self-refusing.
    trusted_wrappers = _trusted_engine_wrappers(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name != function_name:
            continue
        has_chokepoint = False
        scan_vars: set[str] = set()
        for sub in _walk_own_scope(node):
            if isinstance(sub, ast.Call):
                name = _call_name(sub)
                f = sub.func
                # ``privacy.enforce_write_guard(...)`` (qualified) or bare
                # ``enforce_write_guard(...)`` — the audited chokepoint.
                if name == "enforce_write_guard" and (
                    isinstance(f, ast.Name)
                    or (
                        isinstance(f, ast.Attribute)
                        and isinstance(f.value, ast.Name)
                        and f.value.id == "privacy"
                    )
                ):
                    has_chokepoint = True
                # A bare-name engine scan wrapper that self-refuses internally
                # (``scan_mcp_server_text``) — trusted like the chokepoint, but
                # only when soundly bound to the engine import. A shadow / local
                # def / rebinding of the same name does NOT count (#1579).
                elif isinstance(f, ast.Name) and name in trusted_wrappers:
                    has_chokepoint = True
            elif isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Call):
                # Track ``scan = scan_text_content(...)`` target names (plain
                # Name targets only — see the convention note above).
                if _call_name(sub.value) == "scan_text_content":
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            scan_vars.add(target.id)
        if has_chokepoint:
            return True
        if scan_vars:
            return _wrapper_scan_refused(node, scan_vars)
        # Follow one level of local-module delegation, but ONLY for a pure
        # ``return await _update_mcp_server_impl(...)`` trampoline (``update`` /
        # ``patch``). A handler that also writes in its own scope is not a
        # trampoline — that own-scope write could bypass the delegate's scan
        # (#1579 review — Codex). Depth is bounded to 1 via the recursive
        # ``_follow_delegation=False`` — a scan two hops away is not seen.
        if _follow_delegation:
            delegate = _sole_awaited_delegate(node, module_funcs)
            if delegate is not None and _source_scans_and_refuses(
                source, delegate, _follow_delegation=False
            ):
                return True
        return False
    return False


# --- Tests -----------------------------------------------------------------


def test_csrf_classification_covers_every_unsafe_route() -> None:
    """Every unsafe-method route under ``web/routes/`` is classified."""
    handlers = _iter_unsafe_route_handlers()
    assert handlers, "AST walk found no @router.{post,patch,put,delete} handlers"

    seen = {f"{m}.{f}" for m, f, _ in handlers}
    classified = _CSRF_PROTECTED | _CSRF_EXEMPT.keys()

    unclassified = sorted(seen - classified)
    stale = sorted(classified - seen)

    if unclassified or stale:
        msg = []
        if unclassified:
            msg.append(
                "Unclassified unsafe-method routes (add to _CSRF_PROTECTED "
                "or _CSRF_EXEMPT in this file):\n  - " + "\n  - ".join(unclassified)
            )
        if stale:
            msg.append(
                "Stale entries in _CSRF_PROTECTED / _CSRF_EXEMPT (route was "
                "renamed or removed):\n  - " + "\n  - ".join(stale)
            )
        pytest.fail("\n\n".join(msg))


def test_preview_namespace_spa_uses_csrf_protected_post() -> None:
    """The local-path preview must not regress to a token-exempt GET."""
    settings_js = (STATIC_DIR / "settings-config.js").read_text(encoding="utf-8")
    assert "api('POST', '/api/index/preview-namespace'" in settings_js
    assert "api('GET', `/api/index/preview-namespace" not in settings_js


def test_redaction_protected_handlers_scan_and_refuse() -> None:
    """Each ``_REDACTION_PROTECTED`` handler scans user content — and, for the
    thin ``scan_text_content`` wrapper, also refuses on the result (not merely
    "a scan ran" — see ``_function_scans_and_refuses``)."""
    missing: list[str] = []
    for qualname in sorted(_REDACTION_PROTECTED):
        module, _, function = qualname.partition(".")
        if not _function_scans_and_refuses(module, function):
            missing.append(qualname)
    if missing:
        pytest.fail(
            "Handlers classified as _REDACTION_PROTECTED but not scanning "
            "(and, for ``scan_text_content`` wrapper handlers, not refusing "
            "on the result via ``raise_if_privacy_blocked`` / "
            "``raise_or_collect`` / an inline ``.decision`` check that "
            "raises):\n  - "
            + "\n  - ".join(missing)
            + "\n\nAdd the scan (+ refusal for wrapper handlers), or move the "
            "handler to _REDACTION_EXEMPT with a justification. A wrapper scan "
            "whose FileScan is dropped still writes the blocked content."
        )


# --- Unit tests for the scan-and-refuse AST detector -----------------------
#
# These pin the exact false-pass the #1509 re-review (Codex) flagged: a
# wrapper scan whose FileScan is dropped must NOT satisfy the invariant even
# when an unrelated ``.decision`` check and a validation ``raise`` are present.


def _one_handler(body: str) -> str:
    return "async def handler():\n" + textwrap.indent(textwrap.dedent(body), "    ")


# A route module imports the engine wrapper from its canonical module; a bare
# ``scan_mcp_server_text(...)`` call is trusted only under this binding
# (see ``_trusted_engine_wrappers``). Prepended to the engine-wrapper sources.
_ENGINE_IMPORT = f"from {_ENGINE_WRAPPER_MODULE} import scan_mcp_server_text\n\n\n"


def test_detector_rejects_wrapper_scan_without_any_refusal() -> None:
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_wrapper_scan_with_unrelated_decision_and_raise() -> None:
    # The exact adversarial shape: scan result ignored, but an UNRELATED
    # ``.decision`` check and an UNRELATED validation raise are present.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if other.decision == "x":
            raise ValueError("unrelated validation")
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_accepts_wrapper_scan_with_helper_refusal() -> None:
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        raise_if_privacy_blocked(scan, kind="command", artifact_name=name)
        atomic_write_text(path, body.content)
        """
    )
    assert _source_scans_and_refuses(src, "handler")


def test_detector_accepts_wrapper_scan_with_inline_decision_raise() -> None:
    # The ``settings_sync.promote_target_rule`` shape.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if scan.decision in ("blocked", "blocked_project_shared"):
            raise HTTPException(422, "blocked")
        atomic_write_text(path, body.content)
        """
    )
    assert _source_scans_and_refuses(src, "handler")


def test_detector_accepts_direct_chokepoint_without_raise() -> None:
    # Direct enforce_write_guard callers keep the old contract — a non-raising
    # skip (``system.upload_files``) is a valid refusal we do not second-guess.
    src = _one_handler(
        """
        guard = privacy.enforce_write_guard(text, surface="s")
        if guard.decision == "blocked":
            results.append(error)
            return
        dest.write_bytes(content)
        """
    )
    assert _source_scans_and_refuses(src, "handler")


def test_detector_rejects_refusal_in_uncalled_nested_def() -> None:
    # #1509 re-review Blocker: a refusal buried in an uncalled nested def must
    # not count — the write still happens in the handler's own scope.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")

        async def refuse():
            raise_if_privacy_blocked(scan, kind="command", artifact_name=name)

        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_inline_raise_in_nested_def_under_if() -> None:
    # #1509 re-review Nit: an ``if <scan>.decision`` whose raise lives inside a
    # nested def in its body is not a real refusal.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if scan.decision in ("blocked", "blocked_project_shared"):
            def _later():
                raise HTTPException(422, "blocked")
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_inverted_decision_condition() -> None:
    # #1509 re-review Blocker: the ``if`` test must compare against a BLOCKED
    # decision. ``if scan.decision == "allowed": raise`` raises on the allowed
    # case and writes blocked content — not a refusal.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if scan.decision == "allowed":
            raise HTTPException(422, "nope")
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_raise_only_in_else_branch() -> None:
    # #1509 re-review Blocker: the raise must be in the TRUE (blocked) branch.
    # A raise only in ``else`` means the blocked branch falls through to write.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if scan.decision in ("blocked", "blocked_project_shared"):
            atomic_write_text(path, body.content)
        else:
            raise HTTPException(422, "wrong branch")
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_negated_not_equal_decision() -> None:
    # #1509 re-review Blocker: ``!= "blocked"`` raises on the ALLOWED case and
    # writes blocked content — negated polarity is not a refusal.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if scan.decision != "blocked":
            raise HTTPException(422, "inverted")
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_negated_not_in_decision() -> None:
    # #1509 re-review Blocker: ``not in (...)`` is the same inverted trap.
    src = _one_handler(
        """
        scan = scan_text_content(body.content, scope="project_shared")
        if scan.decision not in ("blocked", "blocked_project_shared"):
            raise HTTPException(422, "inverted")
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_walrus_bound_scan() -> None:
    # #1509 re-review Major: only plain ``scan = scan_text_content(...)`` is
    # tracked. A walrus-bound scan is intentionally NOT tracked, so this
    # fails closed — pinning the simple-assignment convention rather than
    # silently accepting an untracked shape.
    src = _one_handler(
        """
        if (scan := scan_text_content(body.content, scope="project_shared")):
            raise_if_privacy_blocked(scan, kind="command", artifact_name=name)
        atomic_write_text(path, body.content)
        """
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_accepts_engine_wrapper_scan() -> None:
    # #1579: an engine scan wrapper that self-refuses internally
    # (``scan_mcp_server_text``) is a chokepoint, like a direct
    # ``enforce_write_guard`` call — the ``create_mcp_server`` shape.
    src = _ENGINE_IMPORT + _one_handler(
        """
        scan_mcp_server_text(body.content, source_path=path, project_root=root, surface="s")
        atomic_write_text(path, body.content)
        """
    )
    assert _source_scans_and_refuses(src, "handler")


def test_detector_accepts_delegated_scan() -> None:
    # #1579: update/patch delegate to the allow-listed ``_update_mcp_server_impl``
    # which scans; the guard follows one level of local-module delegation — the
    # ``update_mcp_server`` → ``_update_mcp_server_impl`` shape.
    src = _ENGINE_IMPORT + (
        "async def _update_mcp_server_impl():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
        "\n"
        "async def handler():\n"
        "    return await _update_mcp_server_impl()\n"
    )
    assert _source_scans_and_refuses(src, "handler")


def test_detector_rejects_two_level_delegation() -> None:
    # #1579: delegation follows exactly ONE level. A scan two hops away
    # (handler → _update_mcp_server_impl → _deeper) is not seen — this keeps the
    # guard from degrading into a full call-graph walker.
    src = _ENGINE_IMPORT + (
        "async def _deeper():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "\n"
        "async def _update_mcp_server_impl():\n"
        "    return await _deeper()\n"
        "\n"
        "async def handler():\n"
        "    return await _update_mcp_server_impl()\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_delegate_without_scan() -> None:
    # #1579: following an allow-listed delegate that does not scan must not
    # fabricate a pass.
    src = (
        "async def _update_mcp_server_impl():\n"
        "    atomic_write_text(path, body.content)\n"
        "\n"
        "async def handler():\n"
        "    return await _update_mcp_server_impl()\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_non_allowlisted_delegate_that_scans() -> None:
    # #1579 (review — Codex): delegation is an explicit allow-list, NOT "any
    # sibling that scans". A handler that delegates to an unrelated sibling
    # which scans *different* content, then writes the body unscanned, must not
    # pass — the sibling is not in ``_DELEGATE_TARGETS``.
    src = _ENGINE_IMPORT + (
        "async def _unrelated_helper():\n"
        '    scan_mcp_server_text(other.content, surface="s")\n'
        "\n"
        "async def handler():\n"
        "    await _unrelated_helper()\n"
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_shadowed_engine_wrapper() -> None:
    # #1579 (review — Codex): a module-local ``def`` shadowing the engine
    # wrapper name is not the trusted engine import — even alongside the real
    # import — so a call to it is not a self-refusing chokepoint.
    src = _ENGINE_IMPORT + (
        "def scan_mcp_server_text(*a, **k):\n"
        "    return None\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_reassigned_engine_wrapper() -> None:
    # #1579 (review — Codex): a top-level reassignment of the wrapper name is
    # not the trusted engine import, even alongside the real import.
    src = _ENGINE_IMPORT + (
        "scan_mcp_server_text = lambda *a, **k: None\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_wrapper_imported_from_other_module() -> None:
    # #1579 (review — Codex): the wrapper name imported from a *different*
    # module is not the trusted engine chokepoint.
    src = (
        "from other.module import scan_mcp_server_text\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_wildcard_import_shadow() -> None:
    # #1579 (review — Codex): a non-canonical ``import *`` could shadow the
    # wrapper name, so the guard fails closed even alongside the real import.
    src = (
        _ENGINE_IMPORT + "from other.module import *\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_tuple_destructured_wrapper() -> None:
    # #1579 (review — Codex): a tuple/list destructuring assignment rebinds the
    # wrapper name just like a plain assignment, even alongside the real import.
    src = _ENGINE_IMPORT + (
        "scan_mcp_server_text, _other = (None, None)\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_def_shadow_in_compound_stmt() -> None:
    # #1579 (review — Codex): a shadow ``def`` nested in a top-level compound
    # statement is still a module-scope binding — it executes in module scope,
    # so it drops trust even alongside the real import.
    src = _ENGINE_IMPORT + (
        "if True:\n"
        "    def scan_mcp_server_text(*a, **k):\n"
        "        return None\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_foreign_import_in_compound_stmt() -> None:
    # #1579 (review — Codex): a foreign import nested in a top-level compound
    # statement is a module-scope rebinding too.
    src = _ENGINE_IMPORT + (
        "if True:\n"
        "    from other.module import scan_mcp_server_text\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_match_capture_shadow() -> None:
    # #1579 (review — Codex): a top-level ``match`` capture binds the wrapper
    # name in module scope, so it drops trust.
    src = _ENGINE_IMPORT + (
        "match value:\n"
        "    case scan_mcp_server_text:\n"
        "        pass\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_except_handler_binding() -> None:
    # #1579 (review — Codex): ``except ... as scan_mcp_server_text`` binds the
    # wrapper name in module scope (the binder is ExceptHandler.name, a str),
    # so it drops trust.
    src = _ENGINE_IMPORT + (
        "try:\n"
        "    pass\n"
        "except Exception as scan_mcp_server_text:\n"
        "    pass\n"
        "\n"
        "async def handler():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_unawaited_delegate() -> None:
    # #1579 (review — Codex): an un-awaited call to the async delegate never
    # runs its scan — constructing the coroutine and then writing the body is a
    # bypass, not a delegated scan. Only a ``return await <delegate>(...)``
    # trampoline is followed.
    src = _ENGINE_IMPORT + (
        "async def _update_mcp_server_impl():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
        "\n"
        "async def handler():\n"
        "    _update_mcp_server_impl()\n"
        "    atomic_write_text(path, body.content)\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_detector_rejects_write_before_awaited_delegate() -> None:
    # #1579 (review — Codex): delegation is followed ONLY for a pure
    # ``return await <delegate>(...)`` trampoline. A handler that writes in its
    # own scope before awaiting the delegate could bypass the delegate's scan,
    # so it is not a trampoline and is not followed.
    src = _ENGINE_IMPORT + (
        "async def _update_mcp_server_impl():\n"
        '    scan_mcp_server_text(body.content, surface="s")\n'
        "    atomic_write_text(path, body.content)\n"
        "\n"
        "async def handler():\n"
        "    atomic_write_text(path, body.content)\n"
        "    return await _update_mcp_server_impl()\n"
    )
    assert not _source_scans_and_refuses(src, "handler")


def test_redaction_classification_covers_every_unsafe_route() -> None:
    """Every unsafe-method route is classified for redaction too."""
    handlers = _iter_unsafe_route_handlers()
    seen = {f"{m}.{f}" for m, f, _ in handlers}
    classified = _REDACTION_PROTECTED | _REDACTION_EXEMPT.keys()
    unclassified = sorted(seen - classified)
    stale = sorted(classified - seen)
    if unclassified or stale:
        msg = []
        if unclassified:
            msg.append(
                "Unclassified for redaction (add to _REDACTION_PROTECTED "
                "with a guard call, or _REDACTION_EXEMPT with a "
                "justification):\n  - " + "\n  - ".join(unclassified)
            )
        if stale:
            msg.append(
                "Stale entries in _REDACTION_PROTECTED / _REDACTION_EXEMPT "
                "(route was renamed or removed):\n  - " + "\n  - ".join(stale)
            )
        pytest.fail("\n\n".join(msg))


# --- SPA-side CSRF threading invariant -------------------------------------
#
# Backend coverage above guarantees every unsafe handler is *gated* by the
# middleware. This complementary test pins the opposite direction: every
# ``/api/...`` ``fetch(...)`` in the SPA must thread the CSRF token, so
# the gate doesn't 403 a legitimate user-initiated write.
#
# Regression history: PR1 (#793), PR #958, and two review-folds of #961
# each leaked sites that bypassed the token. This guard is the
# "stop-the-bleeding" check.
#
# Design notes (informed by Codex review of `c5897b5`):
#
# 1. **Statically-classify every /api fetch.** We list each fetch as
#    "safe method" (GET/HEAD/OPTIONS), "unsafe method with literal", or
#    "method not statically inferable". The third class fails — a
#    ``fetch(url, opts)`` or ``fetch(url, { method })`` shape would hide
#    an unsafe write from the test otherwise. The codebase's convention
#    is inline literal methods; the test enforces it.
#
# 2. **Token must live INSIDE the headers value.** A comment, debug
#    string, or unrelated literal that contains the substring
#    ``X-Memtomem-CSRF`` anywhere in the 800-char fetch window must not
#    cause a pass. Two valid shapes:
#
#    * **Inline-literal** — ``headers: { ..., 'X-Memtomem-CSRF': csrf }``.
#    * **Variable threading** — ``headers`` as a bare identifier passed
#      to fetch, *backed by a local* ``const headers = ... 'X-Memtomem-CSRF'
#      ...`` binding in the preceding ~100 lines.
#
# 3. **No free-form exempt list.** The "function-parameter threading"
#    shape (used by ``_ctxHandleConflict`` in earlier revisions) was
#    refactored to self-thread (call ``ensureCsrfToken()`` inside its
#    own scope) so this test never has to chase across functions —
#    keeping the regex contract tight.

_FETCH_LINE_RE = re.compile(r"\bfetch\(")
_METHOD_LITERAL_RE = re.compile(r"method:\s*['\"](GET|HEAD|OPTIONS|POST|PUT|PATCH|DELETE)['\"]")
_HEADERS_KEY_RE = re.compile(r"\bheaders\s*:")
_HEADERS_SHORTHAND_RE = re.compile(r"[\{,]\s*headers\s*[,}\n]")
_IDENT_ONLY_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
_CSRF_HEADER_RE = re.compile(r"X-Memtomem-CSRF", re.IGNORECASE)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Vendored libraries (Swagger UI, etc.) don't observe our CSRF contract.
_STATIC_SKIP_DIRS = frozenset({"vendor"})


def _scan_fetch_args(window: str) -> tuple[str, str]:
    """Return ``(first_arg_text, second_arg_shape)`` by scanning the
    fetch call's argument list with awareness of strings, template
    literals, and nested braces/parens.

    ``second_arg_shape`` is one of:
    * ``"none"`` — no second argument; JS fetch defaults to GET.
    * ``"inline"`` — second argument starts with ``{`` (inline options
      literal); ``method`` is statically discoverable if present.
    * ``"identifier"`` — second argument starts with an identifier or
      anything that isn't ``{``; the method can't be statically
      determined and the site fails open.
    """
    # The window starts at ``f`` of ``fetch(``. Move past the open paren.
    if not window.startswith("fetch("):
        return ("", "none")
    i = len("fetch(")
    depth_paren = 1
    depth_brace = 0
    depth_bracket = 0
    in_string: str | None = None
    in_template = False
    in_template_expr = 0  # nested ${} inside a template literal
    first_arg_start = i
    while i < len(window):
        c = window[i]
        if in_string is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_string:
                in_string = None
            i += 1
            continue
        if in_template:
            if c == "\\":
                i += 2
                continue
            if c == "`" and in_template_expr == 0:
                in_template = False
            elif c == "$" and i + 1 < len(window) and window[i + 1] == "{":
                in_template_expr += 1
                i += 2
                continue
            elif c == "}" and in_template_expr > 0:
                in_template_expr -= 1
            i += 1
            continue
        if c == "'" or c == '"':
            in_string = c
        elif c == "`":
            in_template = True
        elif c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren -= 1
            if depth_paren == 0:
                return (window[first_arg_start:i], "none")
        elif c == "{":
            depth_brace += 1
        elif c == "}":
            depth_brace -= 1
        elif c == "[":
            depth_bracket += 1
        elif c == "]":
            depth_bracket -= 1
        elif c == "," and depth_paren == 1 and depth_brace == 0 and depth_bracket == 0:
            first_arg = window[first_arg_start:i]
            j = i + 1
            while j < len(window) and window[j].isspace():
                j += 1
            if j >= len(window) or window[j] == ")":
                return (first_arg, "none")
            return (first_arg, "inline" if window[j] == "{" else "identifier")
        i += 1
    # Unterminated — treat as unknown so the test fails loudly.
    return (window[first_arg_start:], "identifier")


def _is_api_fetch(window: str) -> bool:
    """Does this fetch call target ``/api/...``?"""
    first_arg, _ = _scan_fetch_args(window)
    return "/api/" in first_arg


def _extract_methods(window: str) -> list[str]:
    """All HTTP methods statically inferable from inline ``method: '...'``
    literals inside the fetch window."""
    return [m.group(1).upper() for m in _METHOD_LITERAL_RE.finditer(window)]


def _extract_headers_value(window: str) -> str | None:
    """Find ``headers:`` in the fetch options literal and return the
    value expression as raw text, scanning at brace/paren/bracket depth
    0 until the next ``,`` or ``}`` outside strings/templates. Returns
    None if no ``headers:`` key is present.

    The shorthand ``{ method, headers }`` (i.e. ``headers`` without an
    explicit ``:`` value) returns the identifier ``"headers"`` so the
    caller can route it through the local-binding check.
    """
    m = _HEADERS_KEY_RE.search(window)
    if m is None:
        sh = _HEADERS_SHORTHAND_RE.search(window)
        if sh is not None:
            return "headers"
        return None
    i = m.end()
    while i < len(window) and window[i].isspace():
        i += 1
    start = i
    depth_paren = depth_brace = depth_bracket = 0
    in_string: str | None = None
    in_template = False
    in_template_expr = 0
    while i < len(window):
        c = window[i]
        if in_string is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_string:
                in_string = None
            i += 1
            continue
        if in_template:
            if c == "\\":
                i += 2
                continue
            if c == "`" and in_template_expr == 0:
                in_template = False
            elif c == "$" and i + 1 < len(window) and window[i + 1] == "{":
                in_template_expr += 1
                i += 2
                continue
            elif c == "}" and in_template_expr > 0:
                in_template_expr -= 1
            i += 1
            continue
        if c == "'" or c == '"':
            in_string = c
        elif c == "`":
            in_template = True
        elif c == "(":
            depth_paren += 1
        elif c == ")":
            depth_paren -= 1
            if depth_paren < 0:
                # Fell out of the fetch options object.
                return window[start:i].strip()
        elif c == "{":
            depth_brace += 1
        elif c == "}":
            if depth_brace == 0:
                return window[start:i].strip()
            depth_brace -= 1
        elif c == "[":
            depth_bracket += 1
        elif c == "]":
            depth_bracket -= 1
        elif c == "," and depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
            return window[start:i].strip()
        i += 1
    return window[start:].strip()


def _has_local_binding_with_csrf(text: str, fetch_start: int, ident: str) -> bool:
    """True iff a ``const|let|var <ident> = <RHS containing X-Memtomem-CSRF>``
    binding appears in the ~100-line lookback.

    The check is tied to the **exact identifier** passed to fetch — so a
    fetch shaped ``headers: _hdr4`` only passes when there's a local
    ``const _hdr4 = ...'X-Memtomem-CSRF'...`` binding, not when some
    unrelated ``const headers = ...`` happens to live nearby. This is
    the binding-tracing pin Codex requested in round 3.
    """
    if not _IDENT_ONLY_RE.match(ident):
        return False
    lookback = text[max(0, fetch_start - 4000) : fetch_start]
    binding_re = re.compile(
        r"\b(?:const|let|var)\s+" + re.escape(ident) + r"\s*=\s*(.+?)(?:;|\n\n)",
        re.DOTALL,
    )
    for m in binding_re.finditer(lookback):
        rhs = m.group(1)
        if _CSRF_HEADER_RE.search(rhs):
            return True
    return False


def _iter_api_fetch_sites() -> list[tuple[Path, int, str, int]]:
    """Return ``(file, line, window, start_offset)`` for each ``/api/...``
    fetch call (safe or unsafe)."""
    sites: list[tuple[Path, int, str, int]] = []
    for path in sorted(STATIC_DIR.rglob("*.js")):
        if any(part in _STATIC_SKIP_DIRS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for line_match in _FETCH_LINE_RE.finditer(text):
            start = line_match.start()
            window = text[start : start + 800]
            if not _is_api_fetch(window):
                continue
            line_no = text.count("\n", 0, start) + 1
            sites.append((path, line_no, window, start))
    return sites


def _classify_site(path: Path, window: str, start: int) -> tuple[str, str | None]:
    """Return ``(verdict, reason)`` where verdict is one of ``"pass"`` or
    a failure shorthand naming the specific bug shape.
    """
    _first_arg, second_arg_shape = _scan_fetch_args(window)
    if second_arg_shape == "identifier":
        return (
            "method-not-inferable",
            "fetch's second argument is an identifier — the method can't "
            "be classified statically. Inline the options literal at the "
            "callsite so the CSRF guard test can see the method.",
        )

    methods = _extract_methods(window)
    if not methods:
        # No inline ``method:`` AND options is either absent or an inline
        # literal — JS fetch defaults to GET, which is safe.
        return ("pass", None)
    unsafe = [m for m in methods if m in _UNSAFE_METHODS]
    if not unsafe:
        return ("pass", None)  # safe-method fetch

    headers_value = _extract_headers_value(window)
    if headers_value is None:
        return (
            "no-headers-on-unsafe-fetch",
            "unsafe fetch options omit the `headers` key entirely",
        )

    # Shape A: the headers value text contains 'X-Memtomem-CSRF' — either
    # an inline literal, a ternary like ``csrf ? {...CSRF...} : {...}``,
    # or any expression that includes the header name. Covers the
    # canonical inline-literal case and the conditional-headers shape.
    if _CSRF_HEADER_RE.search(headers_value):
        return ("pass", None)

    # Shape B: the headers value is a bare identifier — must be backed
    # by a local ``const|let|var <that-identifier> = ... 'X-Memtomem-CSRF'
    # ...`` binding. The binding name is matched against the literal
    # identifier passed to fetch, so an unrelated ``const headers = ...``
    # binding elsewhere doesn't accidentally rescue a fetch shaped
    # ``headers: someOtherVar``.
    if _IDENT_ONLY_RE.match(headers_value):
        text = path.read_text(encoding="utf-8")
        if _has_local_binding_with_csrf(text, start, headers_value):
            return ("pass", None)
        return (
            "headers-var-without-csrf-binding",
            f"`headers: {headers_value}` passed to unsafe fetch, but no "
            f"local `const {headers_value} = ...'X-Memtomem-CSRF'...` "
            "binding in the preceding ~100 lines",
        )

    return (
        "headers-expression-missing-csrf",
        f"`headers:` value (`{headers_value[:80]}`) is not a bare "
        "identifier and does not contain 'X-Memtomem-CSRF'",
    )


def test_spa_api_fetch_threads_csrf_token() -> None:
    """Every SPA ``/api/...`` fetch is either a safe-method read or
    threads the CSRF token via the local-binding convention.

    Shape-specific failure verdicts make it obvious what to change:
    ``method-not-inferable``, ``inline-literal-missing-csrf``,
    ``no-headers-on-unsafe-fetch``, ``headers-var-without-csrf-binding``.
    """
    sites = _iter_api_fetch_sites()
    assert sites, "static/*.js sweep found no /api/... fetch sites"

    failures: list[str] = []
    for path, line_no, window, start in sites:
        verdict, reason = _classify_site(path, window, start)
        if verdict == "pass":
            continue
        rel = path.relative_to(STATIC_DIR.parent.parent.parent.parent)
        failures.append(f"{rel}:{line_no} — {verdict}: {reason}")

    if failures:
        pytest.fail(
            "Unsafe /api fetch() sites without CSRF threading.\n\n"
            "Safe shapes (one must apply):\n"
            "  A. Inline literal: `headers: { ..., 'X-Memtomem-CSRF': csrf }`.\n"
            "  B. Variable threading with a local binding:\n"
            "       const csrf = await ensureCsrfToken();\n"
            "       const headers = csrf\n"
            "         ? { 'Content-Type': 'application/json', 'X-Memtomem-CSRF': csrf }\n"
            "         : { 'Content-Type': 'application/json' };\n"
            "       fetch(URL, { method: 'POST', headers });\n"
            "  C. Safe-method (GET/HEAD/OPTIONS) — no threading required.\n\n"
            "Offending sites:\n  - " + "\n  - ".join(failures)
        )


def test_context_gateway_fragment_list_matches_disk_and_index_html() -> None:
    """The #1517 gateway-fragment manifest must stay honest against the real
    files and the index.html load order.

    ``CTX_GATEWAY_JS_FILES`` (tests/helpers.py) is the single source the
    whole-file test readers and the vitest bootApp expansion trust. If a new
    ``context-gateway-*.js`` fragment is added but not registered — or the tuple
    order drifts from the ``<script>`` order — the concat readers would silently
    miss content and the ``_langchange_listener_body`` sentinel slice could
    anchor on the wrong listener. Pin both here.
    """
    on_disk = {p.name for p in STATIC_DIR.glob("context-gateway-*.js")}
    assert set(CTX_GATEWAY_JS_FILES) == on_disk, (
        "CTX_GATEWAY_JS_FILES is out of sync with the context-gateway-*.js "
        f"files on disk. registered={sorted(CTX_GATEWAY_JS_FILES)} "
        f"on_disk={sorted(on_disk)}"
    )

    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    tag_order = re.findall(r'src="/(context-gateway-[a-z]+\.js)\?v=\d+"', index_html)
    assert tag_order == list(CTX_GATEWAY_JS_FILES), (
        "index.html <script> order for the gateway fragments must match "
        "CTX_GATEWAY_JS_FILES (the concat load order the langchange sentinel "
        f"slice depends on). index.html={tag_order} "
        f"tuple={list(CTX_GATEWAY_JS_FILES)}"
    )
