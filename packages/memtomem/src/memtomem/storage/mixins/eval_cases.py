"""Evaluation-case storage: promotion, retrieval, export/import (#1802).

Quality Lab Q3 promotes a labeled search run into a durable *evaluation
case* — a self-contained copy of the run's query, filters, and per-hash
relevance labels that survives the 90-day ``query_history`` prune. Cases are
decoupled from ``query_history`` on purpose (``source_run_id`` is provenance
only, never a foreign key), so promotion must **copy** every field it needs
inside one ``BEGIN IMMEDIATE`` transaction that a concurrent prune or feedback
replacement cannot tear.

Labels key on ``content_hash`` — the durable chunk identity across
re-indexing (``chunks.id`` is a fresh uuid4 per index run). ``chunk_id`` is a
re-resolvable cache: promotion fills it from the run snapshot, import leaves it
NULL, and replay (PR-4) resolves it by content_hash.

The fingerprint *read* helpers live here (SQL is storage's job); the hashing
policy lives in :mod:`memtomem.quality.fingerprints`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from memtomem.errors import EvalCaseError, EvalCaseNotFoundError
from memtomem.models import ScopeFilter
from memtomem.privacy import scan as _privacy_scan
from memtomem.storage.mixins.history import FEEDBACK_JUDGMENTS
from memtomem.storage.sqlite_scope import _scopes_glob_clause, _scopes_in_clause

#: The project-tier scope values (ADR-0011). These are the only scopes whose
#: retrievability depends on ``project_context_root`` — which observations do
#: not record — so a run reaching either of them is unpromotable (R8).
_PROJECT_TIER_SCOPES = ("project_shared", "project_local")

#: Export/import envelope version and discriminator. Bumped only if the
#: on-disk case-set shape changes incompatibly.
EVAL_CASE_SET_SCHEMA_VERSION = 1
EVAL_CASE_SET_KIND = "eval_case_set"

#: Case lifecycle states.
EVAL_CASE_STATUSES: frozenset[str] = frozenset({"active", "archived"})

#: Observation ``filters`` booleans that make a run unreplayable — the real
#: filter values were never recorded (#1800 R4), only their presence. Promoting
#: such a run would silently drop the filter at replay, so it is refused unless
#: the caller opts in with ``allow_unreplayable_filters``.
_UNREPLAYABLE_FILTER_KEYS = (
    "has_source_filter",
    "has_tag_filter",
    "has_metadata_filter",
    "has_as_of",
)

#: The only filter keys a portable eval case may carry. ``namespace`` / ``scope``
#: are the replayable subset promotion records; ``unreplayable`` is a
#: provenance-only marker (which unrecorded filters the run had). Anything else
#: was never recorded with enough fidelity to replay — see
#: :func:`validate_portable_filters`.
_PORTABLE_FILTER_KEYS = frozenset({"namespace", "scope", "unreplayable"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


#: Eval-case names double as CLI selectors (``mm quality show <name>``,
#: ``replay --case <name>``) and are echoed into replay reports, so they must be
#: short, path-safe labels — never free-form prose, absolute paths, or secrets.
#: Mirrors ``context._names.validate_name`` without importing across the
#: storage→context layer boundary, and additionally runs the secret-class
#: privacy scanner (the report's "no secrets" guarantee must hold for the name,
#: not only chunk content). Applied at every write ingress (promote + import)
#: so the redaction exemption's "short label, no free-text" rationale holds for
#: all surfaces (#1802 PR-5).
_EVAL_CASE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_EVAL_CASE_NAME_MAX_LEN = 64


def _validate_eval_case_name(name: str | None) -> str | None:
    """Return *name* if it is a valid eval-case label, else raise EvalCaseError.

    ``None`` (no name) passes through. Enforces ``1 <= len <= 64``, the
    ``[A-Za-z0-9._-]+`` charset (no whitespace / slash / control chars), no
    leading dash (CLI-flag collision), not the ``.``/``..`` path tokens, and no
    secret-class token (a credential-shaped label would otherwise persist and
    surface in replay reports). The secret-hit error never echoes the value.
    """
    if name is None:
        return None
    if not isinstance(name, str):
        raise EvalCaseError(f"eval case name must be a string, got {type(name).__name__}")
    if not name.strip():
        raise EvalCaseError("eval case name must not be blank")
    # Secret scan FIRST, before any error that interpolates the value — a long
    # or odd-charset credential (e.g. github_pat_… > 64 chars) must never be
    # echoed by the "too long" / "must match" messages below.
    if _privacy_scan(name):
        raise EvalCaseError("eval case name contains a secret-shaped token and was refused")
    if len(name) > _EVAL_CASE_NAME_MAX_LEN:
        raise EvalCaseError(
            f"eval case name {name!r} is too long (len {len(name)} > {_EVAL_CASE_NAME_MAX_LEN})"
        )
    if name in (".", ".."):
        raise EvalCaseError(f"eval case name {name!r} is a reserved path token")
    if name.startswith("-"):
        raise EvalCaseError(f"eval case name {name!r} must not start with a dash")
    if not _EVAL_CASE_NAME_RE.fullmatch(name):
        raise EvalCaseError(
            f"eval case name {name!r} must match [A-Za-z0-9._-]+ "
            "(no whitespace, slash, or control characters)"
        )
    return name


def _reject_path_shaped(field: str, value: object) -> None:
    """Reject filesystem-path-shaped namespace/scope tokens (#1802).

    Namespace/scope tokens are colon-delimited (``archive:session:*``,
    ``project_shared``), never paths. A value containing a path separator is
    almost certainly an absolute source path that must not ride into a replay
    report — the artifact privacy guarantee bans absolute source paths.
    """
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str) and ("/" in item or "\\" in item):
            raise EvalCaseError(
                f"case filter {field!r} value {item!r} looks like a filesystem path; "
                "namespace/scope tokens are colon-delimited, never paths"
            )


def validate_portable_filters(db: sqlite3.Connection, filters: object) -> None:
    """Validate a case's ``filters`` against the portable vocabulary (#1802).

    A portable case may carry only replayable, path-free, non-project filters:

    - ``namespace`` / ``scope`` — each ``str | list[str] | None`` (the
      ``ScopeFilter``/``NamespaceFilter`` parser contract), never a filesystem
      path, and ``scope`` never reaching a project tier (``project_context_root``
      is not portable, so replay would widen it cross-project — the same reason
      promotion refuses project scopes);
    - ``unreplayable`` — a non-empty, bounded list of known ``has_*`` markers,
      provenance only (the case is excluded from aggregates at replay).

    Applied at *every* boundary that can introduce a case — import, promotion,
    and replay-report assembly — so an unsupported or malformed filter can
    neither be silently ignored at replay nor leak a path into the artifact.
    Raises :class:`EvalCaseError` on any violation.
    """
    if filters is None:
        return
    if not isinstance(filters, dict):
        raise EvalCaseError("case 'filters' must be an object")
    unknown = set(filters) - _PORTABLE_FILTER_KEYS
    if unknown:
        raise EvalCaseError(
            f"case 'filters' has unsupported keys {sorted(unknown)}; "
            f"only {sorted(_PORTABLE_FILTER_KEYS)} are replayable"
        )
    for field in ("namespace", "scope"):
        if field not in filters or filters[field] is None:
            continue
        value = filters[field]
        if isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                raise EvalCaseError(f"case filter {field!r} list must contain only strings")
        elif not isinstance(value, str):
            raise EvalCaseError(f"case filter {field!r} must be a string, list of strings, or null")
        _reject_path_shaped(field, value)
    scope = filters.get("scope")
    if scope is not None and _scope_implies_project(db, scope):
        raise EvalCaseError(
            f"case filter 'scope'={scope!r} reaches a project tier; project_context_root is "
            "not portable, so replay would widen it cross-project"
        )
    if "unreplayable" in filters:
        marks = filters["unreplayable"]
        if (
            not isinstance(marks, list)
            or not marks
            or len(marks) > len(_UNREPLAYABLE_FILTER_KEYS)
            or any(m not in _UNREPLAYABLE_FILTER_KEYS for m in marks)
        ):
            raise EvalCaseError(
                "case filter 'unreplayable' must be a non-empty list of "
                f"{sorted(_UNREPLAYABLE_FILTER_KEYS)}"
            )


def _scope_implies_project(db: sqlite3.Connection, scope: object) -> bool:
    """True when a recorded ``scope`` value pins to any project tier.

    ``project_context_root`` is not recorded by observations (#1800 R8), so a
    project-scoped run replayed without its root silently *widens* to a
    cross-project search (see ``storage/sqlite_scope.py``: an explicit project
    filter with ``project_context_root=None`` drops the project pin). A scope is
    unsafe only when it can actually reach a **project tier**
    (``project_shared`` / ``project_local``); a user-only exact scope, a
    user-only glob like ``user*``, or an unknown token that matches nothing is
    fine, and ``None`` (the default context-boundary search) narrows to ``user``
    deterministically.

    Matching goes through the *same* clause builders ``scope_context_sql`` uses
    and is evaluated by SQLite itself against the three scope literals, so glob
    semantics match production exactly — SQLite ``LIKE`` is ASCII
    case-insensitive and treats the translated ``%`` as a wildcard, which a
    hand-rolled Python matcher would get wrong (``PROJECT_*`` and ``project%*``
    both reach project tiers).
    """
    if scope is None:
        return False
    parsed = ScopeFilter.parse(scope if isinstance(scope, (str, list)) else str(scope))
    if parsed is None:
        return False
    if parsed.scopes:
        clause, params = _scopes_in_clause(parsed.scopes, "")
    elif parsed.pattern is not None:
        clause, params = _scopes_glob_clause(parsed.pattern, "")
    else:
        return False
    for tier in _PROJECT_TIER_SCOPES:
        # ``clause`` is a fixed parameterized fragment produced by
        # scope_context_sql's own builders (``scope IN (?, ...)`` / ``scope LIKE
        # ? ESCAPE '\'``); every user-derived scope value is bound through
        # ``params``, never interpolated — the f-string injects only trusted
        # internal SQL, so the B608 dynamic-SQL warning is a false positive.
        hit = db.execute(
            f"SELECT 1 FROM (SELECT ? AS scope) WHERE {clause}",  # nosec B608
            [tier, *params],
        ).fetchone()
        if hit is not None:
            return True
    return False


class EvalCaseMixin:
    """Storage methods for durable evaluation cases. Requires ``_get_db()``."""

    # ---- promotion --------------------------------------------------------

    async def promote_search_run(
        self,
        run_id: str,
        *,
        name: str | None = None,
        fingerprints: dict[str, str],
        allow_unreplayable_filters: bool = False,
    ) -> dict[str, Any]:
        """Copy one labeled run into a durable eval case, atomically.

        The whole read-then-insert runs under one ``BEGIN IMMEDIATE`` so a
        concurrent ``_prune_old_history`` or ``save_search_feedback(replace=)``
        can never yield a half-copied case. Feedback is grouped by
        ``content_hash`` (feedback identity is per ``chunk_id``, and two
        chunk_ids can share a hash): agreeing judgments collapse to one label,
        conflicting judgments on one hash raise :class:`EvalCaseError`.

        Refuses (all :class:`EvalCaseError`): unknown run; run with no
        feedback; ``name`` collision; runs carrying an unreplayable filter
        (unless ``allow_unreplayable_filters``); project-scoped runs (no
        override — see :func:`_scope_implies_project`).
        """
        for key in ("profile", "corpus", "index"):
            if key not in fingerprints:
                raise EvalCaseError(f"fingerprints must include {key!r}")
        name = _validate_eval_case_name(name)
        if getattr(self, "_in_transaction", False):
            # transaction() suppresses commits but takes no lock — running here
            # would drop the BEGIN IMMEDIATE serialization (mirrors
            # save_search_feedback).
            raise EvalCaseError("promote_search_run cannot run inside a transaction block")

        db = self._get_db()
        db.execute("BEGIN IMMEDIATE")
        try:
            run = db.execute(
                "SELECT query_text, observation_json, result_snapshot_json "
                "FROM query_history WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise EvalCaseNotFoundError(f"run_id {run_id!r} not found")
            query_text, observation_json, snapshot_json = run
            observation = json.loads(observation_json or "{}")
            snapshot = json.loads(snapshot_json or "[]")

            filters = observation.get("filters", {}) if isinstance(observation, dict) else {}
            # Computed regardless of the override so it can be persisted below —
            # a promoted-with-override case records which filters it can't replay
            # so PR-4 replay flags it and excludes it from aggregates.
            offending = [k for k in _UNREPLAYABLE_FILTER_KEYS if filters.get(k)]
            if offending and not allow_unreplayable_filters:
                raise EvalCaseError(
                    f"run {run_id!r} carries unreplayable filters {offending} "
                    "(only their presence was recorded, not their values); "
                    "pass allow_unreplayable_filters=True to promote anyway"
                )
            if _scope_implies_project(db, filters.get("scope")):
                raise EvalCaseError(
                    f"run {run_id!r} is project-scoped (scope={filters.get('scope')!r}); "
                    "project_context_root is not recorded, so replay would widen it "
                    "cross-project — project-scoped runs are not promotable yet"
                )

            feedback = db.execute(
                "SELECT chunk_id, judgment FROM search_feedback WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            if not feedback:
                raise EvalCaseError(f"run {run_id!r} has no feedback to promote")

            hash_by_chunk = {
                entry.get("chunk_id"): entry.get("content_hash")
                for entry in snapshot
                if isinstance(entry, dict)
            }
            labels = self._group_feedback_by_hash(run_id, feedback, hash_by_chunk)

            raw_top_k = (
                observation.get("top_k", len(snapshot))
                if isinstance(observation, dict)
                else len(snapshot)
            )
            try:
                top_k = int(raw_top_k)
            except (TypeError, ValueError):
                # Observations are internally written, so this is trusted in
                # practice — but keep the module's "raise EvalCaseError, never a
                # raw exception" discipline for a corrupt row.
                raise EvalCaseError(
                    f"run {run_id!r} has a non-numeric top_k {raw_top_k!r}"
                ) from None
            case_filters: dict[str, Any] = {
                "namespace": filters.get("namespace"),
                "scope": filters.get("scope"),
            }
            if offending:
                # Provenance for PR-4 replay: which filters this case can't
                # reproduce (promoted only because the caller opted in). Replay
                # flags it and excludes it from aggregate metrics.
                case_filters["unreplayable"] = sorted(offending)
            # Same portable-vocabulary gate import uses, so a promoted case can
            # never carry a path-shaped or project-reaching filter into a report.
            validate_portable_filters(db, case_filters)
            case_id = str(uuid4())
            now = _now_iso()
            try:
                db.execute(
                    """INSERT INTO eval_cases
                       (case_id, name, query_text, top_k, filters_json, source_run_id,
                        promoted_profile_fingerprint, promoted_corpus_fingerprint,
                        promoted_index_fingerprint, promotion_snapshot_json,
                        version, status, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?)""",
                    (
                        case_id,
                        name,
                        query_text,
                        top_k,
                        json.dumps(case_filters, sort_keys=True),
                        run_id,
                        fingerprints["profile"],
                        fingerprints["corpus"],
                        fingerprints["index"],
                        json.dumps(snapshot, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                db.rollback()
                if name is not None and "UNIQUE" in str(exc).upper():
                    raise EvalCaseError(f"eval case name {name!r} already exists") from exc
                raise EvalCaseError(f"failed to create eval case: {exc}") from exc

            db.executemany(
                "INSERT INTO eval_case_labels "
                "(case_id, chunk_id, content_hash, judgment, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (case_id, chunk_id, content_hash, judgment, now)
                    for content_hash, (chunk_id, judgment) in labels.items()
                ],
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return await self.get_eval_case(case_id)

    @staticmethod
    def _group_feedback_by_hash(
        run_id: str,
        feedback: list[tuple[str, str]],
        hash_by_chunk: dict[str | None, str | None],
    ) -> dict[str, tuple[str | None, str]]:
        """Collapse per-chunk judgments to per-hash labels.

        Returns ``{content_hash: (representative_chunk_id, judgment)}``.
        Agreeing judgments on one hash collapse; a conflict raises.
        """
        by_hash: dict[str, tuple[str | None, str]] = {}
        for chunk_id, judgment in feedback:
            content_hash = hash_by_chunk.get(chunk_id)
            if content_hash is None:
                # Feedback is validated against the snapshot at write time, so a
                # missing hash means a malformed snapshot — surface it, don't
                # silently drop a relevance label.
                raise EvalCaseError(
                    f"run {run_id!r} feedback chunk {chunk_id!r} has no content_hash "
                    "in the result snapshot"
                )
            existing = by_hash.get(content_hash)
            if existing is None:
                by_hash[content_hash] = (chunk_id, judgment)
            elif existing[1] != judgment:
                raise EvalCaseError(
                    f"run {run_id!r} has conflicting judgments for content_hash "
                    f"{content_hash!r}: {existing[1]!r} vs {judgment!r}"
                )
        return by_hash

    # ---- retrieval --------------------------------------------------------

    async def list_eval_cases(self, status: str | None = None) -> list[dict[str, Any]]:
        """Newest-first case summaries (no labels), optionally filtered by status."""
        db = self._get_db()
        query = (
            "SELECT case_id, name, query_text, top_k, source_run_id, version, status, "
            "created_at, updated_at, "
            "(SELECT COUNT(*) FROM eval_case_labels l WHERE l.case_id = c.case_id) "
            "FROM eval_cases c"
        )
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC, case_id DESC"
        rows = db.execute(query, params).fetchall()
        return [
            {
                "case_id": r[0],
                "name": r[1],
                "query_text": r[2],
                "top_k": r[3],
                "source_run_id": r[4],
                "version": r[5],
                "status": r[6],
                "created_at": r[7],
                "updated_at": r[8],
                "label_count": r[9],
            }
            for r in rows
        ]

    def _resolve_case_id(self, db: sqlite3.Connection, case_id_or_name: str) -> str:
        """Resolve an identifier to a case_id, case_id first then name.

        A single ``case_id = ? OR name = ?`` query is ambiguous when one case's
        name equals another case's UUID, so the two lookups are ordered
        explicitly: an exact case_id match always wins.
        """
        by_id = db.execute(
            "SELECT case_id FROM eval_cases WHERE case_id = ?", (case_id_or_name,)
        ).fetchone()
        if by_id is not None:
            return by_id[0]
        by_name = db.execute(
            "SELECT case_id FROM eval_cases WHERE name = ?", (case_id_or_name,)
        ).fetchone()
        if by_name is not None:
            return by_name[0]
        raise EvalCaseNotFoundError(f"eval case {case_id_or_name!r} not found")

    async def get_eval_case(self, case_id_or_name: str) -> dict[str, Any]:
        """One case with its labels, looked up by case_id then by name."""
        db = self._get_db()
        case_id = self._resolve_case_id(db, case_id_or_name)
        row = db.execute(
            "SELECT case_id, name, query_text, top_k, filters_json, source_run_id, "
            "promoted_profile_fingerprint, promoted_corpus_fingerprint, "
            "promoted_index_fingerprint, promotion_snapshot_json, version, status, "
            "created_at, updated_at FROM eval_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise EvalCaseNotFoundError(f"eval case {case_id_or_name!r} not found")
        labels = db.execute(
            "SELECT chunk_id, content_hash, judgment, created_at FROM eval_case_labels "
            "WHERE case_id = ? ORDER BY content_hash",
            (row[0],),
        ).fetchall()
        return {
            "case_id": row[0],
            "name": row[1],
            "query_text": row[2],
            "top_k": row[3],
            "filters": json.loads(row[4]) if row[4] else {},
            "source_run_id": row[5],
            "promoted_fingerprints": {
                "profile": row[6],
                "corpus": row[7],
                "index": row[8],
            },
            "promotion_snapshot": json.loads(row[9]) if row[9] else [],
            "version": row[10],
            "status": row[11],
            "created_at": row[12],
            "updated_at": row[13],
            "labels": [
                {
                    "chunk_id": lab[0],
                    "content_hash": lab[1],
                    "judgment": lab[2],
                    "created_at": lab[3],
                }
                for lab in labels
            ],
        }

    async def set_eval_case_status(self, case_id_or_name: str, status: str) -> dict[str, Any]:
        """Set a case's lifecycle status and bump ``updated_at``."""
        if status not in EVAL_CASE_STATUSES:
            raise EvalCaseError(
                f"status must be one of {sorted(EVAL_CASE_STATUSES)}, got {status!r}"
            )
        db = self._get_db()
        case_id = self._resolve_case_id(db, case_id_or_name)
        db.execute(
            "UPDATE eval_cases SET status = ?, updated_at = ? WHERE case_id = ?",
            (status, _now_iso(), case_id),
        )
        db.commit()
        return await self.get_eval_case(case_id)

    # ---- export / import --------------------------------------------------

    async def export_eval_cases(self, case_ids: list[str] | None = None) -> dict[str, Any]:
        """Serialize cases to a portable envelope (labels keyed by content_hash).

        ``chunk_id`` is deliberately omitted — it is index-local and gets
        re-resolved at replay time in the importing index.
        """
        summaries = await self.list_eval_cases()
        wanted = set(case_ids) if case_ids is not None else None
        if wanted is not None:
            # Fail loudly on a stale/misspelled id rather than silently returning
            # a smaller envelope than the caller asked for.
            missing = wanted - {s["case_id"] for s in summaries}
            if missing:
                raise EvalCaseError(f"unknown eval case ids: {sorted(missing)}")
        cases = []
        for summary in summaries:
            if wanted is not None and summary["case_id"] not in wanted:
                continue
            full = await self.get_eval_case(summary["case_id"])
            cases.append(
                {
                    "case_id": full["case_id"],
                    "name": full["name"],
                    "query_text": full["query_text"],
                    "top_k": full["top_k"],
                    "filters": full["filters"],
                    "source_run_id": full["source_run_id"],
                    "promoted_fingerprints": full["promoted_fingerprints"],
                    "version": full["version"],
                    "status": full["status"],
                    "labels": [
                        {"content_hash": lab["content_hash"], "judgment": lab["judgment"]}
                        for lab in full["labels"]
                    ],
                }
            )
        return {
            "schema_version": EVAL_CASE_SET_SCHEMA_VERSION,
            "kind": EVAL_CASE_SET_KIND,
            "cases": cases,
        }

    async def import_eval_cases(
        self, payload: dict[str, Any], *, replace: bool = False
    ) -> dict[str, Any]:
        """Load an exported envelope; labels land with ``chunk_id=NULL``.

        Labels resolve to live chunks only at replay time (PR-4). ``case_id`` is
        the durable identity: it and ``version`` are preserved from the payload
        (minted only when absent). A payload whose ``case_id`` already exists is
        overwritten in place under ``replace=True`` (retaining that id) and
        raises otherwise; a ``name`` owned by a *different* ``case_id`` is always
        a hard conflict, never silently clobbered.

        The whole batch runs under one ``BEGIN IMMEDIATE``: with ``replace=True``
        a case is deleted before its replacement is written, so a failure on a
        *later* case in the batch must roll the deletion back rather than leave a
        half-applied import (and a dangling implicit transaction that a later
        ``BEGIN IMMEDIATE`` — e.g. a promote — would trip over).
        """
        if not isinstance(payload, dict):
            raise EvalCaseError("import payload must be a JSON object")
        if payload.get("schema_version") != EVAL_CASE_SET_SCHEMA_VERSION:
            raise EvalCaseError(
                f"unsupported schema_version {payload.get('schema_version')!r} "
                f"(expected {EVAL_CASE_SET_SCHEMA_VERSION})"
            )
        if payload.get("kind") != EVAL_CASE_SET_KIND:
            raise EvalCaseError(
                f"unsupported kind {payload.get('kind')!r} (expected {EVAL_CASE_SET_KIND!r})"
            )
        cases = payload.get("cases")
        if not isinstance(cases, list):
            raise EvalCaseError("import payload 'cases' must be a list")
        if getattr(self, "_in_transaction", False):
            raise EvalCaseError("import_eval_cases cannot run inside a transaction block")

        # Validate the ENTIRE envelope before touching storage, so a malformed
        # case never leaks a raw ValueError/sqlite error and — with replace=True —
        # never deletes an existing case that a later failure would strand.
        # A duplicate case_id within one payload would silently last-wins under
        # replace=True (unlike duplicate names, which the owner check rejects), so
        # reject it here to keep the two identity axes consistent.
        seen_case_ids: set[str] = set()
        db = self._get_db()
        for case in cases:
            self._validate_import_case(case)
            # Portable-filter gate (keys, value types, path-shaped + project-tier
            # scope rejection). Needs a db connection for the project-scope
            # check, so it lives here rather than in the static validator.
            validate_portable_filters(db, case.get("filters") if isinstance(case, dict) else None)
            cid = case.get("case_id") if isinstance(case, dict) else None
            if cid is not None:
                if cid in seen_case_ids:
                    raise EvalCaseError(f"duplicate case_id {cid!r} in import payload")
                seen_case_ids.add(cid)

        db.execute("BEGIN IMMEDIATE")
        try:
            imported = 0
            for case in cases:
                self._import_one_case(db, case, replace=replace)
                imported += 1
            db.commit()
        except Exception:
            db.rollback()
            raise
        return {"imported": imported}

    @staticmethod
    def _validate_import_case(case: dict[str, Any]) -> None:
        """Structural + domain validation for one imported case (raises EvalCaseError).

        Mirrors the column constraints the schema does not enforce (positive
        top_k, known status, object-shaped filters/fingerprints) so bad input is
        rejected up front rather than persisted or surfaced as a raw exception.
        """
        if not isinstance(case, dict):
            raise EvalCaseError("each imported case must be a JSON object")
        query_text = case.get("query_text")
        if not isinstance(query_text, str) or not query_text:
            raise EvalCaseError("imported case 'query_text' must be a non-empty string")
        case_id = case.get("case_id")
        if case_id is not None and (not isinstance(case_id, str) or not case_id):
            raise EvalCaseError("imported case 'case_id' must be a non-empty string")
        version = case.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise EvalCaseError(
                f"imported case 'version' must be a positive integer, got {version!r}"
            )
        name = case.get("name")
        if name is not None and not isinstance(name, str):
            raise EvalCaseError("imported case 'name' must be a string or null")
        _validate_eval_case_name(name)
        source_run_id = case.get("source_run_id")
        if source_run_id is not None and not isinstance(source_run_id, str):
            raise EvalCaseError("imported case 'source_run_id' must be a string or null")
        labels = case.get("labels", [])
        if not isinstance(labels, list):
            raise EvalCaseError("imported case 'labels' must be a list")
        if not labels:
            # A label-less case is unusable for PR-4 metrics (nothing to score);
            # promotion already requires feedback, so import matches that floor.
            raise EvalCaseError("imported case must have at least one label")
        seen_hashes: set[str] = set()
        for lab in labels:
            if not isinstance(lab, dict):
                raise EvalCaseError("each imported label must be a JSON object")
            content_hash = lab.get("content_hash")
            if not isinstance(content_hash, str) or not content_hash:
                raise EvalCaseError("imported label 'content_hash' must be a non-empty string")
            judgment = lab.get("judgment")
            # scalar-type check BEFORE frozenset membership: `[] in frozenset` raises
            # TypeError (unhashable), which would leak past the EvalCaseError contract.
            if not isinstance(judgment, str) or judgment not in FEEDBACK_JUDGMENTS:
                raise EvalCaseError(
                    f"imported label judgment must be one of {sorted(FEEDBACK_JUDGMENTS)}, "
                    f"got {judgment!r}"
                )
            # Labels are per content_hash (idx_eval_case_labels_case_hash is UNIQUE);
            # reject duplicates here so the executemany can't leak an IntegrityError.
            if content_hash in seen_hashes:
                raise EvalCaseError(
                    f"imported case has duplicate label content_hash {content_hash!r}"
                )
            seen_hashes.add(content_hash)
        top_k = case.get("top_k", len(labels))
        if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
            raise EvalCaseError(f"imported case 'top_k' must be a positive integer, got {top_k!r}")
        filters = case.get("filters")
        if filters is not None and not isinstance(filters, dict):
            raise EvalCaseError("imported case 'filters' must be an object")
        fingerprints = case.get("promoted_fingerprints", {})
        if not isinstance(fingerprints, dict):
            raise EvalCaseError("imported case 'promoted_fingerprints' must be an object")
        for key in ("profile", "corpus", "index"):
            # A present key must be a string: `.get(key, "")` at insert defaults
            # only on an ABSENT key, so a present-but-null value would reach the
            # NOT NULL column and raise a raw IntegrityError. Reject it here.
            if key in fingerprints and not isinstance(fingerprints[key], str):
                raise EvalCaseError(f"imported fingerprint {key!r} must be a string")
        status = case.get("status", "active")
        if not isinstance(status, str) or status not in EVAL_CASE_STATUSES:
            raise EvalCaseError(
                f"imported case 'status' must be one of {sorted(EVAL_CASE_STATUSES)}, got {status!r}"
            )

    def _import_one_case(
        self, db: sqlite3.Connection, case: dict[str, Any], *, replace: bool
    ) -> None:
        # Assumes _validate_import_case already ran for every case in the batch.
        # case_id is the durable identity: preserved from the payload so an
        # export→import round-trip keeps the same case (and case_set_fingerprint)
        # across machines. Only minted when the payload omits it (hand-authored
        # sets). version is likewise preserved.
        labels = case.get("labels", [])
        query_text = case.get("query_text")
        name = case.get("name")
        case_id = case.get("case_id") or str(uuid4())
        version = case.get("version", 1)

        # Name uniqueness: a *different* case already owning this name is a hard
        # conflict (never silently delete an unrelated case, even with replace).
        if name is not None:
            name_owner = db.execute(
                "SELECT case_id FROM eval_cases WHERE name = ?", (name,)
            ).fetchone()
            if name_owner is not None and name_owner[0] != case_id:
                raise EvalCaseError(
                    f"eval case name {name!r} already exists under a different case"
                )
        # Same-id case: overwrite in place under replace, error otherwise.
        existing = db.execute("SELECT 1 FROM eval_cases WHERE case_id = ?", (case_id,)).fetchone()
        if existing is not None:
            if not replace:
                raise EvalCaseError(
                    f"eval case {case_id!r} already exists; pass replace=True to overwrite"
                )
            db.execute("DELETE FROM eval_cases WHERE case_id = ?", (case_id,))

        fingerprints = case.get("promoted_fingerprints", {})
        now = _now_iso()
        db.execute(
            """INSERT INTO eval_cases
               (case_id, name, query_text, top_k, filters_json, source_run_id,
                promoted_profile_fingerprint, promoted_corpus_fingerprint,
                promoted_index_fingerprint, promotion_snapshot_json,
                version, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)""",
            (
                case_id,
                name,
                query_text,
                int(case.get("top_k", len(labels))),
                json.dumps(case.get("filters") or {}, sort_keys=True),
                case.get("source_run_id"),
                fingerprints.get("profile", ""),
                fingerprints.get("corpus", ""),
                fingerprints.get("index", ""),
                version,
                case.get("status", "active"),
                now,
                now,
            ),
        )
        db.executemany(
            "INSERT INTO eval_case_labels "
            "(case_id, chunk_id, content_hash, judgment, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            [(case_id, lab["content_hash"], lab["judgment"], now) for lab in labels],
        )

    # ---- fingerprint read helpers -----------------------------------------
    #
    # Raw-row readers for memtomem.quality.fingerprints. SQL stays here; the
    # hashing/allowlist policy lives in the quality package. Read-only, O(corpus)
    # — only ever called inside explicit ``mm quality`` commands, never on the
    # search hot path.

    def read_corpus_fingerprint_rows(self) -> list[tuple]:
        """Per-chunk retrieval-state tuples, multiplicity-preserving.

        ``content_hash`` already encodes the NFC-normalized body;
        ``heading_hierarchy`` is included separately because it changes the
        embedded retrieval text without changing ``content_hash``. ``project_root``
        is included because ``scope_context_sql`` uses it to decide which
        project-tier chunks are retrievable — an identical row in a different
        project is a different retrieval state.
        """
        return (
            self._get_read_db()
            .execute(
                "SELECT content_hash, heading_hierarchy, namespace, scope, created_at, "
                "updated_at, valid_from_unix, valid_to_unix, importance_score, tags, "
                "source_file, start_line, project_root FROM chunks"
            )
            .fetchall()
        )

    def read_vector_fingerprint_rows(self) -> list[tuple]:
        """(content_hash, namespace, source_file, start_line, embedding blob) per vector.

        The vector is keyed by the **durable** chunk identity (content_hash +
        namespace + source path + start_line), not the storage-local rowid/uuid,
        so a rebuilt-but-equivalent index does not read as drift while a vector
        that detaches from its content still does. ``namespace`` disambiguates the
        same ``(content_hash, source_file, start_line)`` recurring under two
        namespaces. The ``chunks_vec.rowid = chunks.rowid`` join only recovers
        that identity; the rowid never enters the fingerprint.
        """
        db = self._get_read_db()
        has_vec = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
        ).fetchone()
        if not has_vec:
            return []
        return db.execute(
            "SELECT c.content_hash, c.namespace, c.source_file, c.start_line, v.embedding "
            "FROM chunks c JOIN chunks_vec v ON v.rowid = c.rowid"
        ).fetchall()

    def read_fts_fingerprint_rows(self) -> list[tuple]:
        """(content_hash, namespace, source_file, start_line, fts_content) per FTS row.

        Keyed by durable chunk identity (see :meth:`read_vector_fingerprint_rows`)
        so FTS text that has drifted out of sync with its chunk registers, while
        a rekeyed rebuild does not.
        """
        return (
            self._get_read_db()
            .execute(
                "SELECT c.content_hash, c.namespace, c.source_file, c.start_line, f.content "
                "FROM chunks c JOIN chunks_fts f ON f.rowid = c.rowid"
            )
            .fetchall()
        )

    def read_link_topology_rows(self) -> list[tuple]:
        """chunk_links topology by DURABLE endpoint identity (rescue input).

        Each endpoint is resolved to its ``(content_hash, namespace, source_file,
        start_line)`` rather than its storage-local UUID, so a rekeyed rebuild
        does not read as drift. ``source_id`` is nullable (``ON DELETE SET NULL``)
        and dangling sources resolve to NULL columns via the LEFT JOIN — that
        null state is preserved in the fingerprint.
        """
        return (
            self._get_read_db()
            .execute(
                "SELECT s.content_hash, s.namespace, s.source_file, s.start_line, "
                "t.content_hash, t.namespace, t.source_file, t.start_line, "
                "l.link_type, l.namespace_target "
                "FROM chunk_links l "
                "LEFT JOIN chunks s ON s.id = l.source_id "
                "JOIN chunks t ON t.id = l.target_id"
            )
            .fetchall()
        )

    def read_access_counts(self) -> list[tuple]:
        """(content_hash, namespace, source_file, start_line, access_count) per chunk.

        Keyed by durable chunk identity (not content_hash alone) so that swapping
        access counts between two duplicate-content chunks registers as drift —
        access boosting ranks per chunk. Only an input when access/importance
        boost is enabled.
        """
        return (
            self._get_read_db()
            .execute(
                "SELECT content_hash, namespace, source_file, start_line, access_count FROM chunks"
            )
            .fetchall()
        )

    def validate_case_filters(self, filters: object) -> None:
        """Validate a case's ``filters`` against the portable vocabulary.

        Public wrapper over :func:`validate_portable_filters` that supplies this
        backend's own read connection (needed for the project-scope check), so
        callers such as the replay engine don't reach into ``_get_read_db``.
        Raises :class:`~memtomem.errors.EvalCaseError` on any violation.
        """
        validate_portable_filters(self._get_read_db(), filters)
