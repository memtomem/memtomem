"""Detect a test suite writing the developer's (or CI runner's) real ``$HOME``.

Issue #1892: 20 tests wrote the real ``~/.claude/settings.json`` and restored it
in a ``finally``, with the only copy in a local variable. A non-graceful exit
skipped the restore and the file was gone; the next test then read the wreckage
as *its* backup and restored that, so the loss laundered itself into a
stable-looking state. Nothing errored — the residue was valid JSON.

``helpers.set_home`` prevents that for a test that knows it touches home.
``test_no_backup_restore_dance_guard.py`` bans the specific restore spelling.
Neither covers the case that actually caused #1892: **the test body named no
home path at all.** It called an HTTP route, and the route resolved
``Path.home()``. Production has dozens of such call sites, so no static analysis
at any granularity can see it — only watching the filesystem can.

This module is that watcher, as a pure library: derive what memtomem writes
under ``$HOME``, fingerprint it, and compare. It installs no pytest hooks; the
wiring lives in ``conftest.py`` so this stays importable and testable on its own.

Design constraints that are *measured*, not assumed:

* **Per-test hashing of everything is infeasible.** A real ``~/.memtomem`` here
  holds a ~99 MB SQLite DB; hashing the whole protected set on every teardown
  would read on the order of a terabyte across the suite. Hence two tiers: a
  handful of small config **files** are fingerprinted every test (measured: 4
  files, ~534 us, ≈5.5 s across 10,300 tests); whole **trees** get one content
  manifest per session (measured: 259 entries, 0.05 s). Both tiers share one
  :func:`fingerprint` implementation — they used to differ, and the divergence
  was itself a bug.
* **Metadata alone is not a fingerprint.** A same-size rewrite with a restored
  mtime is invisible to ``(size, mtime)`` — and this repo already writes
  preserved-mtime fixtures, so that is reachable today. A byte-identical rewrite
  that moves mtime is the opposite error, a false alarm; one was observed during
  development when an editor rewrote ``settings.json`` with identical bytes. So
  fingerprints are content digests, with metadata used only above the size cap
  and that degradation recorded rather than implied.
* **No backups, and no crashed-session manifest.** Backups were dropped because
  ``tempfile.mkdtemp(mode=…)`` is ignored on Windows (``CreateDirectoryW``
  inherits the parent DACL) and ``os.chmod(0o700)`` only toggles the read-only
  attribute — a "private" copy of files that can contain MCP credentials is not
  private on the platform where it would matter. An on-disk manifest with a lock
  protocol, to catch a session killed mid-run, was built and then removed: three
  review rounds kept finding races and schema-validation holes in it, and it
  covered only the window between a write and the next teardown. The per-test
  tier already reports damage the moment it happens, which is the case that
  matters. Detect and report; recovery is the developer's VCS.
* **Uncovered ground is announced, never assumed.** Anything the walk cannot
  cover (a directory symlink, whose target may live anywhere) is listed in
  :attr:`TreeManifest.uncovered`, and anything it cannot even traverse raises
  :class:`HomeGuardError`. A silently uncovered subtree is the same failure
  shape as a guard that is not armed at all.

The protected set is **asked of production**, never hand-listed, so a new
runtime or a new user-tier path is covered without editing this file.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_mod
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

#: Set to ``off``/``0``/``false``/``no`` to disable. Per-invocation only — it
#: cannot be committed into a test file, which is the point. There is no marker
#: and no allowlist: no test in this repo legitimately writes the real home, and
#: the remedy is always ``helpers.set_home``.
DISABLE_ENV = "MEMTOMEM_TEST_HOME_GUARD"

#: Files larger than this are fingerprinted by metadata instead of content, and
#: the manifest records that so a comparison can say so out loud rather than
#: implying content coverage. The real ~99 MB SQLite DB is the reason.
MAX_DIGEST_BYTES = 8 * 1024 * 1024

#: Refuse to arm rather than silently guard a fraction of a huge tree.
MAX_TREE_ENTRIES = 50_000

#: Mirrors ``context._runtime_targets.ArtifactKind``. Spelled as literals rather
#: than imported so this module stays importable without pulling production in at
#: collection time — the fan-out lookup is keyed on exactly these three.
_ARTIFACTS: tuple[Literal["skills", "agents", "commands"], ...] = (
    "skills",
    "agents",
    "commands",
)


class HomeGuardError(RuntimeError):
    """Arming failed. Raised rather than warned — see ``MAX_TREE_ENTRIES``."""


def guard_enabled(env: dict[str, str] | None = None) -> bool:
    """Pure predicate for :data:`DISABLE_ENV`, so it is testable without env games."""
    raw = (env if env is not None else os.environ).get(DISABLE_ENV, "")
    return raw.strip().lower() not in {"off", "0", "false", "no"}


@contextmanager
def as_home(home: Path) -> Iterator[None]:
    """Point ``Path.home()`` / ``expanduser()`` at ``home`` for the duration.

    The production resolvers read the environment at call time, so this is how
    we ask them *"where would you write, for this home?"* instead of
    re-implementing their path math — which is the whole point, since a new
    runtime must be covered without touching this file. Sets ``USERPROFILE`` as
    well as ``HOME`` for the same reason ``helpers.set_home`` does: Windows
    consults it first.
    """
    previous = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass(frozen=True)
class ProtectedSet:
    """What the guard watches, split by how it can afford to watch it."""

    files: tuple[Path, ...]
    roots: tuple[Path, ...]
    excluded: tuple[Path, ...]


def _uncollapse(value: str, home: Path) -> Path:
    """``registry_location_paths`` returns ``$HOME``-collapsed strings."""
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _effective_config_paths(home: Path) -> list[Path]:
    """Home-contained *write* targets from the user's ``config.json``.

    A developer who moved their DB elsewhere under ``$HOME`` would otherwise have
    a writable, unguarded target — the defaults alone do not cover them. Read as
    plain JSON: cheap, and with no import of the settings machinery whose own
    defaults we already cover. Env-var overrides are deliberately not followed —
    they are per-invocation and in a test run they point at ``tmp_path`` anyway.

    ``indexing.memory_dirs`` is deliberately **excluded**, and the reason is a
    trade-off rather than a claim that memtomem does not write there — it does:
    ``mem_add``, imports, fetch and session summaries all land beneath those
    directories. The problem is that they are *shared*. On a real machine they
    resolve to things like ``~/.claude/projects/<slug>/memory``,
    ``~/.claude/plans`` and ``~/memories`` — 46 roots here — every one of which
    the developer's own coding agent also rewrites continuously, concurrently
    with any test run. Watching them would attribute someone else's ordinary
    write to whichever test happened to be running, and a guard that cries wolf
    gets switched off. So this is a knowing false-negative: a test that writes a
    *user-configured* memory dir is not caught. memtomem's own default memories
    directory is still covered — it lives under ``~/.memtomem``.
    """
    override = home / ".memtomem" / "config.json"
    try:
        raw = json.loads(override.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[Path] = []
    storage = raw.get("storage")
    if isinstance(storage, dict) and isinstance(storage.get("sqlite_path"), str):
        found.extend(_sqlite_family(Path(storage["sqlite_path"]).expanduser()))
    gateway = raw.get("context_gateway")
    if isinstance(gateway, dict) and isinstance(gateway.get("known_projects_path"), str):
        found.append(Path(gateway["known_projects_path"]).expanduser())
    tracing = raw.get("session_trace")
    if isinstance(tracing, dict) and isinstance(tracing.get("jsonl_path"), str):
        found.append(Path(tracing["jsonl_path"]).expanduser())
    return found


def _sqlite_family(db: Path) -> list[Path]:
    """A SQLite DB and the sidecars written alongside it.

    Watching only the ``.db`` file is a hole with a sharp edge: in WAL mode a
    write can land entirely in ``-wal`` and leave the main file's bytes
    untouched, so a moved DB would look pristine while the data changed. The
    provenance key is a real sidecar too (``provenance.py:85`` —
    ``<db-stem>.provenance_key``).
    """
    return [
        db,
        db.with_name(db.name + "-wal"),
        db.with_name(db.name + "-shm"),
        db.with_name(db.name + "-journal"),
        db.with_suffix(".provenance_key"),
    ]


def derive_protected(home: Path) -> ProtectedSet:
    """Ask production which user-tier paths it writes, resolved against ``home``."""
    from memtomem.context._runtime_targets import KNOWN_RUNTIMES, runtime_fanout_root
    from memtomem.context.scope_resolver import DEFAULT_USER_ARTIFACT_BASE
    from memtomem.context.settings import SETTINGS_GENERATORS
    from memtomem.wiki import store as wiki_store

    files: set[Path] = set()
    roots: set[Path] = set()

    with as_home(home):
        # A project root that cannot exist: a "user"-scope target must ignore it
        # entirely, and the pins assert exactly that.
        sentinel_root = home / "__home_guard_no_such_project__"

        for generator in SETTINGS_GENERATORS.values():
            target = generator.target_file(sentinel_root, "user")
            if target is not None:
                files.add(Path(target))

        # NOT registry_location_paths(): that is a *detection* registry — the
        # locations memtomem probes to report which clients are installed. Some
        # (the Antigravity configs) are only ever read and shown to the user as
        # manual paste targets, never written. Watching a file memtomem does not
        # write means blaming that tool's own churn on whichever test is
        # running. Write targets come from the settings generators above.

        for artifact in _ARTIFACTS:
            for runtime in KNOWN_RUNTIMES:
                root = runtime_fanout_root(artifact, runtime, "user", None)
                if root is not None:
                    roots.add(Path(root))

        # ``~/.memtomem`` — covers the DB, memories, known_projects.json,
        # traces/, uploads/, config.d/ in one root rather than enumerating them,
        # so a new sibling is guarded the day it is added.
        roots.add(DEFAULT_USER_ARTIFACT_BASE.expanduser().resolve())
        # Call-time resolver, never the import-time-frozen DEFAULT_WIKI_PATH:
        # that constant is bound to the real home and would survive this
        # function's synthetic-home filtering.
        roots.add(wiki_store._wiki_path_from_env().resolve())
        roots.update(_effective_config_paths(home))

    home = home.resolve()
    # The fastembed model cache is legitimately populated by the golden-path and
    # multilingual runs. Excluding it is load-bearing, not cosmetic: it lives
    # inside ~/.memtomem, which is a protected root.
    excluded = (home / ".memtomem" / "cache",)

    def _keep(path: Path) -> bool:
        path = _abs(path, home)
        if not path.is_relative_to(home):
            return False  # out of contract — this guard only speaks about $HOME
        return not any(path.is_relative_to(skip) for skip in excluded)

    kept_files = tuple(sorted({_abs(p, home) for p in files if _keep(p)}))
    candidate_roots = sorted({_abs(p, home) for p in roots if _keep(p)})
    # A file already watched per-test must not also be a root: the per-test tier
    # wins, since it is the one that can attribute a change to a test.
    candidate_roots = [r for r in candidate_roots if r not in set(kept_files)]
    # Collapse nested roots — the effective DB path resolves under ~/.memtomem by
    # default, and walking it twice would stat and hash every entry twice.
    kept_roots = tuple(
        r
        for r in candidate_roots
        if not any(r != other and r.is_relative_to(other) for other in candidate_roots)
    )

    if not kept_files and not kept_roots:
        raise HomeGuardError(
            "home-guard derivation produced nothing — the derivation is broken "
            "(a production registry moved or renamed). Refusing to arm, because "
            "an empty protected set looks exactly like a clean run."
        )
    return ProtectedSet(files=kept_files, roots=kept_roots, excluded=excluded)


def _abs(path: Path, home: Path) -> Path:
    return path if path.is_absolute() else (home / path)


# -- fingerprints -----------------------------------------------------------


@dataclass(frozen=True)
class Stamp:
    """Cheap identity for the per-test tier."""

    exists: bool
    size: int = -1
    mtime_ns: int = -1


def stat_stamp(path: Path) -> Stamp:
    try:
        st = path.lstat()
    except (OSError, ValueError):
        return Stamp(exists=False)
    return Stamp(exists=True, size=st.st_size, mtime_ns=st.st_mtime_ns)


def file_digest(path: Path) -> str | None:
    """``sha256`` of ``path``'s bytes, or ``None`` if it cannot be read."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def snapshot_files(paths: tuple[Path, ...]) -> dict[str, Stamp]:
    return {str(p): stat_stamp(p) for p in paths}


@dataclass
class Violation:
    path: str
    kind: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.path} — {self.kind}: {self.detail}"


def fingerprint(path: Path) -> str | None:
    """A string that changes iff the entry changed. ``None`` means absent.

    ONE implementation for both tiers. They used to differ — the per-test tier
    hashed blindly while the tree walker handled symlinks and the size cap — and
    the divergence was itself the bug: the per-test tier followed links (so one
    link could pull in a huge or blocking target, once per test), had no cap, and
    made two *different* unreadable files compare equal because both hashed to
    ``None``.

    The regular-file path opens **once** and classifies from ``fstat`` on that
    same descriptor. Separate ``lstat`` → type check → ``open`` calls are three
    races: the file can grow past the cap between them (an 8 MiB+ file was
    reproduced getting a full ``sha256:``), or become a FIFO and block the read
    forever. ``O_NOFOLLOW`` keeps a swapped-in symlink from being followed and
    ``O_NONBLOCK`` keeps a swapped-in device from hanging the open; the read is
    bounded regardless of what ``fstat`` claimed.

    Content-based for regular files, so a byte-identical rewrite that only moves
    mtime is not a change. Above the cap it degrades to ``meta:`` — which does
    mean a byte-identical rewrite of a large file reads as a change; that is the
    documented trade for not hashing a ~99 MB DB.
    """
    try:
        st = path.lstat()  # never follows: the link is the thing we fingerprint
    except (OSError, ValueError):
        return None
    if stat_mod.S_ISLNK(st.st_mode):
        try:
            return f"symlink:{os.readlink(path)}"
        except OSError:
            return "symlink:<unreadable>"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if not stat_mod.S_ISREG(st.st_mode):
        # FIFOs, sockets, devices — describe, never open. Reading one can hang
        # the session outright.
        return f"special:{stat_mod.S_IFMT(st.st_mode)}:{st.st_mtime_ns}"

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        # Distinguish unreadable-here from unreadable-there: two different files
        # that both fail to open are not the same file.
        return f"unreadable:{st.st_size}:{st.st_mtime_ns}"
    try:
        fst = os.fstat(fd)
        if not stat_mod.S_ISREG(fst.st_mode):
            return f"special:{stat_mod.S_IFMT(fst.st_mode)}:{fst.st_mtime_ns}"
        if fst.st_size > MAX_DIGEST_BYTES:
            return f"meta:{fst.st_size}:{fst.st_mtime_ns}"
        digest = hashlib.sha256()
        read = 0
        while True:
            try:
                chunk = os.read(fd, 1024 * 1024)
            except OSError:
                return f"unreadable:{fst.st_size}:{fst.st_mtime_ns}"
            if not chunk:
                break
            read += len(chunk)
            if read > MAX_DIGEST_BYTES:
                # It grew under us. Stop rather than stream an unbounded file.
                return f"meta:{read}+:{fst.st_mtime_ns}"
            digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    finally:
        os.close(fd)


def snapshot_file_digests(paths: tuple[Path, ...]) -> dict[str, str | None]:
    """Fingerprints for the per-test tier — always computed, never stat-gated.

    A stat fast path looks free but is blind to a same-size rewrite whose mtime
    is restored with ``os.utime``, and this repo already writes preserved-mtime
    fixtures. Affordable because the tier is a handful of small config files.
    """
    return {str(p): fingerprint(p) for p in paths}


def diff_files(
    before: dict[str, Stamp],
    after: dict[str, Stamp],
    *,
    digests: dict[str, str | None] | None = None,
    after_digests: dict[str, str | None] | None = None,
) -> list[Violation]:
    """Compare two per-test snapshots.

    Existence comes from the stamps; *content* from the fingerprints.
    """
    violations: list[Violation] = []
    for key, old in before.items():
        new = after.get(key)
        if new is None:
            continue
        path = Path(key)
        if old.exists and not new.exists:
            violations.append(Violation(key, "deleted", "the file is gone"))
            continue
        if not old.exists and new.exists:
            violations.append(Violation(key, "created", f"{new.size} bytes appeared"))
            continue
        if not old.exists and not new.exists:
            continue
        old_print = (digests or {}).get(key)
        if after_digests is not None and key in after_digests:
            new_print = after_digests[key]
        else:
            new_print = fingerprint(path)
        if old_print == new_print:
            continue
        detail = f"size {old.size} → {new.size}"
        if old_print and new_print:
            detail += f", {old_print[:20]}… → {new_print[:20]}…"
        violations.append(Violation(key, "modified", detail))
    return violations


@dataclass
class TreeManifest:
    #: Protected subtrees the walk could not cover — currently directory
    #: symlinks, whose targets may live anywhere. Surfaced rather than skipped
    #: silently: an uncovered subtree that nobody mentions is the same failure
    #: shape as a guard that is not armed at all. PR 3b prints these in the
    #: armed banner.
    entries: dict[str, str] = field(default_factory=dict)
    uncovered: tuple[str, ...] = ()
    #: Relative paths fingerprinted by metadata because they exceed
    #: ``MAX_DIGEST_BYTES``. Reported on a difference so the message never
    #: implies content coverage it does not have.
    metadata_only: tuple[str, ...] = ()


def tree_manifest(
    roots: tuple[Path, ...],
    *,
    max_entries: int = MAX_TREE_ENTRIES,
    excluded: tuple[Path, ...] = (),
) -> TreeManifest:
    """Content manifest for whole trees.

    ``excluded`` is honoured **here, in the walker**, not only when deriving the
    root list. Pruning at derivation alone is a self-certifying half-measure: the
    fastembed cache sits *inside* ``~/.memtomem``, which is itself a protected
    root, so a derivation-level check passes while the walk descends into the
    cache anyway and every model download becomes a violation.
    """
    entries: dict[str, str] = {}
    uncovered: list[str] = []
    count = 0

    def _walk_error(err: OSError) -> None:
        # Default os.walk behaviour is to swallow the error and yield nothing,
        # so an unreadable directory is indistinguishable from an empty one —
        # a change inside it was reproduced as an identical manifest.
        raise HomeGuardError(
            f"home-guard: cannot traverse {getattr(err, 'filename', '?')} ({err}). "
            "Refusing to arm rather than report an unreadable subtree as empty."
        )

    def _excluded(path: Path) -> bool:
        return any(path == skip or path.is_relative_to(skip) for skip in excluded)

    def _bump(path: Path) -> None:
        nonlocal count
        count += 1
        if count > max_entries:
            raise HomeGuardError(
                f"home-guard: more than {max_entries} entries under the protected "
                f"roots (at {path}). Refusing to arm rather than guard a fraction "
                "of a tree — a partially watched root looks identical to a clean one."
            )

    def _record(path: Path) -> None:
        value = fingerprint(path)
        if value is None:
            return
        _bump(path)
        entries[str(path)] = value

    for root in roots:
        if _excluded(root):
            continue
        if not root.exists() and not root.is_symlink():
            continue  # genuinely absent — a DANGLING link still fingerprints
        if root.is_symlink() or root.is_file():
            _record(root)
            continue
        # The root ITSELF is an entry. Without this, a protected root that did
        # not exist and is then created as an empty directory produces identical
        # manifests — the guard says nothing about a directory appearing where
        # it was watching for exactly that.
        _bump(root)
        entries[str(root)] = "dir"
        for current, dirnames, filenames in os.walk(root, onerror=_walk_error):
            kept: list[str] = []
            for name in sorted(dirnames):
                path = Path(current) / name
                if _excluded(path):
                    continue
                if path.is_symlink() or getattr(path, "is_junction", bool)():
                    # ``followlinks=False`` stops the WALK, but the entry is
                    # still listed here — recording it as a plain "dir" made
                    # retargeting the link invisible. Fingerprint the link text,
                    # and report the subtree as uncovered: we detect a retarget,
                    # but writes THROUGH an unchanged link are not visible and
                    # the target may be anywhere. Windows junctions are not
                    # symlinks and ``os.walk`` would descend them.
                    _record(path)
                    uncovered.append(str(path))
                    continue
                kept.append(name)
                _bump(path)
                entries[str(path)] = "dir"
            dirnames[:] = kept
            for name in sorted(filenames):
                path = Path(current) / name
                if _excluded(path):
                    continue
                _record(path)
    metadata_only = tuple(sorted(k for k, v in entries.items() if v.startswith("meta:")))
    return TreeManifest(
        entries=entries, metadata_only=metadata_only, uncovered=tuple(sorted(uncovered))
    )


def diff_trees(before: TreeManifest, after: TreeManifest) -> list[Violation]:
    violations: list[Violation] = []
    for key in sorted(set(before.entries) - set(after.entries)):
        violations.append(Violation(key, "deleted", "removed from a protected tree"))
    for key in sorted(set(after.entries) - set(before.entries)):
        violations.append(Violation(key, "created", "new entry in a protected tree"))
    for key in sorted(set(before.entries) & set(after.entries)):
        if before.entries[key] == after.entries[key]:
            continue
        note = ""
        if key in before.metadata_only or key in after.metadata_only:
            note = " (size/mtime only — file exceeds the digest cap, so content is not covered)"
        violations.append(Violation(key, "modified", f"content changed{note}"))
    return violations
