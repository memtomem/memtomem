"""CLI: ``mm memory doctor`` — hygiene report (+ narrow ``--fix``) for memory stores.

Tier 1 of the ``mm memory doctor`` plan: surface 3-way drift between what's
on disk, what the agent-managed index file (e.g. Claude Code's ``MEMORY.md``)
points at, and what's actually indexed in the searchable DB. The default
command is report-only — no writes to disk, the DB, or config.

Tier 2 adds an opt-in ``--fix`` (ADR-0020): a *subtractive-only* curation that
deletes index-file pointer lines the doctor classifies as ``broken_link`` with
link-class ``missing_target`` (a ``- [title](target)`` whose target resolves
inside the memory root but points at a file that no longer exists). It never
adds, reorders, reformats, or budget-trims, and never touches the DB —
removing a provably-dead pointer is the one curation move that cannot conflict
with the agent's own intent. ``--fix`` is dry-run by default; ``--apply``
writes. See :func:`_run_fix` for the write contract.

Why this exists: a ``memory_dir`` can be *registered* yet barely indexed (the
fs watcher only reacts to live events, so files that landed while the server
was down stay invisible until a forced re-walk), and the index/TOC file can
drift from the files on disk. ``mem_search`` silently can't find the
un-indexed files; this command makes that visible.

Checks (per configured ``memory_dir``):

* **db_coverage** — files on disk the engine would index but that have zero
  chunks in the DB ("indexed N/M"). The headline signal.
* **stale_source** — DB chunks whose ``source_file`` is gone from disk (the
  file was deleted but its chunks linger; there is no single-file delete CLI).
* **convention_violation** — an index/meta file (``MEMORY.md`` / ``README.md``
  for a ``claude-memory`` dir) indexed as searchable content despite the
  provider convention.
* **broken_link** — links in the index file that don't resolve:
  ``missing_target`` (file gone) or ``outside_root`` (escapes the memory
  root). ``url`` and ``anchor`` links are classified and *not* reported.
* **index_orphan** — files on disk that the index file (``MEMORY.md``) does
  not list. Distinct from ``db_coverage``: "not in the TOC" ≠ "not indexed".
* **ambiguous_index_line** — a pointer line whose links cannot be resolved as
  written: a target that isn't a plain relative path (a paren, escape,
  character reference or angle-bracket form makes the literal text differ from
  what Markdown resolves), a link quoted inside a code span, or link syntax the
  grammar cannot close. Its links are left unclassified rather than guessed at,
  so the line is reported for a human instead of link-checked, counted as
  listed, or offered to ``--fix``.
* **budget** — the index file exceeds its byte / line / per-line-char budget
  (the hot cache loaded into the agent's context each session).
* **cold_candidate** — indexed files never accessed since indexing
  (``access_count`` sum 0 and ``last_accessed_at`` NULL). Informational.

Output: human glyphs by default, ``--json`` for a structured payload. Exit
``1`` when any *error*-severity finding exists (``stale_source``,
``convention_violation``, ``broken_link``), else ``0``. Coverage gaps,
orphans, budget and cold candidates are advisory (a partially-indexed dir is
a legitimate steady state) so they warn without failing the exit code —
mirrors ``mm sync-doctor`` (warns don't fail) while exposing a JSON + exit
code for CI like ``mm context settings-doctor``.

Read-only contract: config is read via ``Mem2MemConfig`` +
``load_config_d(quiet=True)`` + ``load_config_overrides(migrate=False)`` so
the diagnostic never triggers the legacy ``auto_discover`` config rewrite
(see PR #838 / #873). The DB is opened through a bare ``sqlite3`` connection
in URI ``mode=ro`` — never the full ``SqliteBackend``, which on
``initialize()`` would create the file/parent dir, run schema migration, and
checkpoint the WAL on close. ``mode=ro`` still surfaces committed rows in a
live writer's WAL (unlike ``immutable=1``); a missing or too-old DB degrades
to disk/index-only checks instead of being created. Discovery reuses the
engine's own ``discover_indexable_files`` (via a storage-less, model-less
engine) so the "should be indexed" set can't drift from what the real indexer
does.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import click

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig

# ── Index-file budget ───────────────────────────────────────────────
# Hot-cache ceiling for an agent-managed index file (``MEMORY.md``). The
# TOC is loaded into the agent's context every session, so it has a budget
# the curation process targets. These live here, not in
# ``ProviderIndexConvention``, because Tier 1 is the only consumer; the
# write-contract ADR promotes them to config when ``--fix`` needs to enforce
# them. Per-line cap is measured in characters (not bytes) so CJK prose isn't
# double-counted.
_INDEX_MAX_BYTES = 24_400
_INDEX_MAX_LINES = 200
_INDEX_MAX_LINE_CHARS = 200

Severity = Literal["error", "warn", "info"]

_GLYPH: dict[Severity, str] = {"error": "✗", "warn": "!", "info": "·"}
_COLOR: dict[Severity, str | None] = {"error": "red", "warn": "yellow", "info": None}

# The exit code flips to 1 when any finding carries ``severity="error"`` (see
# ``memory_doctor``). Today that's ``stale_source`` / ``convention_violation``
# / ``broken_link``; severity is assigned per-finding so the exit logic needs
# no separate check allowlist to stay in sync.

# Max sample items echoed per finding in human output (JSON carries all).
_SAMPLE_LIMIT = 8


# ── Pure: index-file parsing ────────────────────────────────────────


@dataclass(frozen=True)
class IndexEntry:
    """One ``[title](target)`` link occurrence on a pointer line.

    The harness contract is one entry per line, but an index that packs several
    onto a line is parsed faithfully rather than silently truncated: every link
    on a bullet line becomes an entry, so entries may share a ``line_no`` and
    ``raw`` (which is always the whole line, never the link's slice of it).
    """

    line_no: int  # 1-based
    title: str
    target: str
    raw: str


@dataclass(frozen=True)
class ParsedIndex:
    """Parsed index file: pointer entries plus every other (preserved) line.

    ``other_lines`` keeps prose / comments / blanks verbatim with their line
    numbers so a future write phase can round-trip the file; Tier 1 only reads
    them for the budget measurement.

    ``ambiguous_lines`` holds the 1-based numbers of pointer lines the link
    grammar could not consume unambiguously (see :func:`_line_is_ambiguous`).
    Their entries are still parsed — the read is the best available, and the
    raw text is what a human needs to see to repair the line — but a target
    read off such a line may not be the path Markdown resolves, so callers must
    not treat it as evidence: not of a broken link, not of a listed file, and
    least of all of a line safe to delete.
    """

    entries: tuple[IndexEntry, ...]
    other_lines: tuple[tuple[int, str], ...]
    ambiguous_lines: frozenset[int] = frozenset()


# A pointer line is a list item; anything else (headers, prose, comments) is
# preserved verbatim and never link-classified.
_BULLET_RE = re.compile(r"^\s*[-*]\s")

# One ``[title](target)`` link, matched with ``finditer`` so *every* link on a
# pointer line yields an entry. The title is bounded by the first ``]`` so hook
# brackets don't get pulled in; the target by the first ``)``.
_LINK_RE = re.compile(r"\[(?P<title>[^\]]*)\]\((?P<target>[^)]*)\)")

# Link syntax left *outside* every match: the matcher consumed the line's links
# wrong, or there is one it could not see at all.
_UNMATCHED_LINK_SYNTAX_RE = re.compile(r"\]\(")

# What a target must look like for its literal text to be the path Markdown
# resolves: no whitespace, and none of the characters that make Markdown
# transform a destination before resolving it.
#
# This is a whitelist on purpose. The alternative — enumerating the ways a
# destination can lie — does not converge: ``notes_(v2).md`` truncates at the
# inner paren, ``notes_\(v2.md`` hides an escape, ``<x y.md>`` is the angle
# form, ``notes_&amp;v2.md`` is a character reference, and each of those was
# found only after the last was fixed. A pointer in a memory index is a
# relative filename; anything fancier is set aside for a human rather than
# guessed at, which fails closed by construction instead of by enumeration.
_SAFE_TARGET_RE = re.compile(r"^[^\s`<>\\&()\[\]]+$")

# A code span, delimited by a *run* of backticks (CommonMark: any run closes on
# a matching run, so ``` ``[x](y)`` ``` is one span). Its contents are literal
# text, so a link matched inside one is not a pointer at all.
_CODE_SPAN_RE = re.compile(r"(`+).*?\1")


def _parens_balanced(text: str) -> bool:
    """Whether ``(``/``)`` nest properly and close out in *text*."""
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _line_is_ambiguous(line: str, matches: list[re.Match[str]]) -> bool:
    """Whether ``_LINK_RE`` may have mis-read *line*'s links.

    ``_LINK_RE`` is a restricted grammar, not a Markdown parser, so it cannot
    itself tell a clean read from a wrong one: ``[Live](notes_(v2).md)`` matches
    with target ``notes_(v2``, which then classifies ``missing_target`` even
    though the pointer is live. Deleting such a line on that "evidence" would be
    data loss (ADR-0020 §1 permits removing *provably* dead pointers only), and
    reporting it as an error would fail CI on a healthy index.

    A line reads cleanly only when all three hold; anything else is set aside:

    * **Every target is a plain relative path** (:data:`_SAFE_TARGET_RE`) — the
      whitelist that keeps this from being an endless hunt for the next way a
      destination can lie.
    * **No link syntax outside the matches, and balanced parens in what's left**
      — ``](`` in the residue means a link went unread; an unbalanced ``)``
      means a target was cut short at a paren inside it. Balanced parens are
      ordinary prose, so ``- [A](a.md) — decision ([why](b.md))``, the common
      nested-hook shape, reads cleanly and stays fixable.
    * **No link matched inside a code span** — literal text that merely looks
      like a pointer.

    Callers must not read a ``False`` here as "this line is well-formed
    Markdown"; it means only that these links can be resolved as written.
    """
    residue_parts: list[str] = []
    last = 0
    for m in matches:
        residue_parts.append(line[last : m.start()])
        last = m.end()
    residue_parts.append(line[last:])
    residue = "".join(residue_parts)
    if _UNMATCHED_LINK_SYNTAX_RE.search(residue) or not _parens_balanced(residue):
        return True
    if any(not _SAFE_TARGET_RE.match(m.group("target")) for m in matches):
        return True
    code_spans = [(c.start(), c.end()) for c in _CODE_SPAN_RE.finditer(line)]
    return any(start <= m.start() and m.end() <= end for m in matches for start, end in code_spans)


def parse_memory_index(text: str) -> ParsedIndex:
    """Parse an index file into pointer entries + preserved other lines."""
    entries: list[IndexEntry] = []
    other: list[tuple[int, str]] = []
    ambiguous: set[int] = set()
    for i, line in enumerate(text.splitlines(), start=1):
        is_bullet = bool(_BULLET_RE.match(line))
        matches = list(_LINK_RE.finditer(line)) if is_bullet else []
        if not matches:
            # A bullet holding link syntax the grammar could not close
            # (``- [B](b.md``) yields no entry, so it would otherwise slip by as
            # prose — an unread pointer reported as nothing at all. It has no
            # entry to carry, but the line still gets flagged for a human.
            if is_bullet and _UNMATCHED_LINK_SYNTAX_RE.search(line):
                ambiguous.add(i)
            other.append((i, line))
            continue
        if _line_is_ambiguous(line, matches):
            ambiguous.add(i)
        entries.extend(
            IndexEntry(
                line_no=i,
                title=m.group("title").strip(),
                target=m.group("target").strip(),
                raw=line,
            )
            for m in matches
        )
    return ParsedIndex(
        entries=tuple(entries),
        other_lines=tuple(other),
        ambiguous_lines=frozenset(ambiguous),
    )


# ── Pure: link classification ───────────────────────────────────────

LinkClass = Literal["ok", "missing_target", "outside_root", "url", "anchor"]

# A URL needs an explicit ``scheme://`` (so a Windows drive ``C:\…`` isn't
# misread as a scheme) plus the ``mailto:`` special case.
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _is_url(target: str) -> bool:
    return bool(_URL_RE.match(target)) or target.startswith("mailto:")


def classify_link(target: str, *, root: Path, source_dir: Path) -> LinkClass:
    """Classify an index/markdown link target.

    ``root`` is the memory dir (links may not escape it); ``source_dir`` is
    the directory of the file the link lives in (for resolving relatives).
    Resolution is path-only — ``Path.resolve()`` is strict=False so a missing
    target still resolves and is then existence-checked, separating
    ``missing_target`` from ``outside_root``.
    """
    t = target.strip()
    if not t or t.startswith("#"):
        return "anchor"  # in-page anchor or empty — not a file reference
    if _is_url(t):
        return "url"
    path_part = t.split("#", 1)[0]  # strip ``file.md#section`` anchor suffix
    if not path_part:
        return "anchor"
    raw = Path(path_part).expanduser()
    base = raw if raw.is_absolute() else (source_dir / raw)
    try:
        resolved = base.resolve()
        root_resolved = root.resolve()
    except (OSError, RuntimeError):
        return "missing_target"
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return "outside_root"
    return "ok" if resolved.exists() else "missing_target"


# ── Pure: budget measurement ────────────────────────────────────────


@dataclass(frozen=True)
class BudgetMeasure:
    byte_len: int
    line_count: int
    overlong_lines: tuple[int, ...]  # 1-based line numbers exceeding the char cap

    @property
    def over_budget(self) -> bool:
        return (
            self.byte_len > _INDEX_MAX_BYTES
            or self.line_count > _INDEX_MAX_LINES
            or bool(self.overlong_lines)
        )


def measure_budget(text: str) -> BudgetMeasure:
    """Measure an index file against the hot-cache budget.

    Bytes are UTF-8 encoded length; lines are counted from ``splitlines``;
    per-line length is character count (``len``) so a CJK line isn't penalised
    for its multi-byte encoding.
    """
    lines = text.splitlines()
    overlong = tuple(
        i for i, line in enumerate(lines, start=1) if len(line) > _INDEX_MAX_LINE_CHARS
    )
    return BudgetMeasure(
        byte_len=len(text.encode("utf-8")),
        line_count=len(lines),
        overlong_lines=overlong,
    )


# ── Finding + report model ──────────────────────────────────────────


@dataclass
class Finding:
    check: str
    severity: Severity
    summary: str
    items: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def to_json(self) -> dict[str, object]:
        return {
            "check": self.check,
            "severity": self.severity,
            "count": self.count,
            "summary": self.summary,
            "items": self.items,
        }


@dataclass
class DirReport:
    path: str
    category: str
    index_file: str | None
    exists: bool
    disk_indexable: int
    db_covered: int
    findings: list[Finding] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "category": self.category,
            "index_file": self.index_file,
            "exists": self.exists,
            "disk_indexable": self.disk_indexable,
            "db_covered": self.db_covered,
            "findings": [f.to_json() for f in self.findings],
        }


# ── Read-only config + engine plumbing ──────────────────────────────


def _load_config_read_only() -> Mem2MemConfig:
    """Load config without triggering the legacy auto-discover migration.

    Mirrors ``mm sync-doctor``: a read-only diagnostic must not rewrite
    ``config.json`` as a side effect (``migrate=False``).
    """
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    config = Mem2MemConfig()
    load_config_d(config, quiet=True)
    load_config_overrides(config, migrate=False)
    return config


def _build_discovery_engine(config: Mem2MemConfig) -> object:
    """Build an ``IndexEngine`` purely for its file-discovery method.

    ``discover_indexable_files`` reads only ``config`` (supported extensions,
    index roots, exclude rules) — it never touches storage or the embedder —
    so both are passed as inert stand-ins (``None`` storage, a model-less
    ``NoopEmbedder``). Reusing the engine's own discovery is deliberate: the
    doctor's "should be indexed" set is then guaranteed identical to what the
    real indexer produces, so coverage / orphan counts can't drift from
    reality.
    """
    from memtomem.embedding.noop import NoopEmbedder
    from memtomem.indexing.engine import IndexEngine

    return IndexEngine(
        storage=None,  # type: ignore[arg-type]
        embedder=NoopEmbedder(),  # type: ignore[arg-type]
        config=config.indexing,
        namespace_config=config.namespace,
    )


# Per-source aggregate the cold-candidate / coverage checks consume.
# ``MAX(last_accessed_at)`` over ISO-8601 text sorts chronologically and
# SQLite's ``MAX`` ignores NULL, so a never-accessed multi-chunk file reports
# ``(…, None, 0, …)``. ``COALESCE`` guards an all-NULL importance column.
_SOURCE_SIGNALS_SQL = (
    "SELECT source_file, COUNT(*), MAX(last_accessed_at),"
    " COALESCE(SUM(access_count), 0),"
    " COALESCE(MAX(importance_score), 0.0),"
    " COALESCE(AVG(importance_score), 0.0)"
    " FROM chunks GROUP BY source_file ORDER BY source_file"
)


def _read_source_signals(
    db_path: Path,
) -> list[tuple[Path, int, str | None, int, float, float]] | None:
    """Read per-source signal rows from an *existing* DB, strictly read-only.

    Opens the SQLite file with ``mode=ro`` (URI) so the doctor can never
    create the file, run a schema migration, or checkpoint the WAL — the
    report-only contract. Unlike ``immutable=1``, ``mode=ro`` still surfaces
    committed rows sitting in an active writer's WAL (e.g. while ``mm web`` is
    up), so the report reflects live state. Returns ``None`` — not an empty
    list — when the DB is absent, its schema predates the aggregate's columns
    (a fresh or very old install), or the file is corrupt / not a database;
    the caller then degrades to disk/index-only checks rather than crashing or
    creating the DB. ``sqlite3.DatabaseError`` is the parent of
    ``OperationalError`` (missing table/column) and also covers the
    "file is not a database" / malformed-image cases, so one except clause
    handles every "can't read this DB" path.
    """
    db = db_path.expanduser()
    if not db.is_absolute():
        db = db.absolute()
    if not db.exists():
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(_SOURCE_SIGNALS_SQL).fetchall()
    except sqlite3.DatabaseError:
        return None  # missing/old-schema/corrupt — degrade to disk-only checks
    finally:
        if conn is not None:
            conn.close()
    return [(Path(r[0]), int(r[1]), r[2], int(r[3]), float(r[4]), float(r[5])) for r in rows]


# ── Analysis ────────────────────────────────────────────────────────


def _analyze_dir(
    *,
    dir_path: Path,
    config: Mem2MemConfig,
    engine: object,
    db_rows: list[tuple[Path, int, str | None, int, float, float]],
    memory_dirs: list[Path],
) -> DirReport:
    """Run every check for one configured ``memory_dir``. Pure given inputs.

    ``memory_dirs`` is the full set of configured roots — needed to attribute
    each discovered disk file to its *most-specific* owning root, the same
    longest-prefix rule the DB rows are bucketed by. Without that, a recursive
    walk of a parent root would pull in files that belong to a nested child
    root (whose DB rows are bucketed to the child), making the parent falsely
    report the child's already-indexed files as uncovered.
    """
    from memtomem.config import (
        categorize_memory_dir,
        index_excluded_filenames,
        provider_index_file,
    )
    from memtomem.indexing.engine import resolve_owning_memory_dir
    from memtomem.storage.sqlite_helpers import norm_path

    resolved_dir = dir_path.expanduser().resolve()
    exists = resolved_dir.is_dir()
    category = categorize_memory_dir(dir_path)
    index_file_name = provider_index_file(category)
    excluded = index_excluded_filenames(category)
    dir_key = norm_path(dir_path.expanduser())

    # Disk side: files the engine would index (post-exclusion, recursive —
    # faithful to the real indexer), keeping only the files this dir is the
    # most-specific owner of (symmetry with the DB bucketing in
    # ``_gather_reports`` — a nested child root owns its own subtree).
    disk_files = engine.discover_indexable_files(resolved_dir)  # type: ignore[attr-defined]
    disk_norm: dict[str, Path] = {}
    for p in disk_files:
        owning = resolve_owning_memory_dir(p, memory_dirs)
        if owning is not None and norm_path(owning) == dir_key:
            disk_norm[norm_path(p)] = p

    # DB side: source-file signal rows already bucketed to this dir.
    db_norm: dict[str, tuple[Path, int, str | None, int, float, float]] = {
        norm_path(row[0]): row for row in db_rows
    }

    report = DirReport(
        path=str(resolved_dir),
        category=category,
        index_file=index_file_name,
        exists=exists,
        disk_indexable=len(disk_norm),
        db_covered=len(disk_norm.keys() & db_norm.keys()),
    )

    # 1. db_coverage — on disk, no chunks.
    uncovered = sorted(p for k, p in disk_norm.items() if k not in db_norm)
    if uncovered:
        report.findings.append(
            Finding(
                check="db_coverage",
                severity="warn",
                summary=(
                    f"{len(uncovered)}/{len(disk_norm)} indexable file(s) have no DB "
                    "chunks — `mem_search` can't find them (run `mm index <dir> --force`)"
                ),
                items=[p.name for p in uncovered],
            )
        )

    # 2/3. DB-only sources — split into stale (deleted), convention violation
    # (meta file indexed), and an unexpected residue.
    stale: list[str] = []
    violations: list[str] = []
    unexpected: list[str] = []
    for k, row in db_norm.items():
        if k in disk_norm:
            continue
        src = row[0]
        if not Path(src).exists():
            stale.append(str(src))
        elif Path(src).name in excluded:
            violations.append(str(src))
        else:
            unexpected.append(str(src))
    if stale:
        report.findings.append(
            Finding(
                check="stale_source",
                severity="error",
                summary=(
                    f"{len(stale)} DB source file(s) no longer exist on disk — "
                    "chunks linger after the file was deleted"
                ),
                items=sorted(stale),
            )
        )
    if violations:
        report.findings.append(
            Finding(
                check="convention_violation",
                severity="error",
                summary=(
                    f"{len(violations)} index/meta file(s) indexed as content despite the "
                    f"{category} convention (run `mm purge --matching-excluded --apply`)"
                ),
                items=sorted(violations),
            )
        )
    if unexpected:
        report.findings.append(
            Finding(
                check="db_extra",
                severity="warn",
                summary=(
                    f"{len(unexpected)} DB source file(s) exist on disk but fall outside the "
                    "current indexable set (unsupported extension or excluded path)"
                ),
                items=sorted(unexpected),
            )
        )

    # 4. cold_candidate — covered files never accessed since indexing.
    cold = [
        (row[0], row[1])  # (path, chunk_count)
        for k, row in db_norm.items()
        if k in disk_norm and row[3] == 0 and row[2] is None
    ]
    if cold:
        cold.sort(key=lambda pc: (-pc[1], str(pc[0])))
        report.findings.append(
            Finding(
                check="cold_candidate",
                severity="info",
                summary=(
                    f"{len(cold)} indexed file(s) never accessed since indexing "
                    "(access_count 0, last_accessed_at unset)"
                ),
                items=[f"{p.name} ({c} chunk{'s' if c != 1 else ''})" for p, c in cold],
            )
        )

    # 5/6/7. Index-file checks (only for providers with a TOC convention).
    if index_file_name:
        _analyze_index_file(
            report=report,
            root=resolved_dir,
            index_file_name=index_file_name,
            disk_norm=disk_norm,
            norm_path=norm_path,
        )

    return report


def _analyze_index_file(
    *,
    report: DirReport,
    root: Path,
    index_file_name: str,
    disk_norm: dict[str, Path],
    norm_path: object,
) -> None:
    """Broken-link, index-orphan and budget checks against the TOC file."""
    index_path = root / index_file_name
    try:
        text = index_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        report.findings.append(
            Finding(
                check="index_missing",
                severity="warn",
                summary=f"index file {index_file_name} not found in {root}",
            )
        )
        return
    except OSError as exc:
        report.findings.append(
            Finding(
                check="index_missing",
                severity="warn",
                summary=f"could not read {index_file_name}: {exc}",
            )
        )
        return

    parsed = parse_memory_index(text)

    # broken_link — classify every pointer target; report only the broken
    # classes. ``listed_norm`` collects the resolvable targets for the orphan
    # check below.
    #
    # An ambiguous line's targets are not evidence of anything: the text read as
    # a target may not be the path Markdown resolves. Such a line is reported
    # once, on its own, and contributes to *neither* conclusion — not a
    # ``broken_link`` error (it would fail a run over a healthy index) and not a
    # ``listed`` entry (a coincidentally-existing mis-slice would silently mark
    # the wrong file as indexed). The pointed-at file may therefore also surface
    # as an ``index_orphan``; that is the honest read, since the doctor cannot
    # tell which file the line means.
    broken: list[str] = []
    ambiguous_items: list[str] = []
    listed_norm: set[str] = set()
    for entry in parsed.entries:
        if entry.line_no in parsed.ambiguous_lines:
            continue
        cls = classify_link(entry.target, root=root, source_dir=root)
        if cls in ("missing_target", "outside_root"):
            broken.append(f"L{entry.line_no} [{cls}] {entry.target}")
        elif cls == "ok":
            resolved = (root / entry.target.split("#", 1)[0]).resolve()
            listed_norm.add(norm_path(resolved))  # type: ignore[operator]
    # Read the raw text back from the source: an ambiguous line need not have
    # produced an entry (a link the grammar could not close yields none).
    raw_lines = text.splitlines()
    for line_no in sorted(parsed.ambiguous_lines):
        ambiguous_items.append(f"L{line_no} {raw_lines[line_no - 1]}")
    if broken:
        report.findings.append(
            Finding(
                check="broken_link",
                severity="error",
                summary=f"{len(broken)} broken link(s) in {index_file_name}",
                items=broken,
            )
        )
    if ambiguous_items:
        report.findings.append(
            Finding(
                check="ambiguous_index_line",
                severity="warn",
                summary=(
                    f"{len(ambiguous_items)} line(s) in {index_file_name} carry link syntax this "
                    "parser cannot read unambiguously (a link inside a code span, an escaped or "
                    "angle-bracketed target, or a parenthesis inside the target) — their links "
                    "are neither link-checked nor counted as listed, and --fix will not touch "
                    "them; simplify the line's markdown to bring it back under the checks"
                ),
                items=ambiguous_items,
            )
        )

    # index_orphan — indexable files on disk the TOC doesn't point at. The
    # index file itself / excluded meta files are not in ``disk_norm`` (the
    # engine excludes them), so they're never flagged here.
    orphans = sorted(p for k, p in disk_norm.items() if k not in listed_norm)
    if orphans:
        report.findings.append(
            Finding(
                check="index_orphan",
                severity="warn",
                summary=(
                    f"{len(orphans)} file(s) on disk not listed in {index_file_name} "
                    "(index orphans — present and indexable but absent from the TOC)"
                ),
                items=[p.name for p in orphans],
            )
        )

    # budget — hot-cache size.
    budget = measure_budget(text)
    if budget.over_budget:
        parts = [f"{budget.byte_len} bytes (cap {_INDEX_MAX_BYTES})"]
        parts.append(f"{budget.line_count} lines (cap {_INDEX_MAX_LINES})")
        if budget.overlong_lines:
            shown = ", ".join(f"L{n}" for n in budget.overlong_lines[:_SAMPLE_LIMIT])
            parts.append(
                f"{len(budget.overlong_lines)} line(s) over {_INDEX_MAX_LINE_CHARS} chars ({shown})"
            )
        report.findings.append(
            Finding(
                check="budget",
                severity="warn",
                summary=f"{index_file_name} over budget: " + "; ".join(parts),
                items=[f"L{n}" for n in budget.overlong_lines],
            )
        )


def _gather_reports(
    *,
    config: Mem2MemConfig,
    inspect_dirs: list[Path],
) -> list[DirReport]:
    """Read DB signals read-only, bucket by owning dir, analyze each inspected dir.

    Fully synchronous: the only DB access is the read-only aggregate in
    :func:`_read_source_signals`, and discovery is a sync disk walk — no
    embedder, no async storage backend, no writes.
    """
    from collections import defaultdict

    from memtomem.indexing.engine import resolve_owning_memory_dir
    from memtomem.storage.sqlite_helpers import norm_path

    raw_signals = _read_source_signals(Path(config.storage.sqlite_path))
    db_unreadable = raw_signals is None
    signals = raw_signals or []
    memory_dirs = config.indexing.all_index_roots()

    # Bucket every DB source row to the configured dir that owns it
    # (longest-prefix), keyed by the resolved dir string so it matches the
    # per-dir loop below. Unowned rows are surfaced separately.
    by_dir: dict[str, list[tuple[Path, int, str | None, int, float, float]]] = defaultdict(list)
    unowned = 0
    for row in signals:
        owning = resolve_owning_memory_dir(row[0], memory_dirs)
        if owning is None:
            unowned += 1
        else:
            by_dir[norm_path(owning)].append(row)

    engine = _build_discovery_engine(config)

    reports: list[DirReport] = []
    for d in inspect_dirs:
        key = norm_path(d.expanduser())
        reports.append(
            _analyze_dir(
                dir_path=d,
                config=config,
                engine=engine,
                db_rows=by_dir.get(key, []),
                memory_dirs=memory_dirs,
            )
        )

    if db_unreadable:
        # Top-level note: no readable DB, so coverage/stale/cold are unknown
        # and every disk file shows as uncovered. Info — never fails the exit.
        db_report = _note_report("(database)")
        db_report.findings.append(
            Finding(
                check="db_unavailable",
                severity="info",
                summary=(
                    f"no readable memtomem DB at {Path(config.storage.sqlite_path).expanduser()} "
                    "— reporting disk/index-only checks (run `mm index` to build it)"
                ),
            )
        )
        reports.append(db_report)

    if unowned:
        # Top-level info: chunks attributed to no configured memory_dir
        # (e.g. a dir was removed from config, or content added elsewhere).
        # Never affects the exit code.
        unowned_report = _note_report("(unowned)")
        unowned_report.findings.append(
            Finding(
                check="unowned_chunks",
                severity="info",
                summary=(
                    f"{unowned} DB source file(s) under no configured memory_dir "
                    "(removed dir, or content added outside the registry)"
                ),
            )
        )
        reports.append(unowned_report)

    return reports


# ── Rendering ───────────────────────────────────────────────────────


# A "note" report is a synthetic top-level entry (not a real ``memory_dir``):
# its parenthesized path is the sentinel. Carries only top-level findings.
def _note_report(label: str) -> DirReport:
    return DirReport(
        path=label, category="", index_file=None, exists=False, disk_indexable=0, db_covered=0
    )


def _is_note(report: DirReport) -> bool:
    return report.path.startswith("(")


def _severity_totals(reports: list[DirReport]) -> dict[str, int]:
    totals = {"error": 0, "warn": 0, "info": 0}
    for r in reports:
        for f in r.findings:
            totals[f.severity] += 1
    return totals


def _emit_human(reports: list[DirReport]) -> None:
    for r in reports:
        if _is_note(r):
            for f in r.findings:
                click.secho(f"{_GLYPH[f.severity]} {f.summary}", fg=_COLOR[f.severity])
            continue
        header = f"{r.path}"
        suffix = []
        if not r.exists:
            suffix.append("missing")
        if r.index_file:
            suffix.append(f"index={r.index_file}")
        suffix.append(f"indexed {r.db_covered}/{r.disk_indexable}")
        click.secho(f"\n■ {header}", bold=True)
        click.echo(f"  {r.category} · " + " · ".join(suffix))
        if not r.findings:
            click.secho("  ✓ no issues", fg="green")
            continue
        for f in r.findings:
            click.secho(f"  {_GLYPH[f.severity]} {f.summary}", fg=_COLOR[f.severity])
            for item in f.items[:_SAMPLE_LIMIT]:
                click.echo(f"      - {item}")
            if f.count > _SAMPLE_LIMIT:
                click.echo(f"      … and {f.count - _SAMPLE_LIMIT} more")

    totals = _severity_totals(reports)
    click.echo(f"\nSummary: {totals['error']} error, {totals['warn']} warn, {totals['info']} info.")


def _emit_json(reports: list[DirReport]) -> None:
    totals = _severity_totals(reports)
    payload = {
        "status": "issues" if totals["error"] or totals["warn"] else "ok",
        "dirs": [r.to_json() for r in reports],
        "summary": totals,
    }
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


# ── Tier 2: subtractive --fix (ADR-0020) ────────────────────────────
#
# ``--fix`` deletes index-file pointer lines whose link-class is
# ``missing_target`` and nothing else. The contract (ADR-0020):
#   §1 subtractive-only, missing_target-only (outside_root et al. excluded);
#   §2 byte-exact preservation of every surviving line via a line *splice*
#      (keepends) of the ORIGINAL text — never a reconstruction from parsed
#      fields, which would lose CRLF / trailing-newline state;
#   §4 dry-run by default, ``--apply`` to write;
#   §5 atomic write (mode-preserving) under the sidecar lock, re-validating
#      each candidate against fresh content so concurrent agent edits survive.


@dataclass
class FixFileResult:
    """Per-index-file outcome of a ``--fix`` run (preview or applied)."""

    path: str  # the resolved memory_dir
    index_file: str  # the index filename (e.g. MEMORY.md)
    applied: bool
    removed: list[tuple[int, str]]  # (1-based line_no, raw line text)

    def to_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "index_file": self.index_file,
            "removed": [{"line": n, "text": t} for n, t in self.removed],
        }


def _missing_target_entries(text: str, *, root: Path) -> list[IndexEntry]:
    """Pointer entries in *text* whose target classifies as ``missing_target``.

    The single link-class ``--fix`` removes (ADR-0020 §1): a pointer that
    resolves *inside* the memory root but points at a file that does not exist.
    ``outside_root`` / ``url`` / ``anchor`` / ``ok`` are all left untouched —
    only a provably-dead in-root reference is safe to delete subtractively.
    """
    parsed = parse_memory_index(text)
    return [
        e
        for e in parsed.entries
        if classify_link(e.target, root=root, source_dir=root) == "missing_target"
    ]


# The pre-#1757 entry grammar: line-anchored, at most one match per line. The
# parser no longer uses it — it now reads every link on a line — but ``--fix``
# still does, to keep its write scope frozen at exactly the shape it could
# already delete before the parser widened. Relaxing that scope is an ADR-0020
# §1 change and lands with the amendment, not here.
_LEGACY_ENTRY_SHAPE_RE = re.compile(r"^\s*[-*]\s*\[(?P<title>[^\]]*)\]\((?P<target>[^)]*)\)")


def _unfixable_lines(entries: list[IndexEntry], *, ambiguous_lines: frozenset[int]) -> list[str]:
    """Reasons *entries*' lines are out of ``--fix``'s scope, one per line.

    ``--fix`` removes a dead pointer by splicing out its **whole line**
    (:func:`_splice_lines`), which is only sound while "one line = one entry"
    holds — the contract the harness itself states for ``MEMORY.md``. Three
    shapes break that premise, and all three fail closed:

    * **Several entries on one line.** Splicing the line for one dead target
      would also delete every *live* entry beside it.
    * **A line the entry grammar cannot read unambiguously.** ``--fix`` may only
      remove a *provably* dead pointer (ADR-0020 §1); a target mis-sliced out of
      ``[Live](notes_(v2).md)`` classifies ``missing_target`` while naming a file
      that exists, so the proof does not hold.
    * **A pointer the legacy grammar never saw.** The widened parser now reads
      prose-prefixed lines (``- NS: [a](x.md)``), which ``--fix`` could not
      previously delete. Widening the write scope on the back of a parser fix
      would smuggle a contract change in; it waits for the amendment.

    Returns one ``L{n}: {raw}`` line per offending line (deduped, in file
    order), or an empty list when every candidate is safely fixable.
    """
    reasons: dict[int, str] = {}
    for e in entries:
        if e.line_no in reasons:
            continue
        if len(_LINK_RE.findall(e.raw)) > 1:
            why = "carries more than one entry"
        elif e.line_no in ambiguous_lines:
            why = "cannot be read unambiguously (code span, stray bracket, or paren in target)"
        elif _LEGACY_ENTRY_SHAPE_RE.match(e.raw) is None:
            why = "does not have the one-entry-per-line shape --fix is contracted for"
        else:
            continue
        reasons[e.line_no] = f"      - L{e.line_no}: {e.raw}\n        ({why})"
    return [reasons[n] for n in sorted(reasons)]


def _splice_lines(original_text: str, remove_line_nos: set[int]) -> str:
    """Return *original_text* with the given 1-based lines removed, byte-exact.

    ADR-0020 §2: every surviving line keeps its exact end-of-line terminator
    (LF vs CRLF) and the file's trailing-newline state is untouched. The
    mechanism is a *splice* of the original text — ``splitlines(keepends=True)``
    has the same 1-based line indexing as the parser's terminator-stripped
    ``splitlines()``, so a parser ``line_no`` maps directly to an index here —
    NOT a reconstruction from ``IndexEntry.raw`` / ``other_lines`` (which are
    terminator-stripped and could not round-trip CRLF or the EOF newline).
    """
    return "".join(
        line
        for i, line in enumerate(original_text.splitlines(keepends=True), start=1)
        if i not in remove_line_nos
    )


def _apply_fix(index_path: Path, root: Path, candidate_raws: list[str]) -> list[tuple[int, str]]:
    """Under the sidecar lock, splice still-dead candidate lines out of *index_path*.

    Implements ADR-0020 §5's concurrency-aware write. All work happens while
    holding the ``_file_lock`` sidecar lock so a concurrent *memtomem* writer is
    serialized; the agent (the memory hook) does not honor the lock, so a
    residual sub-``os.replace`` race remains and is accepted as bounded — see
    the ADR.

    1. **Fresh, newline-preserving re-read** (``read_bytes().decode`` — NOT
       ``read_text``, which would normalize CRLF→LF before the splice could
       preserve it).
    2. **Re-validate** each candidate against the fresh content + current disk.
       A fresh entry is removed only if it still classifies as ``missing_target``
       (so a target that reappeared on disk is spared) AND its raw line text is
       still "owed" by *candidate_raws*. Matching is *count-bounded*:
       ``candidate_raws`` carries one entry per occurrence analysis (T1) saw, and
       each fresh removal consumes one, so removals never exceed the
       analysis-time count of a given line. Distinct entries the agent added
       after T1 are therefore never removed, and an agent *edit* to a candidate
       line spares it (its raw no longer matches). The one residual case is an
       agent that added an *exact byte-duplicate* of a still-dead candidate: the
       budget keeps the right number of copies (the net of the addition is
       preserved), but because the duplicates are byte-identical, *which*
       physical line survives is unspecified — and irrelevant, since the spliced
       result is byte-identical either way. The budget bounds removals to what
       was provably dead at analysis time.
    3. **Splice** the still-qualifying lines out of the fresh text, carrying
       through everything the agent added before the lock.
    4. **Atomically replace**, preserving the file's existing mode (``mkstemp``
       defaults to ``0o600``, which would silently downgrade a ``0o644`` TOC).

    Returns the removed lines as ``(line_no, raw)`` for the audit report.
    """
    import stat
    from collections import Counter

    from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_text

    with _file_lock(_lock_path_for(index_path)):
        # §5.1 — fresh, newline-preserving read (NOT read_text()).
        fresh_text = index_path.read_bytes().decode("utf-8")
        fresh = parse_memory_index(fresh_text)
        # §5.2 — re-validate against a count-bounded budget: remove at most as
        # many occurrences of each raw line as analysis (T1) saw. Distinct agent
        # additions are never touched; for a byte-identical duplicate of a dead
        # candidate the net addition is preserved (one fewer copy), though which
        # equal copy survives is unspecified (they're identical, so the spliced
        # result is the same either way).
        budget: Counter[str] = Counter(candidate_raws)
        removed: list[tuple[int, str]] = []
        for e in fresh.entries:
            if budget[e.raw] <= 0:
                continue  # not a candidate, or its analysis-time count is exhausted
            if classify_link(e.target, root=root, source_dir=root) != "missing_target":
                continue  # target reappeared since analysis — leave it alone
            budget[e.raw] -= 1
            removed.append((e.line_no, e.raw))
        if not removed:
            return []
        # §5.3 — splice out of the FRESH text (agent additions carried through).
        new_text = _splice_lines(fresh_text, {n for n, _ in removed})
        # §5.4 — atomic replace, preserving the original file mode.
        original_mode = stat.S_IMODE(index_path.stat().st_mode)
        atomic_write_text(index_path, new_text, mode=original_mode)
    return removed


def _collect_fixable(inspect_dirs: list[Path]) -> list[tuple[Path, Path, str]]:
    """Find inspected dirs with a readable index file. Returns ``(dir, index_path, name)``.

    Only providers with a TOC convention (``provider_index_file``) and an index
    file actually on disk are fixable; everything else is skipped silently
    (there is nothing for a subtractive fix to act on).
    """
    from memtomem.config import categorize_memory_dir, provider_index_file

    out: list[tuple[Path, Path, str]] = []
    for d in inspect_dirs:
        resolved = d.expanduser().resolve()
        index_file_name = provider_index_file(categorize_memory_dir(d))
        if not index_file_name:
            continue
        index_path = resolved / index_file_name
        if not index_path.is_file():
            continue
        out.append((resolved, index_path, index_file_name))
    return out


def _run_fix(*, inspect_dirs: list[Path], apply: bool, json_out: bool) -> None:
    """Drive ``--fix`` over every inspected dir's index file (preview or apply).

    Per file the analysis-time read (T1) collects the ``missing_target``
    candidates; ``--apply`` then hands their raw line text to :func:`_apply_fix`,
    which re-reads fresh under the lock (T2) and re-validates before writing.
    Dry-run reports the T1 candidates directly. Each removed line is reported
    (ADR-0020 §5 — even on ``--apply``, the removal must be auditable).
    """
    results: list[FixFileResult] = []
    for resolved, index_path, index_file_name in _collect_fixable(inspect_dirs):
        try:
            # T1 (analysis snapshot). Terminator-stripped ``raw`` is CRLF-agnostic,
            # so this read need not be newline-preserving — only the apply splice is.
            text = index_path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        candidates = _missing_target_entries(text, root=resolved)
        if not candidates:
            results.append(FixFileResult(str(resolved), index_file_name, applied=apply, removed=[]))
            continue
        # Fail closed before promising (dry-run) or performing (--apply) a splice
        # we cannot prove is subtractive-only. Guarded on both paths so the
        # preview never advertises a removal we would refuse to make.
        unsafe = _unfixable_lines(
            candidates, ambiguous_lines=parse_memory_index(text).ambiguous_lines
        )
        if unsafe:
            detail = "\n".join(unsafe)
            raise click.UsageError(
                f"{index_path}: refusing --fix — {len(unsafe)} line(s) are outside the shape "
                f"--fix can safely splice, and it removes whole lines, so proceeding could "
                f"delete a live pointer:\n{detail}\n"
                f"    Repair these lines by hand, or split the index to one entry per line "
                f"(the MEMORY.md contract) and re-run."
            )
        if apply:
            removed = _apply_fix(index_path, resolved, [e.raw for e in candidates])
        else:
            removed = [(e.line_no, e.raw) for e in candidates]
        results.append(
            FixFileResult(str(resolved), index_file_name, applied=apply, removed=removed)
        )

    if json_out:
        _emit_fix_json(results, applied=apply)
    else:
        _emit_fix_human(results, applied=apply)


def _emit_fix_human(results: list[FixFileResult], *, applied: bool) -> None:
    total = sum(len(r.removed) for r in results)
    if total == 0:
        click.secho("No missing_target links to remove.", fg="green")
        return
    verb = "Removed" if applied else "Would remove"
    for r in results:
        if not r.removed:
            continue
        click.secho(f"\n■ {r.path} · {r.index_file}", bold=True)
        click.echo(f"  {verb} {len(r.removed)} missing_target link(s):")
        for line_no, raw in r.removed:
            click.echo(f"      - L{line_no}: {raw}")
    n_files = sum(1 for r in results if r.removed)
    if applied:
        click.secho(f"\n{total} line(s) removed across {n_files} file(s).", fg="green")
    else:
        click.echo(f"\n{total} line(s) across {n_files} file(s). Run with --apply to write.")


def _emit_fix_json(results: list[FixFileResult], *, applied: bool) -> None:
    total = sum(len(r.removed) for r in results)
    status = "clean" if total == 0 else ("fixed" if applied else "would-fix")
    payload = {
        "status": status,
        "applied": applied,
        "files": [r.to_json() for r in results if r.removed],
        "summary": {"files": sum(1 for r in results if r.removed), "lines": total},
    }
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


# ── Click entry point ───────────────────────────────────────────────


@click.group("memory")
def memory() -> None:
    """Memory-store hygiene: inspect index/DB/disk consistency."""


@memory.command("doctor")
@click.argument("path", required=False, type=click.Path())
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit a structured JSON result instead of human-readable output.",
)
@click.option(
    "--fix",
    "fix",
    is_flag=True,
    default=False,
    help=(
        "Subtractively remove broken `missing_target` links from the index file "
        "(ADR-0020). Dry-run unless --apply. Other findings are left untouched."
    ),
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="With --fix, actually rewrite the index file. Without it, --fix is a dry-run.",
)
def memory_doctor(path: str | None, json_out: bool, fix: bool, apply_: bool) -> None:
    """Report drift between disk, the index file, and the searchable DB (read-only).

    With no PATH, inspects every configured ``memory_dir``. Pass a PATH to
    scope the report to one configured dir.

    Exit codes: ``0`` clean (or advisory-only findings), ``1`` when any
    error-severity finding exists (stale DB sources, convention violations,
    or broken index links).

    ``--fix`` switches to a subtractive curation mode: it removes index-file
    pointer lines whose target is missing on disk (``broken_link`` /
    ``missing_target``) and nothing else (ADR-0020). It is a dry-run preview
    unless ``--apply`` is also passed. The default report is read-only and
    unchanged.
    """
    if apply_ and not fix:
        raise click.UsageError("--apply only applies with --fix. See: mm memory doctor --help")

    config = _load_config_read_only()
    memory_dirs = config.indexing.all_index_roots()

    if path is not None:
        target = Path(path).expanduser().resolve()
        inspect_dirs = [d for d in memory_dirs if d.expanduser().resolve() == target]
        if not inspect_dirs:
            raise click.ClickException(
                f"{path} is not a configured memory_dir. Run without PATH to inspect all, "
                "or `mm config` to see the configured dirs."
            )
    else:
        inspect_dirs = list(memory_dirs)

    if not inspect_dirs:
        click.secho("No memory_dirs configured. Run `mm init`.", fg="yellow")
        return

    if fix:
        _run_fix(inspect_dirs=inspect_dirs, apply=apply_, json_out=json_out)
        return

    reports = _gather_reports(config=config, inspect_dirs=inspect_dirs)

    if json_out:
        _emit_json(reports)
    else:
        _emit_human(reports)

    if _severity_totals(reports)["error"] > 0:
        raise click.exceptions.Exit(1)
