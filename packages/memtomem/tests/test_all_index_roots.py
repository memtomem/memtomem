"""``all_index_roots()`` consumer fan-out tests for ADR-0011 PR-B.

Two pins:

1. **Helper composes both fields.** ``all_index_roots()`` returns
   ``memory_dirs + project_memory_dirs``; an empty project list is a
   no-op (= just memory_dirs).
2. **Consumer regression pin (architectural guard).** No source file
   under ``packages/memtomem/src/memtomem/`` reads
   ``.indexing.memory_dirs`` directly OUTSIDE the small allowlist
   (config module itself, migration paths, user-tier registry surfaces).
   The functional consumers — watcher, engine within-roots / exclude
   guards, web sources status — must go through ``all_index_roots()``.

The regression pin is the load-bearing one: a future field add (e.g.
``project_memory_dirs_v2``) without an updated helper would silently
fork the consumers, exactly the failure mode we want to catch.
"""

from __future__ import annotations

import re
from pathlib import Path

from memtomem.config import IndexingConfig

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "memtomem"


class TestAllIndexRootsHelper:
    def test_empty_project_dirs_returns_memory_dirs(self):
        cfg = IndexingConfig(memory_dirs=[Path("/u1"), Path("/u2")], project_memory_dirs=[])
        assert cfg.all_index_roots() == [Path("/u1"), Path("/u2")]

    def test_project_dirs_appended(self):
        cfg = IndexingConfig(
            memory_dirs=[Path("/u1")],
            project_memory_dirs=[Path("/p1/.memtomem/memories"), Path("/p2/.memtomem/memories")],
        )
        roots = cfg.all_index_roots()
        assert roots == [
            Path("/u1"),
            Path("/p1/.memtomem/memories"),
            Path("/p2/.memtomem/memories"),
        ]

    def test_helper_returns_new_list(self):
        # Mutating the returned list MUST NOT mutate the underlying
        # config fields — guards against accidental config corruption.
        cfg = IndexingConfig(memory_dirs=[Path("/u1")], project_memory_dirs=[])
        roots = cfg.all_index_roots()
        roots.append(Path("/leak"))
        assert Path("/leak") not in cfg.memory_dirs
        assert Path("/leak") not in cfg.project_memory_dirs


class TestConsumerRegressionPin:
    """Architectural guard — direct ``.memory_dirs`` access is restricted.

    Allowlist:
    - ``config.py`` — the field definition itself + ``all_index_roots`` body.
    - migration / wizard paths that mutate the user-tier list specifically
      (``cli/init_cmd.py``, the auto-discover migration in ``config.py``).
    - User-tier registry endpoints in ``web/routes/system.py`` that operate
      on the user-tier list (add / remove / list user dirs only).

    Functional consumers (watcher, engine within-roots / exclude guards,
    web sources status, web reindex_all, web upload-path guards) MUST
    use ``all_index_roots()``.
    """

    # Files allowed to touch ``.memory_dirs`` directly. Each entry is a
    # path SUFFIX matched against the absolute file path (so the test
    # works regardless of where the package is checked out). A future
    # contributor extending the allowlist must justify each addition
    # in the PR body — direct access intentionally bypasses the
    # scope-axis fan-out.
    #
    # Snapshot taken at ADR-0011 PR-B: every existing direct reader is
    # user-tier-specific (wizard / write-target derivation / user-tier
    # registry view / filesystem allow-list). Future PRs that decide a
    # listed file SHOULD fan out can migrate it to ``all_index_roots()``
    # and remove the entry. New files NOT on this list that read
    # ``.memory_dirs`` will fail this test, forcing an audit.
    _ALLOWED_DIRECT_ACCESS_SUFFIXES: tuple[str, ...] = (
        # Field definition + helper body itself.
        "memtomem/config.py",
        # Wizard / init flow targets the user-tier registry.
        "memtomem/cli/init_cmd.py",
        # User-tier registry endpoints (POST /memory-dirs add/remove,
        # GET /api/system snapshot, sources tab listing). The functional
        # fan-out call sites in this file (memory_dirs_status,
        # reindex_all, index_path_stream guard, trigger_index guard)
        # were migrated in PR-B to ``all_index_roots()``; the user-tier
        # registry endpoints intentionally stay on ``memory_dirs``.
        "memtomem/web/routes/system.py",
        # Web app bootstrap — initializes the user-tier registry.
        "memtomem/web/app.py",
        # Filesystem browser allow-list — currently user-tier only.
        # PR-F may broaden to project tiers if file-browser access
        # to project_memory_dirs is wanted.
        "memtomem/web/routes/fs.py",
        # User-tier sources tab listing.
        "memtomem/web/routes/sources.py",
        # Scratch promote — derives a write target from user-tier dirs.
        "memtomem/web/routes/scratch.py",
        # CLI agent shell write-target derivation (defaults to user
        # tier; PR-D revisits with --scope flag).
        "memtomem/cli/agent_cmd.py",
        "memtomem/cli/shell.py",
        # ``mm mem add`` user-tier base derivation (ADR-0011 PR-D round 7
        # BLOCKER fix): the CLI must read ``memory_dirs[0]`` so writes
        # land in the same user-tier directory MCP ``_mem_add_core`` uses.
        "memtomem/cli/memory.py",
        # Sync-doctor reads/writes the user-tier registry directly.
        "memtomem/cli/sync_doctor_cmd.py",
        # ``mm context memory-migrate`` derives the user-tier base
        # directory for ``resolve_memory_scope_dir``; legitimately
        # user-tier-only — fan-out across project tiers is not the
        # intent here (ADR-0011 PR-D).
        "memtomem/cli/context_cmd.py",
        # LangGraph adapter resolves user-tier write target.
        "memtomem/integrations/langgraph.py",
        # Session / URL / importer / memory_crud tools all derive a
        # user-tier write target (mem_add default). PR-D refactors
        # these to honor ``--scope`` and route through Gate B.
        "memtomem/server/tools/session.py",
        "memtomem/server/tools/url_index.py",
        "memtomem/server/tools/importers.py",
        "memtomem/server/tools/memory_crud.py",
    )

    def test_no_unaudited_memory_dirs_access(self):
        """Scan src/ for ``.indexing.memory_dirs`` reads outside the allowlist.

        Pattern matches both ``config.indexing.memory_dirs`` (web routes)
        and ``self._config.memory_dirs`` (engine / watcher). The allowlist
        is intentionally narrow — adding a new file requires a comment
        in the PR body explaining why ``all_index_roots()`` is wrong for
        that call site.
        """
        pat = re.compile(r"\.indexing\.memory_dirs\b|_config\.memory_dirs\b|self\.memory_dirs\b")
        offenders: list[tuple[str, int, str]] = []
        for py_file in SRC_ROOT.rglob("*.py"):
            # ADR-0011 PR-D review round 7: normalize to forward-slash via
            # ``as_posix()`` before allowlist match. ``rglob`` returns
            # native paths (backslash on Windows); the allowlist suffixes
            # are forward-slash literals like ``memtomem/cli/agent_cmd.py``.
            # Without normalization every Windows file falls through the
            # allowlist and the test fails the entire src tree.
            rel = py_file.as_posix()
            if any(rel.endswith(s) for s in self._ALLOWED_DIRECT_ACCESS_SUFFIXES):
                continue
            try:
                src = py_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for ln_no, line in enumerate(src.splitlines(), start=1):
                if pat.search(line):
                    offenders.append((rel, ln_no, line.strip()))
        assert not offenders, (
            "Direct ``memory_dirs`` access detected outside the allowlist. "
            "These call sites should use ``config.indexing.all_index_roots()`` "
            "to fan out across user + project tiers (ADR-0011). "
            "If a call site legitimately wants user-tier only, add it to "
            "_ALLOWED_DIRECT_ACCESS_SUFFIXES with a justification comment.\n\n"
            + "\n".join(f"  {f}:{ln}: {snippet}" for f, ln, snippet in offenders)
        )

    def test_engine_uses_all_index_roots(self):
        """Functional pin: engine.py references all_index_roots() at least once."""
        src = (SRC_ROOT / "indexing" / "engine.py").read_text(encoding="utf-8")
        assert "all_index_roots()" in src, (
            "engine.py must call all_index_roots() — within-memory-dirs "
            "guards and exclude checks are scope-fan-out consumers."
        )

    def test_watcher_uses_all_index_roots(self):
        """Functional pin: watcher.py references all_index_roots() at least once."""
        src = (SRC_ROOT / "indexing" / "watcher.py").read_text(encoding="utf-8")
        assert "all_index_roots()" in src

    def test_web_routes_uses_all_index_roots(self):
        """Functional pin: web/routes/system.py uses all_index_roots() for indexing-fan-out."""
        src = (SRC_ROOT / "web" / "routes" / "system.py").read_text(encoding="utf-8")
        # At minimum, the four call sites the PR migrated:
        # memory_dirs_status, reindex_all, index_path_stream guard,
        # trigger_index guard.
        assert src.count("all_index_roots()") >= 4
