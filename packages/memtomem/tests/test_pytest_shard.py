"""Tests for deterministic CI pytest sharding."""

from __future__ import annotations

import importlib.util
import itertools
import os
import subprocess
import sys
import textwrap
import warnings
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "run_pytest_shard.py"
    spec = importlib.util.spec_from_file_location("run_pytest_shard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sharder = _load_tool()


def _write_map(durations: dict[str, float]) -> Path:
    """Write a cost map to a temp file and return its path."""
    import json
    import tempfile

    path = Path(tempfile.mkdtemp()) / "durations.json"
    path.write_text(json.dumps({"durations": durations}), encoding="utf-8")
    return path


@pytest.mark.parametrize("count", [1, 2, 3, 5, 11])
def test_shards_are_disjoint_and_exhaustive(count: int) -> None:
    """Every pair, not just adjacent ones.

    ``zip(shards, shards[1:])`` compares 0-1 and 1-2 but never 0-2, so a bug
    that duplicated a file into the first and last shard read as clean. One
    count is not enough either: the partition arithmetic differs at count=1
    (everything in one shard) and at counts that do not divide the file set.
    """
    files = [Path(f"test_{index}.py") for index in range(11)]
    shards = [sharder.shard_files(files, index=index, count=count) for index in range(count)]

    assert set().union(*map(set, shards)) == set(files)
    assert sum(len(shard) for shard in shards) == len(files)
    for left, right in itertools.combinations(shards, 2):
        assert set(left).isdisjoint(right)


@pytest.mark.parametrize(("index", "count"), [(-1, 2), (2, 2), (0, 0)])
def test_invalid_shard_coordinates_fail(index: int, count: int) -> None:
    with pytest.raises(ValueError):
        sharder.shard_files([Path("test_one.py")], index=index, count=count)


def test_repository_discovery_excludes_golden_path() -> None:
    files = sharder.test_files(_ROOT)

    assert files
    assert all(path.name != "test_golden_path.py" for path in files)
    assert files == sorted(files)


def test_discovery_matches_both_pytest_patterns(tmp_path: Path) -> None:
    tests_root = tmp_path / "packages" / "memtomem" / "tests"
    tests_root.mkdir(parents=True)
    for name in ("test_prefix.py", "example_test.py", "test_golden_path.py", "conftest.py"):
        (tests_root / name).touch()

    names = {path.name for path in sharder.test_files(tmp_path)}

    # Both default pytest patterns are covered; golden-path and non-tests are not.
    assert names == {"test_prefix.py", "example_test.py"}


def test_shards_balance_by_recorded_cost() -> None:
    """The regression #2060 is about: equal file counts, unequal runtime.

    The stride split hands alternating files to each shard, so a suite whose
    cost sits in every *other* file splits evenly by count and lopsidedly by
    time — which is what the real suite did (4m50s against 6m03s). Asserted on
    the resulting loads, not on membership: membership is an implementation
    detail, the balance is the contract. The stride load is computed here too,
    so the fixture proves it is a case the old code got wrong.
    """
    files = [Path(f"tests/test_{index}.py") for index in range(4)]
    durations = {
        "tests/test_0.py": 100.0,
        "tests/test_1.py": 1.0,
        "tests/test_2.py": 100.0,
        "tests/test_3.py": 1.0,
    }
    load = lambda paths: sum(durations[path.as_posix()] for path in paths)  # noqa: E731

    stride = [load(files[index::2]) for index in range(2)]
    balanced = [
        load(sharder.shard_files(files, index=index, count=2, durations=durations))
        for index in range(2)
    ]

    # What the stride did: both heavy files in one shard.
    assert stride == [200.0, 2.0]
    assert max(balanced) - min(balanced) == 0.0


def test_an_unrecorded_file_is_charged_the_average_not_zero() -> None:
    """A new test file must not look free, or new files pile onto one shard.

    Charged zero, an unrecorded file sorts last *and* keeps its shard's load
    at whatever it was, so the next unrecorded file joins it, and the next —
    every file a PR adds lands together on the shard that happened to be
    lightest. The fixture is one recorded file against three new ones, which
    is the shape that tells the two rules apart: charged the average, the four
    split two and two; charged zero, the three newcomers travel as a block.
    """
    files = [Path("tests/test_known.py"), *(Path(f"tests/test_new{i}.py") for i in range(3))]
    durations = {"tests/test_known.py": 100.0}

    shards = [sharder.shard_files(files, index=i, count=2, durations=durations) for i in range(2)]

    assert sorted(len(shard) for shard in shards) == [2, 2]


def test_partition_is_deterministic_across_processes() -> None:
    """Two runners must agree without coordinating — including their hash seeds.

    Repeating the call in one interpreter proves nothing about set or dict
    iteration order, which is fixed for a process's lifetime. Each shard runs
    in its own Windows job, so the partition has to survive a different
    ``PYTHONHASHSEED``; this runs it under two.
    """
    program = textwrap.dedent(
        f"""
        import importlib.util, json
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("s", {str(_ROOT / "tools" / "run_pytest_shard.py")!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        root = Path({str(_ROOT)!r})
        durations = mod.load_durations(
            Path({str(_ROOT / "tools" / sharder.DURATIONS_FILE)!r})
        )
        files = mod.test_files(root)
        out = [
            [p.as_posix() for p in mod.shard_files(
                files, index=i, count=3, durations=durations, repo_root=root
            )]
            for i in range(3)
        ]
        print(json.dumps(out))
        """
    )
    runs = [
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "12345")
    ]

    assert runs[0] == runs[1]


def test_missing_or_corrupt_durations_never_break_the_shard(tmp_path: Path) -> None:
    """A stale cost hint degrades balance, never correctness.

    The file is regenerated by hand and will go stale between refreshes; a CI
    job that refused to run because of that would be worse than an unbalanced
    one.
    """
    absent = tmp_path / "nope.json"
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text('{"durations": [1, 2, 3]}', encoding="utf-8")

    assert sharder.load_durations(absent) == {}
    assert sharder.load_durations(corrupt) == {}
    assert sharder.load_durations(wrong_shape) == {}

    files = [Path(f"tests/test_{index}.py") for index in range(5)]
    shards = [sharder.shard_files(files, index=i, count=2, durations={}) for i in range(2)]
    assert sorted(path for shard in shards for path in shard) == files


def test_the_committed_cost_map_is_a_readable_map() -> None:
    """The committed artifact must parse — its *freshness* is not gated.

    Two different axes, and an earlier draft of this test conflated them.
    Coverage decays by ordinary work: every PR that adds or renames a test
    file leaves the map a little staler, and that is precisely what the
    average-cost fallback is for, so asserting a coverage percentage would
    turn routine work red for a cost hint. What is worth pinning is that the
    file we ship is a map at all — a deleted or truncated artifact is a defect
    in the committed tree, not the consequence of someone adding a test.

    Staleness is reported, not enforced: the count below shows up in ``-v``
    output when someone is already looking.
    """
    durations = sharder.load_durations(_ROOT / "tools" / sharder.DURATIONS_FILE)
    files = {path.relative_to(_ROOT).as_posix() for path in sharder.test_files(_ROOT)}

    assert durations, "committed cost map is missing, empty, or unreadable"

    # Warned, not asserted, and not on every run: a passing test's ``print`` is
    # swallowed under both ``-q`` and ``-v``, while a warning reaches the
    # summary. Silent while the map still describes most of the suite.
    covered = len(files & set(durations))
    if covered < 0.8 * len(files):
        warnings.warn(
            f"pytest shard cost map covers only {covered}/{len(files)} test files; "
            "shards will be balanced on stale data (see the note in the JSON)",
            stacklevel=1,
        )


def test_junit_report_is_summed_per_file(tmp_path: Path) -> None:
    """Per-test rows fold into per-file totals, class rows included.

    pytest emits the test class in ``classname`` and no file attribute, so the
    walk-back is the only thing connecting a row to a file. A row that cannot
    be resolved is dropped rather than guessed.
    """
    tests_root = tmp_path / "packages" / "memtomem" / "tests"
    tests_root.mkdir(parents=True)
    (tests_root / "test_thing.py").touch()
    report = tmp_path / "junit.xml"
    report.write_text(
        """<testsuites><testsuite>
        <testcase classname="packages.memtomem.tests.test_thing" name="a" time="1.5"/>
        <testcase classname="packages.memtomem.tests.test_thing.TestX" name="b" time="2.0"/>
        <testcase classname="packages.memtomem.tests.test_gone" name="c" time="9.0"/>
        </testsuite></testsuites>""",
        encoding="utf-8",
    )

    totals = sharder.durations_from_junit(report, tmp_path)

    assert totals == {"packages/memtomem/tests/test_thing.py": 3.5}


def test_absurd_but_valid_json_numbers_are_dropped_not_raised(tmp_path: Path) -> None:
    """The fallback covers hostile *values*, not just hostile syntax.

    ``load_durations`` already returns ``{}`` for unparseable JSON, but a
    well-formed map can still carry a number Python cannot use: ``10**400``
    raises ``OverflowError`` out of ``float()``, and ``1e999`` parses to
    ``inf``, which would then poison the mean every unrecorded file is charged
    and pile the whole suite onto one shard. Both are dropped per entry, so
    the healthy neighbours in the same file survive.
    """
    hostile = tmp_path / "hostile.json"
    hostile.write_text(
        '{"durations": {"a.py": %s, "b.py": 1e999, "c.py": -5, "d.py": "x", "ok.py": 2.5}}'
        % ("9" * 400),
        encoding="utf-8",
    )

    assert sharder.load_durations(hostile) == {"ok.py": 2.5}


def test_junit_cost_never_lands_on_a_namesake_file(tmp_path: Path) -> None:
    """A test class must not charge its module's cost to a same-named file.

    ``classname`` is ``<module dotted>.<class>``, so the walk-back's first
    candidate for ``test_api.py::TestRoutes`` is ``test_api/TestRoutes.py``.
    Accepting any path that happens to exist hands the module's whole cost to
    that namesake — the sharder then believes ``test_api.py`` is free and the
    balance it computes is fiction. Only files the sharder actually
    distributes are eligible.
    """
    tests_root = tmp_path / "packages" / "memtomem" / "tests"
    (tests_root / "test_api").mkdir(parents=True)
    (tests_root / "test_api.py").touch()
    (tests_root / "test_api" / "TestRoutes.py").touch()
    report = tmp_path / "junit.xml"
    report.write_text(
        "<testsuites><testsuite>"
        '<testcase classname="packages.memtomem.tests.test_api.TestRoutes" name="a" time="7.0"/>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )

    totals = sharder.durations_from_junit(report, tmp_path)

    assert totals == {"packages/memtomem/tests/test_api.py": 7.0}


def test_merely_huge_finite_costs_cannot_overflow_the_shard_loads() -> None:
    """Rejecting ``inf`` per entry is not enough — the sum overflows too.

    Two entries of ``1e308`` are each finite and each pass an ``isfinite``
    check, but ``1e308 + 1e308`` is ``inf``. A shard whose load is ``inf``
    never looks like the lightest one again, so every remaining file lands
    somewhere else and the partition collapses to one loaded shard. The cap on
    a single file's claim is what makes the aggregate safe.
    """
    hostile = _write_map(
        {"a.py": 1e308, "b.py": 1e308, "c.py": 3.0, "d.py": 3.0, "e.py": 3.0, "f.py": 3.0}
    )
    durations = sharder.load_durations(hostile)
    files = [Path(name) for name in ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py")]

    shards = [sharder.shard_files(files, index=i, count=2, durations=durations) for i in range(2)]

    assert sorted(len(shard) for shard in shards) == [3, 3]
