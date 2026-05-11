"""Read-only inventory of installed wiki assets in a project.

Powers ``mm context status`` — a diagnostic verb that walks
``<project>/.memtomem/lock.json``, classifies each entry against the
on-disk dest tree and the wiki, and returns a list of
:class:`StatusRow` for the CLI to render. No writes anywhere; safe to
run in cron pipes (``mm context status && mm context update --all``).

State semantics for each lockfile entry:

- ``ok`` — dest clean, lockfile pin reachable in wiki, pin == HEAD
- ``behind`` — dest clean, pin reachable, pin != HEAD ("update available")
- ``dirty`` — dest has local edits since ``installed_at``
- ``missing`` — lockfile entry exists but ``<project>/.memtomem/<type>/<name>/``
  is gone (collapses :data:`memtomem.context.dirty.DirtyReport.reason`
  values ``missing_dest`` and ``never_installed``)
- ``stale-pin`` — wiki absent OR pin not reachable in the wiki repo
  (history rewrite, force-push past the pin, etc.)

The wiki-absent case still renders rows: status is read-only and the
lockfile is local; we just can't compute ``behind``/``stale-pin``
without a wiki to compare against.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from memtomem.config import TargetScope
from memtomem.context.agents import AGENT_DIR_FILENAME
from memtomem.context.commands import COMMAND_DIR_FILENAME
from memtomem.context.dirty import is_asset_dirty
from memtomem.context.lockfile import Lockfile, LockfileVersionError
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import SKILL_MANIFEST
from memtomem.wiki.store import WikiNotFoundError, WikiStore

__all__ = [
    "StatusRow",
    "StatusState",
    "classify_status",
]


StatusState = Literal["ok", "behind", "dirty", "missing", "stale-pin", "local-draft"]

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
    populated only for ``state == "dirty"`` and reflects the count of
    files with ``mtime > installed_at_epoch``.

    ``reason`` is a human-readable detail line that the CLI appends in
    parens after the row. Common contents: ``"N file(s) modified
    locally"`` for dirty, ``"dest missing"`` for missing, ``"pin <abbr>
    not reachable"`` / ``"wiki not present"`` for stale-pin.

    ``tier`` distinguishes lockfile-tracked installs (``project_shared``)
    from locally-authored drafts discovered by walking
    ``<proj>/.memtomem/<artifact>.local/`` (``project_local``). The CLI
    appends ``(draft, no fan-out)`` after project_local rows per ADR-0011
    §3 / ADR-0016 §7. ``user`` tier is reserved for a future scan;
    today's `mm context install` only writes project_shared, so no
    ``user``-tier rows are produced.
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
    :meth:`WikiStore.commit_is_reachable`. When the wiki is absent,
    that method isn't called.
    """
    project_root_path = Path(project_root).expanduser()
    lockfile = Lockfile.at(project_root_path)

    wiki_head: str | None = None
    wiki_present = False
    if wiki is None:
        wiki = WikiStore.at_default()
    try:
        wiki_head = wiki.current_commit()
        wiki_present = True
    except WikiNotFoundError:
        wiki_head = None
        wiki_present = False

    rows: list[StatusRow] = []
    for asset_type, name, entry in _tolerant_iter_entries(lockfile):
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
        elif report.reason == "dirty":
            state = "dirty"
            dirty_count = len(report.dirty_files)
            reason = f"{dirty_count} file(s) modified locally"
        else:  # report.reason == "clean"
            if not wiki_present:
                state = "stale-pin"
                reason = "wiki not present"
            elif not pin_commit:
                state = "stale-pin"
                reason = "lockfile entry missing wiki_commit"
            elif pin_commit == wiki_head:
                state = "ok"
            elif wiki.commit_is_reachable(pin_commit):
                state = "behind"
                reason = "wiki advanced past pin"
            else:
                state = "stale-pin"
                reason = f"pin {pin_commit[:12]} not reachable"

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


def _scan_project_local_drafts(project_root: Path) -> Iterator[StatusRow]:
    """Yield ``StatusRow``s for every valid project_local draft on disk.

    Walks ``<proj>/.memtomem/{agents,commands,skills}.local/`` and emits
    one row per ``<name>/`` subdirectory whose kind-specific manifest
    file is present (``agent.md`` / ``command.md`` / ``SKILL.md``). The
    presence of the manifest is the same validity probe migrate uses
    to recognise a directory-layout artifact (``migrate._detect_source_scope``).

    Emitted rows carry ``tier="project_local"``, ``state="local-draft"``,
    and empty ``pin_commit``/``installed_at`` — project_local artifacts
    are not currently tracked in the lockfile (install path is
    project_shared-only today; locally-authored drafts never enter
    ``lock.json``). The CLI render layer appends ``(draft, no fan-out)``
    to these rows per ADR-0011 §3 / ADR-0016 §7.
    """
    for asset_type in ("agents", "commands", "skills"):
        local_root = canonical_artifact_dir(
            asset_type,  # type: ignore[arg-type]
            "project_local",
            project_root,
        )
        if not local_root.is_dir():
            continue
        manifest = _LOCAL_DRAFT_MANIFEST[asset_type]
        for entry in sorted(local_root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if not (entry / manifest).is_file():
                # Skip directories that don't satisfy the kind's
                # manifest contract — same convention as migrate's
                # source-scope probe. A future flat-layout sweep would
                # add a sibling check here.
                continue
            yield StatusRow(
                asset_type=asset_type,  # type: ignore[arg-type]
                name=entry.name,
                pin_commit="",
                installed_at="",
                state="local-draft",
                dirty_file_count=0,
                reason=None,
                tier="project_local",
            )


def load_with_recovery(project_root: Path | str) -> tuple[dict, str | None]:
    """Read the lockfile in diagnostic mode, returning ``(doc, error_or_None)``.

    Used by ``status_cmd`` to surface a corrupt-lockfile error row at
    the top of the output without crashing. Wraps
    :meth:`Lockfile.load` with ``strict=False`` so a forward-compat
    version mismatch returns the raw dict; outright JSON corruption
    falls through to ``Lockfile.load``'s warning path which returns
    a fresh ``{"version": LOCKFILE_VERSION}``.
    """
    lockfile = Lockfile.at(project_root)
    try:
        doc = lockfile.load(strict=True)
        return doc, None
    except LockfileVersionError as exc:
        return lockfile.load(strict=False), str(exc)


def _tolerant_iter_entries(
    lockfile: Lockfile,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Like :meth:`Lockfile.iter_entries` but tolerant of version mismatches.

    Mirrors the alphabetical ``(asset_type, name)`` ordering contract.
    Used by :func:`classify_status` so a forward-compat lockfile (or a
    user editing ``version`` by hand) still surfaces its entries —
    callers separately render a top-row error message via
    :func:`load_with_recovery`.
    """
    try:
        doc = lockfile.load(strict=True)
    except LockfileVersionError:
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
