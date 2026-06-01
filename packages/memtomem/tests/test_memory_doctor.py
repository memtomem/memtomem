"""Tests for ``mm memory doctor`` (Tier 1 report-only + Tier 2 ``--fix``).

Layers:

* ``TestParser`` / ``TestClassifyLink`` / ``TestBudget`` — pure functions, no
  DB or disk-config side effects.
* ``TestAnalysis`` — drives ``_gather_reports`` against a **real**
  ``SqliteBackend`` (a tmp DB) + a real on-disk ``claude-memory`` dir, so the
  disk↔DB drift detection exercises the actual SQL aggregate and the engine's
  own discovery (no ``AsyncMock`` masking — memory
  ``feedback_mocked_storage_hides_sql_bugs``).
* ``TestCli`` — Click ``CliRunner`` end-to-end with the read-only config
  loader stubbed, pinning the exit code and the ``--json`` payload shape.
* ``TestDocsParity`` — pins the contract documented in
  ``docs/guides/reference.md`` (check/severity table, error-severity set,
  budget caps, ``--json`` status rule, help text) against the implementation
  so the two can't drift.
* ``TestSpliceRoundTrip`` / ``TestMissingTargetGuard`` / ``TestApplyFix`` /
  ``TestFixCli`` — Tier 2 ``--fix`` (ADR-0020): byte-exact line splicing across
  LF/CRLF ± trailing newline, the missing_target-only subtractive guard, the
  locked re-validate/atomic-write apply path against a real on-disk index file,
  and the CLI dry-run/apply/exit-code surface.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.memory_doctor_cmd import (
    _apply_fix,
    _gather_reports,
    _missing_target_entries,
    _splice_lines,
    classify_link,
    measure_budget,
    parse_memory_index,
)
from memtomem.config import Mem2MemConfig
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_helpers import norm_path


# ── Pure: parser ────────────────────────────────────────────────────


class TestParser:
    def test_extracts_pointer_entries(self):
        text = "- [Alpha](alpha.md) — first\n* [Beta](sub/beta.md) — second\n"
        parsed = parse_memory_index(text)
        assert [(e.title, e.target) for e in parsed.entries] == [
            ("Alpha", "alpha.md"),
            ("Beta", "sub/beta.md"),
        ]
        assert parsed.entries[0].line_no == 1
        assert parsed.entries[1].line_no == 2

    def test_preserves_non_pointer_lines_with_numbers(self):
        text = "# Header\n\n- [A](a.md) — x\nplain prose line\n<!-- comment -->\n"
        parsed = parse_memory_index(text)
        assert len(parsed.entries) == 1
        # Header(1), blank(2), prose(4), comment(5) preserved in order.
        assert [n for n, _ in parsed.other_lines] == [1, 2, 4, 5]

    def test_title_with_brackets_in_hook_not_swallowed(self):
        # Non-greedy title stops at the first ``]``; brackets in the trailing
        # hook prose must not be pulled into the title or target.
        parsed = parse_memory_index("- [Title](t.md) — see [note] and (paren)\n")
        assert parsed.entries[0].title == "Title"
        assert parsed.entries[0].target == "t.md"

    def test_non_ascii_filename_target(self):
        parsed = parse_memory_index("- [한글](한글노트.md) — 메모\n")
        assert parsed.entries[0].target == "한글노트.md"


# ── Pure: link classification ───────────────────────────────────────


class TestClassifyLink:
    def test_existing_file_ok(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        assert classify_link("a.md", root=tmp_path, source_dir=tmp_path) == "ok"

    def test_missing_file(self, tmp_path):
        assert classify_link("gone.md", root=tmp_path, source_dir=tmp_path) == "missing_target"

    def test_dotdot_escape_is_outside_root(self, tmp_path):
        inner = tmp_path / "memory"
        inner.mkdir()
        assert classify_link("../../etc/passwd", root=inner, source_dir=inner) == "outside_root"

    def test_absolute_path_outside_root(self, tmp_path):
        inner = tmp_path / "memory"
        inner.mkdir()
        assert classify_link("/etc/hosts", root=inner, source_dir=inner) == "outside_root"

    def test_url_not_a_file(self, tmp_path):
        assert classify_link("https://example.com/x", root=tmp_path, source_dir=tmp_path) == "url"
        assert classify_link("mailto:a@b.com", root=tmp_path, source_dir=tmp_path) == "url"

    def test_anchor_only(self, tmp_path):
        assert classify_link("#section", root=tmp_path, source_dir=tmp_path) == "anchor"
        assert classify_link("", root=tmp_path, source_dir=tmp_path) == "anchor"

    def test_file_with_anchor_suffix_uses_file_part(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        assert classify_link("a.md#heading", root=tmp_path, source_dir=tmp_path) == "ok"

    def test_whitespace_target_trimmed(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        assert classify_link("  a.md  ", root=tmp_path, source_dir=tmp_path) == "ok"


# ── Pure: budget ────────────────────────────────────────────────────


class TestBudget:
    def test_small_file_under_budget(self):
        m = measure_budget("- [A](a.md) — x\n")
        assert not m.over_budget
        assert m.line_count == 1

    def test_line_count_over_cap(self):
        m = measure_budget("\n".join(["x"] * 250))
        assert m.over_budget
        assert m.line_count == 250

    def test_byte_count_over_cap(self):
        m = measure_budget("x" * 25_000)
        assert m.over_budget
        assert m.byte_len == 25_000

    def test_overlong_line_measured_in_chars_not_bytes(self):
        # 150 CJK chars = 450 UTF-8 bytes but only 150 characters, so it must
        # NOT trip the 200-char per-line cap (char-based, not byte-based).
        m = measure_budget("가" * 150)
        assert m.overlong_lines == ()
        assert not m.over_budget
        # 201 chars does trip it.
        m2 = measure_budget("a" * 201)
        assert m2.overlong_lines == (1,)
        assert m2.over_budget


# ── Integration: real DB + real disk ────────────────────────────────


def _insert_chunk(
    backend: SqliteBackend,
    *,
    chunk_id: str,
    source_file: Path,
    access_count: int = 0,
    last_accessed_at: str | None = None,
    importance_score: float = 0.0,
) -> None:
    """Insert one ``chunks`` row (read-only doctor never touches FTS)."""
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, "
        "created_at, updated_at, access_count, last_accessed_at, importance_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            f"content of {chunk_id}",
            f"hash-{chunk_id}",
            norm_path(source_file),
            "2026-06-01T00:00:00",
            "2026-06-01T00:00:00",
            access_count,
            last_accessed_at,
            importance_score,
        ),
    )
    db.commit()


@pytest.fixture
def doctor_env(tmp_path, monkeypatch):
    """A real ``claude-memory`` dir + a tmp DB wired into a ``Mem2MemConfig``.

    Layout (disk):
      MEMORY.md, README.md   — meta/index (engine-excluded)
      alpha.md               — indexed, listed, accessed       → clean
      beta.md                — indexed, listed, never accessed → cold_candidate
      gamma.md               — NOT indexed, listed             → db_coverage
      delta.md               — indexed, NOT listed             → index_orphan

    DB also has chunks for ghost.md (no disk file → stale_source) and MEMORY.md
    (meta indexed as content → convention_violation). Returns ``(config, dir)``.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)

    # Path must end with ``/.claude/projects/<slug>/memory`` to classify as
    # ``claude-memory`` (so the index_file/exclude convention applies).
    mem_dir = tmp_path / ".claude" / "projects" / "-test-proj" / "memory"
    mem_dir.mkdir(parents=True)
    for name in ("alpha.md", "beta.md", "gamma.md", "delta.md", "README.md"):
        (mem_dir / name).write_text(f"# {name}\n\nbody\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        "- [Alpha](alpha.md) — a\n"
        "- [Beta](beta.md) — b\n"
        "- [Gamma](gamma.md) — c\n"
        "- [Missing](nonexistent.md) — broken\n"
        "- [Escape](../../../../../../etc/passwd) — escapes root\n"
        "- [Web](https://example.com) — external\n"
        "- [Anchor](#top) — in-page\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "doctor.db"
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    return config, mem_dir


def _findings_by_check(report) -> dict[str, object]:
    return {f.check: f for f in report.findings}


@pytest.mark.asyncio
async def test_analysis_detects_all_drift_classes(doctor_env):
    config, mem_dir = doctor_env

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(
            backend,
            chunk_id="a1",
            source_file=mem_dir / "alpha.md",
            access_count=3,
            last_accessed_at="2026-06-01T12:00:00",
            importance_score=0.5,
        )
        # beta: two chunks, never accessed → cold_candidate
        _insert_chunk(backend, chunk_id="b1", source_file=mem_dir / "beta.md")
        _insert_chunk(backend, chunk_id="b2", source_file=mem_dir / "beta.md")
        # delta: indexed + accessed (not cold), not in TOC → index_orphan
        _insert_chunk(backend, chunk_id="d1", source_file=mem_dir / "delta.md", access_count=1)
        # ghost: chunk with no disk file → stale_source
        _insert_chunk(backend, chunk_id="g1", source_file=mem_dir / "ghost.md")
        # MEMORY.md indexed as content → convention_violation
        _insert_chunk(backend, chunk_id="m1", source_file=mem_dir / "MEMORY.md")
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    dir_reports = [r for r in reports if r.path != "(unowned)"]
    assert len(dir_reports) == 1
    report = dir_reports[0]

    assert report.category == "claude-memory"
    assert report.index_file == "MEMORY.md"
    # disk indexable = alpha, beta, gamma, delta (MEMORY.md/README.md excluded)
    assert report.disk_indexable == 4
    # covered = alpha, beta, delta (gamma has no chunk)
    assert report.db_covered == 3

    by = _findings_by_check(report)

    assert by["db_coverage"].items == ["gamma.md"]
    assert by["stale_source"].severity == "error"
    assert by["stale_source"].items == [norm_path(mem_dir / "ghost.md")]
    assert by["convention_violation"].severity == "error"
    assert by["convention_violation"].items == [norm_path(mem_dir / "MEMORY.md")]
    assert by["cold_candidate"].severity == "info"
    assert by["cold_candidate"].count == 1
    assert by["cold_candidate"].items == ["beta.md (2 chunks)"]
    # broken links: missing_target + outside_root; url + anchor NOT reported.
    broken = by["broken_link"]
    assert broken.severity == "error"
    assert broken.count == 2
    assert any("missing_target" in i for i in broken.items)
    assert any("outside_root" in i for i in broken.items)
    assert not any("example.com" in i for i in broken.items)
    assert by["index_orphan"].items == ["delta.md"]
    assert "budget" not in by  # small index file is under budget


@pytest.mark.asyncio
async def test_clean_dir_has_no_findings(tmp_path, monkeypatch):
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-clean" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "alpha.md").write_text("# alpha\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [Alpha](alpha.md) — a\n", encoding="utf-8")

    db_path = tmp_path / "clean.db"
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(
            backend,
            chunk_id="a1",
            source_file=mem_dir / "alpha.md",
            access_count=2,
            last_accessed_at="2026-06-01T00:00:00",
        )
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    dir_reports = [r for r in reports if r.path != "(unowned)"]
    assert len(dir_reports) == 1
    assert dir_reports[0].findings == []


def test_missing_db_is_not_created(tmp_path, monkeypatch):
    """Read-only contract: a missing DB is never created just by diagnosing.

    Pins the report-only guarantee — running the doctor against a config whose
    ``sqlite_path`` (and its parent) does not exist must leave the filesystem
    untouched and degrade to disk/index-only checks.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-absent" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")

    db_path = tmp_path / "nope" / "absent.db"  # parent dir also absent
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])

    assert not db_path.exists()
    assert not db_path.parent.exists()  # doctor must not mkdir the parent either
    note = next(r for r in reports if r.path == "(database)")
    assert note.findings[0].check == "db_unavailable"
    # With no DB, every disk file shows as uncovered.
    dir_report = next(r for r in reports if not r.path.startswith("("))
    cov = next(f for f in dir_report.findings if f.check == "db_coverage")
    assert "a.md" in cov.items


def test_old_schema_db_degrades_gracefully(tmp_path, monkeypatch):
    """A DB whose schema predates the aggregate's columns is reported, not
    crashed on, and is not migrated by the doctor."""
    import sqlite3

    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-old" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    # chunks table missing access_count / last_accessed_at / importance_score.
    conn.execute("CREATE TABLE chunks(id TEXT, source_file TEXT)")
    conn.commit()
    conn.close()

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    note = next(r for r in reports if r.path == "(database)")
    assert note.findings[0].check == "db_unavailable"
    # The doctor must not have added the missing columns (no migration).
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    conn.close()
    assert cols == {"id", "source_file"}


def test_corrupt_db_degrades_gracefully(tmp_path, monkeypatch):
    """A corrupt / non-SQLite file at sqlite_path must not crash the doctor.

    ``mode=ro`` opens the file, but reading it raises
    ``sqlite3.DatabaseError: file is not a database`` — the reader degrades to
    the db_unavailable note instead of propagating the error."""
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-corrupt" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")

    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is definitely not a sqlite database\n" * 8)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    note = next(r for r in reports if r.path == "(database)")
    assert note.findings[0].check == "db_unavailable"


@pytest.mark.asyncio
async def test_nested_roots_no_false_uncovered(tmp_path, monkeypatch):
    """Nested configured roots: a child's indexed file is the child's, not a
    false ``db_coverage`` gap under the parent.

    Disk discovery for the parent is recursive (it sees the child's files),
    but DB rows for the child are bucketed to the child by longest-prefix
    ownership. The parent report must attribute disk files the same way, so it
    reports only its own files — otherwise the child's already-indexed file
    shows as uncovered under the parent.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    parent = tmp_path / ".codex" / "memories"
    child = parent / "project-docs"
    child.mkdir(parents=True)
    (parent / "p.md").write_text("# p\n", encoding="utf-8")
    (child / "c.md").write_text("# c\n", encoding="utf-8")

    db_path = tmp_path / "nested.db"
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [parent, child]

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(backend, chunk_id="p1", source_file=parent / "p.md")
        _insert_chunk(backend, chunk_id="c1", source_file=child / "c.md")
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[parent, child])
    parent_report = next(r for r in reports if Path(r.path) == parent.resolve())
    child_report = next(r for r in reports if Path(r.path) == child.resolve())

    # Parent owns only p.md; child owns c.md — no double counting.
    assert parent_report.disk_indexable == 1
    assert child_report.disk_indexable == 1
    # Both files are indexed, so neither dir has a coverage gap.
    assert not any(f.check == "db_coverage" for f in parent_report.findings)
    assert not any(f.check == "db_coverage" for f in child_report.findings)


# ── CLI ─────────────────────────────────────────────────────────────


class TestCli:
    def _patch_loader(self, monkeypatch, config):
        import memtomem.cli.memory_doctor_cmd as mod

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

    def test_exit_1_on_error_finding(self, doctor_env, monkeypatch):
        config, mem_dir = doctor_env
        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        import asyncio

        async def _setup():
            await backend.initialize()
            _insert_chunk(backend, chunk_id="g1", source_file=mem_dir / "ghost.md")
            await backend.close()

        asyncio.run(_setup())
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor"])
        assert result.exit_code == 1  # stale_source is error-severity
        assert "no longer exist on disk" in result.output

    def test_json_payload_shape(self, doctor_env, monkeypatch):
        config, mem_dir = doctor_env
        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        import asyncio

        async def _setup():
            await backend.initialize()
            _insert_chunk(backend, chunk_id="a1", source_file=mem_dir / "alpha.md")
            await backend.close()

        asyncio.run(_setup())
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] in ("ok", "issues")
        assert "summary" in payload and set(payload["summary"]) == {"error", "warn", "info"}
        dir_entry = next(d for d in payload["dirs"] if d["path"] != "(unowned)")
        assert dir_entry["category"] == "claude-memory"
        assert dir_entry["index_file"] == "MEMORY.md"
        for f in dir_entry["findings"]:
            assert set(f) == {"check", "severity", "count", "summary", "items"}

    def test_unconfigured_path_errors(self, doctor_env, monkeypatch, tmp_path):
        config, _ = doctor_env
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", str(tmp_path / "nope")])
        assert result.exit_code != 0
        assert "not a configured memory_dir" in result.output


# ── Docs-as-tests parity ────────────────────────────────────────────
#
# These guards bind the public contract published in
# ``docs/guides/reference.md`` (§5 "Memory hygiene — `mm memory doctor`")
# directly to the implementation, so neither can drift unnoticed: the
# check/severity table is **parsed out of the markdown** and compared to the
# check/severity pairs **extracted from the command's own ``Finding(...)`` call
# sites via AST**. Adding, renaming, or re-classifying a check fails here until
# the reference table is edited to match — and vice versa. The budget caps
# quoted in the table, the ``--json`` status rule, and the help text are pinned
# too (memory ``feedback_docs_as_tests`` / ``feedback_docs_parity_canonical_fixture``).


def _docs_check_severities() -> dict[str, str]:
    """Parse the published check→severity table from ``reference.md``.

    Matches table rows whose first cell is a backticked identifier and whose
    second cell is a (optionally bold) severity word, so only the checks table
    rows are picked up — not the Glossary or other tables.
    """
    ref = Path(__file__).resolve().parents[3] / "docs" / "guides" / "reference.md"
    assert ref.is_file(), f"reference guide not found at {ref}"
    row = re.compile(r"^\|\s*`(\w+)`\s*\|\s*\*{0,2}(warn|error|info)\*{0,2}\s*\|")
    table: dict[str, str] = {}
    for line in ref.read_text(encoding="utf-8").splitlines():
        m = row.match(line)
        if m:
            table[m.group(1)] = m.group(2)
    return table


def _source_check_severities() -> dict[str, str]:
    """Extract the check→severity map from every ``Finding(...)`` call site in
    the command module via AST.

    Fails loudly if any ``Finding`` call omits a string-literal ``check`` /
    ``severity`` (e.g. a future check moved to a named constant) so a new check
    cannot slip past the parity guard by being non-literal.
    """
    import ast

    import memtomem.cli.memory_doctor_cmd as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Finding":
            continue
        calls += 1
        kw = {k.arg: k.value for k in node.keywords}
        check_node, sev_node = kw.get("check"), kw.get("severity")
        assert isinstance(check_node, ast.Constant) and isinstance(check_node.value, str), (
            f"Finding at line {node.lineno} has a non-literal/missing check= — "
            "the docs-parity guard requires string literals"
        )
        assert isinstance(sev_node, ast.Constant) and isinstance(sev_node.value, str), (
            f"Finding at line {node.lineno} has a non-literal/missing severity="
        )
        check, sev = check_node.value, sev_node.value
        # Same check at two call sites (index_missing) must agree on severity.
        assert found.get(check, sev) == sev, f"{check} has conflicting severities"
        found[check] = sev
    assert calls > 0, "no Finding(...) call sites found — was the symbol renamed?"
    return found


class TestDocsParity:
    def test_docs_table_matches_source(self):
        docs = _docs_check_severities()
        assert docs, "no check rows parsed from reference.md — did the table format change?"
        # The published markdown table must equal the implementation's checks.
        assert docs == _source_check_severities()

    def test_error_severity_set_documented(self):
        # The reference guide names exactly these three as the exit-1 drivers
        # (independent anchor: catches docs+source drifting together).
        errors = {c for c, s in _docs_check_severities().items() if s == "error"}
        assert errors == {"stale_source", "convention_violation", "broken_link"}

    def test_budget_caps_match_documented_numbers(self):
        import memtomem.cli.memory_doctor_cmd as mod

        # Quoted in the checks table: "24,400 bytes / 200 lines / 200 chars".
        assert mod._INDEX_MAX_BYTES == 24_400
        assert mod._INDEX_MAX_LINES == 200
        assert mod._INDEX_MAX_LINE_CHARS == 200

    def test_help_documents_usage_flags_and_exit_codes(self):
        result = CliRunner().invoke(cli, ["memory", "doctor", "--help"])
        assert result.exit_code == 0
        # Click hard-wraps help to the terminal width and breaks on hyphens, so
        # collapse whitespace and assert on non-hyphenated prose tokens.
        out = " ".join(result.output.split())
        for token in (
            "PATH",  # optional dir argument
            "--json",  # structured-output flag
            "Exit codes",  # exit-code documentation
            "stale DB sources",  # the three documented error-severity checks…
            "convention violations",
            "broken index links",
        ):
            assert token in out, f"help text missing {token!r}"

    def test_memory_group_lists_doctor(self):
        result = CliRunner().invoke(cli, ["memory", "--help"])
        assert result.exit_code == 0
        assert "doctor" in result.output
        assert "hygiene" in result.output

    def test_help_documents_fix_flags(self):
        # The Tier 2 write path (ADR-0020) must be discoverable from --help.
        # Click hard-wraps and breaks on hyphens, so assert only on the option
        # names and the underscore-joined (break-safe) scope token.
        result = CliRunner().invoke(cli, ["memory", "doctor", "--help"])
        assert result.exit_code == 0
        out = " ".join(result.output.split())
        assert "--fix" in out
        assert "--apply" in out
        assert "missing_target" in out  # the subtractive scope is documented

    def test_reference_documents_fix(self):
        # reference.md §5 gains the --fix surface when Tier 2 ships (ADR-0020
        # consequence). Pin the usage examples + the subtractive-scope wording
        # so docs can't silently drift from the shipped flags.
        ref = Path(__file__).resolve().parents[3] / "docs" / "guides" / "reference.md"
        text = ref.read_text(encoding="utf-8")
        assert "mm memory doctor --fix --apply" in text
        assert "Fixing broken links" in text  # the subsection heading
        assert "subtractive" in text.lower()
        assert "0020-memory-index-write-contract" in text  # ADR link

    def test_json_status_rule_matches_summary(self, capsys):
        # Documented rule: status is "issues" when any error/warn finding
        # exists; an info-only report stays "ok".
        from memtomem.cli.memory_doctor_cmd import DirReport, Finding, _emit_json

        report = DirReport(
            path="/d",
            category="user",
            index_file=None,
            exists=True,
            disk_indexable=1,
            db_covered=1,
        )
        report.findings.append(Finding(check="cold_candidate", severity="info", summary="x"))
        _emit_json([report])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"

        report.findings.append(Finding(check="db_coverage", severity="warn", summary="y"))
        _emit_json([report])
        assert json.loads(capsys.readouterr().out)["status"] == "issues"


# ── Tier 2: --fix line splice (ADR-0020 §2 — byte-exact preservation) ─
#
# The splice is the load-bearing primitive of the write contract: it must keep
# every surviving line's exact terminator (LF vs CRLF) and the file's
# trailing-newline state, and a no-removal splice must be a byte-for-byte
# identity. These cases are the round-trip proof ADR-0020 §2 requires.


class TestSpliceRoundTrip:
    # Each fixture varies the EOL style and trailing-newline state; the identity
    # case (no removal) must return the input unchanged byte-for-byte.
    @pytest.mark.parametrize(
        "text",
        [
            "- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n",  # LF, trailing newline
            "- [A](a.md)\n- [B](b.md)\n- [C](c.md)",  # LF, no trailing newline
            "- [A](a.md)\r\n- [B](b.md)\r\n- [C](c.md)\r\n",  # CRLF, trailing
            "- [A](a.md)\r\n- [B](b.md)\r\n- [C](c.md)",  # CRLF, no trailing
            "",  # empty file
            "\n\n",  # blank lines only
        ],
    )
    def test_no_removal_is_byte_identity(self, text):
        assert _splice_lines(text, set()) == text

    def test_removes_only_targeted_line_lf(self):
        text = "- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n"
        # Drop line 2 (B); A and C survive with their LF terminators.
        assert _splice_lines(text, {2}) == "- [A](a.md)\n- [C](c.md)\n"

    def test_removes_only_targeted_line_crlf_preserved(self):
        text = "- [A](a.md)\r\n- [B](b.md)\r\n- [C](c.md)\r\n"
        # Survivors keep CRLF — the splice never normalizes to LF.
        out = _splice_lines(text, {2})
        assert out == "- [A](a.md)\r\n- [C](c.md)\r\n"
        assert "\r\n" in out and out.count("\n") == 2

    def test_remove_last_line_without_trailing_newline(self):
        # Removing the final (un-terminated) line leaves the prior line's
        # terminator intact; no spurious newline is added or removed.
        text = "- [A](a.md)\n- [B](b.md)"
        assert _splice_lines(text, {2}) == "- [A](a.md)\n"

    def test_remove_first_of_no_trailing(self):
        text = "- [A](a.md)\n- [B](b.md)"
        assert _splice_lines(text, {1}) == "- [B](b.md)"


# ── Tier 2: missing_target-only subtractive guard (ADR-0020 §1) ───────


class TestMissingTargetGuard:
    def _index(self, tmp_path):
        """A claude-style index with one of every link-class + an existing file."""
        (tmp_path / "exists.md").write_text("x", encoding="utf-8")
        text = (
            "- [Ok](exists.md) — present\n"
            "- [Dead](gone.md) — missing target\n"
            "- [Escape](../../../etc/passwd) — outside root\n"
            "- [Web](https://example.com) — url\n"
            "- [Anchor](#section) — anchor\n"
        )
        return text

    def test_selects_only_missing_target(self, tmp_path):
        text = self._index(tmp_path)
        entries = _missing_target_entries(text, root=tmp_path)
        # ONLY the missing-target line — outside_root/url/anchor/ok excluded.
        assert [e.target for e in entries] == ["gone.md"]
        assert entries[0].line_no == 2

    def test_reappeared_target_not_selected(self, tmp_path):
        text = "- [Dead](gone.md) — x\n"
        assert _missing_target_entries(text, root=tmp_path)  # gone.md absent → selected
        (tmp_path / "gone.md").write_text("back", encoding="utf-8")
        assert _missing_target_entries(text, root=tmp_path) == []  # now present → spared


# ── Tier 2: locked apply path (ADR-0020 §5) ──────────────────────────


def _candidate_raws(text, root):
    # A list, not a set — multiplicity matters: the apply budget removes at most
    # as many occurrences of each raw as analysis saw (ADR-0020 §5).
    return [e.raw for e in _missing_target_entries(text, root=root)]


class TestApplyFix:
    def _setup(self, tmp_path):
        root = tmp_path / "mem"
        root.mkdir()
        (root / "exists.md").write_text("x", encoding="utf-8")
        return root

    def test_removes_missing_keeps_other_classes(self, tmp_path):
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = (
            "# TOC\n"
            "- [Ok](exists.md) — keep\n"
            "- [Dead](gone.md) — drop\n"
            "- [Escape](../../etc/passwd) — keep (outside_root, ambiguous)\n"
            "- [Web](https://x.com) — keep\n"
        )
        index.write_text(text, encoding="utf-8")
        removed = _apply_fix(index, root, _candidate_raws(text, root))
        assert [r[1] for r in removed] == ["- [Dead](gone.md) — drop"]
        out = index.read_text(encoding="utf-8")
        assert "gone.md" not in out
        # Every non-missing_target line — including outside_root — survives.
        for keep in ("# TOC", "exists.md", "etc/passwd", "https://x.com"):
            assert keep in out

    def test_crlf_survivors_preserved(self, tmp_path):
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Ok](exists.md) — keep\r\n- [Dead](gone.md) — drop\r\n"
        index.write_bytes(text.encode("utf-8"))  # write CRLF bytes verbatim
        _apply_fix(index, root, _candidate_raws(text, root))
        raw = index.read_bytes()
        # Survivor keeps CRLF; the dropped line is gone; no LF normalization.
        assert raw == b"- [Ok](exists.md) \xe2\x80\x94 keep\r\n"

    def test_no_trailing_newline_preserved(self, tmp_path):
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Ok](exists.md) — a\n- [Dead](gone.md) — b"  # no EOF newline
        index.write_text(text, encoding="utf-8")
        _apply_fix(index, root, _candidate_raws(text, root))
        # Dropping the un-terminated last line leaves the survivor's LF intact.
        assert index.read_text(encoding="utf-8") == "- [Ok](exists.md) — a\n"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits; NTFS ignores them (atomic_write preserves access via ACL inheritance)",
    )
    def test_file_mode_preserved_not_downgraded(self, tmp_path):
        import os
        import stat

        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n- [Ok](exists.md) — y\n"
        index.write_text(text, encoding="utf-8")
        os.chmod(index, 0o644)  # a typical TOC mode, NOT atomic_write's 0o600
        _apply_fix(index, root, _candidate_raws(text, root))
        assert stat.S_IMODE(index.stat().st_mode) == 0o644

    def test_revalidate_target_reappeared_is_spared(self, tmp_path):
        # Candidate collected while gone.md is absent; the target reappears
        # before apply (the locked re-classify sees it as ok → spares the line).
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n- [Ok](exists.md) — y\n"
        index.write_text(text, encoding="utf-8")
        candidates = _candidate_raws(text, root)
        (root / "gone.md").write_text("resurrected", encoding="utf-8")
        removed = _apply_fix(index, root, candidates)
        assert removed == []
        assert index.read_text(encoding="utf-8") == text  # untouched

    def test_revalidate_agent_edited_candidate_line_is_spared(self, tmp_path):
        # The agent rewrote the candidate line's hook since analysis. Its raw no
        # longer matches the candidate set, so the fix leaves it alone.
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — old hook\n"
        candidates = _candidate_raws(analysis_text, root)
        index.write_text("- [Dead](gone.md) — NEW hook\n", encoding="utf-8")
        removed = _apply_fix(index, root, candidates)
        assert removed == []
        assert index.read_text(encoding="utf-8") == "- [Dead](gone.md) — NEW hook\n"

    def test_agent_additions_carried_through_new_dead_spared(self, tmp_path):
        # Between analysis and apply the agent appended two lines: a real pointer
        # and a *new* dead pointer that was never a candidate. The fix removes
        # only the original candidate; both additions survive (the new dead one
        # is spared because it isn't in the candidate set — it may precede a file
        # the agent is about to create).
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — drop\n"
        candidates = _candidate_raws(analysis_text, root)
        fresh = (
            "- [Dead](gone.md) — drop\n"
            "- [Real](exists.md) — agent added\n"
            "- [Fresh](alsogone.md) — agent added, not yet on disk\n"
        )
        index.write_text(fresh, encoding="utf-8")
        removed = _apply_fix(index, root, candidates)
        assert [r[1] for r in removed] == ["- [Dead](gone.md) — drop"]
        out = index.read_text(encoding="utf-8")
        assert "- [Dead](gone.md) — drop" not in out
        assert "- [Real](exists.md) — agent added" in out
        assert "- [Fresh](alsogone.md) — agent added, not yet on disk" in out

    def test_revalidate_duplicate_dead_removes_only_analysis_count(self, tmp_path):
        # Count-bounded guard (ADR-0020 §5): analysis (T1) saw ONE dead line; the
        # agent added an *identical* dead pointer before apply (fresh has two).
        # The fix removes at most the analysis-time count — exactly one — so the
        # net of the agent's addition is preserved (one copy survives). Which
        # byte-identical copy survives is irrelevant; the spliced result is the
        # same either way. (Regression for the frozenset-membership bug that
        # removed every identical match, emptying the file.)
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        candidates = _candidate_raws("- [Dead](gone.md) — drop\n", root)
        assert len(candidates) == 1
        index.write_text("- [Dead](gone.md) — drop\n- [Dead](gone.md) — drop\n", encoding="utf-8")
        removed = _apply_fix(index, root, candidates)
        assert len(removed) == 1  # bounded by the analysis-time count, not "all matches"
        assert index.read_text(encoding="utf-8") == "- [Dead](gone.md) — drop\n"

    def test_apply_does_not_create_lock_artifacts_in_tree(self, tmp_path):
        # The sidecar lock lives next to the index file; it must be the only
        # extra artifact (no stray .tmp left behind after a successful replace).
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n"
        index.write_text(text, encoding="utf-8")
        _apply_fix(index, root, _candidate_raws(text, root))
        leftovers = {p.name for p in root.iterdir()} - {"MEMORY.md", "exists.md"}
        # Only the sidecar lockfile may remain; no .tmp residue from mkstemp.
        assert not any(name.endswith(".tmp") for name in leftovers)


# ── Tier 2: --fix CLI surface ────────────────────────────────────────


def _fix_env(tmp_path, monkeypatch, *, body):
    """A claude-memory dir with an existing file + a MEMORY.md *body*.

    Returns ``(config, mem_dir)``. The path classifies as ``claude-memory`` so
    ``--fix`` resolves the MEMORY.md index convention.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-fix-proj" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "exists.md").write_text("x", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(body, encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "fix.db"
    config.indexing.memory_dirs = [mem_dir]
    return config, mem_dir


class TestFixCli:
    def _patch_loader(self, monkeypatch, config):
        import memtomem.cli.memory_doctor_cmd as mod

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

    _BODY = "- [Ok](exists.md) — keep\n- [Dead](gone.md) — drop\n"

    def test_dry_run_previews_without_writing(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])
        assert result.exit_code == 0
        assert "Would remove" in result.output
        assert "gone.md" in result.output
        assert "--apply" in result.output
        # Dry-run must not touch the file.
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_apply_writes_and_removes_only_missing(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])
        assert result.exit_code == 0
        assert "Removed" in result.output
        out = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert out == "- [Ok](exists.md) — keep\n"

    def test_apply_without_fix_errors(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--apply"])
        assert result.exit_code != 0
        assert "--apply only applies with --fix" in result.output

    def test_clean_index_reports_nothing_to_remove(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body="- [Ok](exists.md) — keep\n")
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])
        assert result.exit_code == 0
        assert "No missing_target links to remove" in result.output

    def test_fix_json_shape(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] == "would-fix"
        assert payload["applied"] is False
        assert payload["summary"] == {"files": 1, "lines": 1}
        f = payload["files"][0]
        assert f["index_file"] == "MEMORY.md"
        assert f["removed"] == [{"line": 2, "text": "- [Dead](gone.md) — drop"}]

    def test_fix_json_apply_status(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] == "fixed"
        assert payload["applied"] is True
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == "- [Ok](exists.md) — keep\n"
