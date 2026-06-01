"""Tests for ``mm memory doctor`` (Tier 1 report-only hygiene report).

Four layers:

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
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.memory_doctor_cmd import (
    _gather_reports,
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
