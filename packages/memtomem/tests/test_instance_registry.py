"""Instance registry (#1935): registration, probing, GC, and fail-closed probes.

Lock behavior is validated **cross-process** (spawn) per the repo
convention (see ``test_locking_contention.py``): portalocker delegates to
``fcntl.flock`` / ``LockFileEx``, both process-level, and Windows can even
grant a second same-process handle — so in-process contention proves
nothing. In-process tests here cover only pure parsing, state, and
fail-open/fail-closed decision logic.
"""

from __future__ import annotations

import errno
import multiprocessing as mp
import os
import time
from pathlib import Path

import pytest

import memtomem._instance_registry as reg

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


class TestEnumerationInProcess:
    def test_missing_dir_is_complete_and_empty(self, rt):
        result = reg.enumerate_live_instances("0" * 16)
        assert result.complete and result.instances == ()

    def test_own_registration_included_without_probing(self, rt, db):
        """Windows same-process handles can acquire the flock
        (``indexing/debounce.py``) — self must never be probed stale."""
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
