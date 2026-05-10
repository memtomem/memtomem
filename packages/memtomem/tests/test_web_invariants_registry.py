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
   user-supplied content to LTM must call
   ``privacy.enforce_write_guard(...)`` somewhere in its body, or be in
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
from pathlib import Path

import pytest

ROUTES_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "routes"

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
        "context_projects.add_known_project",
        "context_projects.delete_known_project",
        "context_skills.create_skill",
        "context_skills.delete_skill",
        "context_skills.import_skill",
        "context_skills.import_skills",
        "context_skills.sync_skills",
        "context_skills.update_skill",
        "decay.expire_old_chunks",
        "dedup.merge_duplicates",
        "export.import_memories",
        "scratch.delete_scratch",
        "scratch.promote_scratch",
        "scratch.set_scratch",
        "settings_sync.apply_settings_sync",
        "settings_sync.resolve_conflict",
        "sources.delete_source",
        "sources.regenerate_summaries",
        "system.add_memory",
        "system.add_memory_dir",
        "system.embed_text",
        "system.open_memory_dir",
        "system.patch_config",
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
# *must* call ``privacy.enforce_write_guard(...)`` in its body. New
# content-writing handlers either join this list (and the body must
# include the guard call) or join ``_REDACTION_EXEMPT`` with a
# justification.

_REDACTION_PROTECTED: frozenset[str] = frozenset(
    {
        "chunks.edit_chunk",
        "scratch.promote_scratch",
        "system.add_memory",
        "system.upload_files",
    }
)

_REDACTION_EXEMPT: dict[str, str] = {
    # Delete-only / no body content.
    "chunks.delete_chunk": "delete-only, no payload",
    "context_agents.delete_agent": "delete-only, no payload",
    "context_commands.delete_command": "delete-only, no payload",
    "context_projects.delete_known_project": "delete-only, no payload",
    "context_skills.delete_skill": "delete-only, no payload",
    "scratch.delete_scratch": "delete-only, no payload",
    "sources.delete_source": "delete-only, no payload",
    "sources.regenerate_summaries": (
        "no user-supplied content — body is empty; trigger only "
        "rewrites cached LLM summaries from chunks already validated "
        "at index time"
    ),
    # Structured artifacts (skills/commands/agents) — separate redaction
    # policy lives in ``memtomem.context`` ingest path; the HTTP layer
    # validates the schema only.
    "context_agents.create_agent": "structured artifact; redaction at context-ingest layer",
    "context_agents.update_agent": "structured artifact; see above",
    "context_agents.import_agent": "structured artifact import; redaction at context-ingest layer",
    "context_agents.import_agents": "bulk structured artifact import; see above",
    "context_agents.sync_agents": "filesystem-driven sync; redaction "
    "happens at file-write time inside the indexer",
    "context_commands.create_command": "structured artifact; see above",
    "context_commands.update_command": "structured artifact; see above",
    "context_commands.import_command": "structured artifact import; see above",
    "context_commands.import_commands": "bulk structured artifact import",
    "context_commands.sync_commands": "filesystem-driven sync; see above",
    "context_skills.create_skill": "structured artifact; see above",
    "context_skills.update_skill": "structured artifact; see above",
    "context_skills.import_skill": "structured artifact import",
    "context_skills.import_skills": "bulk structured artifact import",
    "context_skills.sync_skills": "filesystem-driven sync",
    "context_projects.add_known_project": "path/label only, no prose",
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
    "export.import_memories": "import bypass: archived chunks already "
    "passed redaction at original write time and re-scanning would "
    "corrupt deterministic round-trip",
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


def _function_calls_write_guard(module: str, function_name: str) -> bool:
    """True iff ``function_name`` in ``module`` calls
    ``privacy.enforce_write_guard`` somewhere in its body."""
    path = ROUTES_DIR / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name != function_name:
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            # Match ``privacy.enforce_write_guard(...)``.
            if (
                isinstance(f, ast.Attribute)
                and f.attr == "enforce_write_guard"
                and isinstance(f.value, ast.Name)
                and f.value.id == "privacy"
            ):
                return True
            # Match bare ``enforce_write_guard(...)`` after
            # ``from memtomem.privacy import enforce_write_guard``.
            if isinstance(f, ast.Name) and f.id == "enforce_write_guard":
                return True
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


def test_redaction_protected_handlers_call_enforce_write_guard() -> None:
    """Each ``_REDACTION_PROTECTED`` handler calls ``enforce_write_guard``."""
    missing: list[str] = []
    for qualname in sorted(_REDACTION_PROTECTED):
        module, _, function = qualname.partition(".")
        if not _function_calls_write_guard(module, function):
            missing.append(qualname)
    if missing:
        pytest.fail(
            "Handlers classified as _REDACTION_PROTECTED but lacking a "
            "``privacy.enforce_write_guard(...)`` call in their body:\n  - "
            + "\n  - ".join(missing)
            + "\n\nEither add the guard call or move the handler to "
            "_REDACTION_EXEMPT with a justification."
        )


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
