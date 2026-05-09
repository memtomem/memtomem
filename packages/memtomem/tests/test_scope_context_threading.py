"""ADR-0011 PR-D round 9 architectural guard.

Every read-surface caller of ``SearchPipeline.search`` and
``recall_chunks`` MUST thread ``project_context_root`` (either by
keyword or via a documented exception). The always-on scope filter
in ``storage/sqlite_scope.scope_context_sql`` defaults missing
context to ``scope = 'user'`` only, so a forgotten kwarg silently
drops project-tier rows for any caller running inside a registered
project.

Pin shape: AST-scan ``packages/memtomem/src/memtomem/`` for matching
call sites; each call's keyword set must include
``project_context_root``. A small allowlist covers call sites that
are intentionally project-context-free (e.g. archive lookups, batch
summary embeddings inside the pipeline that derive context from
nested helpers).

Per ``feedback_ast_architectural_guard_pattern.md`` — N>5
functional sites with identical contract get an AST registry, not
inline grep.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "memtomem"


# Calls that legitimately do NOT need project_context_root threaded.
# Each entry is (file_suffix, callee_name, justification).
#
# - ``pipeline.py``: internal pipeline calls already inside ``search``
#   threading the kwarg from the outer invocation.
# - ``consolidation_engine.py``: archive lookups by chunk_id.
# - ``component_factory.py``: pipeline construction time, not search.
# - ``hot_reload.py``, ``tag_management.py``: cache invalidation hooks.
# - ``test_*``: tests file scanning is excluded from this guard
#   already (the SRC_ROOT walk only covers ``src/memtomem``).
_ALLOWED_CALLSITES: set[tuple[str, str]] = {
    # Pipeline-internal calls — already threading from the outer
    # ``search``/``recall_chunks_with_filters`` invocation.
    ("memtomem/search/pipeline.py", "_session_summary_boost_sources"),
    ("memtomem/search/pipeline.py", "_rescue_retrieval"),
}


# File-level allowlist — entire files exempt because they don't run
# inside a request context (constructors, factories, hot-reload
# callbacks). Storage-layer consumers (sqlite_backend) call themselves
# recursively which is fine because the kwarg is already threaded.
_ALLOWED_FILES: set[str] = {
    "memtomem/storage/sqlite_backend.py",  # backend internals; threading happens at the outer
    "memtomem/search/pipeline.py",  # threaded inside, see _ALLOWED_CALLSITES above
    "memtomem/server/component_factory.py",
    "memtomem/web/hot_reload.py",
    "memtomem/services/tag_management.py",
}


_TARGET_METHODS = {"search", "recall_chunks", "dense_search", "bm25_search"}


class _CallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.offenders: list[tuple[int, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast hook
        # Match ``something.<method>(...)`` for the ADR-0011 read surfaces.
        if isinstance(node.func, ast.Attribute) and node.func.attr in _TARGET_METHODS:
            method = node.func.attr
            # Only check when the receiver looks like a search pipeline
            # or storage object — heuristic match on the accessor name.
            recv = self._receiver_text(node.func)
            if not self._is_target_receiver(recv, method):
                self.generic_visit(node)
                return
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if "project_context_root" not in kwargs:
                self.offenders.append((node.lineno, method, recv))
        self.generic_visit(node)

    @staticmethod
    def _receiver_text(attr: ast.Attribute) -> str:
        """Best-effort source-form of the receiver (e.g. ``app.search_pipeline``)."""
        return ast.unparse(attr.value)

    @staticmethod
    def _is_target_receiver(recv: str, method: str) -> bool:
        if method == "search":
            # ``app.search_pipeline.search`` / ``comp.search_pipeline.search`` /
            # ``pipeline.search`` / ``self._search_pipeline.search``.
            return any(
                token in recv
                for token in ("search_pipeline", ".pipeline", "self.pipeline", "self._pipeline")
            ) or recv.endswith("pipeline")
        if method in ("recall_chunks", "dense_search", "bm25_search"):
            # ``app.storage.recall_chunks`` / ``comp.storage.recall_chunks`` /
            # ``self._storage.recall_chunks`` / ``storage.recall_chunks`` —
            # same shape across the three storage methods.
            return "storage" in recv
        return False


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    try:
        src = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return []
    visitor = _CallVisitor()
    visitor.visit(tree)
    return visitor.offenders


def test_search_and_recall_callers_thread_project_context_root():
    """Every src/ call to ``*.search_pipeline.search`` or
    ``*.storage.recall_chunks`` includes ``project_context_root=`` —
    otherwise the always-on storage scope filter silently drops
    project-tier rows for callers running in a registered project.

    Adding a new call site? Either pass ``project_context_root=`` (use
    ``_resolve_project_context_root(app)`` for MCP/CLI surfaces or
    ``_resolve_project_context_from_dirs(config.indexing.project_memory_dirs)``
    for web routes), or add the file/site to the allowlist with a
    justification comment.
    """
    offenders: list[tuple[str, int, str, str]] = []
    for py_file in SRC_ROOT.rglob("*.py"):
        rel = py_file.as_posix()
        rel_short = rel[rel.find("memtomem/") :] if "memtomem/" in rel else rel
        if rel_short in _ALLOWED_FILES:
            continue
        for ln_no, method, recv in _scan_file(py_file):
            # Per-callsite allowlist (file_suffix, enclosing helper name).
            # The enclosing helper name is part of the receiver text so
            # we match on substring presence.
            file_pin_match = any(
                rel_short.endswith(suffix) and helper in recv
                for suffix, helper in _ALLOWED_CALLSITES
            )
            if file_pin_match:
                continue
            offenders.append((rel_short, ln_no, method, recv))

    assert not offenders, (
        "ADR-0011 PR-D round 9: read-surface call sites missing "
        "``project_context_root=`` kwarg. Each call to a search "
        "pipeline or storage recall must thread project context onto "
        "the always-on scope filter, otherwise project-tier rows are "
        "silently dropped when the server runs in a registered "
        "project cwd. Either pass the kwarg, or add the file/site to "
        "the allowlist with a justification comment.\n\n"
        + "\n".join(f"  {f}:{ln}: {recv}.{method}(...)" for f, ln, method, recv in offenders)
    )
