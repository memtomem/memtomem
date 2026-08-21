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

    async def test_blocked_only_run_keeps_the_prepass_namespace(self, bm25_only_components):
        """A privacy block performs no namespace-bearing write, so its echo
        retains the positional prepass value instead of disappearing."""
        comp, mem_dir = bm25_only_components
        (mem_dir / "leak.md").write_text(_LEAK)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.blocked_files == 1
        assert stats.indexed_chunks == 0
        assert stats.resolved_namespaces == (None,)
        assert stats.applied_namespaces == ()

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

    async def test_stream_blocked_only_run_keeps_the_prepass_namespace(self, bm25_only_components):
        """A stream privacy block retains its positional preview and must not
        manufacture an authoritative per-file namespace result."""
        comp, mem_dir = bm25_only_components
        (mem_dir / "leak.md").write_text(_LEAK)

        events = [ev async for ev in comp.index_engine.index_path_stream(mem_dir, recursive=True)]
        complete = next(ev for ev in events if ev["type"] == "complete")

        assert complete["blocked_files"] == 1
        assert complete["indexed_chunks"] == 0
        assert complete["resolved_namespaces"] == [None]
        assert complete["applied_namespaces"] == []

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

    async def test_declared_exemption_is_named_in_the_shell_summary(
        self, bm25_only_components, capsys
    ):
        # A persistent bypass that nobody re-types per run needs the run to
        # say it is still there (#2076).
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "declared.md").write_text(_DECLARED)

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "declared redaction exemption" in out
        assert "declared.md" in out

    async def test_blocked_markdown_hints_the_frontmatter_declaration(
        self, bm25_only_components, capsys
    ):
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "plain.md").write_text(_DOCUMENTS_PATTERNS)

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "redaction: documents-patterns" in out

    async def test_blocked_non_markdown_gets_no_frontmatter_hint(
        self, bm25_only_components, capsys
    ):
        # A ``.yaml`` source has no frontmatter block — naming the declaration
        # there would be advice that cannot work.
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "conf.yaml").write_text("ok: 1\npassword: hunter2xyz\n")

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "blocked by redaction guard" in out
        assert "redaction: documents-patterns" not in out

    async def test_non_redaction_errors_printed(self, bm25_only_components, capsys):
        from memtomem.cli.shell import _cmd_index

        comp, mem_dir = bm25_only_components
        (mem_dir / "note.md").write_text(_CLEAN)
        (mem_dir / "blob.md").write_bytes(b"\x00\x01binary")

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "ERROR: blob.md: binary file detected, skipping" in out

    async def test_retryable_errors_are_labeled_once_with_shell_guidance(
        self, bm25_only_components, capsys, monkeypatch
    ):
        from memtomem.cli.shell import _cmd_index
        from memtomem.models import IndexingStats

        comp, mem_dir = bm25_only_components
        retryable = "note.md: chunk store unavailable"

        async def _index_path(*_args, **_kwargs):
            return IndexingStats(
                total_files=1,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=(retryable,),
                retryable_errors=(retryable,),
            )

        monkeypatch.setattr(comp.index_engine, "index_path", _index_path)

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert f"ERROR (retryable): {retryable}" in out
        assert out.count(retryable) == 1
        assert f"mm index {mem_dir}" in out
        assert "once the chunk store is reachable" in out

    async def test_real_error_on_a_file_named_redaction_blocked_is_not_swallowed(
        self, bm25_only_components, capsys, monkeypatch
    ):
        """A file literally named ``redaction_blocked.md`` renders its failure
        as ``redaction_blocked.md: <cause>``. The old substring test matched
        that and dropped the line entirely — no error, and no retry hint even
        when the cause was retryable. The reporter matches the engine's whole
        message shape instead, so only genuine privacy blocks are skipped."""
        from memtomem.cli.shell import _cmd_index
        from memtomem.models import IndexingStats

        comp, mem_dir = bm25_only_components
        trap = "redaction_blocked.md: chunk store unavailable"
        genuine = "secret.md: redaction_blocked (hits=2, scope=user, decision=refuse)"

        async def _index_path(*_args, **_kwargs):
            return IndexingStats(
                total_files=2,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=(trap, genuine),
                retryable_errors=(trap,),
                blocked_files=1,
                blocked_paths=("secret.md",),
            )

        monkeypatch.setattr(comp.index_engine, "index_path", _index_path)

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert f"ERROR (retryable): {trap}" in out
        assert "once the chunk store is reachable" in out
        # The genuine block is still routed to print_blocked_summary only, so
        # it must not appear as a raw ERROR line.
        assert f"ERROR: {genuine}" not in out

    async def test_engine_block_message_still_matches_the_reporter_pattern(
        self, bm25_only_components
    ):
        """Drift canary for the pattern above: it is anchored to wording the
        engine owns, so a reworded block message would silently start leaking
        privacy blocks into the raw ERROR list. Assert against a real engine
        run rather than a hand-spelled string."""
        from memtomem.cli._index_progress import _REDACTION_BLOCKED_RE

        comp, mem_dir = bm25_only_components
        (mem_dir / "secret.md").write_text(_SECRET)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        blocked = [e for e in stats.errors if "redaction_blocked" in e]
        assert blocked, f"expected a redaction block, got {stats.errors!r}"
        for entry in blocked:
            assert _REDACTION_BLOCKED_RE.search(entry), (
                f"engine reworded its block message; _REDACTION_BLOCKED_RE no "
                f"longer matches {entry!r} — privacy blocks would now print as "
                f"raw ERROR lines"
            )

    async def test_shell_labels_a_retryable_failure_raised_before_any_stats(
        self, bm25_only_components, capsys, monkeypatch
    ):
        """The engine's pre-write namespace prepass raises instead of
        returning ``IndexingStats``, so ``print_index_errors`` is never
        reached. Without the typed catch the shell printed a bare traceback-y
        error for the one failure class the retryable split exists to name."""
        from memtomem.cli.shell import _cmd_index
        from memtomem.errors import NamespaceResolutionError

        comp, mem_dir = bm25_only_components

        async def _index_path(*_args, **_kwargs):
            raise NamespaceResolutionError("chunk store unreachable")

        monkeypatch.setattr(comp.index_engine, "index_path", _index_path)

        await _cmd_index(comp, [str(mem_dir)])

        out = capsys.readouterr().out
        assert "ERROR (retryable): chunk store unreachable" in out
        assert f"mm index {mem_dir}" in out
        assert "once the chunk store is reachable" in out


# A note that documents the patterns rather than carrying a credential: the
# #2076 case. Both label rules, no token.
_DOCUMENTS_PATTERNS = (
    "# Redaction notes\n\n"
    "The guard matches `api_key=` and `password:` on the keyword alone, so\n"
    "this very paragraph trips it.\n"
)
_DECLARED = f"---\nredaction: documents-patterns\n---\n{_DOCUMENTS_PATTERNS}"


class TestDeclaredExemptionIndexing:
    """#2076 — a file that declares its own exemption indexes normally.

    The declaration reaches surfaces ``force_unsafe`` never could (``mem_index``,
    the watcher, the debounce drain) because it travels with the content the
    gate already reads. These pins cover the three aggregation points and the
    bounds: mixed hits, non-Markdown, and ``project_shared``.
    """

    async def test_bulk_indexes_declared_file_and_reports_it(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "declared.md").write_text(_DECLARED)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.blocked_files == 0
        assert stats.indexed_chunks > 0
        assert stats.exempted_files == 1
        assert any("declared.md" in p for p in stats.exempted_paths)

    async def test_undeclared_sibling_is_still_blocked(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "declared.md").write_text(_DECLARED)
        (mem_dir / "plain.md").write_text(_DOCUMENTS_PATTERNS)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.exempted_files == 1
        assert stats.blocked_files == 1
        assert any("plain.md" in p for p in stats.blocked_paths)

    async def test_single_file_index_reports_the_exemption(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        declared = mem_dir / "declared.md"
        declared.write_text(_DECLARED)

        stats = await comp.index_engine.index_file(declared)

        assert stats.indexed_chunks > 0
        assert stats.exempted_files == 1
        assert stats.exempted_paths == (str(declared),)

    async def test_stream_reports_the_exemption(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "declared.md").write_text(_DECLARED)

        events = [ev async for ev in comp.index_engine.index_path_stream(mem_dir, recursive=True)]
        complete = next(ev for ev in events if ev["type"] == "complete")

        assert complete["blocked_files"] == 0
        assert complete["exempted_files"] == 1
        assert any("declared.md" in p for p in complete["exempted_paths"])

    async def test_no_declaration_means_no_exemption_reported(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        (mem_dir / "clean.md").write_text(_CLEAN)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.exempted_files == 0
        assert stats.exempted_paths == ()

    async def test_declared_file_with_a_real_token_stays_blocked(self, bm25_only_components):
        # The bound that matters most: a declaration written months ago must
        # not wave through a credential pasted into the file later.
        comp, mem_dir = bm25_only_components
        (mem_dir / "declared.md").write_text(f"{_DECLARED}\napi token: {_SECRET}\n")

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.blocked_files == 1
        assert stats.exempted_files == 0

    async def test_yaml_frontmatter_lookalike_is_not_exempt(self, bm25_only_components):
        # A ``.yaml`` source has no frontmatter block; leading dashes do not
        # make one.
        comp, mem_dir = bm25_only_components
        (mem_dir / "conf.yaml").write_text("---\nredaction: documents-patterns\npassword: x\n")

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.blocked_files == 1
        assert stats.exempted_files == 0

    async def test_project_shared_declaration_is_hard_refused(
        self, bm25_only_components, monkeypatch
    ):
        # ADR-0011 §5 — same ceiling as force_unsafe.
        comp, mem_dir = bm25_only_components
        declared = mem_dir / "declared.md"
        declared.write_text(_DECLARED)
        engine = comp.index_engine
        monkeypatch.setattr(engine, "_resolve_scope", lambda p: ("project_shared", mem_dir))

        with pytest.raises(PrivacyRejection) as ei:
            await engine.index_file(declared)

        assert ei.value.decision == "blocked_project_shared"

    async def test_exemption_is_counted_only_after_the_write_commits(
        self, bm25_only_components, monkeypatch
    ):
        # An exempted file whose storage write fails was not indexed under an
        # exemption; counting it would overstate how often the valve is used.
        comp, mem_dir = bm25_only_components
        declared = mem_dir / "declared.md"
        declared.write_text(_DECLARED)
        engine = comp.index_engine

        async def _boom(*args, **kwargs):
            raise RuntimeError("storage down")

        monkeypatch.setattr(engine._storage, "upsert_chunks", _boom)

        # Bulk, so the per-file failure is flattened into ``errors`` and a
        # stats object still comes back to inspect (single-file re-raises).
        stats = await engine.index_path(mem_dir, recursive=True)

        assert any("storage down" in e for e in stats.errors)
        assert stats.exempted_files == 0
        assert stats.exempted_paths == ()

    async def test_already_scanned_ingress_does_not_consult_the_declaration(
        self, bm25_only_components
    ):
        # The feature is scoped to un-adjudicated indexing. An ingress-guarded
        # caller adjudicated request content upstream; the reindex must not
        # re-decide, in either direction.
        comp, mem_dir = bm25_only_components
        declared = mem_dir / "declared.md"
        declared.write_text(_DECLARED)

        stats = await comp.index_engine.index_file(declared, already_scanned=True)

        assert stats.indexed_chunks > 0
        assert stats.exempted_files == 0


@pytest.mark.skipif(
    not hasattr(__import__("os"), "symlink"), reason="platform has no symlink support"
)
class TestDeclaredExemptionSymlinkParity:
    """The gate adjudicates the *canonical* path (#2076).

    ``classify_scope`` reads the path string, and the two bulk paths disagree
    on whether they resolve the discovered leaf. While every bypass required
    an explicit ``force_unsafe`` that divergence was inert; a file-declared
    exemption makes the scope decision load-bearing on its own, so an alias
    must not launder either the tier or the file type.
    """

    @staticmethod
    def _symlink(link, target):
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:  # pragma: no cover - CI platform
            pytest.skip(f"symlink creation unavailable: {exc}")

    async def test_alias_to_a_project_shared_note_is_refused_on_both_paths(
        self, bm25_only_components, monkeypatch, tmp_path
    ):
        comp, mem_dir = bm25_only_components
        shared_dir = tmp_path / "proj" / ".memtomem" / "memories"
        shared_dir.mkdir(parents=True)
        target = shared_dir / "declared.md"
        target.write_text(_DECLARED)
        self._symlink(mem_dir / "alias.md", target)

        engine = comp.index_engine
        # Only the resolved path lands in the project tier; the alias does not.
        monkeypatch.setattr(
            engine,
            "_resolve_scope",
            lambda p: (
                ("project_shared", shared_dir)
                if str(p).startswith(str(shared_dir))
                else ("user", None)
            ),
        )

        stats = await engine.index_path(mem_dir, recursive=True)
        assert stats.blocked_project_shared_files == 1
        assert stats.exempted_files == 0

        events = [ev async for ev in engine.index_path_stream(mem_dir, recursive=True)]
        complete = next(ev for ev in events if ev["type"] == "complete")
        assert complete["blocked_project_shared_files"] == 1
        assert complete["exempted_files"] == 0

    async def test_md_alias_to_a_non_markdown_target_claims_no_declaration(
        self, bm25_only_components, tmp_path
    ):
        comp, mem_dir = bm25_only_components
        target = tmp_path / "conf.yaml"
        target.write_text("---\nredaction: documents-patterns\npassword: x\n")
        self._symlink(mem_dir / "alias.md", target)

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert stats.exempted_files == 0
