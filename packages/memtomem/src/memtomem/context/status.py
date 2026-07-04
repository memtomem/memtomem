"""Read-only inventory of installed wiki assets in a project.

Powers ``mm context status`` — a diagnostic verb that walks
``<project>/.memtomem/lock.json``, classifies each entry against the
on-disk dest tree and the wiki, and returns a list of
:class:`StatusRow` for the CLI to render. No writes anywhere; safe to
run in cron pipes (``mm context status && mm context update <type> <name>``).

State semantics for each lockfile entry:

- ``ok`` — dest clean, lockfile pin reachable in wiki, pin == HEAD
- ``behind`` — dest clean, pin reachable AND an ancestor of HEAD, pin !=
  HEAD ("update available"). Ancestry is required, not just reachability:
  after a wiki reset / force-pull to older history the pin is reachable but
  NEWER than (or divergent from) HEAD, so ``mm context update`` would move
  the pin BACKWARD — that case is ``stale-pin``, not ``behind``
- ``dirty`` — dest has local edits since ``installed_at``
- ``missing`` — lockfile entry exists but ``<project>/.memtomem/<type>/<name>/``
  is gone (collapses :data:`memtomem.context.dirty.DirtyReport.reason`
  values ``missing_dest`` and ``never_installed``); when a legacy
  flat-layout sibling (``<type>/<name>.md``) is what actually serves the
  asset, the row's reason points at ``mm context migrate`` instead of
  claiming "dest missing" (#1247)
- ``stale-pin`` — wiki absent or unusable, OR pin not reachable in the
  wiki repo (history rewrite, force-push past the pin, etc.), OR pin
  reachable but NOT an ancestor of HEAD (wiki reset / force-pull to
  older/divergent history — the pin is newer/divergent, so "updating"
  would silently downgrade it)
- ``untracked`` — project_shared canonical on disk with no lockfile entry
  (reverse-imported via ``mm context init`` or moved in via ``mm context
  migrate --to project_shared``); actively served by sync fan-out, just
  not a wiki install (#1247)

The wiki-absent (or present-but-unusable, e.g. a clone of an empty
remote with no HEAD) case still renders rows: status is read-only and
the lockfile is local; we just can't compute ``behind``/``stale-pin``
without a usable wiki to compare against.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, get_args

from memtomem.config import TargetScope
from memtomem.context._names import is_internal_artifact_dir
from memtomem.context.agents import AGENT_DIR_FILENAME
from memtomem.context.commands import COMMAND_DIR_FILENAME
from memtomem.context.dirty import is_asset_dirty
from memtomem.context.lockfile import Lockfile, LockfileError
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import SKILL_MANIFEST
from memtomem.wiki.store import WikiNotFoundError, WikiStore

__all__ = [
    "DRIFT_STATES",
    "ProjectStatus",
    "StatusRow",
    "StatusState",
    "classify_status",
    "collect_project_status",
    "iter_kind_drift_counts",
    "scan_user_artifacts",
    "summarize_diff_statuses",
    "summarize_diff_with_canonical",
    "summarize_settings_statuses",
]


StatusState = Literal["ok", "behind", "dirty", "missing", "stale-pin", "local-draft", "untracked"]

#: States that mean "this install needs attention" for the cross-project
#: aggregate (#1280). ``ok`` is clean; ``untracked`` and ``local-draft`` are
#: informational tiers (served / draft inventory with no wiki pin to drift
#: from), so a project carrying only those still reads as clean.
DRIFT_STATES: frozenset[str] = frozenset({"behind", "dirty", "missing", "stale-pin"})

_LOCAL_DRAFT_MANIFEST: dict[str, str] = {
    "agents": AGENT_DIR_FILENAME,
    "commands": COMMAND_DIR_FILENAME,
    "skills": SKILL_MANIFEST,
}


@dataclass(frozen=True)
class StatusRow:
    """Per-entry classification produced by :func:`classify_status`.

    ``pin_commit`` is the full 40-char SHA from the lockfile entry;
    callers abbreviate to 12 for display per ADR-0008 line 149.
    ``installed_at`` is the ISO-8601Z value from the lockfile (whatever
    was last written; not re-validated here). ``dirty_file_count`` is
    populated only for ``state == "dirty"`` and counts local edits of
    both classes: files with ``mtime > installed_at_epoch`` plus
    manifest-recorded files deleted from disk (#1247).

    ``reason`` is a human-readable detail line that the CLI appends in
    parens after the row. Common contents: ``"N file(s) modified
    locally"`` for dirty, ``"dest missing"`` for missing, ``"pin <abbr>
    not reachable"`` / ``"wiki not present"`` for stale-pin.

    ``tier`` distinguishes lockfile-tracked installs (``project_shared``)
    from author-managed drafts. :func:`classify_status` walks the
    project-rooted tiers — lockfile installs, non-lockfile project_shared
    canonicals (``state="untracked"``: reverse-imported / migrated-in
    artifacts with no wiki pin, #1247), plus
    ``<proj>/.memtomem/<artifact>.local/`` (``project_local``) drafts. The
    global ``~/.memtomem/<artifact>/`` (``user``) tier is enumerated
    separately by :func:`scan_user_artifacts`, which the CLI surfaces for
    ``--scope user`` (folding it into ``classify_status`` would couple every
    status read to the caller's real home). Both draft tiers carry
    ``state="local-draft"`` and empty ``pin_commit``/``installed_at`` — not
    lockfile-tracked, so no wiki pin to report. The CLI appends
    ``(draft, no fan-out)`` after project_local rows per ADR-0011 §3 /
    ADR-0016 §7; user-tier drafts DO fan out to runtime dirs, so they get no
    such annotation.
    """

    asset_type: Literal["skills", "agents", "commands"]
    name: str
    pin_commit: str
    installed_at: str
    state: StatusState
    dirty_file_count: int
    reason: str | None
    tier: TargetScope = "project_shared"


def classify_status(
    project_root: Path | str,
    *,
    wiki: WikiStore | None = None,
) -> tuple[str | None, list[StatusRow]]:
    """Classify every lockfile entry in *project_root*.

    Returns ``(wiki_head_or_None, rows)``:

    - ``wiki_head_or_None`` is the wiki HEAD SHA when the wiki is
      reachable, ``None`` when the wiki is absent. Rows still render
      either way; the CLI uses ``None`` to suppress the
      ``behind``/``stale-pin`` distinctions (we can't compute them
      without a wiki).
    - ``rows`` preserves :meth:`Lockfile.iter_entries` order
      (alphabetical by ``(asset_type, name)``). Empty when no entries
      exist.

    Lockfile reads use :meth:`Lockfile.load(strict=False)` indirectly —
    if the file's ``version`` is unknown, ``iter_entries`` still yields
    well-formed entries; the corrupt case is the caller's to surface.

    Wiki reachability: probed once per entry via
    :meth:`WikiStore.commit_is_reachable`, then — only for a reachable pin
    that differs from HEAD — :meth:`WikiStore.commit_is_ancestor` decides
    ``behind`` (pin is an ancestor of HEAD, update available) vs ``stale-pin``
    (pin reachable but diverged; "updating" would downgrade). When the wiki is
    absent, neither method is called.
    """
    project_root_path = Path(project_root).expanduser()
    lockfile = Lockfile.at(project_root_path)

    wiki_head: str | None = None
    wiki_present = False
    wiki_unavailable_reason = "wiki not present"
    if wiki is None:
        wiki = WikiStore.at_default()
    try:
        wiki_head = wiki.current_commit()
        wiki_present = True
    except WikiNotFoundError:
        wiki_head = None
        wiki_present = False
    except (RuntimeError, OSError) as exc:
        # Degraded wiki (#1247 id 9): present but unusable — a clone of an
        # empty remote has .git but no HEAD (`rev-parse HEAD` fails as a
        # bare RuntimeError from WikiStore._git), the object store may be
        # corrupt, or git itself is missing from PATH (OSError). status is
        # the read-only diagnostic verb a user would run to investigate, so
        # it must degrade like the absent-wiki case instead of escaping as
        # a traceback. WikiNotFoundError subclasses RuntimeError — its arm
        # above must stay first.
        wiki_head = None
        wiki_present = False
        detail_lines = str(exc).strip().splitlines()
        detail = detail_lines[0] if detail_lines else exc.__class__.__name__
        wiki_unavailable_reason = f"wiki unusable: {detail}"

    rows: list[StatusRow] = []
    tracked: set[tuple[str, str]] = set()
    for asset_type, name, entry in _tolerant_iter_entries(lockfile):
        tracked.add((asset_type, name))
        if asset_type not in ("skills", "agents", "commands"):
            # Unknown asset_type — forward-compat shape is preserved
            # by iter_entries, but we can only render the three known
            # display sections. Skip silently rather than rendering
            # an "unknown" state (no actionable user remedy).
            continue

        pin_commit = entry.get("wiki_commit", "") if isinstance(entry, dict) else ""
        installed_at = entry.get("installed_at", "") if isinstance(entry, dict) else ""

        report = is_asset_dirty(project_root_path, asset_type, name, lock_entry=entry)

        state: StatusState
        dirty_count = 0
        reason: str | None = None

        if report.reason in ("missing_dest", "never_installed"):
            state = "missing"
            reason = "dest missing"
            # Flat-layout population (#1247 id 0): the dir-layout dest is
            # missing but a legacy flat sibling exists and is what fan-out
            # actually serves — "dest missing" would misreport a live
            # asset. Point at the migrate verb instead. Mirrors
            # install._flat_layout_probe's scope (agents/commands only;
            # dest dir absent).
            dest = project_root_path / ".memtomem" / asset_type / name
            flat = project_root_path / ".memtomem" / asset_type / f"{name}.md"
            if asset_type in ("agents", "commands") and not dest.is_dir() and flat.is_file():
                reason = f"flat layout; run `mm context migrate {asset_type} {name}`"
            elif report.reason == "never_installed" and dest.is_dir():
                # Entry present but unusable installed_at over an existing
                # dest — "dest missing" would be flatly wrong. Mirrors the
                # update/install-all "unprovable" refuse reason (#1247).
                reason = "install record unusable (malformed installed_at)"
        elif report.reason == "dirty":
            state = "dirty"
            dirty_count = len(report.dirty_files) + len(report.missing_files)
            reason = report.summary()
        else:  # report.reason == "clean"
            if not wiki_present:
                state = "stale-pin"
                reason = wiki_unavailable_reason
            elif not pin_commit:
                state = "stale-pin"
                reason = "lockfile entry missing wiki_commit"
            elif pin_commit == wiki_head:
                state = "ok"
            elif not wiki.commit_is_reachable(pin_commit):
                state = "stale-pin"
                reason = f"pin {pin_commit[:12]} not reachable"
            elif wiki.commit_is_ancestor(pin_commit):
                # Reachable AND an ancestor of HEAD → HEAD genuinely advanced
                # past the pin; an update is available.
                state = "behind"
                reason = "wiki advanced past pin"
            else:
                # Reachable but NOT an ancestor of HEAD: the wiki was reset or
                # force-pulled to older/divergent history, so the pin is
                # newer/divergent and `mm context update` would move it BACKWARD
                # (a silent downgrade). Not "behind" — surface it as drift so
                # the user investigates instead of "updating" into a downgrade.
                # Folds into stale-pin ("history rewrite, force-push past the
                # pin, etc.") rather than a new state — keeps the 7-key
                # state-count conservation contract intact (#1280).
                state = "stale-pin"
                reason = "wiki history diverged from pin (reset or force-pull?)"

        rows.append(
            StatusRow(
                asset_type=asset_type,  # type: ignore[arg-type]
                name=name,
                pin_commit=pin_commit,
                installed_at=installed_at,
                state=state,
                dirty_file_count=dirty_count,
                reason=reason,
                tier="project_shared",
            )
        )

    rows.extend(_scan_project_local_drafts(project_root_path))
    rows.extend(_scan_project_shared_untracked(project_root_path, tracked))

    # Final order: alphabetical by (asset_type, name); within a name,
    # project_shared (lockfile-tracked install) renders before
    # project_local (locally-authored draft). A name colliding across
    # tiers therefore produces two adjacent rows under the same section
    # header, shared first.
    rows.sort(key=lambda r: (r.asset_type, r.name, _TIER_RENDER_ORDER[r.tier]))
    return wiki_head, rows


_TIER_RENDER_ORDER: dict[str, int] = {
    "user": 0,
    "project_shared": 1,
    "project_local": 2,
}


def _scan_draft_tier(scope: TargetScope, project_root: Path | None) -> Iterator[StatusRow]:
    """Yield ``local-draft`` ``StatusRow``s for every valid artifact in *scope*.

    Shared walk for the two non-lockfile canonical tiers — ``project_local``
    (``<proj>/.memtomem/{kind}.local/``) and ``user`` (``~/.memtomem/{kind}/``).
    Both layouts that ``migrate._detect_source_scope`` accepts are recognised:

    - **Directory layout** (all three kinds): ``<root>/<name>/`` containing
      the kind-specific manifest file (``agent.md`` / ``command.md`` /
      ``SKILL.md``). The manifest presence is the validity probe.
    - **Flat layout** (agents and commands only; skills are dir-only by
      design — see ``migrate._detect_source_scope``): ``<root>/<name>.md``
      as a single file. ``<name>`` is the file stem.

    When the same name exists in both layouts the directory wins (mirrors
    migrate's ``continue`` after a dir match), so a flat sibling of a
    directory artifact is silently shadowed.

    Emitted rows carry ``tier=scope``, ``state="local-draft"`` and empty
    ``pin_commit``/``installed_at`` — neither tier is lockfile-tracked, so
    there is no wiki pin to report.
    """
    for asset_type in ("agents", "commands", "skills"):
        root = canonical_artifact_dir(
            asset_type,  # type: ignore[arg-type]
            scope,
            project_root,
        )
        if not root.is_dir():
            continue
        manifest = _LOCAL_DRAFT_MANIFEST[asset_type]
        seen_names: set[str] = set()

        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if not (entry / manifest).is_file():
                # Skip directories that don't satisfy the kind's
                # manifest contract — same convention as migrate's
                # source-scope probe.
                continue
            if is_internal_artifact_dir(entry.name):
                # Crash-leftover staging/move-aside trees from skill sync —
                # not local drafts (#1229).
                continue
            seen_names.add(entry.name)
            yield StatusRow(
                asset_type=asset_type,  # type: ignore[arg-type]
                name=entry.name,
                pin_commit="",
                installed_at="",
                state="local-draft",
                dirty_file_count=0,
                reason=None,
                tier=scope,
            )

        if asset_type == "skills":
            # Skills have no flat layout (migrate._detect_source_scope:792).
            continue
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or entry.suffix != ".md":
                continue
            name = entry.stem
            if name in seen_names:
                # Dir-layout wins on collision (same convention as
                # migrate._detect_source_scope's `continue` after a
                # dir match).
                continue
            yield StatusRow(
                asset_type=asset_type,  # type: ignore[arg-type]
                name=name,
                pin_commit="",
                installed_at="",
                state="local-draft",
                dirty_file_count=0,
                reason=None,
                tier=scope,
            )


def _scan_project_shared_untracked(
    project_root: Path,
    tracked: set[tuple[str, str]],
) -> Iterator[StatusRow]:
    """Yield ``untracked`` rows for project_shared canonicals with no lockfile entry.

    ``mm context init --include ...`` (reverse import) and ``mm context
    migrate <kind> <name> --to project_shared`` both write the project_shared
    canonical tier without a lockfile entry — they are not wiki installs, so
    there is no pin to record. Those artifacts are actively served (sync fans
    them out from disk via ``list_canonical_*``), and ADR-0016 §6 names
    ``mm context status --scope <tier>`` as the per-tier inspection surface,
    so omitting them misreported a populated tier as "0 asset(s) installed"
    (#1247 id 8).

    Reuses :func:`_scan_draft_tier`'s walk (dir-with-manifest + flat layouts,
    internal-dir skip, dir-wins-on-collision) and rewrites the rows:
    ``state="untracked"`` — deliberately NOT ``local-draft``, because
    project_shared canonicals fan out while drafts don't — with a fixed
    reason. *tracked* carries the lockfile-tracked ``(asset_type, name)``
    pairs to drop: those names already render as lockfile rows (including
    the flat-layout ``missing`` hint, #1247 id 0), so emitting them here
    would double-report.
    """
    for row in _scan_draft_tier("project_shared", project_root):
        if (row.asset_type, row.name) in tracked:
            continue
        yield replace(row, state="untracked", reason="not lockfile-tracked; no wiki pin")


def _scan_project_local_drafts(project_root: Path) -> Iterator[StatusRow]:
    """Yield project_local draft rows (``<proj>/.memtomem/{kind}.local/``).

    Thin wrapper over :func:`_scan_draft_tier`. The CLI render layer appends
    ``(draft, no fan-out)`` to these rows per ADR-0011 §3 / ADR-0016 §7 —
    project_local artifacts have no runtime fan-out path.
    """
    yield from _scan_draft_tier("project_local", project_root)


def scan_user_artifacts() -> Iterator[StatusRow]:
    """Yield user-tier (``~/.memtomem/{kind}/``) draft rows — #1123 B4-2.

    Thin public wrapper over :func:`_scan_draft_tier` for the CLI's
    ``mm context status --scope user`` view. The user tier is the global
    ``~/.memtomem`` store, NOT under the project root that
    :func:`classify_status` walks, so it is enumerated here on demand
    rather than folded into ``classify_status`` — folding it in would make
    every status call (and every test exercising one) depend on the
    caller's real home directory. Without this, ``--scope user`` filtered an
    all-empty row set and reported "nothing" even when ``~/.memtomem`` held
    artifacts. Unlike project_local, user artifacts DO fan out to runtime
    dirs, so the CLI must not tag them ``(no fan-out)``.
    """
    yield from _scan_draft_tier("user", None)


def load_with_recovery(project_root: Path | str) -> tuple[dict, str | None]:
    """Read the lockfile in diagnostic mode, returning ``(doc, error_or_None)``.

    Used by ``status_cmd`` to surface a corrupt-lockfile error row at
    the top of the output without crashing. Catches the
    :class:`LockfileError` base so both a forward-compat version mismatch
    (``strict=False`` then returns the raw dict) and outright corruption
    (``strict=False`` degrades to a fresh ``{"version": LOCKFILE_VERSION}``)
    render as an error message instead of a traceback.
    """
    lockfile = Lockfile.at(project_root)
    try:
        doc = lockfile.load(strict=True)
        return doc, None
    except LockfileError as exc:
        return lockfile.load(strict=False), str(exc)


# ── Cross-project aggregation (#1280) ───────────────────────────────────
#
# One derivation consumed by BOTH batch surfaces — ``mm context status
# --all-projects`` and web ``GET /api/context/status-all`` — so the CLI
# table and the web payload cannot drift on classification vocabulary
# (the #1280 acceptance criterion). The web overview route imports the
# ``summarize_*`` helpers too, so the per-kind count keys here are the
# SAME keys ``GET /api/context/overview`` has always emitted.


def summarize_diff_statuses(triples: Sequence[tuple[str, str, str]]) -> dict:
    """Summarise ``(runtime, name, status)`` triples into per-status counts.

    Key vocabulary: ``total`` (distinct names) plus one count key per engine
    status with spaces snake_cased — ``in sync`` → ``in_sync``, ``out of
    sync`` → ``out_of_sync``, ``missing target`` → ``missing_target``,
    ``missing canonical`` → ``missing_canonical``, ``parse error`` →
    ``parse_error``, ``invalid name`` → ``invalid_name``. Moved verbatim
    from the web overview's ``_count_statuses`` (#1280) so the CLI batch
    and every web aggregate share one keying rule.
    """
    names: set[str] = set()
    counts: dict[str, int] = {}
    for _runtime, name, status in triples:
        names.add(name)
        key = status.replace(" ", "_")
        counts[key] = counts.get(key, 0) + 1
    return {"total": len(names), **counts}


def summarize_diff_with_canonical(
    triples: Sequence[tuple[str, str, str]],
    canonical_names: set[str],
) -> dict:
    """Summarise runtime diffs plus canonical-only draft rows.

    ``project_local`` agents / skills / commands have no runtime fan-out, so
    their diff list can be empty even when canonical drafts exist. Count the
    canonical names explicitly so aggregate totals match list views. Moved
    verbatim from the web overview's ``_count_context_statuses`` (#1280).
    """
    result = summarize_diff_statuses(triples)
    runtime_names = {name for _runtime, name, _status in triples}
    canonical_only = canonical_names - runtime_names
    if canonical_only:
        result["total"] = len(runtime_names | canonical_names)
        result["local_draft"] = len(canonical_only)
    return result


def summarize_settings_statuses(statuses: Sequence[str]) -> dict[str, int | str]:
    """Roll ``diff_settings`` per-generator statuses into the overview shape.

    Extracted verbatim from the web overview's inline settings block (#1280).
    ``total`` counts only **applicable** generators (``skipped`` items are
    N/A — an uninstalled runtime must not read as actionable work). The four
    non-skipped categories are all represented so ``in_sync + out_of_sync +
    missing_target + error == total`` holds — consumers can render
    per-status segments without entries silently dropping on the floor.
    ``status`` is the roll-up: ``in_sync`` (everything in sync or skipped),
    ``error`` (any per-file failure), else ``out_of_sync``. ``error`` is a
    COUNT here, parallel to its siblings — distinct from the bool ``error``
    flag web error envelopes carry; the two shapes live on disjoint paths.
    """
    total_applicable = sum(1 for s in statuses if s != "skipped")
    in_sync = sum(1 for s in statuses if s == "in sync")
    out_of_sync = sum(1 for s in statuses if s == "out of sync")
    missing_target = sum(1 for s in statuses if s == "missing target")
    error_count = sum(1 for s in statuses if s == "error")
    if all(s in ("in sync", "skipped") for s in statuses):
        status = "in_sync"
    elif any(s == "error" for s in statuses):
        status = "error"
    else:
        status = "out_of_sync"
    return {
        "total": total_applicable,
        "in_sync": in_sync,
        "out_of_sync": out_of_sync,
        "missing_target": missing_target,
        "error": error_count,
        "status": status,
    }


#: Per-kind count keys that do NOT signal drift. Anything else positive —
#: ``out_of_sync``, ``missing_target``, ``missing_canonical``,
#: ``parse_error``, ``invalid_name``, and any FUTURE engine status — reads
#: as drift, so a new status defaults to loud rather than silently clean
#: (Codex design-gate fold on #1280).
_CLEAN_DIFF_KEYS: frozenset[str] = frozenset({"total", "in_sync", "local_draft"})
#: The settings summary's clean keys (``status`` is the roll-up string, not
#: a count; ``skipped`` generators are excluded from ``total`` upstream).
_CLEAN_SETTINGS_KEYS: frozenset[str] = frozenset({"total", "in_sync", "status"})


def iter_kind_drift_counts(
    diff_counts: dict[str, dict[str, int | str]],
) -> Iterator[tuple[str, str, int]]:
    """Yield ``(kind, status_key, count)`` for every drift-signaling count.

    THE drift enumeration both batch surfaces derive from — the CLI renders
    its ``runtime drift:`` line from these triples and ``ProjectStatus.drift``
    is ``any()`` over the same stream, so what the table shows and what the
    roll-up says cannot disagree. Iteration order follows ``diff_counts``
    insertion order (skills → commands → agents → mcp_servers → settings as
    built by :func:`collect_project_status`), keys within a kind in summary
    insertion order — deterministic output for tests and humans alike.
    """
    for kind, counts in diff_counts.items():
        clean = _CLEAN_SETTINGS_KEYS if kind == "settings" else _CLEAN_DIFF_KEYS
        for key, value in counts.items():
            if key not in clean and isinstance(value, int) and value > 0:
                yield kind, key, value


@dataclass(frozen=True)
class ProjectStatus:
    """One project's full drift aggregate (#1280).

    ``rows`` / ``state_counts`` cover the wiki-install axis
    (:func:`classify_status`, filtered to the requested tier;
    ``state_counts`` always carries all seven :data:`StatusState` keys and
    conserves ``sum(values) == len(rows)``). ``diff_counts`` covers the
    canonical→runtime axis: skills / commands / agents / mcp_servers via
    :func:`summarize_diff_with_canonical` plus settings via
    :func:`summarize_settings_statuses`. A kind whose diff scan RAISED is
    absent from ``diff_counts`` and present in ``diff_errors`` with the raw
    exception — redaction/classification is a surface concern (the web
    formats an error envelope, the CLI prints the message).

    ``drift`` is the shared roll-up predicate: any row state in
    :data:`DRIFT_STATES`, any diff error, or any per-kind count outside the
    kind's clean key set. ``lockfile_error`` does NOT imply drift — surfaces
    report it as an error condition instead.
    """

    wiki_head: str | None
    lockfile_error: str | None
    rows: list[StatusRow]
    state_counts: dict[str, int]
    diff_counts: dict[str, dict[str, int | str]]
    diff_errors: dict[str, BaseException]
    drift: bool


def collect_project_status(
    project_root: Path | str,
    *,
    wiki: WikiStore | None = None,
    target_scope: TargetScope = "project_shared",
) -> ProjectStatus:
    """Aggregate one project's wiki-install states + runtime diff counts.

    The per-project unit both #1280 batch surfaces call once per eligible
    discovered scope. *wiki* should be the caller's single
    ``WikiStore.at_default()`` instance reused across the batch (the wiki
    is global); each call still probes ``current_commit`` itself via
    :func:`classify_status`, so ``wiki_head`` is honest per classification.
    Wiki-unreachable degrades exactly like single-project status — rows
    render with ``stale-pin`` reasons and ``wiki_head`` is ``None``.

    *target_scope* pins the row tier filter AND the diff/settings scope.
    Batch surfaces pass ``project_shared`` (the only tier the batch verbs
    accept): lockfile-tracked rows plus ``untracked`` canonicals stay,
    ``project_local`` draft rows drop, and the settings leg cannot inherit
    a config-pinned ``hooks.target_scope = "user"`` (the A-9 precedent —
    a host-tier scope must not leak into a batch over N projects).

    Per-kind diff failures are contained (recorded in ``diff_errors``);
    only :func:`classify_status` / lockfile reads raising would escape,
    and those already degrade internally — callers still isolate per
    project defensively.
    """
    from memtomem.context.agents import canonical_agent_name, diff_agents, list_canonical_agents
    from memtomem.context.commands import (
        canonical_command_name,
        diff_commands,
        list_canonical_commands,
    )
    from memtomem.context.mcp_servers import diff_mcp_servers, list_canonical_mcp_servers
    from memtomem.context.settings import diff_settings
    from memtomem.context.skills import diff_skills, list_canonical_skills

    root = Path(project_root).expanduser()
    _doc, lockfile_error = load_with_recovery(root)
    wiki_head, all_rows = classify_status(root, wiki=wiki)
    rows = [row for row in all_rows if row.tier == target_scope]

    state_counts: dict[str, int] = {state: 0 for state in get_args(StatusState)}
    for row in rows:
        state_counts[row.state] += 1

    diff_counts: dict[str, dict[str, int | str]] = {}
    diff_errors: dict[str, BaseException] = {}

    try:
        diff_counts["skills"] = summarize_diff_with_canonical(
            diff_skills(root, scope=target_scope),
            {p.name for p in list_canonical_skills(root, scope=target_scope)},
        )
    except Exception as exc:
        diff_errors["skills"] = exc

    try:
        # Layout-aware name extraction — under directory layout the manifest
        # is ``<name>/command.md``, so ``p.stem`` would collapse every draft
        # to one phantom "command" row (the overview's #624 lesson).
        diff_counts["commands"] = summarize_diff_with_canonical(
            diff_commands(root, scope=target_scope),
            {
                canonical_command_name(p, layout)
                for p, layout in list_canonical_commands(root, scope=target_scope)
            },
        )
    except Exception as exc:
        diff_errors["commands"] = exc

    try:
        diff_counts["agents"] = summarize_diff_with_canonical(
            diff_agents(root, scope=target_scope),
            {
                canonical_agent_name(p, layout)
                for p, layout in list_canonical_agents(root, scope=target_scope)
            },
        )
    except Exception as exc:
        diff_errors["agents"] = exc

    try:
        if target_scope == "project_shared":
            diff_counts["mcp_servers"] = summarize_diff_with_canonical(
                diff_mcp_servers(root),
                {p.stem for p in list_canonical_mcp_servers(root)},
            )
        else:
            # Single-tier by design — mirror the overview's placeholder.
            diff_counts["mcp_servers"] = {"total": 0, "local_draft": 0}
    except Exception as exc:
        diff_errors["mcp_servers"] = exc

    try:
        diff_counts["settings"] = summarize_settings_statuses(
            [r.status for r in diff_settings(root, scope=target_scope).values()]
        )
    except Exception as exc:
        diff_errors["settings"] = exc

    drift = (
        any(row.state in DRIFT_STATES for row in rows)
        or bool(diff_errors)
        or any(True for _ in iter_kind_drift_counts(diff_counts))
    )
    return ProjectStatus(
        wiki_head=wiki_head,
        lockfile_error=lockfile_error,
        rows=rows,
        state_counts=state_counts,
        diff_counts=diff_counts,
        diff_errors=diff_errors,
        drift=drift,
    )


def _tolerant_iter_entries(
    lockfile: Lockfile,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Like :meth:`Lockfile.iter_entries` but tolerant of unreadable lockfiles.

    Mirrors the alphabetical ``(asset_type, name)`` ordering contract.
    Used by :func:`classify_status` so a forward-compat lockfile (or a
    user editing ``version`` by hand) still surfaces its entries, and an
    outright corrupt lockfile yields zero entries instead of crashing —
    callers separately render a top-row error message via
    :func:`load_with_recovery`.
    """
    try:
        doc = lockfile.load(strict=True)
    except LockfileError:
        doc = lockfile.load(strict=False)
    for asset_type in sorted(doc):
        section = doc.get(asset_type)
        if not isinstance(section, dict):
            continue
        for name in sorted(section):
            entry = section[name]
            if not isinstance(entry, dict):
                continue
            yield asset_type, name, entry
