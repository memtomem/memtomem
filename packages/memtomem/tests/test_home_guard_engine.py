"""Pins for the narrow real-settings write detector (#1892, #1903).

All filesystem mutation in this module uses a synthetic home.  The final
integration pins start a nested pytest process whose *process home* is also a
synthetic directory, so testing the guard cannot damage the files it protects.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest

from . import _home_guard as hg


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def watched(fake_home: Path) -> Path:
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir()
    target.write_text('{"model": "opus"}', encoding="utf-8")
    return target


def _cycle(paths: tuple[Path, ...], mutate) -> list[hg.Violation]:
    before = hg.snapshot_files(paths)
    hg.require_armable(before)
    mutate()
    return hg.diff_files(before, hg.snapshot_files(paths))


# -- switch and target derivation ------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ({}, True),
        ({hg.DISABLE_ENV: "off"}, False),
        ({hg.DISABLE_ENV: "OFF"}, False),
        ({hg.DISABLE_ENV: "0"}, False),
        ({hg.DISABLE_ENV: "false"}, False),
        ({hg.DISABLE_ENV: " no "}, False),
        ({hg.DISABLE_ENV: "on"}, True),
        ({hg.DISABLE_ENV: ""}, True),
    ],
)
def test_guard_enabled_parses_the_off_switch(value: dict[str, str], expected: bool) -> None:
    assert hg.guard_enabled(value) is expected


def test_as_home_restores_the_previous_environment(fake_home: Path) -> None:
    before = (os.environ.get("HOME"), os.environ.get("USERPROFILE"))
    with hg.as_home(fake_home):
        assert Path.home() == fake_home
        assert os.environ["USERPROFILE"] == str(fake_home)
    assert (os.environ.get("HOME"), os.environ.get("USERPROFILE")) == before


def test_derivation_is_exactly_the_four_settings_targets(fake_home: Path) -> None:
    assert set(hg.derive_targets(fake_home)) == {
        fake_home / ".claude" / "settings.json",
        fake_home / ".codex" / "hooks.json",
        fake_home / ".gemini" / "settings.json",
        fake_home / ".kimi-code" / "config.toml",
    }


def test_every_current_settings_generator_is_included(fake_home: Path) -> None:
    from memtomem.context.settings import SETTINGS_GENERATORS

    targets = set(hg.derive_targets(fake_home))
    with hg.as_home(fake_home):
        expected = {
            Path(target)
            for generator in SETTINGS_GENERATORS.values()
            if (target := generator.target_file(fake_home / "no-project", "user")) is not None
        }
    assert targets == expected


def test_future_settings_generator_is_included(fake_home: Path, monkeypatch) -> None:
    from memtomem.context import settings

    class FutureGenerator:
        name = "future"

        @staticmethod
        def target_file(project_root: Path, scope: str) -> Path:
            del project_root
            assert scope == "user"
            return Path.home() / ".future" / "settings.json"

    monkeypatch.setitem(settings.SETTINGS_GENERATORS, "future", FutureGenerator())
    assert fake_home / ".future" / "settings.json" in hg.derive_targets(fake_home)


def test_derivation_refuses_a_target_outside_home(
    fake_home: Path, tmp_path: Path, monkeypatch
) -> None:
    from memtomem.context import settings

    class EscapingGenerator:
        name = "escape"

        @staticmethod
        def target_file(project_root: Path, scope: str) -> Path:
            del project_root, scope
            return tmp_path / "outside.json"

    monkeypatch.setattr(settings, "SETTINGS_GENERATORS", {"escape": EscapingGenerator()})
    with pytest.raises(hg.HomeGuardError, match="escapes the real home"):
        hg.derive_targets(fake_home)


def test_derivation_refuses_an_empty_registry(fake_home: Path, monkeypatch) -> None:
    from memtomem.context import settings

    monkeypatch.setattr(settings, "SETTINGS_GENERATORS", {})
    with pytest.raises(hg.HomeGuardError, match="produced no settings targets"):
        hg.derive_targets(fake_home)


# -- bounded file fingerprints ---------------------------------------------


def test_missing_file_is_armable(fake_home: Path) -> None:
    target = fake_home / ".claude" / "settings.json"
    snapshot = hg.snapshot_files((target,))
    assert snapshot[str(target)].state == "missing"
    hg.require_armable(snapshot)


def test_content_change_is_a_violation(watched: Path) -> None:
    violations = _cycle((watched,), lambda: watched.write_text("{}", encoding="utf-8"))
    assert [item.kind for item in violations] == ["modified"]
    assert violations[0].detail == "byte content changed"


def test_same_size_restored_mtime_change_is_a_violation(watched: Path) -> None:
    watched.write_text("AAAA", encoding="utf-8")
    original = watched.stat()

    def rewrite() -> None:
        watched.write_text("BBBB", encoding="utf-8")
        os.utime(watched, ns=(original.st_atime_ns, original.st_mtime_ns))

    assert [item.kind for item in _cycle((watched,), rewrite)] == ["modified"]


def test_byte_identical_rewrite_is_clean(watched: Path) -> None:
    original = watched.read_bytes()

    def rewrite() -> None:
        watched.write_bytes(original)
        stat = watched.stat()
        os.utime(watched, ns=(stat.st_atime_ns + 10**9, stat.st_mtime_ns + 10**9))

    assert _cycle((watched,), rewrite) == []


def test_deletion_is_a_violation(watched: Path) -> None:
    assert [item.kind for item in _cycle((watched,), watched.unlink)] == ["deleted"]


def test_creation_is_a_violation(fake_home: Path) -> None:
    target = fake_home / ".claude" / "settings.json"
    target.parent.mkdir()
    assert [
        item.kind for item in _cycle((target,), lambda: target.write_text("{}", encoding="utf-8"))
    ] == ["created"]


def test_untouched_file_is_clean(watched: Path) -> None:
    assert _cycle((watched,), lambda: None) == []


def test_oversized_file_is_refused(fake_home: Path) -> None:
    target = fake_home / "large.json"
    target.write_bytes(b"12345")
    value = hg.fingerprint(target, max_bytes=4)
    assert value.state == "unsafe"
    assert "limit is 4" in value.detail


def test_file_that_grows_during_read_is_refused(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fake_home / "growing.json"
    target.write_bytes(b"x")
    chunks = iter((b"1234", b"5"))
    monkeypatch.setattr(hg.os, "read", lambda fd, size: next(chunks, b""))
    value = hg.fingerprint(target, max_bytes=4)
    assert value.state == "unsafe"
    assert "grew beyond" in value.detail


def test_unreadable_file_is_refused(fake_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = fake_home / "unreadable.json"
    target.write_text("{}", encoding="utf-8")

    def deny_open(path, flags):
        del path, flags
        raise PermissionError("denied for pin")

    monkeypatch.setattr(hg.os, "open", deny_open)
    value = hg.fingerprint(target)
    assert value.state == "unsafe"
    assert "denied for pin" in value.detail


def test_directory_is_refused(fake_home: Path) -> None:
    target = fake_home / "settings.json"
    target.mkdir()
    value = hg.fingerprint(target)
    assert value.state == "unsafe"
    assert "not a regular file" in value.detail


@pytest.mark.requires_symlinks
def test_final_file_symlink_is_refused(fake_home: Path, tmp_path: Path) -> None:
    target_file = tmp_path / "target.json"
    target_file.write_text("{}", encoding="utf-8")
    link = fake_home / "settings.json"
    link.symlink_to(target_file)
    value = hg.fingerprint(link)
    assert value.state == "unsafe"
    assert "symlink or reparse point" in value.detail


@pytest.mark.requires_symlinks
def test_parent_directory_symlink_is_accepted(fake_home: Path, tmp_path: Path) -> None:
    real_parent = tmp_path / "claude-real"
    real_parent.mkdir()
    target = real_parent / "settings.json"
    target.write_text("{}", encoding="utf-8")
    (fake_home / ".claude").symlink_to(real_parent, target_is_directory=True)
    lexical_target = fake_home / ".claude" / "settings.json"
    assert hg.fingerprint(lexical_target).state == "regular"


def test_reparse_attribute_is_refused(watched: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hg, "_is_reparse_point", lambda stat: True)
    value = hg.fingerprint(watched)
    assert value.state == "unsafe"
    assert "reparse point" in value.detail


def test_require_armable_aggregates_unsafe_paths(fake_home: Path) -> None:
    first = str(fake_home / "a.json")
    second = str(fake_home / "b.json")
    snapshot = {
        first: hg.FileFingerprint("unsafe", detail="first reason"),
        second: hg.FileFingerprint("unsafe", detail="second reason"),
    }
    with pytest.raises(hg.HomeGuardError) as caught:
        hg.require_armable(snapshot)
    assert first in str(caught.value)
    assert second in str(caught.value)
    assert f"{hg.DISABLE_ENV}=off" in str(caught.value)


def test_transition_to_unsafe_is_a_violation(watched: Path) -> None:
    before = hg.snapshot_files((watched,))
    after = {str(watched): hg.FileFingerprint("unsafe", detail="became unsafe")}
    violations = hg.diff_files(before, after)
    assert [item.kind for item in violations] == ["unsafe"]
    assert violations[0].detail == "became unsafe"


def test_failure_message_contains_no_digest(watched: Path) -> None:
    before = hg.snapshot_files((watched,))
    watched.write_text("changed", encoding="utf-8")
    violations = hg.diff_files(before, hg.snapshot_files((watched,)))
    message = hg.format_violations("test_example", violations)
    assert "test_example" in message
    assert "set_home" in message
    assert before[str(watched)].digest not in message


# -- actual pytest wiring ---------------------------------------------------


def _run_nested_pytest(
    tmp_path: Path,
    body: str,
    *,
    disabled: bool = False,
    mode: str | None = None,
    prepare: Callable[[Path], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    suite = tmp_path / (mode or ("disabled-suite" if disabled else "enabled-suite"))
    suite.mkdir()
    synthetic_home = suite / "home"
    synthetic_home.mkdir()
    if prepare is not None:
        prepare(synthetic_home)
    test_file = suite / "test_nested_guard.py"
    test_file.write_text(body, encoding="utf-8")

    package_root = Path(__file__).parents[1]
    env = os.environ.copy()
    env["HOME"] = str(synthetic_home)
    env["USERPROFILE"] = str(synthetic_home)
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    if disabled:
        env[hg.DISABLE_ENV] = "off"
    elif mode is not None:
        env[hg.DISABLE_ENV] = mode
    else:
        # The outer run may itself be in strict mode (CI sets it), which would
        # otherwise decide the nested run's default-mode behaviour for it.
        env.pop(hg.DISABLE_ENV, None)

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            "--confcutdir",
            str(suite),
            "-q",
            str(test_file),
        ],
        cwd=package_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


_INDIRECT_WRITE_TEST = """
from pathlib import Path

def test_indirect_production_target_write():
    from memtomem.context.settings import SETTINGS_GENERATORS
    target = SETTINGS_GENERATORS["claude_settings"].target_file(Path.cwd(), "user")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{}", encoding="utf-8")
"""


#: Writes the watched path from a *child* process, so the parent's audit hook
#: cannot see it.  This is the shape the editor session has on a developer
#: machine: the bytes change, and nothing in pytest wrote them (#2355).
_EXTERNAL_WRITE_TEST = """
import os
import subprocess
import sys
from pathlib import Path

def test_a_concurrent_writer_owns_the_file():
    from memtomem.context.settings import SETTINGS_GENERATORS
    target = SETTINGS_GENERATORS["claude_settings"].target_file(Path.cwd(), "user")
    target.parent.mkdir(parents=True, exist_ok=True)
    script = "from pathlib import Path; Path(%r).write_text('{}')" % str(target)
    subprocess.run([sys.executable, "-c", script], check=True)
"""


@pytest.mark.parametrize("mode", [None, "strict"])
def test_pytest_wiring_fails_the_indirect_writer(tmp_path: Path, mode: str | None) -> None:
    result = _run_nested_pytest(tmp_path, _INDIRECT_WRITE_TEST, mode=mode)
    assert result.returncode == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "changed real user settings outside its test sandbox" in combined
    assert "created" in combined
    assert "observed open on thread MainThread" in combined
    assert "test_nested_guard.py" in combined


def test_pytest_wiring_reports_an_unattributed_change_without_blaming_a_test(
    tmp_path: Path,
) -> None:
    result = _run_nested_pytest(tmp_path, _EXTERNAL_WRITE_TEST)
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined
    assert "home guard: unattributed real-settings change(s)" in combined
    assert "test_a_concurrent_writer_owns_the_file" in combined
    assert "changed real user settings outside its test sandbox" not in combined
    assert "set_home" not in combined


def test_pytest_wiring_fails_an_unattributed_change_in_strict_mode(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, _EXTERNAL_WRITE_TEST, mode="strict")
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "no in-process write" in combined
    assert "This run is in strict mode" in combined
    assert "set_home" not in combined


def test_pytest_wiring_stays_silent_for_a_clean_run(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, "def test_clean():\n    assert True\n", mode="strict")
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined
    assert "home guard" not in combined


def test_pytest_wiring_allows_a_clean_test(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, "def test_clean():\n    assert True\n")
    assert result.returncode == 0, result.stdout + result.stderr


# -- a settings target that cannot be fingerprinted (#3019) -----------------

_CODEX_HOOKS = (".codex", "hooks.json")
_ALL_TARGETS = (
    (".claude", "settings.json"),
    (".codex", "hooks.json"),
    (".gemini", "settings.json"),
    (".kimi-code", "config.toml"),
)


def _symlink_targets(*relative: tuple[str, str]) -> Callable[[Path], None]:
    """Make each named settings target a symlink into a dotfiles dir beside the home.

    The link destinations live in the nested suite's own temporary directory,
    never under the process home that the outer run is guarding.
    """

    def prepare(home: Path) -> None:
        dotfiles = home.parent / "dotfiles"
        for parent, name in relative:
            destination = dotfiles / parent.lstrip(".") / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("{}", encoding="utf-8")
            link = home / parent / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(destination)

    return prepare


#: The production writer's shape: a tempfile renamed onto the target pathname.
#: Onto a symlink, that rename replaces the link itself.
_REPLACE_THE_LINK_TEST = """
import os
import tempfile
from pathlib import Path

def test_replaces_the_symlinked_target():
    target = Path.home() / ".codex" / "hooks.json"
    fd, tmp = tempfile.mkstemp(dir=target.parent)
    os.close(fd)
    os.replace(tmp, target)
"""

#: The shape of a writer that keeps the link: it replaces the link's destination.
_WRITE_THROUGH_THE_LINK_TEST = """
import os
import tempfile
from pathlib import Path

def test_replaces_the_link_destination():
    target = Path(os.path.realpath(Path.home() / ".codex" / "hooks.json"))
    fd, tmp = tempfile.mkstemp(dir=target.parent)
    os.close(fd)
    os.replace(tmp, target)
"""

_CLEAN_TEST = "def test_clean():\n    assert True\n"


def test_partition_degrades_an_unsafe_target_outside_strict(fake_home: Path) -> None:
    regular = fake_home / "regular.json"
    regular.write_text("{}", encoding="utf-8")
    unsafe = fake_home / "unsafe.json"
    unsafe.mkdir()
    targets = (regular, unsafe)
    watched, degraded = hg.partition_baseline(targets, hg.snapshot_files(targets), "on")
    assert watched == (regular,)
    assert [(item.path, item.reason) for item in degraded] == [
        (str(unsafe), "final entry is not a regular file")
    ]
    assert degraded[0].aliases == (hg.normalise_target(unsafe),)


def test_partition_refuses_an_unsafe_target_in_strict(fake_home: Path) -> None:
    unsafe = fake_home / "unsafe.json"
    unsafe.mkdir()
    with pytest.raises(hg.HomeGuardError, match="not a regular file"):
        hg.partition_baseline((unsafe,), hg.snapshot_files((unsafe,)), "strict")


@pytest.mark.requires_symlinks
def test_partition_names_a_symlinks_destination(fake_home: Path, tmp_path: Path) -> None:
    destination = tmp_path / "dotfiles" / "hooks.json"
    destination.parent.mkdir()
    destination.write_text("{}", encoding="utf-8")
    link = fake_home / "hooks.json"
    link.symlink_to(destination)
    _, degraded = hg.partition_baseline((link,), hg.snapshot_files((link,)), "on")
    assert degraded[0].aliases == (
        hg.normalise_target(link),
        hg.normalise_target(os.path.realpath(destination)),
    )


def test_degraded_writes_report_only_observed_aliases(fake_home: Path) -> None:
    item = hg.DegradedTarget(
        str(fake_home / "link.json"),
        "final entry is a symlink or reparse point",
        (hg.normalise_target(fake_home / "link.json"), hg.normalise_target(fake_home / "dest")),
    )

    def seen(path: str) -> hg.WriteObservation:
        return hg.WriteObservation("os.rename", path, "t", "MainThread")

    assert hg.degraded_writes((item,), []) == []
    assert hg.degraded_writes((item,), [seen(hg.normalise_target(fake_home / "x"))]) == []
    [own] = hg.degraded_writes((item,), [seen(item.aliases[0])])
    assert (own.path, own.kind) == (item.aliases[0], "written")
    assert "observed on this target" in own.detail
    [through] = hg.degraded_writes((item,), [seen(item.aliases[1])])
    assert through.path == item.aliases[1]
    assert f"the symlink {item.path} points to" in through.detail


@pytest.mark.requires_symlinks
def test_pytest_wiring_still_guards_a_regular_target_beside_a_symlink(tmp_path: Path) -> None:
    result = _run_nested_pytest(
        tmp_path, _INDIRECT_WRITE_TEST, prepare=_symlink_targets(_CODEX_HOOKS)
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "changed real user settings outside its test sandbox" in combined
    assert "settings.json — created" in combined
    assert "HomeGuardError" not in combined


@pytest.mark.requires_symlinks
def test_pytest_wiring_runs_beside_a_symlink_and_names_it(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, _CLEAN_TEST, prepare=_symlink_targets(_CODEX_HOOKS))
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "1 passed" in combined
    assert "home guard: settings target(s) checked by write observation only" in combined
    assert "hooks.json — final entry is a symlink or reparse point" in combined
    assert "home guard: unattributed real-settings change(s)" not in combined


@pytest.mark.requires_symlinks
def test_pytest_wiring_refuses_a_symlink_in_strict_mode(tmp_path: Path) -> None:
    result = _run_nested_pytest(
        tmp_path, _CLEAN_TEST, mode="strict", prepare=_symlink_targets(_CODEX_HOOKS)
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "1 error" in combined
    assert "home guard cannot safely watch" in combined
    assert "final entry is a symlink or reparse point" in combined


@pytest.mark.requires_symlinks
@pytest.mark.parametrize(
    "body, where",
    [
        (_REPLACE_THE_LINK_TEST, "observed on this target"),
        (_WRITE_THROUGH_THE_LINK_TEST, "observed on the file the symlink"),
    ],
    ids=["replace-the-link", "write-through-the-link"],
)
def test_pytest_wiring_fails_a_write_to_a_symlinked_target(
    tmp_path: Path, body: str, where: str
) -> None:
    result = _run_nested_pytest(tmp_path, body, prepare=_symlink_targets(_CODEX_HOOKS))
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "changed real user settings outside its test sandbox" in combined
    assert "— written: " in combined
    assert where in combined
    assert "observed os.rename on thread MainThread" in combined


@pytest.mark.requires_symlinks
def test_pytest_wiring_watches_writes_when_every_target_is_a_symlink(tmp_path: Path) -> None:
    result = _run_nested_pytest(
        tmp_path, _REPLACE_THE_LINK_TEST, prepare=_symlink_targets(*_ALL_TARGETS)
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 1, combined
    assert "— written: " in combined
    assert combined.count("final entry is a symlink or reparse point") >= len(_ALL_TARGETS)


def _directory_targets(home: Path) -> None:
    """Make every settings target a directory: unsafe without needing symlinks."""
    for parent, name in _ALL_TARGETS:
        (home / parent / name).mkdir(parents=True)


#: Moving a target aside is a write to its pathname whatever the entry is.
_MOVE_THE_TARGET_ASIDE_TEST = """
import os
from pathlib import Path

def test_moves_the_target_aside():
    target = Path.home() / ".codex" / "hooks.json"
    os.rename(target, target.with_name("hooks.json.aside"))
"""


def test_pytest_wiring_watches_writes_when_no_target_can_be_fingerprinted(
    tmp_path: Path,
) -> None:
    """The degraded wiring on a host that cannot create symlinks (Windows CI)."""
    (tmp_path / "clean").mkdir()
    (tmp_path / "moved").mkdir()
    clean = _run_nested_pytest(tmp_path / "clean", _CLEAN_TEST, prepare=_directory_targets)
    combined = clean.stdout + clean.stderr
    assert clean.returncode == 0, combined
    assert combined.count("final entry is not a regular file") == len(_ALL_TARGETS)

    moved = _run_nested_pytest(
        tmp_path / "moved", _MOVE_THE_TARGET_ASIDE_TEST, prepare=_directory_targets
    )
    combined = moved.stdout + moved.stderr
    assert moved.returncode == 1, combined
    assert "— written: a write was observed on this target" in combined
    assert "observed os.rename on thread MainThread" in combined


def test_pytest_wiring_honours_the_escape_hatch(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, _INDIRECT_WRITE_TEST, disabled=True)
    assert result.returncode == 0, result.stdout + result.stderr


# -- mode switch ------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ({}, "on"),
        ({hg.DISABLE_ENV: ""}, "on"),
        ({hg.DISABLE_ENV: "on"}, "on"),
        ({hg.DISABLE_ENV: "yes-please"}, "on"),
        ({hg.DISABLE_ENV: "off"}, "off"),
        ({hg.DISABLE_ENV: " NO "}, "off"),
        ({hg.DISABLE_ENV: "strict"}, "strict"),
        ({hg.DISABLE_ENV: "STRICT"}, "strict"),
        ({hg.DISABLE_ENV: "  Strict "}, "strict"),
    ],
)
def test_guard_mode_parses_the_three_modes(value: dict[str, str], expected: str) -> None:
    assert hg.guard_mode(value) == expected
    assert hg.guard_enabled(value) is (expected != "off")


# -- in-process write witness ----------------------------------------------


@pytest.fixture(scope="module")
def installed_witness() -> hg.WriteWitness:
    """One installed witness for the whole module.

    An audit hook cannot be uninstalled, so a fresh witness per test would
    leave one armed hook per pin for the rest of the pytest process.
    """
    witness = hg.WriteWitness(())
    witness.install()
    return witness


@pytest.fixture
def witness(
    installed_witness: hg.WriteWitness, watched: Path, request: pytest.FixtureRequest
) -> Iterator[hg.WriteWitness]:
    installed_witness.watch((watched,))
    installed_witness.begin_window(request.node.nodeid)
    yield installed_witness
    installed_witness.watch(())
    installed_witness.drain()


def test_witness_sees_a_plain_write(witness: hg.WriteWitness, watched: Path) -> None:
    watched.write_text("{}", encoding="utf-8")
    assert [(item.event, item.path) for item in witness.drain()] == [
        ("open", hg.normalise_target(watched))
    ]


@pytest.mark.parametrize(
    "flags",
    [
        pytest.param(os.O_WRONLY, id="wronly"),
        pytest.param(os.O_RDWR, id="rdwr"),
        pytest.param(os.O_WRONLY | os.O_APPEND, id="append"),
        pytest.param(os.O_RDONLY | os.O_CREAT, id="creat"),
        pytest.param(os.O_WRONLY | os.O_TRUNC, id="trunc"),
    ],
)
def test_witness_sees_a_write_capable_open(
    witness: hg.WriteWitness, watched: Path, flags: int
) -> None:
    """Real opens, in the combinations a caller can actually pass."""
    fd = os.open(watched, flags)
    os.close(fd)
    assert [item.event for item in witness.drain()] == ["open"]


@pytest.mark.parametrize(
    "flag",
    [
        pytest.param(os.O_WRONLY, id="wronly"),
        pytest.param(os.O_RDWR, id="rdwr"),
        pytest.param(os.O_APPEND, id="append"),
        pytest.param(os.O_CREAT, id="creat"),
        pytest.param(os.O_TRUNC, id="trunc"),
    ],
)
def test_each_flag_alone_makes_an_open_a_write(watched: Path, flag: int) -> None:
    """One flag per case, so dropping any single flag from the mask shows up.

    Some of these combinations are not openable on their own; the classifier
    is asked directly, which is also what the audit hook sees.
    """
    assert hg._candidate_paths("open", (str(watched), None, flag)) == (str(watched),)


def test_a_read_only_open_is_not_a_write(watched: Path) -> None:
    assert hg._candidate_paths("open", (str(watched), "r", os.O_RDONLY)) == ()


def test_witness_attributes_an_atomic_replace_to_the_destination(
    witness: hg.WriteWitness, watched: Path
) -> None:
    """The production settings writer is mkstemp + os.replace (#2302)."""
    handle, temp_name = tempfile.mkstemp(dir=watched.parent)
    os.write(handle, b"{}")
    os.close(handle)
    os.replace(temp_name, watched)

    observed = witness.drain()
    assert [(item.event, item.path) for item in observed] == [
        ("os.rename", hg.normalise_target(watched))
    ]
    assert hg.normalise_target(temp_name) not in {item.path for item in observed}


def test_witness_sees_a_rename_that_moves_the_watched_file_away(
    witness: hg.WriteWitness, watched: Path
) -> None:
    """Renaming the file out of the way removes it just as surely as unlink."""
    os.rename(watched, watched.parent / "settings.json.bak")
    assert [(item.event, item.path) for item in witness.drain()] == [
        ("os.rename", hg.normalise_target(watched))
    ]


def test_witness_sees_both_endpoints_of_a_rename_between_watched_paths(
    installed_witness: hg.WriteWitness, watched: Path, request: pytest.FixtureRequest
) -> None:
    destination = watched.parent / "settings.json.next"
    destination.write_text("{}", encoding="utf-8")
    installed_witness.watch((watched, destination))
    installed_witness.begin_window(request.node.nodeid)
    try:
        os.replace(watched, destination)
        observed = installed_witness.drain()
    finally:
        installed_witness.watch(())
    assert {item.path for item in observed} == {
        hg.normalise_target(watched),
        hg.normalise_target(destination),
    }


def test_witness_sees_a_rename_of_the_watched_files_parent(
    installed_witness: hg.WriteWitness, watched: Path, request: pytest.FixtureRequest
) -> None:
    """Moving ~/.claude aside removes the settings file inside it."""
    installed_witness.watch((watched,))
    installed_witness.begin_window(request.node.nodeid)
    try:
        os.rename(watched.parent, watched.parent.parent / ".claude.bak")
        observed = installed_witness.drain()
    finally:
        installed_witness.watch(())
    assert [(item.event, item.path) for item in observed] == [
        ("os.rename", hg.normalise_target(watched))
    ]


@pytest.mark.parametrize(
    "event, args, expected",
    [
        pytest.param(
            "os.rename",
            ("{src}", "{watched}", -1, 3),
            ("{src}", "{watched}"),
            id="rename-dst",
        ),
        pytest.param("os.remove", ("{watched}", 3), ("{watched}",), id="remove"),
    ],
)
def test_an_absolute_path_is_attributed_despite_a_dir_fd(
    watched: Path, event: str, args: tuple[object, ...], expected: tuple[str, ...]
) -> None:
    """A ``dir_fd`` is ignored by the OS for an absolute path (#2355 review)."""
    fill = {"watched": str(watched), "src": str(watched.parent / "incoming.json")}
    filled = tuple(value.format(**fill) if isinstance(value, str) else value for value in args)
    assert hg._candidate_paths(event, filled) == tuple(value.format(**fill) for value in expected)


@pytest.mark.parametrize(
    "event, args, expected",
    [
        pytest.param(
            "os.rename",
            ("incoming.json", "settings.json", -1, 3),
            ("incoming.json",),
            id="rename-dst",
        ),
        pytest.param("os.remove", ("settings.json", 3), (), id="remove"),
    ],
)
def test_a_relative_path_under_a_dir_fd_is_not_a_pathname(
    event: str, args: tuple[object, ...], expected: tuple[str, ...]
) -> None:
    """A relative name resolves under the descriptor, not under the cwd.

    Attributing it by pathname would blame a test for touching a watched file
    when the descriptor pointed somewhere else entirely.  A pathname passed
    *without* a descriptor still resolves against the cwd and stays in.
    """
    assert hg._candidate_paths(event, args) == expected


def test_the_window_label_is_taken_when_the_observation_is_recorded(
    installed_witness: hg.WriteWitness, watched: Path
) -> None:
    """The #2211 shape: a late worker must not be charged to a closed window.

    The worker is held between "this is a watched write" and "record it", the
    window is advanced while it waits, and the label it lands with says which
    of the two moments the guard used.
    """
    reached = threading.Event()
    installed_witness.watch((watched,))
    installed_witness.begin_window("test_a")

    original = hg.traceback.extract_stack

    def blocking_extract_stack(*args: object, **kwargs: object) -> object:
        frames = original(*args, **kwargs)
        reached.set()
        return frames

    worker = threading.Thread(
        target=watched.write_text, args=("late",), kwargs={"encoding": "utf-8"}, name="late"
    )
    hg.traceback.extract_stack = blocking_extract_stack  # type: ignore[assignment]
    try:
        with installed_witness._lock:
            worker.start()
            assert reached.wait(timeout=10)
            installed_witness._window = "test_b"
        worker.join(timeout=10)
        observed = installed_witness.drain()
    finally:
        hg.traceback.extract_stack = original  # type: ignore[assignment]
        installed_witness.watch(())

    assert [item.window for item in observed] == ["test_b"]


def test_witness_sees_a_truncate(witness: hg.WriteWitness, watched: Path) -> None:
    os.truncate(watched, 0)
    assert [item.event for item in witness.drain()] == ["os.truncate"]


def test_witness_sees_a_deletion(witness: hg.WriteWitness, watched: Path) -> None:
    watched.unlink()
    assert [item.event for item in witness.drain()] == ["os.remove"]


def test_witness_records_the_window_and_the_write_site(
    witness: hg.WriteWitness, watched: Path, request: pytest.FixtureRequest
) -> None:
    watched.write_text("{}", encoding="utf-8")
    observation = witness.drain()[0]
    assert observation.window == request.node.nodeid
    assert observation.thread == threading.current_thread().name
    assert any("test_home_guard_engine.py" in frame for frame in observation.stack)


def test_witness_records_a_write_from_another_thread(
    witness: hg.WriteWitness, watched: Path
) -> None:
    worker = threading.Thread(
        target=watched.write_text,
        args=("{}",),
        kwargs={"encoding": "utf-8"},
        name="late-worker",
    )
    worker.start()
    worker.join()
    assert [item.thread for item in witness.drain()] == ["late-worker"]


def test_witness_ignores_reads(witness: hg.WriteWitness, watched: Path) -> None:
    watched.read_text(encoding="utf-8")
    fd = os.open(watched, os.O_RDONLY)
    os.close(fd)
    assert witness.drain() == []


def test_the_guards_own_fingerprint_read_is_not_a_write(
    witness: hg.WriteWitness, watched: Path
) -> None:
    assert hg.fingerprint(watched).state == "regular"
    assert witness.drain() == []


def test_witness_ignores_a_sibling_file(witness: hg.WriteWitness, watched: Path) -> None:
    (watched.parent / "other.json").write_text("{}", encoding="utf-8")
    assert witness.drain() == []


def test_witness_ignores_the_same_name_under_another_home(
    witness: hg.WriteWitness, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "other-home" / ".claude"
    elsewhere.mkdir(parents=True)
    (elsewhere / "settings.json").write_text("{}", encoding="utf-8")
    assert witness.drain() == []


@pytest.mark.skipif(os.path.normcase("A") != "a", reason="path comparison is case-sensitive here")
def test_witness_sees_a_differently_cased_spelling(witness: hg.WriteWitness, watched: Path) -> None:
    Path(str(watched).upper()).write_text("{}", encoding="utf-8")
    assert [item.event for item in witness.drain()] == ["open"]


def test_a_raising_hook_body_does_not_break_the_caller(
    witness: hg.WriteWitness, watched: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception raised inside an audit hook propagates into the caller."""

    def explode(path: object) -> str:
        del path
        raise RuntimeError("witness bug")

    monkeypatch.setattr(hg, "normalise_target", explode)
    watched.write_text("survived", encoding="utf-8")
    monkeypatch.undo()

    assert watched.read_text(encoding="utf-8") == "survived"
    assert witness.drain() == []


def test_watch_replaces_the_previous_set(witness: hg.WriteWitness, watched: Path) -> None:
    witness.watch(())
    watched.write_text("{}", encoding="utf-8")
    assert witness.drain() == []


# -- attribution ------------------------------------------------------------


def _observation(path: Path, event: str = "open") -> hg.WriteObservation:
    return hg.WriteObservation(
        event=event,
        path=hg.normalise_target(path),
        window="test_window",
        thread="MainThread",
        stack=('  File "prod.py", line 1, in write\n',),
    )


@pytest.mark.parametrize("kind", ["modified", "created", "deleted"])
def test_a_matching_observation_attributes_the_change(kind: str, watched: Path) -> None:
    violation = hg.Violation(str(watched), kind, "detail")
    assert hg.classify([violation], [_observation(watched)]) == ([violation], [])


@pytest.mark.parametrize("kind", ["modified", "created", "deleted"])
def test_no_observation_leaves_the_change_unattributed(kind: str, watched: Path) -> None:
    violation = hg.Violation(str(watched), kind, "detail")
    assert hg.classify([violation], []) == ([], [violation])


def test_an_observation_on_another_path_does_not_attribute(watched: Path) -> None:
    violation = hg.Violation(str(watched), "modified", "byte content changed")
    other = _observation(watched.parent / "other.json")
    assert hg.classify([violation], [other]) == ([], [violation])


def test_classify_splits_a_mixed_window(watched: Path) -> None:
    written = hg.Violation(str(watched), "modified", "byte content changed")
    foreign = hg.Violation(str(watched.parent / "other.json"), "modified", "byte content changed")
    assert hg.classify([written, foreign], [_observation(watched)]) == ([written], [foreign])


def test_attributed_failure_names_the_observed_write_site(watched: Path) -> None:
    violation = hg.Violation(str(watched), "modified", "byte content changed")
    message = hg.format_violations("test_x", [violation], [_observation(watched, "os.rename")])
    assert "test_x changed real user settings outside its test sandbox" in message
    assert "observed os.rename on thread MainThread" in message
    assert 'File "prod.py", line 1, in write' in message
    assert "set_home" in message


def test_attributed_failure_omits_a_foreign_observation(watched: Path) -> None:
    violation = hg.Violation(str(watched), "modified", "byte content changed")
    message = hg.format_violations("test_x", [violation], [_observation(watched.parent / "o.json")])
    assert "observed" not in message


@pytest.mark.parametrize("mode", ["on", "strict"])
def test_unattributed_message_blames_no_test(mode: str, watched: Path) -> None:
    violation = hg.Violation(str(watched), "modified", "byte content changed")
    message = hg.format_unattributed("test_x", [violation], mode)
    assert message.startswith("real user settings changed during test_x")
    assert "no in-process write" in message
    assert "set_home" not in message
    assert "outside its test sandbox" not in message
    assert str(watched) in message


def test_unattributed_message_offers_strict_only_when_it_is_off(watched: Path) -> None:
    violation = hg.Violation(str(watched), "modified", "byte content changed")
    assert f"{hg.DISABLE_ENV}=strict" in hg.format_unattributed("t", [violation], "on")
    assert "This run is in strict mode" in hg.format_unattributed("t", [violation], "strict")
