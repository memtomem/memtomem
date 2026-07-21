"""Pins for the narrow real-settings write detector (#1892, #1903).

All filesystem mutation in this module uses a synthetic home.  The final
integration pins start a nested pytest process whose *process home* is also a
synthetic directory, so testing the guard cannot damage the files it protects.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
        fake_home / ".kimi" / "config.toml",
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
    tmp_path: Path, body: str, *, disabled: bool = False
) -> subprocess.CompletedProcess[str]:
    suite = tmp_path / ("disabled-suite" if disabled else "enabled-suite")
    suite.mkdir()
    synthetic_home = suite / "home"
    synthetic_home.mkdir()
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
    else:
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


def test_pytest_wiring_fails_the_indirect_writer(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, _INDIRECT_WRITE_TEST)
    assert result.returncode == 1, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "changed real user settings outside its test sandbox" in combined
    assert "created" in combined


def test_pytest_wiring_allows_a_clean_test(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, "def test_clean():\n    assert True\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_pytest_wiring_honours_the_escape_hatch(tmp_path: Path) -> None:
    result = _run_nested_pytest(tmp_path, _INDIRECT_WRITE_TEST, disabled=True)
    assert result.returncode == 0, result.stdout + result.stderr
