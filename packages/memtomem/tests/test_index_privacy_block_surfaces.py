"""ADR-0006 PR-A — the folder-index redaction gate.

Bulk / un-adjudicated indexing (``index_path`` / ``index_file`` /
``index_path_stream``) scans each file's content and refuses to index
secret-bearing files unless ``force_unsafe`` — closing the gap where
``mm reindex`` / the watcher / ``mem_index`` / ``mem_fetch`` pulled arbitrary
secrets into the store, bypassing the trust boundary the write ingresses
already enforce. Ingress-guarded callers pass ``already_scanned=True`` so the
whole-file reindex neither re-litigates already-adjudicated content nor breaks
their exception-based rollback.

Pins:
- bulk ``index_path`` skips a secret file (raise → aggregated) and still
  indexes the clean sibling; ``blocked_files`` / ``blocked_paths`` surface it.
- ``force_unsafe=True`` bypasses (bulk indexes the secret anyway).
- single-file ``index_file`` propagates ``PrivacyRejection`` (so callers can
  roll back / surface, rather than silently succeeding).
- ``already_scanned=True`` skips the gate (regression guard for the
  ingress-guarded mutation callers).
- ``project_shared`` + ``force_unsafe=True`` is still hard-refused
  (ADR-0011 §5 — the bypass valve never applies to the git-tracked tier).
"""

from __future__ import annotations

import pytest

from memtomem import privacy
from memtomem.indexing.engine import PrivacyRejection


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    # The gate calls ``privacy.record(...)`` (record_outcome=True) on the
    # process-global counters; reset around each test so we don't leak state
    # into counter-asserting tests elsewhere in the suite.
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


# HuggingFace-token shape assembled at runtime so GitHub push-protection does
# not flag this file (mirrors tests/test_privacy.py:90).
_SECRET = "hf" + "_FAKEfake0123456789FAKEfake01234567"

_CLEAN = "# Notes\n\nJust some ordinary prose with nothing sensitive in it.\n"
_LEAK = f"# Leak\n\napi token: {_SECRET}\n"


class TestBulkIndexRedactionGate:
    async def test_secret_file_blocked_clean_indexed(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "clean.md").write_text(_CLEAN)
        (mem_dir / "leak.md").write_text(_LEAK)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.blocked_files == 1
        assert any("leak.md" in p for p in stats.blocked_paths)
        assert any("redaction_blocked" in e for e in stats.errors)
        # The clean sibling was still indexed — one flagged file does not abort
        # the whole run.
        assert stats.indexed_chunks > 0

    async def test_force_unsafe_bypasses_bulk(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "leak.md").write_text(_LEAK)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True, force_unsafe=True)

        assert stats.blocked_files == 0
        assert stats.indexed_chunks > 0

    async def test_stream_reports_blocked(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "clean.md").write_text(_CLEAN)
        (mem_dir / "leak.md").write_text(_LEAK)

        events = [ev async for ev in comp.index_engine.index_path_stream(mem_dir, recursive=True)]
        complete = next(ev for ev in events if ev["type"] == "complete")

        assert complete["blocked_files"] == 1
        assert any("leak.md" in p for p in complete["blocked_paths"])

    async def test_single_file_index_raises(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        leak = mem_dir / "leak.md"
        leak.write_text(_LEAK)

        with pytest.raises(PrivacyRejection) as ei:
            await comp.index_engine.index_file(leak)

        assert ei.value.hit_count >= 1
        assert ei.value.decision == "blocked"
        # The exception message must not echo the matched secret bytes.
        assert _SECRET not in str(ei.value)

    async def test_already_scanned_skips_gate(self, bm25_only_components):
        # Ingress-guarded callers (mem_add / mem_edit / upload / chunk edit)
        # already adjudicated the content upstream; the whole-file reindex must
        # NOT re-block it — else their rollback fires and storage goes stale.
        comp, mem_dir = bm25_only_components
        leak = mem_dir / "leak.md"
        leak.write_text(_LEAK)

        stats = await comp.index_engine.index_file(leak, already_scanned=True)

        assert stats.blocked_files == 0
        assert stats.indexed_chunks > 0  # indexed despite the secret

    async def test_project_shared_force_unsafe_hard_refused(
        self, bm25_only_components, monkeypatch
    ):
        # ADR-0011 §5: the force_unsafe bypass valve never applies to the
        # git-tracked project_shared tier — a hit there is hard-refused even
        # with force_unsafe=True.
        comp, mem_dir = bm25_only_components
        leak = mem_dir / "leak.md"
        leak.write_text(_LEAK)

        engine = comp.index_engine
        monkeypatch.setattr(engine, "_resolve_scope", lambda p: ("project_shared", mem_dir))

        with pytest.raises(PrivacyRejection) as ei:
            await engine.index_file(leak, force_unsafe=True)

        assert ei.value.decision == "blocked_project_shared"

    async def test_bulk_project_shared_counted_distinctly(self, bm25_only_components, monkeypatch):
        # A project_shared block is counted in blocked_project_shared_files so
        # surfaces can give scope-correct guidance (force_unsafe never applies).
        comp, mem_dir = bm25_only_components
        (mem_dir / "leak.md").write_text(_LEAK)
        engine = comp.index_engine
        monkeypatch.setattr(engine, "_resolve_scope", lambda p: ("project_shared", mem_dir))

        stats = await engine.index_path(mem_dir, recursive=True)

        assert stats.blocked_files == 1
        assert stats.blocked_project_shared_files == 1

    async def test_stream_project_shared_force_unsafe_flagged(
        self, bm25_only_components, monkeypatch
    ):
        # Codex-requested: index_path_stream(force_unsafe=True) on project_shared
        # is still hard-refused; the complete event flags it distinctly (and the
        # decision is preserved in the error) so the CLI does not tell the user
        # to retry with --force-unsafe.
        comp, mem_dir = bm25_only_components
        (mem_dir / "leak.md").write_text(_LEAK)
        engine = comp.index_engine
        monkeypatch.setattr(engine, "_resolve_scope", lambda p: ("project_shared", mem_dir))

        events = [
            ev async for ev in engine.index_path_stream(mem_dir, recursive=True, force_unsafe=True)
        ]
        complete = next(ev for ev in events if ev["type"] == "complete")

        assert complete["blocked_files"] == 1
        assert complete["blocked_project_shared_files"] == 1
        assert any("blocked_project_shared" in e for e in complete["errors"])


class TestShellIndexBlockedSurfacing:
    """The interactive shell's ``index`` command (``cli/shell.py:_cmd_index``)
    previously printed only a blocked-files count — no paths, no scope
    guidance, and ``stats.errors`` not at all (the ADR-0006 "known,
    lower-severity partial gap"). It now prints the shared blocked summary
    and the non-redaction error lines, mirroring ``mm index``."""

    async def test_blocked_paths_and_bypass_hint_printed(self, bm25_only_components, capsys):
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "clean.md").write_text(_CLEAN)
        (mem_dir / "leak.md").write_text(_LEAK)

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "1 file(s) blocked by redaction guard:" in out
        assert "leak.md" in out
        # The shell has no inline force-unsafe syntax (mirrors _cmd_add) —
        # the hint names the CLI command the user can actually run.
        assert "mm index --force-unsafe" in out
        # The matched secret bytes never echo to the terminal.
        assert _SECRET not in out
        # The redaction_blocked stats.errors entry is folded into the blocked
        # summary, not double-printed as a raw ERROR line.
        assert "ERROR:" not in out

    async def test_project_shared_block_messaged_as_hard_refused(
        self, bm25_only_components, capsys, monkeypatch
    ):
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "leak.md").write_text(_LEAK)
        monkeypatch.setattr(
            comp.index_engine, "_resolve_scope", lambda p: ("project_shared", mem_dir)
        )

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "project_shared tier" in out
        assert "hard-refused" in out
        # Scope-correct guidance: no bypass hint — force_unsafe never
        # applies to the git-tracked tier (ADR-0011 §5).
        assert "mm index --force-unsafe" not in out

    async def test_non_redaction_errors_printed(self, bm25_only_components, capsys):
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "note.md").write_text(_CLEAN)
        (mem_dir / "blob.md").write_bytes(b"\x00\x01binary")

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "ERROR: blob.md: binary file detected, skipping" in out
