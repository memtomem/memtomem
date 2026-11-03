"""Settings sync writes through a symlinked target instead of replacing it.

A user-scope settings file is often a symlink into a dotfiles repository. The
sync used to rename its tempfile onto the link, which swapped the link for a
regular file: the dotfiles copy never saw the change, the result still said
``ok``, and from then on the two files drifted apart (#3021).
"""

from __future__ import annotations

import contextlib
import json
import os
import sys

import pytest

from memtomem.context import settings as settings_mod
from memtomem.context._atomic import _lock_path_for
from memtomem.context.settings import CANONICAL_SETTINGS_FILE, generate_all_settings

from .helpers import set_home

pytestmark = pytest.mark.requires_symlinks

_RULE = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi", "timeout": 5}]}

# runtime -> (runtime dir under HOME, target file under HOME, initial content,
# a token of that content the merge must carry over)
_USER_TARGETS = {
    "claude_settings": (".claude", "settings.json", '{"keep": "me", "hooks": {}}\n', '"keep"'),
    "codex_settings": (".codex", "hooks.json", '{"keep": "me", "hooks": {}}\n', '"keep"'),
    "gemini_settings": (".gemini", "settings.json", '{"keep": "me", "hooks": {}}\n', '"keep"'),
    "kimi_settings": (".kimi-code", "config.toml", 'default_model = "keep-me"\n', "keep-me"),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    return home


def _project(tmp_path):
    project = tmp_path / "project"
    (project / ".memtomem").mkdir(parents=True)
    (project / CANONICAL_SETTINGS_FILE).write_text(
        json.dumps({"hooks": {"PreToolUse": [_RULE]}}), encoding="utf-8"
    )
    return project


def _tempfiles(*dirs):
    return sorted(str(p) for d in dirs for p in d.glob(".*.tmp"))


def _codex_link(home):
    (home / ".codex").mkdir()
    return home / ".codex" / "hooks.json"


@pytest.mark.parametrize("name", sorted(_USER_TARGETS))
def test_user_target_symlink_is_written_through(name, home, tmp_path):
    """The issue's reproduction, on every user-scope generator."""
    runtime_dir, filename, initial, kept = _USER_TARGETS[name]
    (home / runtime_dir).mkdir()
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    destination = dotfiles / filename
    destination.write_text(initial, encoding="utf-8")
    link = home / runtime_dir / filename
    link.symlink_to(destination)
    # Read back rather than spelled: Windows reports an absolute link target in
    # the ``\\?\`` extended-length form.
    before = os.readlink(link)
    project = _project(tmp_path)

    results = generate_all_settings(project, scope="user", allow_host_writes=True)

    assert results[name].status == "ok", results[name].reason
    assert results[name].target == link
    assert link.is_symlink()
    assert os.readlink(link) == before
    written = destination.read_text(encoding="utf-8")
    assert "echo hi" in written
    assert kept in written
    assert _tempfiles(link.parent, dotfiles) == []


def test_relative_link_chain_is_written_through(home, tmp_path):
    """A relative link to a second link: both survive, the last file is written."""
    link = _codex_link(home)
    dotfiles = home / "dotfiles"
    dotfiles.mkdir()
    final = tmp_path / "final.json"
    final.write_text('{"hooks": {}}\n', encoding="utf-8")
    middle = dotfiles / "middle.json"
    middle.symlink_to(final)
    link.symlink_to(os.path.join("..", "dotfiles", "middle.json"))
    before = (os.readlink(link), os.readlink(middle))

    results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    assert results["codex_settings"].status == "ok", results["codex_settings"].reason
    assert (os.readlink(link), os.readlink(middle)) == before
    assert "echo hi" in final.read_text(encoding="utf-8")


def test_chain_longer_than_the_kernel_follows_is_an_error(home, tmp_path):
    """On POSIX ``realpath`` walks links itself, past the kernel's limit (32
    on macOS, 40 on Linux), so it would name a file the runtime cannot open
    through the link; reading through the link would also see "no file" and
    write a merge built from nothing over that file. Windows resolves natively
    and stops at 63 reparse points, so there both checks refuse."""
    link = _codex_link(home)
    final = tmp_path / "final.json"
    final.write_text('{"keep": "me", "hooks": {}}\n', encoding="utf-8")
    previous = final
    for i in range(70):
        hop = tmp_path / f"hop{i}.json"
        hop.symlink_to(previous)
        previous = hop
    link.symlink_to(previous)
    before = os.readlink(link)

    results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    assert results["codex_settings"].status == "error"
    assert "could not be followed" in results["codex_settings"].reason
    assert os.readlink(link) == before
    assert final.read_text(encoding="utf-8") == '{"keep": "me", "hooks": {}}\n'


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="pins POSIX realpath's textual '..' folding; Windows resolves natively",
)
@pytest.mark.parametrize("through", ["regular-file", "missing-dir"])
def test_dotdot_past_a_non_directory_is_an_error(through, home, tmp_path):
    """The kernel fails ``x/..`` when ``x`` is a regular file or does not
    exist. ``realpath(strict=True)`` agrees on 3.13, but on 3.12 it folds
    ``regular-file/..`` textually and names the neighbouring file, which the
    link does not reach; only the kernel check refuses it there."""
    link = _codex_link(home)
    victim = tmp_path / "victim.json"
    victim.write_text('{"hooks": {}}\n', encoding="utf-8")
    if through == "regular-file":
        (tmp_path / "x").write_text("", encoding="utf-8")
    link_target = os.path.join(str(tmp_path), "x", "..", "victim.json")
    link.symlink_to(link_target)
    before = os.readlink(link)

    results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    assert results["codex_settings"].status == "error"
    assert os.readlink(link) == before
    assert victim.read_text(encoding="utf-8") == '{"hooks": {}}\n'


@pytest.mark.parametrize("parent_exists", [True, False])
def test_dangling_link_is_an_error(parent_exists, home, tmp_path):
    """Nothing is created through a dangling link, into an existing directory
    or a missing one, and the error names the configured path only: the
    destination can sit anywhere."""
    link = _codex_link(home)
    parent = tmp_path / ("dotfiles" if parent_exists else "gone")
    if parent_exists:
        parent.mkdir()
    destination = parent / "codex-hooks.json"
    link.symlink_to(destination)
    before = os.readlink(link)
    (home / ".claude").mkdir()
    claude_target = home / ".claude" / "settings.json"

    results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    result = results["codex_settings"]
    assert result.status == "error"
    assert str(link) in result.reason
    assert "does not exist" in result.reason
    assert str(destination) not in result.reason
    assert os.readlink(link) == before
    assert not os.path.lexists(destination)
    assert parent.exists() is parent_exists
    # Other targets are not held back by one bad link.
    assert results["claude_settings"].status == "ok"
    assert "echo hi" in claude_target.read_text(encoding="utf-8")


def test_link_to_a_directory_is_an_error(home, tmp_path):
    link = _codex_link(home)
    directory = tmp_path / "a-directory"
    directory.mkdir()
    link.symlink_to(directory, target_is_directory=True)

    results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    assert results["codex_settings"].status == "error"
    assert "non-regular file" in results["codex_settings"].reason
    assert link.is_symlink()
    assert list(directory.iterdir()) == []


@pytest.mark.parametrize("shape", ["self", "cycle"])
def test_link_loop_is_an_error(shape, home, tmp_path):
    """``Path.resolve(strict=False)`` returns a looping link unchanged on
    3.13+, which would have sent the write straight back onto the link."""
    link = _codex_link(home)
    if shape == "self":
        link.symlink_to(link)
    else:
        other = tmp_path / "other.json"
        other.symlink_to(link)
        link.symlink_to(other)
    before = os.readlink(link)

    results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    assert results["codex_settings"].status == "error"
    assert "could not be followed" in results["codex_settings"].reason
    assert os.readlink(link) == before


def test_project_tier_link_into_home_writes_the_confirmed_host_file(home, tmp_path):
    """The host-write gate already judges the resolved path. Once the user
    confirms that host write, the host file is what gets written; before,
    the project-side link was replaced and the confirmed file never changed."""
    project = _project(tmp_path)
    (project / ".codex").mkdir()
    link = project / ".codex" / "hooks.json"
    destination = home / "codex-hooks.json"
    destination.write_text('{"hooks": {}}\n', encoding="utf-8")
    link.symlink_to(destination)

    refused = generate_all_settings(project, scope="project_shared")
    assert refused["codex_settings"].status == "needs_confirmation"
    assert "echo hi" not in destination.read_text(encoding="utf-8")

    results = generate_all_settings(project, scope="project_shared", allow_host_writes=True)
    assert results["codex_settings"].status == "ok", results["codex_settings"].reason
    assert link.is_symlink()
    assert "echo hi" in destination.read_text(encoding="utf-8")


def _retarget_at_acquire(link, new_destination):
    """A ``_file_lock`` stand-in that repoints *link* the moment the sync
    takes its lock — another writer finishing while this sync waited."""
    real_lock = settings_mod._file_lock

    @contextlib.contextmanager
    def lock(lock_path, *, timeout=None):
        if lock_path == _lock_path_for(link):
            link.unlink()
            link.symlink_to(new_destination)
        with real_lock(lock_path, timeout=timeout):
            yield

    return lock


def test_destination_is_taken_under_the_lock(home, tmp_path):
    """Read, recheck and write all follow the link as it is under the lock,
    so a retarget during the wait cannot merge one file into another."""
    link = _codex_link(home)
    old = tmp_path / "old.json"
    old.write_text('{"hooks": {}}\n', encoding="utf-8")
    new = tmp_path / "new.json"
    new.write_text('{"keep": "new", "hooks": {}}\n', encoding="utf-8")
    link.symlink_to(old)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings_mod, "_file_lock", _retarget_at_acquire(link, new))
        results = generate_all_settings(_project(tmp_path), scope="user", allow_host_writes=True)

    assert results["codex_settings"].status == "ok", results["codex_settings"].reason
    assert old.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    written = json.loads(new.read_text(encoding="utf-8"))
    assert written["keep"] == "new"
    assert "echo hi" in json.dumps(written)


def test_link_retargeted_outside_the_project_needs_confirmation(home, tmp_path):
    """The pre-lock gate passed a project-tier link that pointed inside the
    project; repointed outside during the wait, it is a host write again."""
    project = _project(tmp_path)
    (project / ".codex").mkdir()
    inside = project / "hooks-inside.json"
    inside.write_text('{"hooks": {}}\n', encoding="utf-8")
    outside = home / "codex-hooks.json"
    outside.write_text('{"hooks": {}}\n', encoding="utf-8")
    link = project / ".codex" / "hooks.json"
    link.symlink_to(inside)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(settings_mod, "_file_lock", _retarget_at_acquire(link, outside))
        results = generate_all_settings(project, scope="project_shared")

    assert results["codex_settings"].status == "needs_confirmation"
    assert outside.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    assert inside.read_text(encoding="utf-8") == '{"hooks": {}}\n'
    assert link.is_symlink()
