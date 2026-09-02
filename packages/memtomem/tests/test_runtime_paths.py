"""Tests for the stable runtime-coordination anchor (#412, #2037)."""

from __future__ import annotations

import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path

import pytest

import memtomem._runtime_paths as runtime_paths
from memtomem._runtime_paths import (
    RuntimeDirValidationError,
    _hint_quote,
    _legacy_environment_runtime_dir,
    candidate_runtime_dirs,
    ensure_runtime_dir,
    ensure_runtime_dir_at,
    legacy_server_pid_path,
    runtime_dir,
    scrub_text,
    server_pid_path,
    store_pid_digest,
    validate_runtime_dir,
)
from .helpers import set_home


def _make_safe_xdg(tmp_path: Path) -> Path:
    """Create an ``$XDG_RUNTIME_DIR``-shaped base under ``tmp_path``.

    ``Path.mkdir`` applies umask to the mode, and system umask may strip
    owner-only bits we wanted. ``chmod`` after the fact neutralizes that
    so the happy-path tests don't depend on the developer's umask.
    """
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    return xdg


@pytest.mark.skipif(
    os.name == "nt",
    reason="XDG / POSIX mode-bit semantics; Windows coverage lives in TestWindowsRuntimeDir",
)
class TestRuntimeDir:
    def test_anchor_is_invariant_to_xdg_and_tmp_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        baseline = runtime_dir()
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        monkeypatch.setenv("TMPDIR", str(tmp_path / "other-tmp"))
        tempfile.tempdir = None
        try:
            assert runtime_dir() == baseline
        finally:
            tempfile.tempdir = None

    def test_resolver_does_not_create_directory(self):
        result = runtime_dir()
        assert not result.exists(), "runtime_dir() must not mkdir"

    def test_legacy_candidate_retains_safe_nonstandard_xdg(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")

        assert _legacy_environment_runtime_dir() == xdg / "memtomem"
        assert runtime_dir() == Path("/tmp") / f"memtomem-{os.geteuid()}"
        assert candidate_runtime_dirs()[0] == runtime_dir()
        assert xdg / "memtomem" in candidate_runtime_dirs()


@pytest.mark.skipif(
    os.name == "nt",
    reason="0o700 mode bits + umask + chmod don't translate to NTFS; "
    "Windows coverage lives in TestWindowsRuntimeDir",
)
class TestEnsureRuntimeDir:
    def test_creates_directory_with_owner_only_mode(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        d = ensure_runtime_dir()

        assert d.exists() and d.is_dir()
        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_explicit_chmod_survives_wild_umask(self, tmp_path, monkeypatch):
        """``mkdir(mode=0o700)`` is still subject to umask masking. A
        pathological ``umask 0o177`` would clear the owner-exec bit and
        silently produce an unusable 0o600 dir. The belt-and-suspenders
        explicit ``chmod`` neutralizes that."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        old_umask = os.umask(0o177)
        try:
            d = ensure_runtime_dir()
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_failed_chmod_leaves_a_leftover_that_is_refused_not_adopted(
        self, tmp_path, monkeypatch
    ):
        """A half-secured leftover must be refused, never adopted.

        Under ``umask 0o177`` ``mkdir(mode=0o700)`` actually produces 0o600.
        If the ``fchmod`` that repairs it then fails, that 0o600 directory is
        what the *next* call finds. It used to be adopted as pre-existing —
        the group/world check passes — and then failed one lock and marker at
        a time with ``PermissionError`` because owner-exec is missing.
        """
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        def boom(*_args, **_kwargs):
            raise PermissionError(1, "Operation not permitted")

        # Restored by hand rather than with ``monkeypatch.undo()``: undo drops
        # *every* patch on this fixture, including the autouse runtime-dir
        # isolation, and the retry below would then run against the real
        # ``/tmp/memtomem-<uid>``.
        real_fchmod = os.fchmod
        os.fchmod = boom  # type: ignore[assignment]
        old_umask = os.umask(0o177)
        try:
            with pytest.raises(PermissionError):
                ensure_runtime_dir()
            target = runtime_paths.runtime_dir()
            # Left in place on purpose — removal here could only be path-based
            # and would race a directory that took the name meanwhile. What
            # matters is that the leftover is refused, not silently adopted.
            assert target.exists()
            assert stat.S_IMODE(target.stat().st_mode) == 0o600
            with pytest.raises(PermissionError, match="owner read/write/execute"):
                ensure_runtime_dir()

            os.fchmod = real_fchmod  # type: ignore[assignment]
            target.rmdir()
            d = ensure_runtime_dir()
        finally:
            os.fchmod = real_fchmod  # type: ignore[assignment]
            os.umask(old_umask)

        assert stat.S_IMODE(d.stat().st_mode) == 0o700

    def test_refuses_existing_dir_missing_owner_access(self, tmp_path, monkeypatch):
        """Owner rwx is not implied by the absence of group/world bits: 0o600
        passes the ``& 0o077`` test while being unusable."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        runtime = runtime_paths.runtime_dir()
        runtime.mkdir(parents=True)
        os.chmod(runtime, 0o600)
        try:
            with pytest.raises(PermissionError) as excinfo:
                ensure_runtime_dir()
        finally:
            os.chmod(runtime, 0o700)

        assert "owner read/write/execute" in str(excinfo.value)

    def test_idempotent_does_not_fail_on_existing_dir(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        ensure_runtime_dir()
        # Second call must not raise FileExistsError, must re-validate,
        # must return the same path.
        ensure_runtime_dir()

    @pytest.mark.requires_symlinks
    def test_refuses_existing_symlink(self, tmp_path, monkeypatch):
        """Symlink-at-the-runtime-path attack: attacker symlinks
        ``$XDG_RUNTIME_DIR/memtomem`` into the user's home. Pre-M1 fix,
        ``mkdir(exist_ok=True)`` followed the link silently and
        ``open(server_pid_path(), "w")`` wrote into the target. Now the
        validator raises before we touch the file."""
        runtime = runtime_dir()
        target = tmp_path / "real-target"
        target.mkdir(mode=0o700)
        os.symlink(target, runtime)

        with pytest.raises(RuntimeDirValidationError, match="symlink") as exc_info:
            ensure_runtime_dir()
        assert exc_info.value.reason == "symlink"
        assert exc_info.value.short_reason() == "symlink"

    def test_refuses_existing_loose_mode(self, tmp_path, monkeypatch):
        """Regression for M3 in the #412 review: a pre-existing dir at
        mode 0o755 used to be silently accepted, leaking the pid file
        into a group/world-readable location. The contract now enforces
        0o700 and surfaces remediation ``rm -rf`` in the error."""
        runtime = runtime_dir()
        runtime.mkdir(mode=0o755)
        os.chmod(runtime, 0o755)  # neutralize umask

        with pytest.raises(RuntimeDirValidationError, match="unsafe permissions") as exc_info:
            ensure_runtime_dir()
        assert exc_info.value.reason == "unsafe_permissions"
        assert exc_info.value.short_reason() == "unsafe permissions 0o755"

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="owner check is POSIX-only")
    def test_refuses_existing_wrong_owner(self, tmp_path, monkeypatch):
        """Stub ``geteuid`` to simulate a ``root``-owned leftover from a
        prior ``sudo mm …`` run. The validator must raise with a
        clean-up hint rather than proceed against a dir we don't own.

        We route through the ``TMPDIR`` fallback rather than XDG because
        ``runtime_dir()`` also consults ``geteuid()`` for the XDG safety
        gate — a uid-stub applied before resolution would flip the whole
        path to fallback before ``ensure_runtime_dir`` ever stat'd the
        memtomem subdir, missing the existing-dir branch we want to
        cover. Pre-creating the fallback subdir at the stubbed uid's
        expected name lets us exercise that branch end-to-end.
        """
        target = runtime_dir()
        real_uid = os.geteuid()
        stubbed_uid = real_uid + 1
        target.mkdir(mode=0o700)
        monkeypatch.setattr(os, "geteuid", lambda: stubbed_uid)

        with pytest.raises(RuntimeDirValidationError, match="owned by uid") as exc_info:
            ensure_runtime_dir()
        message = str(exc_info.value)
        command = f"rm -rf -- {_hint_quote(target)}"
        assert "Ask an administrator" in message
        assert "XDG_RUNTIME_DIR or TMPDIR" not in message
        assert message.endswith(command)
        assert exc_info.value.reason == "wrong_owner"
        assert exc_info.value.short_reason() == f"owned by uid {real_uid}"

    def test_refuses_non_directory(self, tmp_path, monkeypatch):
        """A regular file where we expected a directory — unlikely but
        the validator should refuse rather than try to ``mkdir`` over
        it (which would ``FileExistsError`` anyway, just less clearly)."""
        runtime_dir().write_text("accidentally a file")

        with pytest.raises(RuntimeDirValidationError, match="not a directory") as exc_info:
            ensure_runtime_dir()
        assert exc_info.value.reason == "not_directory"
        assert exc_info.value.short_reason() == "not a directory"

    def test_cannot_stat_reason_is_structured_and_scrubbed(self, tmp_path, monkeypatch):
        target = tmp_path / "blocked-runtime"
        real_stat = os.stat

        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError("denied\x1b")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", denied)

        with pytest.raises(RuntimeDirValidationError, match="cannot stat") as exc_info:
            validate_runtime_dir(target)

        assert exc_info.value.reason == "cannot_stat"
        assert exc_info.value.short_reason() == "cannot stat (PermissionError)"
        assert "\x1b" not in str(exc_info.value)
        assert "\\x1b" in str(exc_info.value)

    def test_new_runtime_dir_chmod_failure_is_not_hidden(self, tmp_path, monkeypatch):
        target = tmp_path / "new-runtime"

        def denied(*args, **kwargs):
            raise PermissionError("chmod denied")

        if os.name == "nt":
            monkeypatch.setattr(os, "chmod", denied)
        else:
            monkeypatch.setattr(os, "fchmod", denied)

        with pytest.raises(PermissionError, match="chmod denied"):
            ensure_runtime_dir_at(target)

    @pytest.mark.skipif(os.name == "nt", reason="POSIX inode identity check")
    def test_new_runtime_dir_rejects_path_swap_after_open(self, tmp_path, monkeypatch):
        target = tmp_path / "new-runtime"
        real_stat = os.stat

        def swapped(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if path == target and kwargs.get("follow_symlinks") is False:
                values = list(result)
                values[1] = result.st_ino + 1
                return os.stat_result(values)
            return result

        monkeypatch.setattr(os, "stat", swapped)
        with pytest.raises(RuntimeDirValidationError, match="cannot stat"):
            ensure_runtime_dir_at(target)


def _make_spacey_xdg(tmp_path: Path) -> Path:
    """Like :func:`_make_safe_xdg` but the base name contains a space, so
    the resolved ``target`` (``<base>/memtomem``) has whitespace — the
    case that makes an unquoted ``rm``/``rmdir`` hint dangerous to paste
    (#1956)."""
    xdg = tmp_path / "x dg"
    xdg.mkdir()
    os.chmod(xdg, 0o700)
    return xdg


@pytest.mark.skipif(
    os.name == "nt",
    reason="drives the refusal branches through POSIX chmod/geteuid fixtures; "
    "the quoting helper itself is pinned platform-agnostically in TestHintQuote",
)
class TestRemovalHintQuoting:
    """#1956 — ``ensure_runtime_dir`` splices ``target`` into a
    copy-pasteable ``rm``/``rmdir`` command. When the runtime path carries
    whitespace or a shell metacharacter (it derives from
    ``$XDG_RUNTIME_DIR``/``$TMPDIR``), an unquoted hint would delete the
    wrong path on paste. Each hint site is pinned separately — sibling
    guards don't vouch for each other, and the quote could regress at one
    call site while the others stay correct.
    """

    @pytest.mark.requires_symlinks
    def test_symlink_hint_quotes_spacey_path(self, tmp_path, monkeypatch):
        xdg = _make_spacey_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        target = tmp_path / "real-target"
        target.mkdir(mode=0o700)
        os.symlink(target, xdg / "memtomem")

        with pytest.raises(PermissionError) as exc:
            ensure_runtime_dir_at(xdg / "memtomem")
        expected = f"rm -f -- {shlex.quote(str(xdg / 'memtomem'))}"
        assert expected in str(exc.value)

    def test_non_directory_hint_quotes_spacey_path(self, tmp_path, monkeypatch):
        xdg = _make_spacey_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        (xdg / "memtomem").write_text("accidentally a file")

        with pytest.raises(PermissionError) as exc:
            ensure_runtime_dir_at(xdg / "memtomem")
        expected = f"rm -f -- {shlex.quote(str(xdg / 'memtomem'))}"
        assert expected in str(exc.value)

    def test_loose_mode_hint_quotes_spacey_path(self, tmp_path, monkeypatch):
        xdg = _make_spacey_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        (xdg / "memtomem").mkdir(mode=0o755)
        os.chmod(xdg / "memtomem", 0o755)  # neutralize umask

        with pytest.raises(PermissionError) as exc:
            ensure_runtime_dir_at(xdg / "memtomem")
        expected = f"rm -rf -- {shlex.quote(str(xdg / 'memtomem'))}"
        assert expected in str(exc.value)

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="owner check is POSIX-only")
    def test_wrong_owner_hint_quotes_spacey_path(self, tmp_path, monkeypatch):
        """Owner-mismatch branch, routed through the ``TMPDIR`` fallback so
        the uid stub doesn't flip resolution before the existing-dir stat
        (same reasoning as ``test_refuses_existing_wrong_owner``). The temp
        base carries a space so the ``rm -rf`` hint must quote it."""
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        tmp_tmp = tmp_path / "t mp"
        tmp_tmp.mkdir()
        os.chmod(tmp_tmp, 0o700)
        monkeypatch.setenv("TMPDIR", str(tmp_tmp))
        tempfile.tempdir = None

        real_uid = os.geteuid()
        stubbed_uid = real_uid + 1
        target = tmp_tmp / f"memtomem-{stubbed_uid}"
        target.mkdir(mode=0o700)
        monkeypatch.setattr(os, "geteuid", lambda: stubbed_uid)

        try:
            with pytest.raises(PermissionError) as exc:
                ensure_runtime_dir_at(target)
            expected = f"rm -rf -- {shlex.quote(str(target))}"
            assert expected in str(exc.value)
        finally:
            tempfile.tempdir = None

    def test_junction_hint_quotes_spacey_path(self, tmp_path, monkeypatch):
        """POSIX filesystems can't create a real junction, so stub
        ``Path.is_junction`` to reach the junction branch. A normal dir
        passes the symlink check and lands there; the ``rmdir`` hint must
        quote the whitespace path."""
        xdg = _make_spacey_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        (xdg / "memtomem").mkdir(mode=0o700)
        monkeypatch.setattr(Path, "is_junction", lambda self: True)

        with pytest.raises(RuntimeDirValidationError) as exc:
            ensure_runtime_dir_at(xdg / "memtomem")
        expected = f"rmdir -- {shlex.quote(str(xdg / 'memtomem'))}"
        assert expected in str(exc.value)
        assert exc.value.reason == "junction"
        assert exc.value.short_reason() == "junction"

    def test_control_char_path_renders_escaped_everywhere(self, tmp_path, monkeypatch):
        """A path with an embedded control character (ESC) must render
        escaped in the *entire* message — ANSI-C ``$'...'`` form in the
        command portion (byte-exact on paste), ``scrub_text``-ed in the
        ``runtime dir`` prose prefix. A single raw byte anywhere would
        misrender the terminal before the safe command is even reached."""
        xdg = tmp_path / "x\x1bdg"
        xdg.mkdir()
        os.chmod(xdg, 0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        (xdg / "memtomem").mkdir(mode=0o755)
        os.chmod(xdg / "memtomem", 0o755)

        with pytest.raises(PermissionError) as exc:
            ensure_runtime_dir_at(xdg / "memtomem")
        msg = str(exc.value)
        assert "rm -rf -- $'" in msg and "\\x1b" in msg
        # No raw control byte may survive anywhere — prose included.
        assert not any(not ch.isprintable() for ch in msg)

    def test_leading_hyphen_path_tokenizes_as_operand(self, tmp_path, monkeypatch):
        """A relative ``$XDG_RUNTIME_DIR`` beginning with ``-`` resolves to a
        ``-rf/memtomem``-shaped target. ``shlex.quote`` leaves it unchanged
        (no metacharacters), so without the ``--`` end-of-options marker the
        pasted ``rm -rf -rf/memtomem`` would read the path as options. Assert
        the command tokenizes with the path as a trailing operand after
        ``--``, not as flags."""
        monkeypatch.chdir(tmp_path)
        base = tmp_path / "-rf"
        base.mkdir()
        os.chmod(base, 0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "-rf")  # relative, hyphen-leading
        (base / "memtomem").mkdir(mode=0o755)
        os.chmod(base / "memtomem", 0o755)

        with pytest.raises(PermissionError) as exc:
            ensure_runtime_dir_at(Path("-rf") / "memtomem")
        msg = str(exc.value)
        assert "rm -rf -- -rf/memtomem" in msg
        # The command portion must tokenize so the path is a lone operand.
        command = msg.split("Remove it and retry: ", 1)[1]
        tokens = shlex.split(command)
        assert tokens == ["rm", "-rf", "--", "-rf/memtomem"]
        assert tokens[-1] == "-rf/memtomem"  # operand, not an option bundle


class TestHintQuote:
    """Unit coverage for the quoting helper itself — :func:`shlex.quote`
    for printable paths, ANSI-C ``$'...'`` with fsencoded-byte escapes when
    a non-printable is present. ``Path`` inputs assert on the *contract*
    (the result is one shell token equal to the path), not the exact quoted
    string: ``str(Path(...))`` uses ``\\`` on Windows, so a hard-coded
    POSIX string would spuriously fail there while the helper is in fact
    correct. Exact-form assertions use ``str`` inputs, which bypass that
    rewriting."""

    def test_whitespace_path_becomes_a_single_operand(self):
        """A path with a space must not stay a bare word (it would split
        into two args); quoting collapses it back to exactly one operand."""
        p = Path("/tmp/my dir/memtomem-501")
        quoted = _hint_quote(p)
        assert quoted != str(p)  # the space forced quoting
        assert shlex.split(quoted) == [str(p)]  # ...to one token, the path

    def test_ordinary_path_is_a_single_operand(self):
        """The quoted operand always tokenizes back to the exact path — the
        property the uninstall side relies on when it forwards the producer's
        ``detail`` verbatim (#1948/#1955)."""
        p = Path("/run/user/501/memtomem")
        assert shlex.split(_hint_quote(p)) == [str(p)]

    def test_control_char_uses_ansi_c_byte_escapes(self):
        # str inputs bypass Path's platform separator rewriting, so the
        # exact-form assertions below are OS-independent.
        quoted = _hint_quote("/tmp/x\x1b]0;pwned\x07/memtomem-501")
        assert quoted == "$'/tmp/x\\x1b]0;pwned\\x07/memtomem-501'"
        assert "\x1b" not in quoted and "\x07" not in quoted

    def test_multibyte_non_printable_escapes_filesystem_bytes(self):
        # U+200B (zero-width space) is non-printable and multi-byte in
        # UTF-8. ``$'\xNN'`` produces raw *bytes* in bash/zsh — escaping
        # the code point (``​``) would name a different path, and
        # macOS bash 3.2 has no ``\u`` at all — so the escape must spell
        # out the fsencoded bytes.
        assert _hint_quote("/tmp/a​b") == "$'/tmp/a\\xe2\\x80\\x8bb'"

    def test_backslash_and_nul_escape_inside_ansi_c(self):
        assert _hint_quote("/tmp/a\\b\x00") == "$'/tmp/a\\\\b\\x00'"

    def test_printable_non_ascii_stays_literal_inside_ansi_c(self):
        # A control char forces the $'...' branch, but printable
        # non-ASCII (e.g. Hangul) must pass through as-is — only the
        # non-printables get byte-escaped.
        assert _hint_quote("/tmp/한글\x1b") == "$'/tmp/한글\\x1b'"

    def test_single_quote_with_control_char_stays_one_token(self):
        # ' inside the ANSI-C branch must be escaped or the token ends
        # early; bash tokenization of the result must give the raw path.
        quoted = _hint_quote("/tmp/o'brien\x1b/memtomem")
        assert quoted == "$'/tmp/o\\'brien\\x1b/memtomem'"


class TestScrub:
    """``scrub_text`` is the prose-side counterpart of ``_hint_quote``: the
    ``runtime dir {target}`` prefix renders the same environment-derived
    path, so a raw ESC/OSC there would reach the terminal before the
    safely quoted command ever does (#1956)."""

    def test_printable_text_passes_through(self):
        assert scrub_text("/tmp/my dir/memtomem-501") == "/tmp/my dir/memtomem-501"

    def test_control_chars_become_byte_escapes(self):
        assert scrub_text("x\x1b]0;pwned\x07y") == "x\\x1b]0;pwned\\x07y"

    def test_multibyte_non_printable_scrubs_filesystem_bytes(self):
        assert scrub_text("a​b") == "a\\xe2\\x80\\x8bb"


@pytest.mark.skipif(
    os.name == "nt",
    reason="depends on _make_safe_xdg fixture's POSIX chmod; "
    "Windows path is covered indirectly in TestWindowsRuntimeDir",
)
class TestServerPidPath:
    def test_resolves_to_runtime_dir_server_pid(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        assert server_pid_path() == runtime_dir() / "server.pid"

    def test_does_not_create_parent(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        server_pid_path()
        server_pid_path(tmp_path / "store" / "memtomem.db")

        assert not runtime_dir().exists(), (
            "server_pid_path() is a path resolver; use ensure_runtime_dir() "
            "explicitly when opening the file"
        )

    def test_store_scoped_name_matches_digest_pattern(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        p = server_pid_path(tmp_path / "a" / "memtomem.db")

        assert p.parent == runtime_dir()
        assert re.fullmatch(r"server-[0-9a-f]{16}\.pid", p.name), p.name

    def test_different_stores_get_different_names(self, tmp_path, monkeypatch):
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        a = server_pid_path(tmp_path / "a" / "memtomem.db")
        b = server_pid_path(tmp_path / "b" / "memtomem.db")

        assert a != b

    def test_spelling_variants_of_one_store_get_one_name(self, tmp_path, monkeypatch):
        """expanduser / trailing-slash / relative-segment spellings of the
        same path must all land on one digest — the server resolves through
        ``expanduser().resolve()`` while ``mm uninstall`` passes an
        ``expanduser()``-only path, and the helper must erase that caller
        discrepancy (#1990)."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        monkeypatch.setenv("HOME", str(tmp_path))

        db = tmp_path / ".memtomem" / "memtomem.db"

        assert server_pid_path(db) == server_pid_path("~/.memtomem/memtomem.db")
        assert server_pid_path(db) == server_pid_path(
            tmp_path / ".memtomem" / "sub" / ".." / "memtomem.db"
        )

    def test_memory_store_falls_back_to_bare_name(self, tmp_path, monkeypatch):
        """Non-file SQLite targets have no per-store identity to hash —
        ``:memory:`` resolved as a path text would just be CWD-relative
        noise. They degrade to the transitional bare name (store-agnostic,
        fail-closed for the liveness probes)."""
        xdg = _make_safe_xdg(tmp_path)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))

        assert server_pid_path(":memory:") == runtime_dir() / "server.pid"
        assert store_pid_digest(":memory:") is None
        assert store_pid_digest("file:mem?mode=memory") is None

    @pytest.mark.skipif(sys.platform != "darwin", reason="APFS default is case-insensitive")
    def test_darwin_case_variants_collapse(self, tmp_path):
        assert store_pid_digest(tmp_path / "Store" / "DB.db") == store_pid_digest(
            tmp_path / "store" / "db.db"
        )


@pytest.mark.skipif(
    os.name != "nt",
    reason="NTFS-specific: tempdir-only resolution, mode-bit gate disabled",
)
class TestWindowsRuntimeDir:
    """Windows counterpart to ``TestRuntimeDir`` / ``TestEnsureRuntimeDir``.

    The runtime resolver collapses to one branch on Windows
    (``FOLDERID_LocalAppData / 'Temp' / 'memtomem-0'``) and the security gates
    that depend on POSIX mode bits or ``geteuid`` are off — see the
    module docstring of ``_runtime_paths``. These tests pin the
    NTFS-equivalent behaviour so a future contributor can't silently
    re-enable a check that doesn't have NTFS semantics.
    """

    def test_real_known_folder_api_smoke(self):
        """Exercise the real ctypes signature and process-lifetime cache."""
        runtime_paths._windows_local_app_data.cache_clear()
        result = runtime_paths._windows_local_app_data()

        assert result.is_absolute()
        assert result.exists()
        assert result.is_dir()
        assert runtime_paths._windows_local_app_data() == result
        assert runtime_paths._windows_local_app_data.cache_info().hits == 1

    def test_runtime_dir_uses_known_folder_with_uid_zero(self, tmp_path, monkeypatch):
        """Windows has no ``geteuid``, so the suffix collapses to ``0``;
        ``%LOCALAPPDATA%\\Temp\\`` is already per-user, so the cross-user
        collision risk that motivates the suffix on shared ``/tmp`` does
        not apply here."""
        local_app_data = tmp_path / "LocalAppData"
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)

        result = runtime_dir()

        assert result == local_app_data / "Temp" / "memtomem-0"

    def test_runtime_dir_does_not_create(self, tmp_path, monkeypatch):
        """Pure resolver — same contract as POSIX. The uninstall
        inventory walk needs this to be side-effect free."""
        local_app_data = tmp_path / "LocalAppData"
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)

        result = runtime_dir()
        # Don't assert non-existence: a previous test in the session may
        # have created it. The contract is "this call doesn't create",
        # not "the path is missing".
        before = result.exists()
        runtime_dir()
        assert result.exists() == before

    def test_runtime_dir_ignores_environment_on_windows(self, tmp_path, monkeypatch):
        """``XDG_RUNTIME_DIR`` is a Linux/systemd convention; honoring
        it on Windows would require validating the base without POSIX
        mode bits, which is half-baked. Always fall through to tempdir
        on Windows so behaviour is uniform regardless of environment."""
        xdg = tmp_path / "xdg"
        xdg.mkdir()
        local_app_data = tmp_path / "LocalAppData"
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
        monkeypatch.setenv("TMP", str(tmp_path / "other-tmp"))

        result = runtime_dir()

        assert result == local_app_data / "Temp" / "memtomem-0"

    def test_ensure_creates_directory(self, tmp_path, monkeypatch):
        """``ensure_runtime_dir`` mkdirs the resolved path. Mode bits
        are not asserted: NTFS synthesizes them and the ``chmod`` call
        is effectively a no-op for POSIX-style permissions."""
        local_app_data = tmp_path / "LocalAppData"
        (local_app_data / "Temp").mkdir(parents=True)
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)

        d = ensure_runtime_dir()
        assert d.exists() and d.is_dir()
        assert d == local_app_data / "Temp" / "memtomem-0"

    def test_ensure_idempotent_on_windows(self, tmp_path, monkeypatch):
        """Regression for #637 — pre-fix, the second call would refuse
        the dir it had just created because NTFS reports synthesized
        mode bits like ``0o666`` and ``stat.S_IMODE(...) & 0o077`` is
        non-zero. After the fix the mode-bit gate is skipped on
        Windows so successive calls are idempotent."""
        local_app_data = tmp_path / "LocalAppData"
        (local_app_data / "Temp").mkdir(parents=True)
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)

        ensure_runtime_dir()
        # Must not raise on the second pass.
        ensure_runtime_dir()

    def test_ensure_refuses_non_directory(self, tmp_path, monkeypatch):
        """Same contract as POSIX: a regular file at the runtime path
        is rejected with a clear remediation hint, regardless of NTFS
        mode-bit synthesis."""
        local_app_data = tmp_path / "LocalAppData"
        (local_app_data / "Temp").mkdir(parents=True)
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)
        (local_app_data / "Temp" / "memtomem-0").write_text("a file")

        with pytest.raises(PermissionError, match="not a directory"):
            ensure_runtime_dir()

    @pytest.mark.requires_symlinks
    def test_ensure_refuses_existing_symlink(self, tmp_path, monkeypatch):
        """Windows symlinks need Developer Mode or admin to create,
        but once present they are exploitable identically to POSIX
        symlinks. Auto-skipped via ``requires_symlinks`` when the
        runner can't create one (see conftest)."""
        local_app_data = tmp_path / "LocalAppData"
        (local_app_data / "Temp").mkdir(parents=True)
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.setattr(runtime_paths, "_windows_local_app_data", lambda: local_app_data)
        target = tmp_path / "real-target"
        target.mkdir()
        os.symlink(target, local_app_data / "Temp" / "memtomem-0")

        with pytest.raises(PermissionError, match="symlink"):
            ensure_runtime_dir()


class TestLegacyServerPidPath:
    def test_evaluates_home_lazily(self, tmp_path, monkeypatch):
        """Import-time ``Path.home()`` would capture the developer's
        real home and leak across fixtures; the function must re-read
        ``$HOME`` every call."""
        set_home(monkeypatch, tmp_path)

        assert legacy_server_pid_path() == tmp_path / ".memtomem" / ".server.pid"


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="uid fallback only meaningful on POSIX")
class TestUidFallback:
    def test_stable_dir_contains_effective_uid(self, monkeypatch):
        """The stable shared-``/tmp`` anchor includes the effective uid."""
        monkeypatch.delenv("_MEMTOMEM_TEST_RUNTIME_DIR")
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

        result = runtime_dir()

        assert result.parent == Path("/tmp")
        assert result.name == f"memtomem-{os.geteuid()}"
