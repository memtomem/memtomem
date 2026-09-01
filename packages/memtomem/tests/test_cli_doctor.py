"""Tests for ``mm doctor`` — the runtime-axis health command (#2226).

The command is patched at its seams (``snapshot_all_instances``, ``probe_pid``,
``validate_runtime_dir``) rather than driven through a real registry, following
``test_cli_status.py``'s concurrent-writer tests: the registry's own behavior is
covered in ``test_instance_registry.py``, and what matters here is how a given
snapshot is *reported*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem._instance_registry import InstanceInfo, RegistrySnapshot
from memtomem.cli import cli
from memtomem.cli import doctor_cmd


def _snapshot(instances=(), **kw) -> RegistrySnapshot:
    defaults = dict(
        instances=tuple(instances),
        complete=True,
        stale_seen=0,
        unlocked_fresh_seen=0,
        unparseable_seen=0,
        roots_consulted=1,
        canonical_error=None,
        refusal=None,
    )
    defaults.update(kw)
    return RegistrySnapshot(**defaults)


def _info(pid: int, ppid: int = 500, digest: str = "a" * 16, procid: str | None = None):
    return InstanceInfo(
        pid=pid,
        ppid=ppid,
        digest=digest,
        procid=procid or f"{pid:08x}",
        path=Path(f"/nonexistent/{pid}.lock"),
    )


def _row(*, procid: str, digest: str = "a" * 16, age: float | None = 1.0, pid: int = 1000):
    return doctor_cmd.InstanceRow(
        pid=pid,
        procid=procid,
        recorded_ppid=500,
        store_digest=digest,
        age_seconds=age,
        recorded_parent="alive",
        recorded_ppid_is_one=False,
    )


@pytest.fixture
def seeded(monkeypatch):
    """Patch the three seams; ages default to a fixed, readable value."""

    def _seed(snapshot, *, parent="alive", age=3600.0, runtime_ok=True):
        monkeypatch.setattr(doctor_cmd, "snapshot_all_instances", lambda: snapshot)
        monkeypatch.setattr(doctor_cmd, "probe_pid", lambda pid: parent)
        monkeypatch.setattr(doctor_cmd, "validate_runtime_dir", lambda p: runtime_ok)

        class _Stat:
            st_mtime = 0.0

        if age is None:
            monkeypatch.setattr(
                Path, "stat", lambda self, **kw: (_ for _ in ()).throw(OSError("gone"))
            )
        else:
            monkeypatch.setattr(doctor_cmd.time, "time", lambda: age)
            monkeypatch.setattr(Path, "stat", lambda self, **kw: _Stat())

    return _seed


def _run(args=()):
    return CliRunner().invoke(cli, ["doctor", *args])


def _payload(result):
    return json.loads(result.output)


class TestReportsAccumulationWithoutAnOrphanGate:
    def test_live_parents_still_surface_the_count_and_age(self, seeded):
        """The measured real-world case: many servers, every parent alive.

        An orphan-keyed report calls this machine clean, which is precisely how
        29 resident servers stayed invisible. Count and age must be stated
        regardless of what the parent probe says.
        """
        seeded(_snapshot(_info(1000 + i, digest=f"{i:016x}") for i in range(12)))
        result = _run()
        assert result.exit_code == 0
        assert "12 live server processes" in result.output
        assert "all recorded parents alive" in result.output
        # Accumulation is worth flagging, but never as a failure.
        assert "!" in result.output

    def test_small_healthy_count_is_neutral_not_a_warning(self, seeded):
        seeded(_snapshot([_info(1000), _info(1001)]))
        result = _run()
        assert result.exit_code == 0
        assert "2 live server processes" in result.output
        assert _payload(_run(["--json"]))["status"] == "pass"

    def test_zero_instances_is_a_clean_pass(self, seeded):
        seeded(_snapshot())
        result = _run()
        assert result.exit_code == 0
        assert "no live server processes" in result.output
        assert _payload(_run(["--json"]))["status"] == "pass"

    def test_zero_from_an_incomplete_scan_is_not_stated_as_an_absence(self, seeded):
        """Zero is a lower bound like any other count when the scan failed."""
        seeded(_snapshot(complete=False))
        out = _run().output
        assert "lower bound" in out
        assert _payload(_run(["--json"]))["status"] == "warn"


class TestProcessIdentity:
    def test_one_process_registered_against_two_stores_counts_once(self, seeded):
        rows = [
            _info(1000, digest="a" * 16, procid="deadbeef"),
            _info(1000, digest="b" * 16, procid="deadbeef"),
        ]
        seeded(_snapshot(rows))
        result = _run()
        assert "1 live server process across 2 stores" in result.output
        data = _payload(_run(["--json"]))["checks"][1]["data"]
        assert data["processes"] == 1
        assert data["registrations"] == 2


class TestParentAnnotation:
    def test_missing_recorded_parent_is_annotated_not_gated(self, seeded):
        seeded(_snapshot([_info(1000)]), parent="dead")
        result = _run()
        assert result.exit_code == 0, "a gone parent is an observation, not a failure"
        assert "1 gone" in result.output
        assert _payload(_run(["--json"]))["instances"][0]["recorded_parent"] == "missing"

    def test_unknown_probe_is_neither_alive_nor_missing(self, seeded):
        seeded(_snapshot([_info(1000)]), parent="unknown")
        payload = _payload(_run(["--json"]))
        assert payload["instances"][0]["recorded_parent"] == "unknown"
        assert payload["checks"][1]["data"]["missing_recorded_parents"] == 0

    def test_all_unknown_parents_is_never_reported_as_all_alive(self, seeded):
        """The overclaim the tri-state probe exists to prevent.

        Learning nothing about any parent must not read as "everything is fine";
        that is indistinguishable from a genuinely healthy machine.
        """
        seeded(_snapshot([_info(1000), _info(1001)]), parent="unknown")
        out = _run().output
        assert "all recorded parents alive" not in out
        assert "could not be probed" in out
        assert _payload(_run(["--json"]))["checks"][1]["data"]["unknown_recorded_parents"] == 2

    def test_mixed_missing_and_unknown_are_reported_separately(self, seeded, monkeypatch):
        rows = [_info(1000, ppid=10), _info(1001, ppid=11)]
        seeded(_snapshot(rows))
        monkeypatch.setattr(doctor_cmd, "probe_pid", lambda pid: "dead" if pid == 10 else "unknown")
        out = _run().output
        assert "1 gone" in out
        assert "1 could not be probed" in out
        assert "0 alive" in out, "the alive count is stated even when it is zero"
        data = _payload(_run(["--json"]))["checks"][1]["data"]
        assert (data["missing_recorded_parents"], data["unknown_recorded_parents"]) == (1, 1)

    def test_parent_states_are_counted_per_process_not_per_registration(self, seeded):
        """One process holding three stores has one parent, not three.

        Counting registrations would make the parent tallies disagree with the
        process count printed beside them.
        """
        rows = [_info(1000, digest=f"{i:016x}", procid="deadbeef") for i in range(3)]
        seeded(_snapshot(rows), parent="dead")
        data = _payload(_run(["--json"]))["checks"][1]["data"]
        assert data["processes"] == 1
        assert data["registrations"] == 3
        assert data["missing_recorded_parents"] == 1, "one process, one parent"

    def test_a_process_is_probed_once_and_its_rows_agree(self, seeded, monkeypatch):
        """Probing per registration lets one process report two parent states.

        A parent exiting between probes would otherwise leave a single process
        holding rows that say both "alive" and "missing", and any summary over
        them would be picking one arbitrarily.
        """
        rows = [_info(1000, digest=f"{i:016x}", procid="deadbeef") for i in range(3)]
        seeded(_snapshot(rows))
        answers = iter(["alive", "dead", "dead"])
        calls = {"n": 0}

        def _flaky(pid):
            calls["n"] += 1
            return next(answers)

        monkeypatch.setattr(doctor_cmd, "probe_pid", _flaky)
        payload = _payload(_run(["--json"]))
        assert calls["n"] == 1, "one probe per process, not per registration"
        states = {r["recorded_parent"] for r in payload["instances"]}
        assert states == {"alive"}, "a process's rows must not disagree with each other"

    def test_mixed_ppids_for_one_process_pair_each_row_with_its_own_parent(
        self, seeded, monkeypatch
    ):
        """Registrations capture os.getppid() independently.

        A process reparented between two registrations records two different
        parents, so a procid-only probe cache would pair one row's ppid with a
        state probed from the other's.
        """
        rows = [
            _info(1000, ppid=500, digest="a" * 16, procid="deadbeef"),
            _info(1000, ppid=1, digest="b" * 16, procid="deadbeef"),
        ]
        seeded(_snapshot(rows))
        monkeypatch.setattr(doctor_cmd, "probe_pid", lambda pid: "alive" if pid == 500 else "dead")
        instances = _payload(_run(["--json"]))["instances"]
        by_ppid = {r["recorded_ppid"]: r["recorded_parent"] for r in instances}
        assert by_ppid == {500: "alive", 1: "missing"}

    def test_a_process_with_one_live_recorded_parent_counts_as_alive(self, seeded, monkeypatch):
        """The reduction must not depend on directory order."""
        rows = [
            _info(1000, ppid=1, digest="a" * 16, procid="deadbeef"),
            _info(1000, ppid=500, digest="b" * 16, procid="deadbeef"),
        ]
        seeded(_snapshot(rows))
        monkeypatch.setattr(doctor_cmd, "probe_pid", lambda pid: "alive" if pid == 500 else "dead")
        data = _payload(_run(["--json"]))["checks"][1]["data"]
        assert data["processes"] == 1
        assert (data["alive_recorded_parents"], data["missing_recorded_parents"]) == (1, 0)
        assert data["recorded_ppid_is_one"] == 1, "the ppid-1 flag still counts the process once"

    def test_alive_count_is_stated_when_some_parents_are_not(self, seeded, monkeypatch):
        seeded(_snapshot([_info(1000, ppid=10), _info(1001, ppid=11)]))
        monkeypatch.setattr(doctor_cmd, "probe_pid", lambda pid: "alive" if pid == 10 else "dead")
        out = _run().output
        assert "1 alive" in out and "1 gone" in out
        assert _payload(_run(["--json"]))["checks"][1]["data"]["alive_recorded_parents"] == 1

    def test_ppid_one_is_its_own_flag_not_a_missing_parent(self, seeded):
        seeded(_snapshot([_info(1000, ppid=1)]), parent="alive")
        payload = _payload(_run(["--json"]))
        row = payload["instances"][0]
        assert row["recorded_ppid_is_one"] is True
        assert row["recorded_parent"] == "alive"
        assert payload["checks"][1]["data"]["missing_recorded_parents"] == 0
        assert "parent pid of 1" in _run().output


class TestAgeAggregation:
    def test_unknown_ages_do_not_crash_and_are_reported(self, seeded):
        seeded(_snapshot([_info(1000)]), age=None)
        result = _run()
        assert result.exit_code == 0
        assert "1 live server process" in result.output
        assert "median age" not in result.output, "no distribution without a known age"
        assert _payload(_run(["--json"]))["instances"][0]["age_seconds"] is None

    def test_process_age_is_the_oldest_of_its_registrations(self):
        """One process, two stores, two mtimes — the older one defines its age.

        Otherwise a multi-store server contributes twice to the median and can
        be counted as "young" on the strength of its most recent registration.
        """
        rows = [
            _row(procid="deadbeef", digest="a" * 16, age=100.0),
            _row(procid="deadbeef", digest="b" * 16, age=900.0),
            _row(procid="feedface", digest="c" * 16, age=50.0),
        ]
        ages, unknown = doctor_cmd._process_ages(rows)
        assert ages == [900.0, 50.0], "one age per process, oldest registration wins"
        assert unknown == 0

    def test_a_process_with_no_readable_mtime_is_counted_not_invented(self):
        rows = [
            _row(procid="deadbeef", age=None),
            _row(procid="feedface", age=10.0),
        ]
        ages, unknown = doctor_cmd._process_ages(rows)
        assert ages == [10.0]
        assert unknown == 1

    def test_mixed_known_and_unknown_within_one_process_uses_the_known_one(self):
        rows = [
            _row(procid="deadbeef", digest="a" * 16, age=None),
            _row(procid="deadbeef", digest="b" * 16, age=700.0),
        ]
        ages, unknown = doctor_cmd._process_ages(rows)
        assert ages == [700.0]
        assert unknown == 0


class TestDegradedScans:
    def test_incomplete_enumeration_warns_and_says_lower_bound(self, seeded):
        seeded(_snapshot([_info(1000)], complete=False))
        result = _run()
        assert result.exit_code == 0
        assert "lower bound" in result.output
        assert _payload(_run(["--json"]))["status"] == "warn"

    def test_unusable_runtime_dir_fails_and_exits_nonzero(self, seeded):
        seeded(_snapshot(canonical_error=OSError("unsafe permissions 0o755")))
        result = _run()
        assert result.exit_code == 1
        assert "runtime directory is unusable" in result.output
        assert _payload(_run(["--json"]))["status"] == "fail"

    def test_absent_canonical_dir_does_not_deny_servers_found_elsewhere(self, seeded):
        """A pre-move server registers under a historical root and still counts."""
        seeded(_snapshot([_info(1000)]), runtime_ok=False)
        out = _run().output
        assert "no server has registered here" not in out
        assert "historical runtime directory" in out
        assert "1 live server process" in out

    def test_absent_canonical_dir_with_nothing_found_says_so(self, seeded):
        seeded(_snapshot(), runtime_ok=False)
        out = _run().output
        assert "no server has registered here" in out

    def test_refused_historical_root_warns_without_failing(self, seeded):
        seeded(_snapshot(refusal=(Path("/legacy/rt"), OSError("symlink"))))
        result = _run()
        assert result.exit_code == 0
        assert "historical runtime directory was refused" in result.output
        assert _payload(_run(["--json"]))["status"] == "warn"

    def test_unattributable_sentinel_warns(self, seeded):
        seeded(_snapshot([_info(1000)], unparseable_seen=1, complete=False))
        payload = _payload(_run(["--json"]))
        assert payload["status"] == "warn"
        assert payload["checks"][2]["data"]["unparseable"] == 1

    def test_incomplete_scan_never_reports_the_registry_as_clean(self, seeded):
        """Zero counters from a scan that could not finish is not evidence."""
        seeded(_snapshot([_info(1000)], complete=False))
        out = _run().output
        assert "instance registry clean" not in out
        assert "could not be fully assessed" in out
        assert _payload(_run(["--json"]))["checks"][2]["status"] == "warn"

    def test_hygiene_counts_from_an_incomplete_scan_are_lower_bounds_too(self, seeded):
        """The sibling server count says so; this check must not disagree."""
        seeded(_snapshot([_info(1000)], stale_seen=2, complete=False))
        out = _run().output
        assert "lower bounds" in out
        assert _payload(_run(["--json"]))["checks"][2]["status"] == "warn"

    def test_environment_derived_paths_cannot_smuggle_terminal_escapes(self, seeded):
        """A runtime-dir candidate is environment-derived and reaches a tty."""
        seeded(_snapshot(refusal=(Path("/legacy\x1b[2Jrt"), OSError("symlink"))))
        out = _run().output
        assert "\x1b" not in out, "control sequences must be escaped before printing"
        assert "\\x1b" in out
        # JSON keeps the real path: a consumer needs it and is not a terminal.
        assert "\x1b" in _payload(_run(["--json"]))["checks"][0]["data"]["refused"]

    def test_fresh_unlocked_sentinel_is_informational(self, seeded):
        seeded(_snapshot([_info(1000)], unlocked_fresh_seen=1))
        payload = _payload(_run(["--json"]))
        assert payload["status"] == "pass", "a server still starting up is healthy"
        assert payload["checks"][2]["data"]["unlocked_fresh"] == 1


class TestJsonContract:
    def test_instance_rows_carry_the_documented_fields(self, seeded):
        seeded(_snapshot([_info(1000)]))
        row = _payload(_run(["--json"]))["instances"][0]
        assert set(row) == {
            "pid",
            "procid",
            "recorded_ppid",
            "store_digest",
            "age_seconds",
            "recorded_parent",
            "recorded_ppid_is_one",
            "kind",
        }

    def test_top_level_shape(self, seeded):
        seeded(_snapshot([_info(1000)]))
        payload = _payload(_run(["--json"]))
        assert set(payload) == {"status", "checks", "instances", "complete"}
        assert [c["name"] for c in payload["checks"]] == [
            "runtime-dir",
            "server-instances",
            "registry-hygiene",
        ]


class TestHelpBoundary:
    def test_epilog_points_at_the_other_doctors(self):
        out = CliRunner().invoke(cli, ["doctor", "--help"]).output
        for other in ("mm memory doctor", "mm sync-doctor", "mm context settings-doctor"):
            assert other in out

    def test_registered_on_the_live_cli(self):
        assert "doctor" in cli.commands


class TestIdleServers:
    """Handshake-only servers (#2230) — the population the registry once missed."""

    def test_marker_only_process_is_counted_and_named(self, seeded):
        seeded(_snapshot(presence=(_info(1000),)))
        payload = _payload(_run(["--json"]))
        servers = payload["checks"][1]
        assert servers["data"]["processes"] == 1
        assert servers["data"]["processes_without_store_registration"] == 1
        assert servers["data"]["processes_with_store_registered"] == 0
        # Stated as an observation about records: a sentinel can also be
        # missing for a server that did open a store, since its registration
        # is best-effort.
        assert "no store registration observed" in servers["message"]
        assert "idle" not in servers["message"]

    def test_idle_process_does_not_inflate_the_store_count(self, seeded):
        # A marker's digest names configured path text, not an open store.
        seeded(_snapshot([_info(1000, digest="a" * 16)], presence=(_info(2000, digest="e" * 16),)))
        servers = _payload(_run(["--json"]))["checks"][1]
        assert servers["data"]["stores"] == 1, "only sentinels name a store"
        assert servers["data"]["processes"] == 2

    def test_one_process_in_both_populations_counts_once(self, seeded):
        # The join key is procid: a server that registered at startup and then
        # opened a store is one process, not two.
        seeded(
            _snapshot(
                [_info(1000, procid="deadbeef")],
                presence=(_info(1000, procid="deadbeef", digest="e" * 16),),
            )
        )
        servers = _payload(_run(["--json"]))["checks"][1]
        assert servers["data"]["processes"] == 1
        assert servers["data"]["processes_without_store_registration"] == 0
        assert servers["data"]["registrations"] == 2

    def test_rows_carry_their_kind_and_hide_a_marker_digest(self, seeded):
        seeded(_snapshot([_info(1000)], presence=(_info(2000),)))
        kinds = {r["pid"]: r["kind"] for r in _payload(_run(["--json"]))["instances"]}
        # The row names the record it came from, never a state inferred from
        # it — one process can legitimately produce both.
        assert kinds == {1000: "sentinel", 2000: "presence"}
        table = _run([]).output
        assert "presence" in table
        # The table must not print a marker's path digest where readers expect
        # a store identity they could match against a sentinel's.
        assert f"{'a' * 10}" in table and table.count("a" * 10) == 1

    def test_marker_residue_is_reported_as_hygiene(self, seeded):
        seeded(_snapshot(presence_stale_seen=2))
        hygiene = _payload(_run(["--json"]))["checks"][2]
        assert hygiene["data"]["presence_stale"] == 2
        assert hygiene["data"]["stale"] == 2, "both directories leak the same way"
        assert hygiene["status"] == "info"
