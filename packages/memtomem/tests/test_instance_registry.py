"""Instance registry (#1935): registration, probing, GC, and fail-closed probes.

Lock behavior is validated **cross-process** (spawn) per the repo
convention (see ``test_locking_contention.py``): in-process contention rides
on backend details — ``fcntl.flock`` attaches per open file description, and
Windows uses a different backend entirely (``msvcrt.locking``, portalocker's
default for exclusive locks since 3.2) — so in-process contention proves
nothing. In-process tests here cover only pure parsing, state, and
fail-open/fail-closed decision logic.
"""

from __future__ import annotations

import contextlib
import errno
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

import memtomem._instance_registry as reg
from memtomem._runtime_paths import store_pid_digest

_CTX = mp.get_context("spawn")


# ----------------------------------------------------------------- helpers


def _point_registry_at(rt: Path) -> None:
    """Redirect the registry module (in *this* process) at ``rt``."""

    def _rt() -> Path:
        return rt

    def _ensure() -> Path:
        rt.mkdir(mode=0o700, exist_ok=True)
        return rt

    reg.runtime_dir = _rt  # type: ignore[assignment]
    reg.ensure_runtime_dir = _ensure  # type: ignore[assignment]


@pytest.fixture
def rt(tmp_path, monkeypatch) -> Path:
    """A registry-of-record for one test, overriding the conftest default
    so spawned children (which see neither fixture) can be pointed at the
    same directory by path string."""
    target = tmp_path / "rt"

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    monkeypatch.setattr(reg, "runtime_dir", _rt)
    monkeypatch.setattr(reg, "ensure_runtime_dir", _ensure)
    return target


@pytest.fixture
def db(tmp_path) -> Path:
    p = tmp_path / "store.db"
    p.write_bytes(b"sqlite-fake")
    return p


# ------------------------------------------------------- spawn child bodies


def _child_setup(rt_str: str):
    import memtomem._instance_registry as _reg

    target = Path(rt_str)

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    _reg.runtime_dir = _rt
    _reg.ensure_runtime_dir = _ensure
    return _reg


def _child_register_hold(rt_str: str, db_str: str, q, release) -> None:
    _reg = _child_setup(rt_str)
    inst = _reg.register_instance(Path(db_str))
    q.put(("registered", inst is not None, os.getpid()))
    release.wait(60)
    if inst is not None:
        inst.cleanup()
    q.put(("done",))


def _child_register_hold_forever(rt_str: str, db_str: str, q) -> None:
    _reg = _child_setup(rt_str)
    inst = _reg.register_instance(Path(db_str))
    q.put(("registered", inst is not None, os.getpid()))
    time.sleep(600)  # parent kills us


def _child_register_and_enumerate(rt_str: str, db_str: str, q, release) -> None:
    _reg = _child_setup(rt_str)
    inst = _reg.register_instance(Path(db_str))
    digest = _reg.store_digest_for(Path(db_str))
    result = _reg.enumerate_live_instances(digest)
    q.put(
        (
            "enumerated",
            result.complete,
            sorted((i.pid, i.procid) for i in result.instances),
            os.getpid(),
        )
    )
    release.wait(60)
    if inst is not None:
        inst.cleanup()
    q.put(("done",))


def _child_register_fork_grandchild(rt_str: str, db_str: str, q, release) -> None:
    import sys

    _reg = _child_setup(rt_str)
    inst = _reg.register_instance(Path(db_str))
    grand = os.fork()
    if grand == 0:
        # normal interpreter exit — the inherited atexit stack (incl. the
        # registry handler over the inherited active dict) must run and,
        # thanks to the pid guard, leave the parent's sentinel alone
        sys.exit(0)
    _, status = os.waitpid(grand, 0)
    survived = inst is not None and inst.path.exists()
    q.put(("forked", survived, os.waitstatus_to_exitcode(status), os.getpid()))
    release.wait(60)
    if inst is not None:
        inst.cleanup()


def _child_hold_sidecar(rt_str: str, q, release) -> None:
    import portalocker

    target = Path(rt_str)
    target.mkdir(mode=0o700, exist_ok=True)
    fp = open(target / "instances.registry.lock", "a+b")
    portalocker.lock(fp, portalocker.LOCK_EX)
    q.put(("held",))
    release.wait(60)
    fp.close()


def _drain_until(q, tag: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            msg = q.get(timeout=1.0)
        except Exception:
            continue
        if msg[0] == tag:
            return msg
    raise AssertionError(f"child never reported {tag!r}")


def _stop(proc: mp.Process) -> None:
    if proc.is_alive():
        proc.kill()
    proc.join(timeout=30)


# --------------------------------------------------------------- in-process


class TestStoreDigest:
    def test_missing_path_is_none(self, tmp_path):
        assert reg.store_digest_for(tmp_path / "nope.db") is None

    def test_directory_is_none(self, tmp_path):
        assert reg.store_digest_for(tmp_path) is None

    def test_stable_across_calls_and_spellings(self, db):
        a = reg.store_digest_for(db)
        b = reg.store_digest_for(Path(str(db)))
        assert a == b
        assert a is not None and len(a) == 16

    def test_symlink_collapses_to_target(self, db, tmp_path):
        link = tmp_path / "alias.db"
        try:
            link.symlink_to(db)
        except OSError:
            pytest.skip("symlinks unavailable")
        assert reg.store_digest_for(link) == reg.store_digest_for(db)


class TestFilenameRoundTrip:
    def test_registration_filename_parses_back(self, rt, db):
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            info = reg._parse_entry(inst.path)
            assert info is not None
            assert info.pid == os.getpid()
            assert info.ppid == os.getppid()
            assert info.digest == reg.store_digest_for(db)
            assert len(info.procid) == 8 and len(inst.path.name.split("-")) == 5
        finally:
            inst.cleanup()

    def test_unparseable_names_rejected(self, rt):
        assert reg._parse_entry(Path("garbage.lock")) is None
        assert reg._parse_entry(Path("1-2-3.lock")) is None


class TestRegistrationState:
    def test_non_file_store_skips(self, rt, tmp_path):
        assert reg.register_instance(tmp_path / "missing.db") is None
        assert not (rt / "instances").exists() or not any((rt / "instances").iterdir())

    def test_cleanup_idempotent(self, rt, db):
        inst = reg.register_instance(db)
        assert inst is not None
        inst.cleanup()
        inst.cleanup()
        assert not inst.path.exists()
        assert inst.path not in reg._active

    def test_pid_guard_no_ops_before_any_state_mutation(self, rt, db):
        """Fork contract: a foreign-pid cleanup must not unlink or touch state."""
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            real_pid = inst.pid
            inst.pid = real_pid + 1  # simulate the inherited copy in a forked child
            inst.cleanup()
            assert inst.path.exists()
            assert reg._active.get(inst.path) is inst
        finally:
            inst.pid = real_pid
            inst.cleanup()

    def test_old_cleanup_after_new_registration_leaves_new_intact(self, rt, db):
        first = reg.register_instance(db)
        assert first is not None
        first_path = first.path
        first.cleanup()
        second = reg.register_instance(db)
        assert second is not None
        try:
            first.cleanup()  # late double-cleanup of the old registration
            assert second.path.exists()
            assert reg._active.get(second.path) is second
            assert second.path != first_path  # nonce makes names unique
        finally:
            second.cleanup()

    def test_registration_never_unlinks_same_pid_foreign_entry(self, rt, db):
        digest = reg.store_digest_for(db)
        foreign = reg.instances_dir()
        foreign.mkdir(parents=True, exist_ok=True)
        entry = foreign / f"{os.getpid()}-1-{digest}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            assert entry.exists()
        finally:
            inst.cleanup()
            entry.unlink()


class TestRegistrationRetry:
    """#1939: a sidecar-lock timeout is retried; other failures are one-shot.

    In-process is correct here — these pin the *decision* logic around the
    lock, not lock contention itself (which the file convention validates
    cross-process). Some tests script ``_mutation_lock`` /
    ``_MutationLockTimeout`` directly; the contention-classification tests
    instead monkeypatch ``portalocker.lock`` to raise a specific exception
    shape and exercise the real poll loop, which catches ``_LOCK_CONTENDED``
    and then defers to :func:`_is_lock_contention` (shared with the barrier,
    #1957): genuine contention is polled, every durable lock-call I/O error
    propagates one-shot.
    """

    def test_lost_sidecar_race_once_still_registers(self, rt, db, monkeypatch):
        real_lock = reg._mutation_lock
        calls = {"n": 0}

        def flaky_lock(deadline):
            calls["n"] += 1
            if calls["n"] == 1:
                raise reg._MutationLockTimeout
            return real_lock(deadline)

        monkeypatch.setattr(reg, "_mutation_lock", flaky_lock)
        inst = reg.register_instance(db)
        try:
            assert inst is not None
            assert inst.path.exists()
            assert reg._active.get(inst.path) is inst
            assert calls["n"] == 2  # one loss, one success
        finally:
            if inst is not None:
                inst.cleanup()

    def test_persistent_timeout_bounded_and_returns_none(self, rt, db, monkeypatch):
        calls = {"n": 0}

        def always_timeout(deadline):
            calls["n"] += 1
            raise reg._MutationLockTimeout

        monkeypatch.setattr(reg, "_mutation_lock", always_timeout)
        inst = reg.register_instance(db)
        assert inst is None
        assert calls["n"] == reg._REGISTRATION_ATTEMPTS  # bounded, no infinite loop
        # The lock never yielded, so no sentinel could have been created.
        assert not list((rt / "instances").glob("*.lock")) if (rt / "instances").is_dir() else True

    def test_non_file_store_returns_none_without_lock_attempt(self, rt, tmp_path, monkeypatch):
        calls = {"n": 0}

        def counting_lock(deadline):
            calls["n"] += 1
            raise reg._MutationLockTimeout

        monkeypatch.setattr(reg, "_mutation_lock", counting_lock)
        # ``digest is None`` short-circuits before the loop — the lock is
        # never taken and no retry happens.
        assert reg.register_instance(tmp_path / "missing.db") is None
        assert calls["n"] == 0

    def test_untrusted_instances_dir_not_retried(self, rt, db, monkeypatch):
        # A permanent in-loop refusal via ``return None`` (the sentinel
        # directory is untrusted — ``_dir_state != "dir"``) exits on the
        # first attempt: retrying cannot change a persistent cause.
        real_lock = reg._mutation_lock
        calls = {"n": 0}

        def counting_lock(deadline):
            calls["n"] += 1
            return real_lock(deadline)

        monkeypatch.setattr(reg, "_mutation_lock", counting_lock)
        monkeypatch.setattr(reg, "_dir_state", lambda _p: "untrusted")
        assert reg.register_instance(db) is None
        assert calls["n"] == 1  # entered the lock once, refused, did not retry

    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(
                lambda: reg.portalocker.LockException("simulated EIO wrapper"),
                id="lockexception-wrapper",
            ),
            pytest.param(
                lambda: OSError(errno.EIO, "simulated raw I/O error"),
                id="raw-oserror",
            ),
        ],
    )
    def test_durable_lock_error_is_one_shot(self, rt, db, monkeypatch, exc_factory):
        # A durable sidecar-flock failure must propagate to the never-raise
        # handler and return ``None`` after exactly one lock call, never be
        # polled to a timeout and retried (#1939). The ``LockException``
        # wrapper is the real production shape: portalocker maps a
        # non-contention ``OSError`` (``EIO``/``ENOLCK``) to a bare
        # ``LockException`` (not the ``AlreadyLocked`` subclass) on both
        # 3.x and 4.x, which
        # ``_is_lock_contention`` rejects, so the poll loop re-raises it
        # despite the retry.
        calls = {"n": 0}

        def failing_lock(fp, flags):
            calls["n"] += 1
            raise exc_factory()

        monkeypatch.setattr(reg.portalocker, "lock", failing_lock)
        assert reg.register_instance(db) is None
        assert calls["n"] == 1  # first sidecar-lock call propagated; no retry

    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(lambda: reg.portalocker.AlreadyLocked("held"), id="already-locked"),
            pytest.param(
                lambda: OSError(errno.EAGAIN, "resource temporarily unavailable"),
                id="raw-eagain",
            ),
        ],
    )
    def test_contention_is_retried_and_registers(self, rt, db, monkeypatch, exc_factory):
        # Genuine contention must be polled and retried, not propagated, so
        # a single lost race still ends up registered. portalocker maps
        # POSIX ``EACCES``/``EAGAIN`` and Win32 ``ERROR_LOCK_VIOLATION`` to
        # ``AlreadyLocked``, but ``_is_lock_contention`` also accepts a
        # *raw* ``EACCES``/``EAGAIN`` ``OSError`` — a version-drift shape a
        # bare ``isinstance(AlreadyLocked)`` check would have dropped,
        # regressing #1939 on real contention.
        real_lock = reg.portalocker.lock
        state = {"raised": False}

        def flaky_lock(fp, flags):
            if not state["raised"]:
                state["raised"] = True
                raise exc_factory()
            return real_lock(fp, flags)

        monkeypatch.setattr(reg.portalocker, "lock", flaky_lock)
        inst = reg.register_instance(db)
        try:
            assert inst is not None  # contention polled through, not propagated
            assert inst.path.exists()
        finally:
            if inst is not None:
                inst.cleanup()


class TestEnumerationInProcess:
    def test_missing_dir_is_complete_and_empty(self, rt):
        result = reg.enumerate_live_instances("0" * 16)
        assert result.complete and result.instances == ()

    def test_own_registration_included_without_probing(self, rt, db):
        """Own registrations come from ``_active``, not from a probe —
        self must never be probed stale."""
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            result = reg.enumerate_live_instances(reg.store_digest_for(db))
            assert result.complete
            assert [i.pid for i in result.instances] == [os.getpid()]
        finally:
            inst.cleanup()

    def test_fresh_unlocked_entry_kept(self, rt, db):
        """Publication-window protection: unlocked-but-fresh is never GC'd."""
        digest = reg.store_digest_for(db)
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{digest}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        result = reg.enumerate_live_instances(digest)
        assert result.complete
        assert entry.exists()
        assert result.instances == ()  # unlocked → not live

    def test_aged_unlocked_entry_gcd(self, rt, db):
        digest = reg.store_digest_for(db)
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{digest}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        aged = time.time() - reg._STALE_GRACE_S - 10
        os.utime(entry, (aged, aged))
        reg.enumerate_live_instances(digest)
        assert not entry.exists()

    def test_fresh_corrupt_name_kept_aged_corrupt_name_gcd(self, rt):
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        fresh = d / "not-a-sentinel.txt"
        fresh.touch()
        aged = d / "also-garbage.bin"
        aged.touch()
        old = time.time() - reg._STALE_GRACE_S - 10
        os.utime(aged, (old, old))
        reg.enumerate_live_instances("0" * 16)
        assert fresh.exists()
        assert not aged.exists()

    def test_sidecar_outside_scanned_dir_survives_aged(self, rt):
        """The mutation sidecar is retained infrastructure — an aged
        sidecar must never be treated as a corrupt sentinel."""
        reg.instances_dir().mkdir(parents=True, exist_ok=True)
        with reg._mutation_lock(time.monotonic() + 1):
            pass  # creates the sidecar
        sidecar = reg.registry_sidecar_path()
        assert sidecar.exists()
        old = time.time() - reg._STALE_GRACE_S - 10
        os.utime(sidecar, (old, old))
        result = reg.enumerate_live_instances("0" * 16)
        assert result.complete
        assert sidecar.exists()

    def test_mutation_lock_contention_fails_open(self, rt, db, monkeypatch):
        """Enumeration against a held mutation lock times out fail-open
        (complete=False) and mutates nothing — a fresh unlocked entry in
        the dir survives untouched."""
        monkeypatch.setattr(reg, "_LOCK_TIMEOUT_S", 0.2)
        digest = reg.store_digest_for(db)
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{digest}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        old = time.time() - reg._STALE_GRACE_S - 10
        os.utime(entry, (old, old))  # would be GC'd if the pass ran
        assert reg._mutation_thread_lock.acquire(timeout=5)
        try:
            result = reg.enumerate_live_instances(digest)
        finally:
            reg._mutation_thread_lock.release()
        assert not result.complete
        assert entry.exists()


class TestUninstallProbeInProcess:
    def test_empty_registry_is_none(self, rt):
        assert reg.probe_all_for_uninstall().state == "NONE"

    def test_own_registration_is_live(self, rt, db):
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            assert reg.probe_all_for_uninstall().state == "LIVE"
        finally:
            inst.cleanup()

    def test_stale_entries_are_none_and_not_mutated(self, rt):
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / "not-parseable-at-all"
        entry.touch()
        old = time.time() - reg._STALE_GRACE_S - 10
        os.utime(entry, (old, old))
        assert reg.probe_all_for_uninstall().state == "NONE"
        # fail-closed probe performs no GC — uninstall must not mutate
        # the registry it is judging
        assert entry.exists()

    def test_mutation_lock_contention_is_unknown(self, rt, monkeypatch):
        """A timeout never means empty (fail-closed) — and it is the
        *transient* verdict, so it must not carry an untrusted path."""
        monkeypatch.setattr(reg, "_LOCK_TIMEOUT_S", 0.2)
        reg.instances_dir().mkdir(parents=True, exist_ok=True)
        (reg.instances_dir() / "whatever").touch()
        assert reg._mutation_thread_lock.acquire(timeout=5)
        try:
            verdict = reg.probe_all_for_uninstall()
        finally:
            reg._mutation_thread_lock.release()
        assert verdict.state == "UNKNOWN"
        assert verdict.untrusted_path is None

    def test_entry_verdict_unknown_propagates_as_unknown(self, rt, monkeypatch):
        """A generic ``"unknown"`` entry verdict (transient I/O failure)
        stays UNKNOWN — only the *persistent* ``"untrusted"`` entry
        verdict is promoted to UNTRUSTED (#1938)."""
        reg.instances_dir().mkdir(parents=True, exist_ok=True)
        (reg.instances_dir() / "entry").touch()
        monkeypatch.setattr(reg, "_probe_entry", lambda _p: "unknown")
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNKNOWN"
        assert verdict.untrusted_path is None

    def test_runtime_dir_refusal_is_untrusted_not_unknown(self, rt, monkeypatch):
        """``ensure_runtime_dir`` refusing its own directory (symlink,
        junction, wrong owner, unsafe mode — #1940) is persistent, not
        transient: the probe must answer UNTRUSTED naming the runtime
        dir, not collapse into UNKNOWN's "retry" advice (#1942)."""

        def _refuse() -> Path:
            raise PermissionError(f"runtime dir {rt} is a junction; refusing to follow.")

        monkeypatch.setattr(reg, "ensure_runtime_dir", _refuse)
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == rt

    def test_runtime_dir_refusal_carries_the_cause_detail(self, rt, monkeypatch):
        """The exact ``ensure_runtime_dir`` message — the owner/mode cause
        and its removal hint that the generic redirected-path sentence
        cannot name — must survive into ``detail`` for the CLI to surface
        (#1948), not vanish into the debug log."""
        message = (
            f"runtime dir {rt} is owned by uid 501 (expected 0). Remove it and retry: rm -rf {rt}"
        )

        def _refuse() -> Path:
            raise PermissionError(message)

        monkeypatch.setattr(reg, "ensure_runtime_dir", _refuse)
        verdict = reg.probe_all_for_uninstall()
        assert verdict.detail == message

    def test_sidecar_failure_is_unknown_not_untrusted(self, rt, monkeypatch):
        """A ``PermissionError`` from the sidecar layer proves nothing
        about the runtime dir — attributing it there would tell the user
        to remove a directory that may be fine. Only the translated
        ``ensure_runtime_dir`` refusal maps to UNTRUSTED (#1942)."""

        def _bad_sidecar() -> Path:
            raise PermissionError("sidecar open denied")

        monkeypatch.setattr(reg, "registry_sidecar_path", _bad_sidecar)
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNKNOWN"
        assert verdict.untrusted_path is None

    def test_entry_unlock_failure_is_unknown_not_untrusted(self, rt, monkeypatch):
        """``portalocker.unlock`` / ``close`` on a sentinel are the one
        unguarded spot inside the probe loop — an escaping
        ``PermissionError`` there must read as UNKNOWN, never as an
        untrusted runtime dir (#1942)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"12345-1-{'f' * 16}-aaaaaaaa-bbbbbbbb.lock").touch()

        def _bad_unlock(_fp):
            raise PermissionError("unlock denied")

        monkeypatch.setattr(reg.portalocker, "unlock", _bad_unlock)
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNKNOWN"
        assert verdict.untrusted_path is None

    def test_stray_subdirectory_entry_is_untrusted_with_entry_path(self, rt):
        """A stray subdirectory inside ``instances/`` is not a probeable
        sentinel and never ages out — persistent, so it must read
        UNTRUSTED naming the entry (kind ``"unprobeable"``), not collapse
        into UNKNOWN's "retry" advice (#1938). No platform skip: the
        no-follow stat classifies it before the open, so the POSIX
        ``IsADirectoryError`` vs Windows ``PermissionError`` split at
        ``open`` never comes into play."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        subdir = d / "stray-subdir"
        subdir.mkdir()
        assert reg._probe_entry(subdir) == "untrusted"
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == subdir
        assert verdict.untrusted_kind == "unprobeable"

    def test_symlinked_entry_is_untrusted_never_probed_through(self, rt, tmp_path):
        """A symlinked entry would follow silently and flock an
        *unrelated* file, fabricating a live/stale verdict on a foreign
        path. The no-follow stat classifies it UNTRUSTED first, and the
        victim is never opened (#1938)."""
        victim = tmp_path / "victim.lock"
        victim.write_text("do not touch")
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        try:
            entry.symlink_to(victim)
        except OSError:
            pytest.skip("symlinks unavailable")
        assert reg._probe_entry(entry) == "untrusted"
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == entry
        assert verdict.untrusted_kind == "unprobeable"
        assert victim.read_text() == "do not touch"

    @pytest.mark.skipif(os.name == "nt", reason="chmod bits are a no-op on Windows")
    def test_unreadable_entry_file_is_untrusted_with_entry_path(self, rt):
        """A mode-000 (or root-owned) sentinel raises ``PermissionError``
        at open *for that exact entry* — persistent and precisely
        attributable, so UNTRUSTED naming the entry, not UNKNOWN (#1938).
        Distinct from a sidecar/unlock ``PermissionError``, which proves
        nothing about the entry and stays UNKNOWN (#1942)."""
        if getattr(os, "geteuid", lambda: 1)() == 0:
            pytest.skip("root ignores file modes")
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        entry.chmod(0o000)
        try:
            assert reg._probe_entry(entry) == "untrusted"
            verdict = reg.probe_all_for_uninstall()
            assert verdict.state == "UNTRUSTED"
            assert verdict.untrusted_path == entry
            assert verdict.untrusted_kind == "unprobeable"
        finally:
            entry.chmod(0o600)

    @pytest.mark.skipif(os.name == "nt", reason="chmod bits are a no-op on Windows")
    def test_unlistable_instances_dir_is_untrusted_with_dir_path(self, rt):
        """A real private ``instances/`` that cannot be *listed* (mode-000
        / ACL-denied) fails ``iterdir`` with ``PermissionError`` —
        persistent, and the offending path is the directory itself. It is
        a real directory, so it carries kind ``"unprobeable"`` (the
        "cannot be probed" wording), not ``"redirected"`` (#1938)."""
        if getattr(os, "geteuid", lambda: 1)() == 0:
            pytest.skip("root ignores directory modes")
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / "entry").touch()
        d.chmod(0o000)
        try:
            verdict = reg.probe_all_for_uninstall()
            assert verdict.state == "UNTRUSTED"
            assert verdict.untrusted_path == d
            assert verdict.untrusted_kind == "unprobeable"
        finally:
            d.chmod(0o700)

    def test_untrusted_entry_beats_earlier_unknown_entry(self, rt, monkeypatch):
        """Verdict precedence LIVE > UNTRUSTED > UNKNOWN: a transient
        ``unknown`` on an entry visited *first* must not mask a
        persistent ``untrusted`` entry visited later — otherwise the user
        is told to "retry" a condition only removal can clear (#1938).
        Iteration order is pinned by sorting so the unknown entry leads."""
        real_iterdir = reg.Path.iterdir

        def sorted_iterdir(self):
            return iter(sorted(real_iterdir(self)))

        monkeypatch.setattr(reg.Path, "iterdir", sorted_iterdir)
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        unknown_entry = d / f"00-12345-1-{'e' * 16}-aaaaaaaa-bbbbbbbb.lock"
        unknown_entry.touch()
        subdir = d / "99-stray-subdir"
        subdir.mkdir()

        real_lock = reg.portalocker.lock

        def flaky_lock(fp, flags):
            if getattr(fp, "name", "").endswith("bbbbbbbb.lock"):
                raise OSError("disk went away")
            return real_lock(fp, flags)

        monkeypatch.setattr(reg.portalocker, "lock", flaky_lock)
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == subdir
        assert verdict.untrusted_kind == "unprobeable"

    def test_probe_entry_open_generic_oserror_stays_unknown(self, rt, monkeypatch):
        """A non-``ELOOP``, non-``PermissionError`` ``OSError`` at open is a
        transient I/O failure, not a persistent untrusted entry — it must
        stay ``"unknown"`` so the classification is not over-broad (#1938)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()

        real_os_open = os.open

        def flaky_open(path, *args, **kwargs):
            if str(path).endswith("bbbbbbbb.lock"):
                raise OSError(errno.EIO, "disk went away")
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", flaky_open)
        assert reg._probe_entry(entry) == "unknown"

    def test_probe_entry_open_sharing_violation_stays_unknown(self, rt, monkeypatch):
        """A Windows sharing/lock violation at open (``winerror`` 32/33) is
        *transient* contention — it must stay ``"unknown"`` and never
        become a persistent ``"untrusted"`` prescribing remove/repair for
        a file another handle is merely holding for a moment (#1938)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()

        real_os_open = os.open

        def flaky_open(path, *args, **kwargs):
            if str(path).endswith("bbbbbbbb.lock"):
                exc = PermissionError("sharing violation")
                exc.winerror = 32  # ERROR_SHARING_VIOLATION
                raise exc
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", flaky_open)
        assert reg._probe_entry(entry) == "unknown"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX directory search-bit semantics")
    def test_search_denied_dir_entry_is_untrusted_not_unknown(self, rt):
        """A listable-but-unsearchable ``instances/`` (mode ``0o400``):
        ``iterdir`` yields the entry name, but statting the entry needs
        the directory's *search* bit and raises ``PermissionError`` —
        persistent, so ``UNTRUSTED`` naming the entry, not the transient
        "retry" verdict (#1938). This is the pre-open-``stat`` denial the
        mode-000 tests (which deny at ``open``) do not reach."""
        if getattr(os, "geteuid", lambda: 1)() == 0:
            pytest.skip("root bypasses directory permission bits")
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        d.chmod(0o400)
        try:
            verdict = reg.probe_all_for_uninstall()
            assert verdict.state == "UNTRUSTED"
            assert verdict.untrusted_path == entry
            assert verdict.untrusted_kind == "unprobeable"
        finally:
            d.chmod(0o700)

    def test_probe_entry_pre_stat_sharing_violation_stays_unknown(self, rt, monkeypatch):
        """A Windows sharing/lock violation at the *pre-open* stat is
        transient, exactly as at open — it must stay ``"unknown"``, not
        become a persistent ``"untrusted"`` (#1938)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        real_stat = os.stat

        def denying_stat(p, *, follow_symlinks=True):
            if os.fspath(p) == os.fspath(entry) and not follow_symlinks:
                exc = PermissionError("sharing violation")
                exc.winerror = 33  # ERROR_LOCK_VIOLATION
                raise exc
            return real_stat(p, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "stat", denying_stat)
        assert reg._probe_entry(entry) == "unknown"

    def test_probe_entry_open_eloop_is_untrusted(self, rt, monkeypatch):
        """A regular file swapped for a symlink *between* the no-follow stat
        and the open trips ``O_NOFOLLOW`` (``ELOOP``) — persistent, so
        ``"untrusted"``, closing the TOCTOU the stat alone leaves open
        (#1938)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()

        real_os_open = os.open

        def flaky_open(path, *args, **kwargs):
            if str(path).endswith("bbbbbbbb.lock"):
                raise OSError(errno.ELOOP, "too many symbolic links")
            return real_os_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", flaky_open)
        assert reg._probe_entry(entry) == "untrusted"

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: PermissionError("unlock denied"),
            lambda: reg.portalocker.LockException("unlock denied"),
        ],
        ids=["oserror", "lockexception"],
    )
    def test_untrusted_entry_survives_later_entry_unlock_failure(
        self, rt, monkeypatch, exc_factory
    ):
        """Precedence UNTRUSTED > UNKNOWN must hold even when a *later*
        entry's unlock/close raises: the escaping error is absorbed as
        that entry's ``unknown``, not allowed to unwind the loop and
        discard an ``untrusted`` already seen (#1938). Order-independent —
        both entries are always visited, so correct code yields UNTRUSTED
        regardless of which is probed first. Covers both the POSIX
        ``OSError`` shape and portalocker's Windows ``LockException``
        (which is *not* an ``OSError``)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        subdir = d / "stray-subdir"
        subdir.mkdir()
        sentinel = d / f"12345-1-{'f' * 16}-aaaaaaaa-bbbbbbbb.lock"
        sentinel.touch()

        def _bad_unlock(_fp):
            raise exc_factory()

        monkeypatch.setattr(reg.portalocker, "unlock", _bad_unlock)
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == subdir
        assert verdict.untrusted_kind == "unprobeable"

    def test_entry_swapped_after_stat_is_untrusted_by_identity(self, rt, monkeypatch):
        """A redirect that slips past ``O_NOFOLLOW`` (a no-op on Windows)
        opens a *different* inode than the no-follow stat saw — the
        post-open ``fstat`` identity check catches it as ``untrusted``,
        so a foreign file is never flock-probed as if it were the
        sentinel (#1938). Simulated by making ``fstat`` report a
        different regular file's ``st_dev``/``st_ino``."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        other = d / "other-regular-file"
        other.write_text("x")
        other_stat = os.stat(other)

        monkeypatch.setattr(os, "fstat", lambda _fd: other_stat)
        assert reg._probe_entry(entry) == "untrusted"

    def test_entry_diverging_post_open_path_stat_is_untrusted(self, rt, monkeypatch):
        """The identity check is enforced from the *path* side too: if the
        post-open no-follow ``stat`` of the path diverges from the open
        descriptor (a redirect swapped in past ``O_NOFOLLOW``, which is a
        no-op on Windows — a symlink has its own distinct inode), the
        entry is ``untrusted``, never flock-probed (#1938). Simulated by
        making the post-open re-stat report a different object so no
        symlink privilege is needed."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'a' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        diverging = os.stat(d, follow_symlinks=False)  # a different inode
        real_stat = os.stat
        seen = {"n": 0}

        def fake_stat(p, *, follow_symlinks=True):
            if os.fspath(p) == os.fspath(entry) and not follow_symlinks:
                seen["n"] += 1
                if seen["n"] >= 2:  # the post-open re-stat, not the pre-open gate
                    return diverging
            return real_stat(p, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(os, "stat", fake_stat)
        assert reg._probe_entry(entry) == "untrusted"

    def test_enumerate_with_untrusted_entry_is_incomplete(self, rt):
        """The fail-open status path treats an untrusted entry as
        uncertainty (``complete=False``) and never GC's it — only
        ``"stale"`` reaches ``_gc_stale_entry`` (#1938)."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        subdir = d / "stray-subdir"
        subdir.mkdir()
        result = reg.enumerate_live_instances("0" * 16)
        assert not result.complete
        assert result.instances == ()
        assert subdir.exists()


# ------------------------------------------------------------ cross-process


class TestCrossProcess:
    def test_child_registration_visible_and_digest_scoped(self, rt, db, tmp_path):
        other_db = tmp_path / "other.db"
        other_db.write_bytes(b"other")
        q1, q2 = _CTX.Queue(), _CTX.Queue()
        release = _CTX.Event()
        same = _CTX.Process(target=_child_register_hold, args=(str(rt), str(db), q1, release))
        other = _CTX.Process(
            target=_child_register_hold, args=(str(rt), str(other_db), q2, release)
        )
        same.start()
        other.start()
        try:
            _, ok1, child_pid = _drain_until(q1, "registered")
            _, ok2, _ = _drain_until(q2, "registered")
            assert ok1 and ok2

            result = reg.enumerate_live_instances(reg.store_digest_for(db))
            assert result.complete
            assert [i.pid for i in result.instances] == [child_pid]

            # all-store probe sees both children regardless of digest
            assert reg.probe_all_for_uninstall().state == "LIVE"
        finally:
            release.set()
            same.join(timeout=30)
            other.join(timeout=30)
            _stop(same)
            _stop(other)

    def test_live_registration_in_transition_root_is_aggregated(
        self, rt, db, tmp_path, monkeypatch
    ):
        legacy = tmp_path / "legacy-runtime"
        monkeypatch.setattr(reg, "candidate_runtime_dirs", lambda: [rt, legacy])
        q, release = _CTX.Queue(), _CTX.Event()
        holder = _CTX.Process(
            target=_child_register_hold,
            args=(str(legacy), str(db), q, release),
        )
        holder.start()
        try:
            _, registered, child_pid = _drain_until(q, "registered")
            assert registered

            result = reg.enumerate_live_instances(reg.store_digest_for(db))
            assert result.complete
            assert [item.pid for item in result.instances] == [child_pid]
            assert reg.probe_all_for_uninstall().state == "LIVE"
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_two_children_same_store_sorted(self, rt, db):
        qs = [_CTX.Queue() for _ in range(2)]
        release = _CTX.Event()
        procs = [
            _CTX.Process(target=_child_register_hold, args=(str(rt), str(db), q, release))
            for q in qs
        ]
        for p in procs:
            p.start()
        try:
            pids = sorted(_drain_until(q, "registered")[2] for q in qs)
            result = reg.enumerate_live_instances(reg.store_digest_for(db))
            assert result.complete
            assert [i.pid for i in result.instances] == pids
        finally:
            release.set()
            for p in procs:
                p.join(timeout=30)
                _stop(p)

    def test_child_sees_itself_and_sibling(self, rt, db):
        """Self-inclusion without probing, verified from inside a child
        that both registers and enumerates."""
        q1, q2 = _CTX.Queue(), _CTX.Queue()
        release = _CTX.Event()
        holder = _CTX.Process(target=_child_register_hold, args=(str(rt), str(db), q1, release))
        holder.start()
        try:
            _, ok, holder_pid = _drain_until(q1, "registered")
            assert ok
            enumerator = _CTX.Process(
                target=_child_register_and_enumerate, args=(str(rt), str(db), q2, release)
            )
            enumerator.start()
            try:
                _, complete, seen, enum_pid = _drain_until(q2, "enumerated")
                assert complete
                assert sorted(p for p, _ in seen) == sorted([holder_pid, enum_pid])
            finally:
                release.set()
                enumerator.join(timeout=30)
                _stop(enumerator)
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)

    def test_killed_child_probes_stale_then_ages_out(self, rt, db):
        q = _CTX.Queue()
        child = _CTX.Process(target=_child_register_hold_forever, args=(str(rt), str(db), q))
        child.start()
        try:
            _, ok, _ = _drain_until(q, "registered")
            assert ok
            digest = reg.store_digest_for(db)
            assert reg.enumerate_live_instances(digest).instances != ()
        finally:
            _stop(child)  # kill() + bounded join — portable, no SIGKILL name

        # flock released by the kernel on death → probes stale; fresh
        # mtime keeps it through the grace window
        result = reg.enumerate_live_instances(digest)
        assert result.complete and result.instances == ()
        d = reg.instances_dir()
        leftovers = list(d.iterdir())
        assert len(leftovers) == 1
        # age it past the grace period → next pass GCs it
        old = time.time() - reg._STALE_GRACE_S - 10
        os.utime(leftovers[0], (old, old))
        reg.enumerate_live_instances(digest)
        assert list(d.iterdir()) == []

    def test_child_held_sidecar_times_out_fail_open_and_fail_closed(self, rt, db, monkeypatch):
        monkeypatch.setattr(reg, "_LOCK_TIMEOUT_S", 0.3)
        q = _CTX.Queue()
        release = _CTX.Event()
        # something must exist for the probes to need the lock for
        reg.instances_dir().mkdir(parents=True, exist_ok=True)
        (reg.instances_dir() / "whatever").touch()
        holder = _CTX.Process(target=_child_hold_sidecar, args=(str(rt), q, release))
        holder.start()
        try:
            _drain_until(q, "held")
            # status surface: fail-open (no warning material, no hang)
            result = reg.enumerate_live_instances("0" * 16)
            assert not result.complete
            # registration: fail-open (None, server still starts)
            assert reg.register_instance(db) is None
            # uninstall surface: fail-closed
            assert reg.probe_all_for_uninstall().state == "UNKNOWN"
        finally:
            release.set()
            holder.join(timeout=30)
            _stop(holder)


# ------------------------------------------------------------- fork contract


@pytest.mark.skipif(os.name == "nt", reason="fork is POSIX-only")
class TestForkContract:
    def test_forked_child_normal_exit_cannot_unlink_parent_sentinel(self, rt, db):
        """Real interpreter-exit path: a spawned worker registers, forks,
        and the forked grandchild exits *normally* (``sys.exit(0)`` →
        the inherited atexit stack, including the registry handler,
        runs). The pid guard makes the inherited cleanup a no-op, so the
        worker's sentinel must survive and stay live."""
        q = _CTX.Queue()
        release = _CTX.Event()
        worker = _CTX.Process(
            target=_child_register_fork_grandchild, args=(str(rt), str(db), q, release)
        )
        worker.start()
        try:
            _, survived, grand_code, worker_pid = _drain_until(q, "forked")
            assert grand_code == 0
            assert survived, "sentinel must survive the grandchild's normal exit"
            # cross-process view: the worker's registration is still live
            result = reg.enumerate_live_instances(reg.store_digest_for(db))
            assert [i.pid for i in result.instances] == [worker_pid]
        finally:
            release.set()
            worker.join(timeout=30)
            _stop(worker)


class TestListingUnderMutationLock:
    def test_both_probes_list_the_directory_only_while_holding_the_lock(self, rt, db, monkeypatch):
        """A directory snapshot taken outside the mutation lock can miss a
        registrar that publishes right after it (uninstall would judge
        NONE from a stale view). Pin the ordering structurally: every
        ``instances_dir()`` resolution inside the probes happens while
        the intra-process mutation lock is held."""
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            real_dir = reg.instances_dir
            held: list[bool] = []

            def spying_dir():
                held.append(reg._mutation_thread_lock.locked())
                return real_dir()

            monkeypatch.setattr(reg, "instances_dir", spying_dir)
            assert reg.probe_all_for_uninstall().state == "LIVE"
            result = reg.enumerate_live_instances(reg.store_digest_for(db))
            assert result.complete
        finally:
            inst.cleanup()
        assert held and all(held)

    def test_probe_lock_oserror_is_unknown_not_live(self, rt, db, monkeypatch):
        """A generic I/O failure during the flock probe is uncertainty —
        claiming 'live' would fabricate a concurrent-writer warning."""
        d = reg.instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = d / f"12345-1-{'e' * 16}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()

        real_lock = reg.portalocker.lock

        def flaky_lock(fp, flags):
            if getattr(fp, "name", "").endswith("bbbbbbbb.lock"):
                raise OSError("disk went away")
            return real_lock(fp, flags)

        monkeypatch.setattr(reg.portalocker, "lock", flaky_lock)
        assert reg._probe_entry(entry) == "unknown"
        assert reg.probe_all_for_uninstall().state == "UNKNOWN"
        result = reg.enumerate_live_instances("e" * 16)
        assert not result.complete
        assert result.instances == ()


class TestSymlinkedRegistryDir:
    def test_symlinked_instances_dir_is_never_trusted_or_traversed(self, rt, tmp_path):
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        (victim_dir / "precious.txt").write_text("do not touch")
        reg.ensure_runtime_dir()
        try:
            reg.instances_dir().symlink_to(victim_dir)
        except OSError:
            pytest.skip("symlinks unavailable")
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == reg.instances_dir()
        # detail is producer-scoped: only the runtime-dir refusal sets it.
        # The redirected instances dir's cause is already in the generic
        # sentence, so it stays None (#1948).
        assert verdict.detail is None
        result = reg.enumerate_live_instances("0" * 16)
        assert not result.complete
        assert result.instances == ()
        assert (victim_dir / "precious.txt").read_text() == "do not touch"


class TestDanglingSymlinkedRegistryDir:
    def test_dangling_symlink_is_untrusted_not_missing(self, rt, tmp_path):
        """A dangling ``instances`` symlink must read as *untrusted*:
        collapsing it into 'missing' (via a follow-the-link exists())
        would let the fail-closed uninstall probe answer NONE against a
        registry it cannot actually see — and collapsing it into
        UNKNOWN would prescribe "retry" for a link only removal can
        clear (#1942)."""
        reg.ensure_runtime_dir()
        try:
            reg.instances_dir().symlink_to(tmp_path / "no-such-target")
        except OSError:
            pytest.skip("symlinks unavailable")
        verdict = reg.probe_all_for_uninstall()
        assert verdict.state == "UNTRUSTED"
        assert verdict.untrusted_path == reg.instances_dir()
        result = reg.enumerate_live_instances("0" * 16)
        assert not result.complete
        assert result.instances == ()


class TestUninstallProbeResultInvariant:
    """``untrusted_path`` <-> ``UNTRUSTED``, and ``untrusted_kind`` /
    ``detail`` only alongside it, enforced at construction (#1948,
    #1938). Each guard is asserted on its own so a sibling cannot mask a
    regression."""

    def test_untrusted_without_path_is_rejected(self):
        with pytest.raises(ValueError):
            reg.UninstallProbeResult("UNTRUSTED")

    def test_path_without_untrusted_state_is_rejected(self):
        with pytest.raises(ValueError):
            reg.UninstallProbeResult("NONE", untrusted_path=Path("/x"))

    def test_detail_without_untrusted_state_is_rejected(self):
        with pytest.raises(ValueError):
            reg.UninstallProbeResult("UNKNOWN", detail="whatever")

    def test_kind_without_untrusted_state_is_rejected(self):
        with pytest.raises(ValueError):
            reg.UninstallProbeResult("UNKNOWN", untrusted_kind="unprobeable")

    def test_untrusted_with_path_and_kind_is_accepted(self):
        result = reg.UninstallProbeResult(
            "UNTRUSTED", untrusted_path=Path("/x"), untrusted_kind="unprobeable"
        )
        assert result.untrusted_kind == "unprobeable"

    def test_untrusted_with_path_and_detail_is_accepted(self):
        result = reg.UninstallProbeResult("UNTRUSTED", untrusted_path=Path("/x"), detail="cause")
        assert result.untrusted_path == Path("/x")
        assert result.detail == "cause"

    def test_untrusted_with_path_and_no_detail_is_accepted(self):
        assert reg.UninstallProbeResult("UNTRUSTED", untrusted_path=Path("/x")).detail is None

    @pytest.mark.parametrize("state", ["NONE", "LIVE", "UNKNOWN"])
    def test_non_untrusted_states_construct_bare(self, state):
        result = reg.UninstallProbeResult(state)
        assert result.untrusted_path is None
        assert result.detail is None


class TestSnapshotAllInstances:
    """``snapshot_all_instances`` — the all-store, read-only view (#2226).

    The store-scoped enumerator answers "who else writes my store"; this one
    answers "what is running on this host", which is the question no existing
    surface could ask. Its defining constraint is that it observes without
    changing anything: a diagnostic that garbage-collects cannot be run twice
    to compare, and one that creates coordination state alters the machine it
    was asked to inspect.
    """

    @staticmethod
    def _instances_dir(rt: Path) -> Path:
        """Create the registry the way a real server would: 0o700 root.

        ``mkdir(parents=True)`` would make the root at umask (0o755), which the
        snapshot's own validation correctly refuses — the runtime dir is
        owner-only by contract.
        """
        rt.mkdir(mode=0o700, exist_ok=True)
        d = reg.instances_dir()
        d.mkdir(exist_ok=True)
        return d

    def test_spans_stores_that_the_scoped_enumerator_filters_out(self, rt, db):
        digest_a = reg.store_digest_for(db)
        digest_b = "b" * 16
        self._instances_dir(rt)
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            snap = reg.snapshot_all_instances()
            assert digest_a in {i.digest for i in snap.instances}
            # The scoped view of an unrelated store sees nothing, which is
            # exactly how 29 servers can be invisible on a real machine.
            assert reg.enumerate_live_instances(digest_b).instances == ()
        finally:
            inst.cleanup()

    def test_does_not_collect_an_aged_stale_sentinel(self, rt, db):
        """Mutation-validation: the scoped enumerator GCs this same fixture."""
        digest = reg.store_digest_for(db)
        d = self._instances_dir(rt)
        entry = d / f"12345-1-{digest}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        aged = time.time() - reg._STALE_GRACE_S - 10
        os.utime(entry, (aged, aged))

        snap = reg.snapshot_all_instances()
        assert entry.exists(), "snapshot must observe, not collect"
        assert snap.stale_seen == 1
        assert snap.unlocked_fresh_seen == 0

        # The pin: the very same entry is collected by the scoped enumerator,
        # so the assertion above is about this function, not about the fixture.
        reg.enumerate_live_instances(digest)
        assert not entry.exists()

    def test_counts_a_fresh_unlocked_sentinel_separately_from_stale(self, rt, db):
        digest = reg.store_digest_for(db)
        d = self._instances_dir(rt)
        entry = d / f"12345-1-{digest}-aaaaaaaa-bbbbbbbb.lock"
        entry.touch()
        snap = reg.snapshot_all_instances()
        assert snap.unlocked_fresh_seen == 1
        assert snap.stale_seen == 0
        assert entry.exists()

    def test_absent_runtime_dir_stays_absent(self, rt):
        assert not rt.exists()
        snap = reg.snapshot_all_instances()
        assert snap.instances == ()
        assert snap.complete
        assert not rt.exists(), "a read must not create the runtime dir"

    def test_does_not_create_the_mutation_sidecar(self, rt, db):
        rt.mkdir(mode=0o700, exist_ok=True)
        sidecar = reg.registry_sidecar_path()
        assert not sidecar.exists()
        reg.snapshot_all_instances()
        assert not sidecar.exists(), "a read must not take (or create) the sidecar"

    def test_untrusted_root_is_not_traversed(self, rt, db, tmp_path):
        """A symlinked runtime dir must be refused before anything under it."""
        real = tmp_path / "elsewhere"
        (real / "instances").mkdir(parents=True)
        digest = reg.store_digest_for(db)
        (real / "instances" / f"999-1-{digest}-aaaaaaaa-bbbbbbbb.lock").touch()
        rt.parent.mkdir(parents=True, exist_ok=True)
        rt.symlink_to(real, target_is_directory=True)

        snap = reg.snapshot_all_instances()
        assert snap.instances == (), "a redirected root must not be read"
        assert snap.canonical_error is not None
        assert not snap.complete

    def test_own_registration_included_without_probing(self, rt, db, monkeypatch):
        inst = reg.register_instance(db)
        assert inst is not None
        try:
            monkeypatch.setattr(
                reg,
                "_probe_entry",
                lambda p: pytest.fail(f"own entry must not be probed: {p}"),
            )
            snap = reg.snapshot_all_instances()
            assert [i.pid for i in snap.instances] == [os.getpid()]
        finally:
            inst.cleanup()

    def test_unparseable_held_entry_makes_the_count_a_lower_bound(self, rt, db, monkeypatch):
        d = self._instances_dir(rt)
        entry = d / "not-a-valid-sentinel-name.lock"
        entry.touch()
        monkeypatch.setattr(reg, "_probe_entry", lambda p: "live")
        snap = reg.snapshot_all_instances()
        assert snap.unparseable_seen == 1
        assert not snap.complete, "an unattributable holder means we counted low"


class TestPresenceMarkers:
    """Startup presence markers (#2230): the population sentinels cannot see."""

    def test_marker_filename_parses_back_with_the_path_digest(self, rt, db):
        inst = reg.register_server_presence(db)
        assert inst is not None
        try:
            assert inst.path.parent == reg.presence_dir()
            info = reg._parse_entry(inst.path)
            assert info is not None
            assert info.pid == os.getpid()
            assert info.ppid == os.getppid()
            # The whole point of the second digest: it is the *path text*
            # key that already names the pid file, not the store's inode
            # identity, because at startup no store exists to stat.
            assert info.digest == store_pid_digest(db)
            assert info.digest != reg.store_digest_for(db)
        finally:
            inst.cleanup()

    def test_marker_path_is_reserved_before_publication(self, rt, db):
        reserved: list[Path] = []

        def _reserve(path: Path) -> None:
            assert not path.exists()
            reserved.append(path)

        inst = reg.register_server_presence(db, on_path_reserved=_reserve)
        assert inst is not None
        try:
            assert reserved == [inst.path]
            assert inst.path.exists()
        finally:
            inst.cleanup()

    def test_unnamed_store_still_registers(self, rt):
        """A store with no path must not silently drop the process."""
        inst = reg.register_server_presence(":memory:")
        assert inst is not None
        try:
            info = reg._parse_entry(inst.path)
            assert info is not None and info.digest == reg._UNKNOWN_STORE_DIGEST
        finally:
            inst.cleanup()

    def test_missing_store_file_still_registers(self, rt, tmp_path):
        """The startup case itself: the DB is configured but not yet created."""
        absent = tmp_path / "not-created-yet.db"
        assert reg.store_digest_for(absent) is None, "premise: no inode digest exists"
        inst = reg.register_server_presence(absent)
        assert inst is not None
        try:
            assert reg._parse_entry(inst.path).digest == store_pid_digest(absent)
        finally:
            inst.cleanup()

    def test_cleanup_removes_the_marker_and_unpublishes_it(self, rt, db):
        inst = reg.register_server_presence(db)
        assert inst is not None
        path = inst.path
        inst.cleanup()
        assert not path.exists()
        assert path not in reg._active_presence
        assert reg.snapshot_all_instances().presence == ()

    def test_symlinked_presence_dir_is_refused(self, rt, tmp_path, db):
        rt.mkdir(mode=0o700, exist_ok=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        try:
            reg.presence_dir().symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        assert reg.register_server_presence(db) is None
        assert list(elsewhere.iterdir()) == [], "must not write through the link"

    def test_stale_marker_is_collected_by_a_later_registration(self, rt, db):
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        stale = directory / f"999999-1-{'b' * 16}-{'c' * 8}-{'d' * 8}.lock"
        stale.touch()
        # Backdate past the publication grace window rather than sleeping:
        # a fresh unlocked entry is a registrar mid-publication, not residue.
        old = time.time() - (reg._STALE_GRACE_S + 60)
        os.utime(stale, (old, old))
        inst = reg.register_server_presence(db)
        try:
            assert not stale.exists(), "an unlocked, aged marker is residue"
        finally:
            if inst is not None:
                inst.cleanup()

    def test_fresh_unlocked_marker_survives_registration(self, rt, db):
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fresh = directory / f"999999-1-{'b' * 16}-{'c' * 8}-{'d' * 8}.lock"
        fresh.touch()
        inst = reg.register_server_presence(db)
        try:
            assert fresh.exists(), "inside the grace window it may be mid-publication"
        finally:
            if inst is not None:
                inst.cleanup()


class TestPresenceStaysOutOfTheSentinelPopulation:
    """A marker is weaker evidence than a sentinel and must not stand in for one."""

    def test_snapshot_reports_the_two_populations_separately(self, rt, db):
        marker = reg.register_server_presence(db)
        sentinel = reg.register_instance(db)
        assert marker is not None and sentinel is not None
        try:
            snap = reg.snapshot_all_instances()
            assert len(snap.instances) == 1
            assert len(snap.presence) == 1
            # procid is the join key: one process, two records.
            assert snap.instances[0].procid == snap.presence[0].procid
            assert snap.presence[0].digest != snap.instances[0].digest
        finally:
            sentinel.cleanup()
            marker.cleanup()

    def test_marker_alone_is_not_a_same_store_writer(self, rt, db):
        """``mm status``'s concurrent-writer signal must not fire on idle servers."""
        marker = reg.register_server_presence(db)
        assert marker is not None
        try:
            digest = reg.store_digest_for(db)
            assert digest is not None
            result = reg.enumerate_live_instances(digest)
            assert result.instances == ()
            assert result.complete
        finally:
            marker.cleanup()

    def test_marker_alone_does_not_block_uninstall(self, rt, db):
        """Uninstall keeps deciding on its own probe, not on a startup marker."""
        marker = reg.register_server_presence(db)
        assert marker is not None
        try:
            assert reg.probe_all_for_uninstall().state == "NONE"
        finally:
            marker.cleanup()


class TestPresenceSweep:
    """The destructive boundary collects residue without touching live markers."""

    def test_sweep_leaves_a_live_marker_alone(self, rt, db):
        """An idle server does not block uninstall, so it must survive it.

        Staging a held marker would unregister a running process — and on
        Windows would fail outright on the open handle.
        """
        inst = reg.register_server_presence(db)
        assert inst is not None
        try:
            reg.sweep_stale_presence()
            assert inst.path.exists()
        finally:
            inst.cleanup()

    def test_sweep_collects_an_abandoned_marker(self, rt):
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        residue = directory / f"999999-1-{'b' * 16}-{'c' * 8}-{'d' * 8}.lock"
        residue.touch()
        old = time.time() - (reg._STALE_GRACE_S + 60)
        os.utime(residue, (old, old))
        reg.sweep_stale_presence()
        assert not residue.exists()

    def test_sweep_is_inert_when_nothing_was_ever_registered(self, rt):
        reg.sweep_stale_presence()
        assert not reg.presence_dir().exists(), "a sweep must not create the directory"

    def test_sweep_prunes_the_emptied_directory(self, rt):
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        residue = directory / f"999999-1-{'b' * 16}-{'c' * 8}-{'d' * 8}.lock"
        residue.touch()
        old = time.time() - (reg._STALE_GRACE_S + 60)
        os.utime(residue, (old, old))
        reg.sweep_stale_presence()
        assert not directory.exists()

    def test_sweep_prunes_under_the_lock_that_registration_holds(self, rt, db, monkeypatch):
        """The prune must not land between a registration's mkdir and its open.

        Registration creates the directory and opens its marker inside one
        locked span. An ``rmdir`` from an unlocked second pass could slip
        between the two, and the registration would fail on a directory that
        vanished underneath it — leaving a live server invisible to
        ``mm doctor``, which is the exact failure this feature exists to fix.
        """
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        swept: list[str] = []
        real_rmdir = Path.rmdir

        def _rmdir(self):
            swept.append(str(self))
            # The lock is held for this call, or the race is open.
            assert reg._mutation_thread_lock.locked(), (
                "the prune ran outside the registry mutation lock"
            )
            return real_rmdir(self)

        monkeypatch.setattr(Path, "rmdir", _rmdir)
        reg.sweep_stale_presence()
        assert swept, "premise: the prune was attempted"

    def test_sweep_leaves_a_directory_that_still_holds_a_marker(self, rt, db):
        inst = reg.register_server_presence(db)
        assert inst is not None
        try:
            reg.sweep_stale_presence()
            assert reg.presence_dir().exists(), "rmdir refuses a non-empty directory"
            assert inst.path.exists()
        finally:
            inst.cleanup()

    def test_sweep_refuses_a_redirected_anchor(self, rt, tmp_path, monkeypatch):
        """A junctioned runtime root holds an ordinary ``presence/`` inside the
        *target*, which passes every check made on the leaf alone."""
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        residue = directory / f"999999-1-{'b' * 16}-{'c' * 8}-{'d' * 8}.lock"
        residue.touch()
        old = time.time() - (reg._STALE_GRACE_S + 60)
        os.utime(residue, (old, old))
        monkeypatch.setattr(reg, "_dir_state", lambda p: "untrusted" if p == rt else "dir")

        reg.sweep_stale_presence()

        assert residue.exists(), "nothing under a redirected anchor may be touched"


class TestPresenceRegistrationBudget:
    def test_startup_gives_up_quickly_rather_than_retrying(self, rt, db, monkeypatch):
        """Startup latency, not registry completeness, is what bounds this.

        ``register_instance`` retries because a lost sentinel leaves a *writer*
        invisible; a lost marker costs one row in a diagnostic. This runs ahead
        of ``mcp.run()``, so it must not spend the sentinel budget.
        """
        attempts = []

        @contextlib.contextmanager
        def _always_timeout(deadline, *, root=None):
            attempts.append(deadline)
            raise reg._MutationLockTimeout("busy")
            yield  # pragma: no cover

        monkeypatch.setattr(reg, "_mutation_lock", _always_timeout)
        started = time.monotonic()
        assert reg.register_server_presence(db) is None
        assert len(attempts) == 1, "one attempt only"
        # Pin the budget the call actually passed, not merely that the shorter
        # constant exists: reverting the call site to ``_LOCK_TIMEOUT_S`` must
        # fail here. The deadline is monotonic, so compare it against the
        # instant before the call.
        assert reg._PRESENCE_LOCK_TIMEOUT_S < reg._LOCK_TIMEOUT_S
        budget = attempts[0] - started
        assert budget <= reg._PRESENCE_LOCK_TIMEOUT_S + 0.2
        assert budget < reg._LOCK_TIMEOUT_S

    def test_gc_touches_no_further_entry_once_the_deadline_passes(self, rt, db, monkeypatch):
        """The budget must bound the work done *under* the lock, not only the
        acquisition of it.

        What is pinned is the loop, not wall-clock: the bound is cooperative
        and per-entry, so a probe already in flight finishes. The guarantee is
        that no *further* entry is touched — the sweep cannot run the
        directory's length while holding the shared lock.

        The sweep runs with the shared mutation lock held — the same lock that
        serializes sentinel registration, the concurrent-writer enumeration and
        the uninstall probe — on a startup path a client is waiting on. A host
        with a large residue directory (exactly the host this feature is for)
        would otherwise let one probe loop hold it for as long as the directory
        is long.
        """
        directory = reg.presence_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        old = time.time() - (reg._STALE_GRACE_S + 60)
        residue = []
        for i in range(40):
            entry = directory / f"9{i:05d}-1-{'b' * 16}-{'c' * 8}-{i:08x}.lock"
            entry.touch()
            os.utime(entry, (old, old))
            residue.append(entry)

        probes = []
        real_probe = reg._probe_entry

        def _slow_probe(path):
            probes.append(path)
            # Burn the budget on the first entry, so every later one is past
            # the deadline and must not be probed at all.
            if len(probes) == 1:
                monkeypatch.setattr(
                    reg.time, "monotonic", lambda base=time.monotonic(): base + 3600
                )
            return real_probe(path)

        monkeypatch.setattr(reg, "_probe_entry", _slow_probe)
        inst = reg.register_server_presence(db)
        try:
            assert len(probes) == 1, f"the sweep must stop at the deadline, probed {len(probes)}"
            assert sum(1 for e in residue if e.exists()) >= 39, (
                "un-swept residue stays for the next registration; it is already inert"
            )
            # Registration itself is never skipped by a spent budget — the
            # process must still be counted.
            assert inst is not None and inst.path.exists()
        finally:
            if inst is not None:
                inst.cleanup()
