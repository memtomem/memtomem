"""Tests for ``memtomem.context.install`` — wiki-asset install pipeline.

Covers ADR-0008 PR-B: Invariant 1 (copytree snapshot), Invariant 3
(precise wiki-not-found error), and the OR-refusal forward-protection of
Invariant 2 (refuse-on-conflict instead of silent clobber).
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli.context_cmd import context as context_group
from memtomem.context._names import InvalidNameError
from memtomem.context.install import (
    AlreadyInstalledError,
    AssetNotFoundError,
    install_agent,
    install_command,
    install_skill,
)
from memtomem.context.lockfile import LOCKFILE_VERSION, Lockfile, LockfileCorruptError
from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.wiki.store import WikiNotFoundError, WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _seed_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> None:
    """Drop a skill into an initialized wiki and commit."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
        check=True,
        capture_output=True,
    )


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


# ── install_skill: happy paths ───────────────────────────────────────────


def test_install_skill_copies_tree(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"# foo skill\n",
            "scripts/run.sh": b"#!/bin/bash\necho hi\n",
            "overrides/claude.md": b"claude-only override\n",
        },
    )
    project = tmp_path

    result = install_skill(project, "foo")

    assert result.asset_type == "skills"
    assert result.name == "foo"
    assert result.files_written == 3
    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").read_bytes() == b"# foo skill\n"
    assert (dest / "scripts" / "run.sh").read_bytes() == b"#!/bin/bash\necho hi\n"
    assert (dest / "overrides" / "claude.md").read_bytes() == b"claude-only override\n"


def test_install_records_lockfile_entry(wiki_root: Path, tmp_path: Path) -> None:
    store = _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    result = install_skill(project, "foo")

    expected_commit = store.current_commit()
    assert result.wiki_commit == expected_commit
    assert len(result.wiki_commit) == 40

    # ISO8601-Z with microseconds: YYYY-MM-DDTHH:MM:SS.ffffffZ
    assert result.installed_at.endswith("Z")
    assert "." in result.installed_at  # microsecond separator
    assert "T" in result.installed_at

    lock_doc = json.loads((project / ".memtomem" / "lock.json").read_text())
    assert lock_doc["version"] == LOCKFILE_VERSION
    assert lock_doc["skills"]["foo"]["wiki_commit"] == expected_commit
    assert lock_doc["skills"]["foo"]["installed_at"] == result.installed_at


def test_installed_at_after_all_writes(wiki_root: Path, tmp_path: Path) -> None:
    """``installed_at`` is captured *after* every dest file is written.

    Regression for ADR-0008 PR-D's ``mtime > installed_at`` dirty check.
    If ``installed_at`` were captured before ``copy_tree_atomic``, a large
    copytree could leave dest files with mtimes later than ``installed_at``,
    false-positiving as dirty on the very next ``mm context update``.
    """
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"# foo skill\n",
            "scripts/run.sh": b"#!/bin/bash\necho hi\n",
            "scripts/helper.py": b"print('helper')\n",
            "overrides/claude.md": b"claude-only override\n",
        },
    )
    project = tmp_path

    result = install_skill(project, "foo")

    installed_at_epoch = datetime.fromisoformat(result.installed_at).timestamp()

    dest = project / ".memtomem" / "skills" / "foo"
    file_mtimes = [f.stat().st_mtime for f in dest.rglob("*") if f.is_file()]
    assert file_mtimes, "expected dest tree to contain files"

    max_mtime = max(file_mtimes)
    # installed_at MUST be >= max(dest file mtime). Allow 1ms slack to absorb
    # fs precision differences between the kernel's mtime clock and Python's
    # datetime.now() — both are wallclock-derived but may round at different
    # resolutions on some platforms.
    assert installed_at_epoch + 0.001 >= max_mtime, (
        f"installed_at ({result.installed_at}, epoch={installed_at_epoch:.6f}) "
        f"earlier than newest dest file mtime ({max_mtime:.6f}); "
        f"diff={max_mtime - installed_at_epoch:.6f}s"
    )


def test_install_skips_dotgit_in_source(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"x",
            ".git/HEAD": b"ref: refs/heads/main\n",  # synthetic — won't really be added by git
        },
    )
    project = tmp_path

    install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / ".git").exists()


def test_install_skips_dsstore_and_pycache(wiki_root: Path, tmp_path: Path) -> None:
    """COPY_SKIP_NAMES: macOS Finder + Python bytecode side-effects don't propagate."""
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"x",
            ".DS_Store": b"\x00\x00\x00\x00",
            "__pycache__/foo.cpython-312.pyc": b"\x00\x00",
        },
    )
    project = tmp_path

    install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / ".DS_Store").exists()
    assert not (dest / "__pycache__").exists()


@pytest.mark.requires_symlinks
def test_install_skips_symlinks_in_source(
    wiki_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """copy_tree_atomic refuses to dereference symlinks — would silently
    leak out-of-tree bytes (e.g., /etc/passwd) into the project otherwise."""
    _initialized_wiki(wiki_root)
    skill_dir = wiki_root / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("real", encoding="utf-8")
    # Dangling symlink — entry.is_symlink() fires regardless of target validity.
    (skill_dir / "danger.md").symlink_to("/nonexistent/target")
    subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "commit", "-m", "add foo with symlink"],
        check=True,
        capture_output=True,
    )
    project = tmp_path

    with caplog.at_level("WARNING", logger="memtomem.context._atomic"):
        install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / "danger.md").exists()
    assert any("skipping symlink" in r.message for r in caplog.records)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX file mode (stat.S_IMODE) — Windows ignores POSIX permission bits",
)
def test_install_files_written_with_default_mode(wiki_root: Path, tmp_path: Path) -> None:
    """Asset content lands at 0o644 (readable by other tools), not at the
    0o600 atomic_write_bytes default reserved for state files."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")

    skill_md = project / ".memtomem" / "skills" / "foo" / "SKILL.md"
    # Owner-readable + group/other-readable; no write for group/other.
    assert (skill_md.stat().st_mode & 0o777) == 0o644


# ── install_skill: failure paths ─────────────────────────────────────────


def test_install_project_root_missing(wiki_root: Path, tmp_path: Path) -> None:
    """A typo'd project root errors loudly instead of silently creating it."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    missing = tmp_path / "nonexistent"

    with pytest.raises(FileNotFoundError, match="project root does not exist"):
        install_skill(missing, "foo")


def test_install_wiki_missing_invariant3(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_identity: None,
) -> None:
    """Invariant 3: precise message including path and `mm wiki init`."""
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    project = tmp_path

    with pytest.raises(WikiNotFoundError) as excinfo:
        install_skill(project, "foo")
    assert "wiki not found at" in str(excinfo.value)
    assert "mm wiki init" in str(excinfo.value)


def test_install_asset_missing(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    project = tmp_path
    with pytest.raises(AssetNotFoundError, match="skills/nope"):
        install_skill(project, "nope")


def test_install_refuses_when_lockfile_and_dest_present(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    assert "lockfile_entry=yes" in msg
    assert "dest=yes" in msg


def test_install_refuses_when_only_lockfile_present(wiki_root: Path, tmp_path: Path) -> None:
    """The OR-not-AND case: user wiped dest but left lockfile orphaned."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")
    shutil.rmtree(project / ".memtomem" / "skills" / "foo")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    assert "lockfile_entry=yes" in msg
    assert "dest=no" in msg


def test_install_refuses_when_only_dest_present(wiki_root: Path, tmp_path: Path) -> None:
    """Lockfile damaged (or external copy) but dest exists."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path
    pre = project / ".memtomem" / "skills" / "foo"
    pre.mkdir(parents=True)
    (pre / "stray.txt").write_text("hand-placed", encoding="utf-8")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    assert "lockfile_entry=no" in msg
    assert "dest=yes" in msg
    # Hand-placed file must not be clobbered.
    assert (pre / "stray.txt").read_text() == "hand-placed"


def test_install_over_corrupt_lockfile_names_the_lockfile(wiki_root: Path, tmp_path: Path) -> None:
    """Corrupt lock.json + dest on disk used to wedge: install said
    AlreadyInstalled ("run update"), update said NotInstalled ("run
    install") — each pointing at the other while the tolerant reset was
    one upsert away from wiping every sibling entry (#1247 id 16). The
    strict read now names the real problem before either check runs."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    install_skill(project, "foo")
    lock_json = project / ".memtomem" / "lock.json"
    corrupt = b"not valid json {{"
    lock_json.write_bytes(corrupt)

    with pytest.raises(LockfileCorruptError, match="lock.json"):
        install_skill(project, "foo")
    assert lock_json.read_bytes() == corrupt  # refusal left the file untouched


def test_install_invalid_name(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki(wiki_root)
    project = tmp_path
    with pytest.raises(InvalidNameError):
        install_skill(project, "../escape")


# ── concurrency ──────────────────────────────────────────────────────────


def _install_worker(wiki_path_str: str, project_str: str, name: str) -> None:
    """Subprocess body — one install per worker, distinct skill names."""
    import os

    os.environ["MEMTOMEM_WIKI_PATH"] = wiki_path_str
    install_skill(Path(project_str), name)


def test_install_two_skills_concurrent(wiki_root: Path, tmp_path: Path) -> None:
    """Two installers, distinct skill names, share one lockfile."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    _seed_skill(wiki_root, "bar", {"SKILL.md": b"y"})
    project = tmp_path

    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_install_worker, args=(str(wiki_root), str(project), "foo"))
    p2 = ctx.Process(target=_install_worker, args=(str(wiki_root), str(project), "bar"))
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert p1.exitcode == 0, "installer 1 crashed"
    assert p2.exitcode == 0, "installer 2 crashed"

    lock_doc = json.loads((project / ".memtomem" / "lock.json").read_text())
    assert "foo" in lock_doc["skills"]
    assert "bar" in lock_doc["skills"]
    assert (project / ".memtomem" / "skills" / "foo" / "SKILL.md").read_bytes() == b"x"
    assert (project / ".memtomem" / "skills" / "bar" / "SKILL.md").read_bytes() == b"y"


# ── CLI ──────────────────────────────────────────────────────────────────


@pytest.fixture
def project_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp project root (with a sentinel ``.git``) wired as cwd so
    ``_find_project_root`` resolves there. Uses a subdirectory of
    ``tmp_path`` so ``_find_project_root`` doesn't accidentally walk up
    into the test runner's ``tmp_path`` parent and find an unrelated
    ``.git``/``pyproject.toml`` first."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return project


def test_cli_install_success(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "hello", {"SKILL.md": b"# hello\n"})

    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "hello"])

    assert result.exit_code == 0, result.output
    assert "Installed skills/hello" in result.output
    assert (project_cwd / ".memtomem" / "skills" / "hello" / "SKILL.md").is_file()
    assert (project_cwd / ".memtomem" / "lock.json").is_file()


def test_cli_install_wiki_missing_message(
    monkeypatch: pytest.MonkeyPatch,
    project_cwd: Path,
    tmp_path: Path,
    git_identity: None,
) -> None:
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "anything"])

    assert result.exit_code != 0
    assert "wiki not found at" in result.output


def test_cli_install_rejects_unknown_type(project_cwd: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "settings", "foo"])
    # PR-C: only skill / agent / command are allowed types.
    assert result.exit_code != 0
    assert "settings" in result.output  # click usage error mentions the bad value


def test_cli_install_already_installed_classified_message(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "hello", {"SKILL.md": b"# hello\n"})

    runner = CliRunner()
    runner.invoke(context_group, ["install", "skill", "hello"])
    result = runner.invoke(context_group, ["install", "skill", "hello"])

    assert result.exit_code != 0
    assert "lockfile_entry=yes" in result.output
    assert "dest=yes" in result.output


# ── Lockfile assertions about the live install ──────────────────────────


def test_lockfile_contains_only_mandated_keys_per_entry(wiki_root: Path, tmp_path: Path) -> None:
    """Schema discipline: install writes exactly the mandated keys.

    ``files`` + ``files_commit`` joined the schema with the #1247
    deletion-fidelity work (the installed file manifest enabling
    deletion-dirty detection and update reconciliation); ``digests`` +
    ``digests_installed_at`` with the #1247 id 15 content-digest work
    (byte-exact dirty detection closing the during-install absorption
    window). The digest is the SHA-256 of the bytes the copier wrote,
    paired to the entry's own ``installed_at``.
    """
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path
    install_skill(project, "foo")

    lock = Lockfile.at(project)
    entry = lock.read_entry("skills", "foo")
    assert entry is not None
    assert set(entry) == {
        "wiki_commit",
        "installed_at",
        "files",
        "files_commit",
        "digests",
        "digests_installed_at",
    }
    assert entry["files"] == ["SKILL.md"]
    assert entry["files_commit"] == entry["wiki_commit"]
    assert entry["digests"] == {"SKILL.md": hashlib.sha256(b"x").hexdigest()}
    assert entry["digests_installed_at"] == entry["installed_at"]
    assert entry["files"] == sorted(entry["digests"])  # same written set, one source


# ── Gate A on wiki-install ingress (ADR-0011 §5, #1247 id 3) ─────────────

# AKIA fixture per feedback_force_unsafe_redaction_valve_only.md — a clean
# string never trips the scan, so every block assertion below would
# false-pass without it.
SECRET = "api_key=AKIA1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    """Zeroed counters so surface-attribution asserts see only their own test."""
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def test_install_blocks_secret_in_wiki_src_zero_residue(wiki_root: Path, tmp_path: Path) -> None:
    """Gate A fires on wiki ingress: a poisoned ``SKILL.md`` never lands —
    scan precedes ``dest.parent.mkdir`` and the lockfile upsert, so a
    refusal leaves zero residue in the project tree."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "leak", {"SKILL.md": b"# leak skill\n" + SECRET.encode() + b"\n"})
    project = tmp_path

    with pytest.raises(PrivacyBlockedError) as excinfo:
        install_skill(project, "leak")

    assert not (project / ".memtomem" / "skills" / "leak").exists()
    assert Lockfile.at(project).read_entry("skills", "leak") is None
    # Matched bytes never reach the error message (path + hit count only).
    assert "AKIA1234567890ABCDEF" not in excinfo.value.message
    # Audit attributes the block to the single-install ingress surface
    # (#1246/#1248 rule), not a sibling sync/migrate surface.
    by_tool = privacy.snapshot()["by_tool"]
    assert by_tool.get("cli_context_install", {}).get("blocked", 0) == 1
    assert "cli_context_sync" not in by_tool


@pytest.mark.parametrize(
    ("asset_type", "verb"),
    [("skills", install_skill), ("agents", install_agent), ("commands", install_command)],
)
def test_install_surface_kwarg_threads_to_audit(
    wiki_root: Path, tmp_path: Path, asset_type: str, verb
) -> None:
    """A caller-supplied ``surface=`` reaches the Gate-A audit record on all
    three install wrappers — the web route relies on this so a browser-
    triggered block is not logged as ``cli_context_install`` (audit
    misattribution). Parametrized because the route dispatches per asset
    type; a wrapper that silently drops the kwarg would only misattribute
    that one kind."""
    _initialized_wiki(wiki_root)
    asset_dir = wiki_root / asset_type / "leak"
    asset_dir.mkdir(parents=True)
    (asset_dir / "main.md").write_bytes(b"# leak\n" + SECRET.encode() + b"\n")
    subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "commit", "-m", "add leak"],
        check=True,
        capture_output=True,
    )

    with pytest.raises(PrivacyBlockedError):
        verb(tmp_path, "leak", surface="web_context_install")

    by_tool = privacy.snapshot()["by_tool"]
    assert by_tool["web_context_install"]["blocked"] >= 1
    assert "cli_context_install" not in by_tool


def test_install_no_false_block_on_wiki_shipped_bak(wiki_root: Path, tmp_path: Path) -> None:
    """Scan set == copy set: a wiki-shipped secret ``.bak`` is outside the
    copier's effective set (#1250), so it must neither land in dest nor
    false-block the otherwise-clean install."""
    _initialized_wiki(wiki_root)
    _seed_skill(
        wiki_root,
        "foo",
        {
            "SKILL.md": b"# clean skill\n",
            "foo.md.bak": SECRET.encode() + b"\n",
        },
    )
    project = tmp_path

    result = install_skill(project, "foo")  # must NOT raise

    dest = project / ".memtomem" / "skills" / "foo"
    assert (dest / "SKILL.md").read_bytes() == b"# clean skill\n"
    assert result.files_written == 1
    assert not list(dest.rglob("*.bak"))


def test_cli_install_privacy_block_exit_and_message(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    """CLI boundary: Gate A block maps to ``click.ClickException`` (exit 1)
    with the remediation message — never a traceback, never the secret."""
    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "leak", {"SKILL.md": b"# leak skill\n" + SECRET.encode() + b"\n"})

    runner = CliRunner()
    result = runner.invoke(context_group, ["install", "skill", "leak"])

    assert result.exit_code == 1, result.output
    assert "Gate A" in result.output
    assert "AKIA1234567890ABCDEF" not in result.output
    assert "Traceback" not in result.output


# ── interrupted-install hint (#1247 id 4) ────────────────────────────────


def test_install_interrupted_before_lockfile_hint_breaks_circle(
    wiki_root: Path, tmp_path: Path
) -> None:
    """#1247 id 4: the dest-only state must not point at `mm context update`.

    A copy that succeeds but whose lockfile upsert fails (disk full,
    Ctrl-C) leaves dest with no entry. Pre-fix, install's refusal said
    "run `mm context update`" unconditionally, and update raised
    NotInstalledError saying "run `mm context install` first" — a closed
    circle with no in-product exit."""
    from memtomem.context.install import NotInstalledError, update_skill

    _initialized_wiki(wiki_root)
    _seed_skill(wiki_root, "foo", {"SKILL.md": b"x"})
    project = tmp_path

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    with pytest.MonkeyPatch.context() as patch_ctx:
        patch_ctx.setattr(Lockfile, "upsert_entry", _boom)
        with pytest.raises(OSError):
            install_skill(project, "foo")

    dest = project / ".memtomem" / "skills" / "foo"
    assert dest.is_dir()  # half-installed: copy landed, lockfile write failed

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)
    # Pin-and-invert: the old circular hint must be gone for dest-only...
    assert "mm context update" not in msg
    assert "lockfile_entry=no" in msg
    assert "dest=yes" in msg
    assert "interrupted" in msg
    assert "mm context install skill foo" in msg
    assert str(dest) in msg

    # ...and the other half of the old circle still dead-ends by design:
    # update without an entry refuses toward install. Proves the pre-fix
    # hint pair really was a closed loop.
    with pytest.raises(NotInstalledError):
        update_skill(project, "foo")
