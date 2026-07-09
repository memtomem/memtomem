"""``mm wiki <kind> promote`` engine — import a project canonical into the wiki.

The wiki ↔ context-gateway lifecycle is otherwise one-way: ``mm context
install`` / ``update`` snapshot committed wiki assets into a project's
``.memtomem/``. Promote is the missing inbound verb — it copies a
``project_shared`` canonical (the ``untracked`` rows ``mm context status``
reports) into the host-global wiki, validates it with the same
:func:`memtomem.wiki.inspect.lint_asset` gate the CLI ``lint`` verb uses, and
commits it through the shared :func:`memtomem.wiki.commit.commit_targets`
engine (issue #1683).

Design constraints (all enforced below):

- **Privacy is a hard gate, no bypass.** Promote crosses project → host-global
  and the wiki can be pushed to a backup remote (``mm wiki push``), so the
  copied bytes go through the audited :func:`memtomem.privacy.enforce_write_guard`
  chokepoint at ``scope="project_shared"`` — the tier whose Gate A ban is
  absolute regardless of ``force_unsafe`` (ADR-0011 §5). The scan set is
  *exactly* the write set: each file is read once, scanned, and the same bytes
  are what land in the wiki (no scan≠copy TOCTOU).
- **Fail-closed regular-file walk.** Source enumeration reuses
  :func:`memtomem.context._atomic.iter_installed_files` — it skips
  ``.git``/``__pycache__``/``.DS_Store``/``*.bak`` and symlinks (a symlink to
  an out-of-tree target must never be dereferenced into wiki history) and
  raises rather than silently shrinking on an unreadable subtree.
- **One lock across the whole critical section.** The absent re-check, the
  copy, the lint, and the commit all run while holding the wiki-root commit
  lock (the same lock ``mm wiki commit`` / ``mm web`` commits take, keyed on
  the wiki root). A concurrent promote of the same name therefore serializes:
  the second one blocks, then its under-lock re-check sees the asset now exists
  and refuses cleanly — before copying anything — so the rollback below can
  never delete another commit's just-landed files.
- **Scan set == commit set.** The bytes are committed via
  :meth:`WikiStore.commit_paths`, which hashes the in-memory bytes directly
  (``git hash-object`` on the saved blob) rather than re-reading the working
  tree, so the bytes that were privacy-scanned are byte-for-byte the bytes that
  land in history — no scan≠write TOCTOU. The working-tree copy still carries
  the exec bit so ``commit_paths``' disk-first mode resolution keeps ``100755``
  scripts runnable. The commit is CAS-guarded on the HEAD read inside the lock.
- **Rollback owns only its own copy.** The asset dir did not exist when the
  under-lock re-check passed, so on any failure after the first copy the
  created dir is removed, restoring the working tree; because the whole section
  is single-locked, that dir is exclusively this invocation's.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memtomem import privacy
from memtomem.context._atomic import _file_lock, atomic_write_bytes, iter_installed_files
from memtomem.context._names import validate_name
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.wiki.commit import wiki_commit_lock_path
from memtomem.wiki.inspect import LintReport, lint_asset
from memtomem.wiki.store import WikiStore

# Cross-process wiki commit lock budget (seconds) — matches
# ``wiki.commit._COMMIT_LOCK_TIMEOUT`` so promote and a concurrent
# ``mm wiki commit`` / ``mm web`` commit wait on each other symmetrically.
_PROMOTE_LOCK_TIMEOUT = 30.0

logger = logging.getLogger(__name__)

__all__ = [
    "PromoteLintError",
    "PromotePrivacyError",
    "PromoteResult",
    "PromoteSourceError",
    "WikiAssetExistsError",
    "promote_asset",
]

AssetType = Literal["skills", "agents", "commands"]

# Kind → canonical manifest filename. Same probe context/status.py uses to
# decide a dir is a real artifact (not a stray directory).
_MANIFEST: dict[str, str] = {
    "skills": "SKILL.md",
    "agents": "agent.md",
    "commands": "command.md",
}


class PromoteSourceError(RuntimeError):
    """The project has no promotable canonical for ``<kind>/<name>``.

    Covers a missing source directory, a directory without its kind manifest
    (``SKILL.md`` / ``agent.md`` / ``command.md``), and an empty source tree.
    The message is safe to surface verbatim (it names the project-local path,
    which is the user's own).
    """


class WikiAssetExistsError(RuntimeError):
    """The wiki already holds ``<kind>/<name>`` (in the working tree or at HEAD).

    Promote is non-destructive: refuse rather than overwrite. Even a
    byte-identical asset refuses — adopting an existing project copy into the
    lockfile is a separate verb (issue #1684), and a silent no-op here would
    blur "already promoted" with "still needs adoption". Message is path-free.
    """


class PromotePrivacyError(RuntimeError):
    """A source file tripped the Gate A privacy scan; promote is refused.

    Carries the offending relative path and the hit count only — never the
    matched bytes (the "never echo secrets" contract,
    ``feedback_force_unsafe_redaction_valve_only.md``). No force bypass:
    promote writes host-global git history (ADR-0011 §5).
    """

    def __init__(self, rel: str, hits_count: int) -> None:
        super().__init__(
            f"Gate A: {rel} contains {hits_count} privacy pattern hit(s); "
            f"promote to the host-global wiki is rejected. git history is "
            f"forever and the wiki can be pushed — no force bypass available. "
            f"Remove the secret from the project canonical first."
        )
        self.rel = rel
        self.hits_count = hits_count


class PromoteLintError(RuntimeError):
    """The copied asset failed :func:`lint_asset`; the copy was rolled back.

    Carries the :class:`~memtomem.wiki.inspect.LintReport` so the CLI can print
    the error findings the same way ``mm wiki <kind> lint`` does.
    """

    def __init__(self, report: LintReport) -> None:
        errors = [f.message for f in report.findings if f.level == "error"]
        super().__init__(f"{report.asset_type}/{report.name} failed lint: " + "; ".join(errors))
        self.report = report


@dataclass(frozen=True)
class PromoteResult:
    """Outcome of a successful :func:`promote_asset`.

    ``wiki_dirty`` is read back after the commit so the caller can warn about
    residue a concurrent writer left elsewhere in the wiki. ``commit_message``
    is the resolved message (user-supplied or the default) so the caller can
    run the same soft privacy scan ``mm wiki commit`` applies to its message.
    """

    asset_type: AssetType
    name: str
    wiki_head: str
    wiki_dirty: bool
    files_committed: int
    lint_warnings: tuple[str, ...]
    commit_message: str


def promote_asset(
    store: WikiStore,
    project_root: Path,
    asset_type: AssetType,
    name: str,
    *,
    message: str | None = None,
) -> PromoteResult:
    """Copy a ``project_shared`` canonical into the wiki, lint it, and commit.

    Args:
        store: The target wiki.
        project_root: Project root owning the source ``.memtomem/`` tree.
        asset_type: Plural kind (``"skills"`` / ``"agents"`` / ``"commands"``).
        name: Asset name (validated).
        message: Commit message; defaults to
            ``"wiki: promote <type>/<name> from <project-dir-name>"``.

    Returns:
        :class:`PromoteResult`.

    Raises:
        PromoteSourceError: No promotable source canonical.
        WikiAssetExistsError: The wiki already holds this asset.
        PromotePrivacyError: A source file tripped Gate A.
        PromoteLintError: The copied asset failed lint (copy rolled back).
        WikiHeadMovedError / WikiDetachedHeadError / WikiNothingToCommitError /
        TimeoutError / RuntimeError: propagated from the commit (copy rolled
        back). ``TimeoutError`` means a concurrent committer held the lock past
        the budget.
    """
    validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    store.require_exists()

    source_dir = canonical_artifact_dir(asset_type, "project_shared", project_root) / name
    manifest = _MANIFEST[asset_type]
    if not (source_dir / manifest).is_file():
        raise PromoteSourceError(
            f"no project_shared {asset_type.removesuffix('s')} to promote: "
            f"{source_dir}/{manifest} does not exist"
        )

    dest_dir = store.root / asset_type / name

    # Cheap fast-fail before the privacy scan; the authoritative check is under
    # the lock below.
    if dest_dir.exists():
        raise WikiAssetExistsError(
            f"wiki already has {asset_type}/{name} in its working tree — "
            f"refusing to overwrite (promote is non-destructive)"
        )

    # Read → scan → stage: read each source file once so the bytes scanned are
    # exactly the bytes committed (commit_paths hashes these in-memory bytes,
    # never a working-tree re-read). Preserve the exec bit so commit_paths'
    # disk-first mode resolution keeps 100755 scripts runnable. This touches no
    # wiki state, so it runs before the lock.
    staged: list[tuple[Path, bytes, int]] = []  # (rel_path, data, mode)
    try:
        source_files = sorted(iter_installed_files(source_dir))
    except OSError as exc:
        # iter_installed_files is fail-closed — an unreadable subtree raises
        # rather than silently shrinking. Classify it instead of leaking a
        # traceback (the CLI promises no traceback).
        raise PromoteSourceError(
            f"cannot enumerate {source_dir} — a file or subdirectory is unreadable ({exc})"
        ) from exc
    for src_file in source_files:
        rel = src_file.relative_to(source_dir)
        try:
            data = src_file.read_bytes()
        except OSError as exc:
            raise PromoteSourceError(f"cannot read {source_dir / rel} — {exc}") from exc
        # errors="replace" so non-UTF8 bytes cannot mask an embedded ASCII
        # secret from the scanner.
        guard = privacy.enforce_write_guard(
            data.decode("utf-8", errors="replace"),
            surface="cli_wiki_promote",
            force_unsafe=False,
            scope="project_shared",
            audit_context={
                "asset_type": asset_type,
                "name": name,
                "source_file": rel.as_posix(),
                "wiki_scope": "host_global",
            },
            record_outcome=True,
        )
        if guard.decision in ("blocked", "blocked_project_shared"):
            raise PromotePrivacyError(rel.as_posix(), len(guard.hits))
        mode = 0o755 if (os.name != "nt" and os.access(src_file, os.X_OK)) else 0o644
        staged.append((rel, data, mode))

    if not staged:
        # Manifest existed but the walk yielded nothing (e.g. the manifest is a
        # symlink). Nothing safe to promote.
        raise PromoteSourceError(
            f"no regular files to promote under {source_dir} "
            f"(symlinks and skip-listed files are excluded)"
        )

    # Critical section under the wiki commit lock: absent re-check → copy →
    # lint → commit, so a concurrent promote of the same name serializes and the
    # rollback below can only ever remove this invocation's own copied dir.
    lock_path = wiki_commit_lock_path(store.root)
    with _file_lock(lock_path, timeout=_PROMOTE_LOCK_TIMEOUT):
        head = store.current_commit()
        if dest_dir.exists():
            raise WikiAssetExistsError(
                f"wiki already has {asset_type}/{name} in its working tree — "
                f"refusing to overwrite (promote is non-destructive)"
            )
        if _asset_committed_at(store, head, asset_type, name):
            raise WikiAssetExistsError(
                f"wiki already has {asset_type}/{name} committed at HEAD — "
                f"refusing to overwrite (promote is non-destructive)"
            )

        files_map: dict[str, bytes] = {}
        try:
            for rel, data, mode in staged:
                out = dest_dir / rel
                atomic_write_bytes(out, data, mode=mode)
                files_map[out.relative_to(store.root).as_posix()] = data

            report = lint_asset(store, asset_type, name)
            if not report.ok:
                raise PromoteLintError(report)

            msg = (message or "").strip() or (
                f"wiki: promote {asset_type}/{name} from {project_root.name}"
            )
            # commit_paths hashes the in-memory bytes and CAS-advances the ref
            # against the HEAD we read inside this lock, so nothing lands if
            # HEAD moved underneath (an external git that honours no lock).
            new_head = store.commit_paths(files_map, message=msg, expected_head=head)
        except BaseException:
            # The under-lock re-check proved dest_dir absent before the first
            # copy, and we hold the lock exclusively, so this dir is ours alone;
            # removing it restores the working tree. commit_paths raises before
            # advancing the ref, so on any exception nothing was committed.
            shutil.rmtree(dest_dir, ignore_errors=True)
            raise

        wiki_dirty = store.is_dirty()

    warnings = tuple(f.message for f in report.findings if f.level == "warning")
    return PromoteResult(
        asset_type=asset_type,
        name=name,
        commit_message=msg,
        wiki_head=new_head,
        wiki_dirty=wiki_dirty,
        files_committed=len(files_map),
        lint_warnings=warnings,
    )


def _asset_committed_at(store: WikiStore, commit: str, asset_type: str, name: str) -> bool:
    """True iff ``<asset_type>/<name>`` has any tracked entry at *commit*.

    ``asset_files_at_commit`` raises ``AssetNotFoundError`` when the asset path
    holds no entries at the revision; any other return (including ``[]`` for an
    asset of only skipped entries) means it exists.
    """
    from memtomem.context.install import AssetNotFoundError

    try:
        store.asset_files_at_commit(commit, asset_type, name)
    except AssetNotFoundError:
        return False
    return True
