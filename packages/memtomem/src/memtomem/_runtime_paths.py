"""Runtime-state path resolution.

All cooperating memtomem processes rendezvous through one per-user path
that does not depend on the calling process's environment (#2037):

* POSIX: ``/tmp/memtomem-{euid}`` — literal ``/tmp``; ``XDG_RUNTIME_DIR``
  and the ``TMP*`` variables never affect writer identity.
* Windows: ``<FOLDERID_LocalAppData>\\Temp\\memtomem-0`` — resolved through
  the shell Known Folder API rather than ``TEMP``/``TMP``/``USERPROFILE``.

This stable anchor holds server/Web pid files, instance-registry sentinels,
the registry mutation sidecar, and the lifecycle barrier. It remains outside
the persistent ``~/.memtomem/`` store, preserving lazy MCP handshakes (#412).

For a transition period :func:`candidate_runtime_dirs` also returns the old
environment-derived locations. Those are read/fence candidates only; new
writers always use :func:`runtime_dir`.

Security posture for the runtime directory itself:

- Never follow a directory link — an attacker on a shared ``/tmp``
  could pre-create ``memtomem-{uid}`` as a link into the user's home,
  and a naive ``mkdir`` would silently no-op through it. Worse, the
  uninstall path stages and deletes what it finds under this directory,
  so a redirect here costs the user files outside it. ``os.stat`` with
  ``follow_symlinks=False`` catches symlinks, on every platform (Windows
  symlinks need Developer Mode/admin to create but are exploitable the
  same way once present). Junctions need their own check: they redirect
  identically while keeping ``S_IFDIR``, so ``S_ISLNK`` never sees one.
  Only this directory is judged — an ancestor may be a junction without
  being refused.
- POSIX only — refuse any pre-existing directory not owned by the
  effective uid or not at mode ``0o700``. This trades convenience for a
  predictable contract: a ``root``-owned leftover from ``sudo mm …`` or
  a mode-degraded dir raises :class:`PermissionError` with a remediation
  hint instead of silently writing the pid file into a world-readable
  directory. The only fix is ``rm -rf`` the runtime dir and retry.
- Windows — skip the mode-bit + owner-uid gates. NTFS synthesizes POSIX
  mode bits as ``0o666``/``0o777`` (the values do not reflect the real
  ACL), so ``& 0o077`` would always trip and ``ensure_runtime_dir`` would
  refuse the second invocation against its own previously-created dir.
  Proper NTFS owner-SID validation needs ``GetSecurityInfo`` and is out of
  scope; we rely on the OS-resolved LocalAppData known folder being per-user.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Literal, TypeVar


_TEST_RUNTIME_DIR_ENV = "_MEMTOMEM_TEST_RUNTIME_DIR"


def _test_runtime_dir_override() -> Path | None:
    """Return the pytest-only coordination root inherited by subprocesses.

    The full suite starts real server/Web subprocesses; without an inherited
    seam they would touch the developer's live per-user locks.  Requiring
    pytest's own marker as well as the private variable prevents this from
    becoming a supported production override that could recreate #2037.
    """
    raw = os.environ.get(_TEST_RUNTIME_DIR_ENV)
    if not raw or "PYTEST_CURRENT_TEST" not in os.environ:
        return None
    return Path(raw)


def _hint_quote(target: Path | str) -> str:
    """Quote *target* for embedding in a suggested removal command.

    The remediation hints below splice ``target`` into a copy-pasteable
    ``rm``/``rmdir`` invocation. ``target`` derives from the environment
    (``$XDG_RUNTIME_DIR``/``$TMPDIR``); a path with whitespace or shell
    metacharacters would otherwise yield a command that deletes the wrong
    path when pasted (``rm -rf /tmp/my dir/memtomem-501`` splits into two
    args). Prose renderings of the same path (the ``runtime dir {target}``
    prefix) go through :func:`scrub_text` instead — display-safe, not a shell
    token.

    A printable path is :func:`shlex.quote`-d. A path carrying any
    non-printable character (terminal control/escape sequences) renders in
    ANSI-C form (``$'...'``) with each non-printable expanded to ``\\xNN``
    escapes of its **filesystem bytes** (``os.fsencode``) — ``$'\\xNN'``
    produces raw bytes in both bash and zsh, so escaping code points would
    name a different path for anything multi-byte, and bash 3.2 (macOS) has
    no ``\\uNNNN`` at all. Display-safe *and* byte-for-byte resolving to
    the real path when pasted.

    :func:`shlex.quote` is POSIX-shell quoting. On PowerShell its
    single-quote form is also the literal (no ``$``/backtick expansion), so
    the hint stays paste-safe there too (the ANSI-C form is bash/zsh-only,
    but only ever appears for paths that are already hostile on any shell).
    ``cmd.exe`` is out of scope: it expands ``%VAR%`` regardless of any
    quoting and has no ``rm``, so these Unix-shaped hints are illustrative
    rather than literally executable there — a Windows-native rewrite is a
    separate change (see #1956).

    Quoting alone is not enough for a leading-hyphen path:
    ``shlex.quote("-rf/memtomem")`` returns it unchanged, and ``rm`` would
    then read it as options, not an operand. The call sites pair this with
    a ``--`` end-of-options marker so the quoted path is always treated as
    a filename (a relative ``$XDG_RUNTIME_DIR``/``$TMPDIR`` can begin with
    ``-``; an absolute one never does).
    """
    s = str(target)
    if all(ch.isprintable() for ch in s):
        return shlex.quote(s)
    out: list[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "'":
            out.append("\\'")
        elif ch.isprintable():
            out.append(ch)
        else:
            out.extend(f"\\x{b:02x}" for b in os.fsencode(ch))
    return "$'" + "".join(out) + "'"


def scrub_text(text: str) -> str:
    """Make environment-derived *text* safe to print as prose.

    Non-printable characters (terminal control/escape sequences smuggled
    into ``$XDG_RUNTIME_DIR`` or a workspace path) are replaced with
    ``\\xNN`` escapes of their **filesystem bytes** (``os.fsencode``), so
    nothing in the rendered message can move the cursor or retitle the
    terminal — even the prose ahead of the safely quoted command. Printable
    text — including non-ASCII — passes through untouched. Display-only:
    for a copy-paste *command*, use :func:`_hint_quote` instead.
    """
    if all(ch.isprintable() for ch in text):
        return text
    out: list[str] = []
    for ch in text:
        if ch.isprintable():
            out.append(ch)
        else:
            out.extend(f"\\x{b:02x}" for b in os.fsencode(ch))
    return "".join(out)


RuntimeDirReason = Literal[
    "cannot_stat",
    "symlink",
    "junction",
    "not_directory",
    "wrong_owner",
    "unsafe_permissions",
]

_T = TypeVar("_T")


def _required_validation_field(
    value: _T | None,
    *,
    reason: RuntimeDirReason,
    field: str,
) -> _T:
    """Return a reason-specific field or reject an invalid error instance."""
    if value is None:
        raise ValueError(f"runtime-dir reason {reason!r} requires {field}")
    return value


class RuntimeDirValidationError(PermissionError):
    """Structured runtime-dir refusal with long and concise renderings."""

    def __init__(
        self,
        target: Path,
        reason: RuntimeDirReason,
        *,
        cause: OSError | None = None,
        actual_uid: int | None = None,
        expected_uid: int | None = None,
        mode: int | None = None,
        unsafe_bits: int | None = None,
    ) -> None:
        self.target = target
        self.reason = reason
        self.cause = cause
        self.actual_uid = actual_uid
        self.expected_uid = expected_uid
        self.mode = mode
        self.unsafe_bits = unsafe_bits
        super().__init__(self._long_message())

    def _long_message(self) -> str:
        target = scrub_text(str(self.target))
        command_target = _hint_quote(self.target)
        if self.reason == "cannot_stat":
            cause = _required_validation_field(
                self.cause,
                reason=self.reason,
                field="cause",
            )
            return (
                f"runtime dir {target}: cannot stat ({scrub_text(str(cause))}). "
                "Remove it and retry."
            )
        if self.reason == "symlink":
            return (
                f"runtime dir {target} is a symlink; refusing to follow. "
                f"Remove it: rm -f -- {command_target}"
            )
        if self.reason == "junction":
            return (
                f"runtime dir {target} is a junction; refusing to follow. "
                f"Remove it: rmdir -- {command_target}"
            )
        if self.reason == "not_directory":
            return (
                f"runtime dir {target} exists but is not a directory. "
                f"Remove it: rm -f -- {command_target}"
            )
        if self.reason == "wrong_owner":
            actual_uid = _required_validation_field(
                self.actual_uid,
                reason=self.reason,
                field="actual_uid",
            )
            expected_uid = _required_validation_field(
                self.expected_uid,
                reason=self.reason,
                field="expected_uid",
            )
            return (
                f"runtime dir {target} is owned by uid {actual_uid} "
                f"(expected {expected_uid}). "
                "Ask an administrator to remove it, then retry:\n"
                f"rm -rf -- {command_target}"
            )
        mode = _required_validation_field(
            self.mode,
            reason=self.reason,
            field="mode",
        )
        unsafe_bits = _required_validation_field(
            self.unsafe_bits,
            reason=self.reason,
            field="unsafe_bits",
        )
        return (
            f"runtime dir {target} has unsafe permissions 0o{mode:o} "
            f"(expected 0o700, group/world bits: 0o{unsafe_bits:o}). "
            f"Remove it and retry: rm -rf -- {command_target}"
        )

    def short_reason(self) -> str:
        """Return stable non-remediation text for a skipped candidate."""
        if self.reason == "cannot_stat":
            cause = _required_validation_field(
                self.cause,
                reason=self.reason,
                field="cause",
            )
            return f"cannot stat ({type(cause).__name__})"
        if self.reason == "symlink":
            return "symlink"
        if self.reason == "junction":
            return "junction"
        if self.reason == "not_directory":
            return "not a directory"
        if self.reason == "wrong_owner":
            actual_uid = _required_validation_field(
                self.actual_uid,
                reason=self.reason,
                field="actual_uid",
            )
            return f"owned by uid {actual_uid}"
        mode = _required_validation_field(
            self.mode,
            reason=self.reason,
            field="mode",
        )
        return f"unsafe permissions 0o{mode:o}"


def _is_safe_dir(path: Path) -> bool:
    """POSIX-only safety gate: ``path`` must be a regular directory,
    owner-match the effective uid, and have no group/world permission
    bits. Callers must guard with ``os.name != "nt"`` — NTFS synthesizes
    POSIX mode bits and the owner check has no Windows equivalent here.

    Used by the ``$XDG_RUNTIME_DIR`` gate (where we silently fall
    through to the tempdir form on failure). ``lstat``-style semantics
    — we reject a symlink outright, never its target.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if stat.S_IMODE(st.st_mode) & 0o077:
        return False
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        return False
    return True


def _windows_local_app_data() -> Path:
    """Resolve ``FOLDERID_LocalAppData`` without consulting environment vars.

    ``Path.home`` and ``tempfile.gettempdir`` both inherit caller-local
    environment, which is exactly the split #2037 removes.  The shell Known
    Folder API resolves the current token's LocalAppData location instead.
    Keep imports local so non-Windows interpreters never need Win32 ctypes
    symbols merely to import this module.
    """
    if os.name != "nt":
        raise OSError("FOLDERID_LocalAppData is only available on Windows")

    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    folder_id = _GUID(
        0xF1B32785,
        0x6FBA,
        0x4FCF,
        (ctypes.c_ubyte * 8)(0x9D, 0x55, 0x7B, 0x8E, 0x7F, 0x15, 0x70, 0x91),
    )
    raw_path = ctypes.c_void_p()
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    sh_get_known_folder_path = shell32.SHGetKnownFolderPath
    sh_get_known_folder_path.argtypes = [
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    sh_get_known_folder_path.restype = ctypes.c_long
    result = sh_get_known_folder_path(ctypes.byref(folder_id), 0, None, ctypes.byref(raw_path))
    if result != 0 or not raw_path.value:
        code = result & 0xFFFFFFFF
        raise OSError(f"SHGetKnownFolderPath(FOLDERID_LocalAppData) failed: 0x{code:08x}")
    try:
        return Path(ctypes.wstring_at(raw_path.value))
    finally:
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        ole32.CoTaskMemFree(raw_path)


def _legacy_environment_runtime_dir() -> Path:
    """Resolve the pre-#2037 writer location for compatibility probes only."""
    if os.name != "nt":
        xdg = os.environ.get("XDG_RUNTIME_DIR", "")
        if xdg and _is_safe_dir(Path(xdg)):
            return Path(xdg) / "memtomem"

    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    return Path(tempfile.gettempdir()) / f"memtomem-{uid}"


def runtime_dir() -> Path:
    """Return the canonical coordination directory without creating it."""
    test_override = _test_runtime_dir_override()
    if test_override is not None:
        return test_override
    if os.name == "nt":
        return _windows_local_app_data() / "Temp" / "memtomem-0"
    return Path("/tmp") / f"memtomem-{os.geteuid()}"


def candidate_runtime_dirs() -> list[Path]:
    """Return canonical and historical runtime dirs for transition probes.

    The canonical directory is always first. Historical candidates retain
    the pre-#2037 caller-derived XDG/temp path plus the deterministic systemd
    path that #2036 already probed. Writers must use :func:`runtime_dir` only.
    """
    test_override = _test_runtime_dir_override()
    if test_override is not None:
        return [test_override]

    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    dirs = [
        runtime_dir(),
        _legacy_environment_runtime_dir(),
        Path(tempfile.gettempdir()) / f"memtomem-{uid}",
    ]
    if os.name != "nt":
        systemd_base = Path(f"/run/user/{uid}")
        if _is_safe_dir(systemd_base):
            dirs.append(systemd_base / "memtomem")
    seen: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def validate_runtime_dir(target: Path) -> bool:
    """Validate one runtime directory without creating it.

    Returns ``False`` only when *target* is absent. Every existing path must
    satisfy the same redirect, ownership, and permission policy as
    :func:`ensure_runtime_dir`; otherwise ``PermissionError`` is raised with
    the existing remediation text. Read-only liveness probes use this helper
    for candidate directories that may belong to another process context.
    """
    try:
        st = os.stat(target, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeDirValidationError(target, "cannot_stat", cause=exc) from exc

    if stat.S_ISLNK(st.st_mode):
        raise RuntimeDirValidationError(target, "symlink")
    # Windows junctions redirect exactly like a symlink but keep
    # ``S_IFDIR``, so ``S_ISLNK`` above never sees them. Without this
    # every consumer treats the *target* as the runtime dir — and the
    # uninstall path stages and deletes what it finds there.
    if target.is_junction():
        raise RuntimeDirValidationError(target, "junction")
    if not stat.S_ISDIR(st.st_mode):
        raise RuntimeDirValidationError(target, "not_directory")
    if os.name != "nt":
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise RuntimeDirValidationError(
                target,
                "wrong_owner",
                actual_uid=st.st_uid,
                expected_uid=os.geteuid(),
            )
        unsafe = stat.S_IMODE(st.st_mode) & 0o077
        if unsafe:
            raise RuntimeDirValidationError(
                target,
                "unsafe_permissions",
                mode=stat.S_IMODE(st.st_mode),
                unsafe_bits=unsafe,
            )
    return True


def ensure_runtime_dir() -> Path:
    """Return the runtime directory, creating it with ``mode=0o700`` if missing.

    A pre-existing directory is validated: symlink, junction, wrong
    owner, or any group/world bit set raises :class:`PermissionError`
    with a removal hint. We never ``chmod`` an existing directory
    (silent permission changes would hide the underlying misconfiguration
    — and bypass any audit a sysadmin might run against the parent).

    On creation we ``chmod`` explicitly to ``0o700`` as a belt-and-suspenders
    fix for exotic ``umask`` values (e.g. ``umask 0o177`` would clear the
    owner-execute bit supplied to ``mkdir``, leaving an unusable directory
    on silent success).
    """
    return ensure_runtime_dir_at(runtime_dir())


def ensure_runtime_dir_at(target: Path) -> Path:
    """Validate/create an explicit canonical or historical runtime leaf.

    Destructive commands use this only while fencing derivable pre-#2037
    locations. Keeping the target lexical across the retry avoids resolving a
    different environment-derived directory after a concurrent mkdir.
    """
    if validate_runtime_dir(target):
        return target

    # Missing — create with 0o700, then chmod explicitly to neutralize any
    # umask that would have masked the mode bits. Both calls are racy if
    # another process interposes, but the worst case is a
    # FileExistsError → re-validate via recursion.
    try:
        target.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        return ensure_runtime_dir_at(target)
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def store_pid_digest(db_path: Path | str) -> str | None:
    """Derive the per-store pid-file digest from a SQLite path (#1990).

    This is a **pre-open coordination key**, not authoritative store
    identity: it hashes the normalized path *text* (expanduser + non-strict
    ``resolve()`` + ``normcase``, force-lowered on macOS like
    ``context/projects.py:_normalize_for_scope_id``) so it exists before
    the DB file does — which is exactly when a fresh server must name its
    pid file. That is why it deliberately differs from
    ``_instance_registry.store_digest_for`` (inode identity, undefined
    pre-creation). Known limits, all fail-safe: hard links to one DB get
    distinct digests (missed contention warning; destructive gates are
    still covered by the DB-lock probe and the instance registry), and
    case folding on a case-sensitive macOS volume can collapse distinct
    files (a spurious same-store warning, never a skipped refusal).

    Returns ``None`` when no per-store name can be derived — non-file
    targets (``:memory:``, ``file:`` URIs) or a normalization failure
    (symlink loop, unreadable ancestor). Callers must degrade to the
    store-agnostic behavior: the server falls back to the transitional
    bare ``server.pid``; liveness probes fall back to scanning every
    ``server-*.pid`` (fail-closed).
    """
    raw = str(db_path)
    if raw == ":memory:" or raw.startswith("file:"):
        return None
    try:
        s = os.path.normcase(str(Path(raw).expanduser().resolve()))
    except (OSError, RuntimeError, ValueError):
        return None
    if sys.platform == "darwin":
        s = s.lower()
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]


def server_pid_path(db_path: Path | str | None = None) -> Path:
    """Return the path to ``memtomem-server``'s pid / flock file.

    With *db_path*, the name is scoped to that store —
    ``server-<digest16>.pid`` via :func:`store_pid_digest` — so servers
    on different stores no longer contend for one per-user lock (#1990).
    Without it (or when no digest can be derived), the transitional bare
    ``server.pid`` is returned: the name servers started by older
    versions still hold, which liveness probes keep checking fail-closed.

    Does not create the parent directory — callers that intend to open the
    path for write should go through :func:`ensure_runtime_dir` first.
    """
    if db_path is not None:
        digest = store_pid_digest(db_path)
        if digest is not None:
            return runtime_dir() / f"server-{digest}.pid"
    return runtime_dir() / "server.pid"


def web_pid_path() -> Path:
    """Return the path to ``mm web``'s pid / flock file.

    Does not create the parent directory — callers that intend to open the
    path for write should go through :func:`ensure_runtime_dir` first.
    """
    return runtime_dir() / "web.pid"


_LEGACY_PID_NAME = ".server.pid"


def legacy_server_pid_path() -> Path:
    """Return the pre-relocation pid file path (``~/.memtomem/.server.pid``).

    No longer created or locked by servers as of #2003 (the transition-era
    B1 interlock is retired); retained for CLI liveness probes — which gate
    only on an exclusive pre-0.1.25 holder — and for ``mm uninstall``'s
    legacy-file inventory.

    ``Path.home()`` is evaluated every call so tests that monkeypatch
    ``HOME`` get the isolated path — import-time binding would capture
    the developer's real home and leak across the fixture.
    """
    return Path.home() / ".memtomem" / _LEGACY_PID_NAME
