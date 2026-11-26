"""Offline tests for tools/agent-eval/plugin/launch_server.py (issue #3627).

The launcher starts a memtomem MCP server for ``claude plugin eval`` runs. These tests pin
the checks that keep it from touching anything outside an eval run's temporary HOME. They
never start a server or call a model.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the launcher is POSIX-only (pwd, O_NOFOLLOW)"
)

_PATH = Path(__file__).resolve().parents[3] / "tools" / "agent-eval" / "plugin" / "launch_server.py"
_MARKED = {"CLAUDE_CODE_EVAL_CONFINED": "1"}


@pytest.fixture(scope="module")
def launcher():
    spec = importlib.util.spec_from_file_location("agent_eval_launch_server", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run(tmp_path: Path):
    """An eval-run-shaped tree: HOME with a workspace inside, and a separate real home."""
    home = tmp_path / "e-abc" / "home"
    cwd = home / "cwd"
    cwd.mkdir(parents=True)
    real = tmp_path / "realhome"
    real.mkdir()
    return home, cwd, real


def test_accepts_an_eval_run(launcher, run):
    home, cwd, real = run
    launcher.check_sandbox(_MARKED, home, cwd, real)


def test_refuses_without_the_host_marker(launcher, run):
    home, cwd, real = run
    with pytest.raises(launcher.LaunchError, match="CLAUDE_CODE_EVAL_CONFINED"):
        launcher.check_sandbox({}, home, cwd, real)


def test_refuses_the_real_home(launcher, run):
    home, cwd, _ = run
    with pytest.raises(launcher.LaunchError, match="real home"):
        launcher.check_sandbox(_MARKED, home, cwd, home)


def test_refuses_a_home_inside_the_real_home(launcher, run):
    home, cwd, _ = run
    with pytest.raises(launcher.LaunchError, match="real home"):
        launcher.check_sandbox(_MARKED, home, cwd, home.parent)


def test_refuses_a_home_above_the_real_home(launcher, tmp_path):
    # HOME=<parent of the real home> with cwd inside the real home must not pass.
    home = tmp_path / "e-abc" / "home"
    real = home / "users" / "me"
    cwd = real / "project"
    cwd.mkdir(parents=True)
    with pytest.raises(launcher.LaunchError, match="above it"):
        launcher.check_sandbox(_MARKED, home, cwd, real)


def test_refuses_a_cwd_outside_home(launcher, run, tmp_path):
    home, _, real = run
    with pytest.raises(launcher.LaunchError, match="not inside HOME"):
        launcher.check_sandbox(_MARKED, home, tmp_path, real)


@pytest.mark.parametrize("existing", [".mm-eval", "mm-notes"])
def test_prepare_refuses_existing_paths_and_leaves_their_targets_alone(
    launcher, run, tmp_path, existing
):
    home, cwd, _ = run
    target = tmp_path / "live"
    target.mkdir()
    (target / "config.json").write_text("live", encoding="utf-8")
    link = (home if existing == ".mm-eval" else cwd) / existing
    link.symlink_to(target)
    with pytest.raises(launcher.LaunchError, match="already exists"):
        launcher.prepare(home, cwd)
    assert sorted(p.name for p in target.iterdir()) == ["config.json"]
    assert (target / "config.json").read_text(encoding="utf-8") == "live"


def test_prepare_and_config_point_only_inside_the_run(launcher, run):
    home, cwd, _ = run
    paths = launcher.prepare(home, cwd)
    launcher.write_config(paths)
    config = json.loads((paths["home"] / ".memtomem" / "config.json").read_text("utf-8"))
    assert config["indexing"]["memory_dirs"] == [str(cwd / "mm-notes")]
    assert Path(config["storage"]["sqlite_path"]).parent == home / ".mm-eval"
    assert stat.S_IMODE(paths["runtime"].stat().st_mode) == 0o700


def test_server_env_is_built_from_scratch(launcher, run, monkeypatch, tmp_path):
    home, cwd, _ = run
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/real/store.db")
    paths = launcher.prepare(home, cwd)
    env = launcher.server_env(paths, tmp_path / "repo")
    assert {k for k in env if k.startswith("MEMTOMEM_")} == {"MEMTOMEM_TOOL_MODE"}
    assert env["PYTHON_DOTENV_DISABLED"] == "1"
    assert env["_MEMTOMEM_TEST_RUNTIME_DIR"] == str(paths["runtime"])
    assert env["PYTEST_CURRENT_TEST"]
    assert env["HOME"] == str(paths["home"])
    assert env["PYTHONPATH"] == str(tmp_path / "repo" / "packages" / "memtomem" / "src")


@pytest.fixture
def seeds(tmp_path: Path) -> Path:
    root = tmp_path / "seeds"
    (root / "good").mkdir(parents=True)
    (root / "good" / "note.md").write_text("## Note\n\nTOKEN-1\n", encoding="utf-8")
    return root


def test_seed_files_accepts_a_plain_seed(launcher, seeds):
    assert [p.name for p in launcher.seed_files("good", seeds)] == ["note.md"]


@pytest.mark.parametrize("bad", ["Good", "good/../good", "-good", ""])
def test_seed_files_refuses_names_outside_the_grammar(launcher, seeds, bad):
    # The match on the message pins the grammar: without it these fail later (or not at all)
    # with a different error. On a case-insensitive filesystem "Good" even opens "good".
    (seeds / "-good").mkdir()
    (seeds / "-good" / "note.md").write_text("x", encoding="utf-8")
    with pytest.raises(launcher.LaunchError, match="invalid seed name"):
        launcher.seed_files(bad, seeds)


def test_seed_files_refuses_a_symlinked_seed_dir(launcher, seeds, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("secret", encoding="utf-8")
    (seeds / "linked").symlink_to(outside)
    with pytest.raises(launcher.LaunchError, match="unknown seed"):
        launcher.seed_files("linked", seeds)


def test_seed_files_refuses_a_symlink_to_a_sibling_seed(launcher, seeds):
    # resolve() alone would accept this one: the target's parent is the seeds root.
    (seeds / "alias").symlink_to(seeds / "good")
    with pytest.raises(launcher.LaunchError, match="unknown seed"):
        launcher.seed_files("alias", seeds)


def test_seed_files_refuses_a_symlinked_file(launcher, seeds, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("secret", encoding="utf-8")
    (seeds / "good" / "leak.md").symlink_to(secret)
    with pytest.raises(launcher.LaunchError, match="not a regular file"):
        launcher.seed_files("good", seeds)


def test_copy_seed_refuses_to_overwrite(launcher, seeds, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "note.md").write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        launcher.copy_seed(launcher.seed_files("good", seeds), notes)
    assert (notes / "note.md").read_text(encoding="utf-8") == "existing"


def _fake_checkout(root: Path, body: str = "") -> Path:
    pkg = root / "packages" / "memtomem" / "src" / "memtomem"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")
    return root


def _probe_env(repo: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(repo / "packages" / "memtomem" / "src")}


def test_import_origin_accepts_the_checkout(launcher, tmp_path):
    repo = _fake_checkout(tmp_path / "repo")
    launcher.check_import_origin(Path(sys.executable), _probe_env(repo), repo)


def test_import_origin_refuses_another_tree(launcher, tmp_path):
    repo = _fake_checkout(tmp_path / "repo")
    other = _fake_checkout(tmp_path / "other")
    with pytest.raises(launcher.LaunchError, match="imports from"):
        launcher.check_import_origin(Path(sys.executable), _probe_env(other), repo)


def test_import_origin_refuses_a_foreign_package_with_a_linked_init(launcher, tmp_path):
    repo = _fake_checkout(tmp_path / "repo")
    foreign = tmp_path / "foreign" / "packages" / "memtomem" / "src" / "memtomem"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").symlink_to(
        repo / "packages" / "memtomem" / "src" / "memtomem" / "__init__.py"
    )
    with pytest.raises(launcher.LaunchError, match="imports from"):
        launcher.check_import_origin(Path(sys.executable), _probe_env(tmp_path / "foreign"), repo)


def test_import_origin_refuses_an_import_failure(launcher, tmp_path):
    repo = _fake_checkout(tmp_path / "repo", "raise ImportError('broken')\n")
    with pytest.raises(launcher.LaunchError, match="cannot import"):
        launcher.check_import_origin(Path(sys.executable), _probe_env(repo), repo)


class _Exec(Exception):
    """Stands in for os.execve so main() stops where the server would start."""


def test_main_gives_every_child_the_isolated_env_and_one_interpreter(
    launcher, run, seeds, monkeypatch
):
    home, cwd, real = run
    python = Path("/opt/venv/bin/python")
    calls: list[tuple[list[str], dict[str, str], str | None]] = []
    origin = launcher.REPO / "packages" / "memtomem" / "src" / "memtomem"
    executed: list[str] = []
    chdirs: list[str] = []

    def fake_run(argv, *, env, cwd=None, **_):
        calls.append((list(argv), dict(env), cwd))
        out = str(origin) if "memtomem.__path__" in argv[-1] else ""
        return launcher.subprocess.CompletedProcess(argv, 0, out, "")

    def fake_execve(path, argv, env):
        executed.append(str(path))
        calls.append((list(argv), dict(env), chdirs[-1] if chdirs else None))
        raise _Exec

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CODE_EVAL_CONFINED", "1")
    monkeypatch.setenv("EVAL_MM_SEED", "good")
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/real/store.db")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(launcher, "SEEDS", seeds)
    monkeypatch.setattr(launcher, "select_python", lambda: python)
    monkeypatch.setattr(launcher.pwd, "getpwuid", lambda _: type("P", (), {"pw_dir": str(real)}))
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.os, "execve", fake_execve)
    monkeypatch.setattr(launcher.os, "dup2", lambda *_: None)
    monkeypatch.setattr(launcher.os, "chdir", lambda path: chdirs.append(str(path)))

    with pytest.raises(_Exec):
        launcher.main()

    box = home.resolve() / ".mm-eval"
    assert len(calls) == 3  # import probe, indexer, server
    assert "memtomem.__path__" in calls[0][0][-1]
    assert executed == [str(python)]
    assert calls[1][0] == [
        str(python),
        "-P",
        "-c",
        launcher._INDEX,
        "index",
        str(cwd.resolve() / "mm-notes"),
    ]
    assert calls[2][0][-1] == launcher._SERVE
    expected_env = launcher.server_env(
        {
            "box": box,
            "home": box / "home",
            "runtime": box / "runtime",
            "db": box / "memtomem.db",
            "notes": cwd.resolve() / "mm-notes",
        }
    )
    for argv, env, child_cwd in calls:
        assert argv[0] == str(python)
        assert argv[1] == "-P"
        assert env == expected_env
        assert child_cwd == str(box)  # probe, indexer, and the server's chdir
    assert (cwd / "mm-notes" / "note.md").read_text(encoding="utf-8") == "## Note\n\nTOKEN-1\n"


def test_link_today_aliases_an_empty_day_file(launcher, run):
    from datetime import datetime, timezone

    home, cwd, _ = run
    notes = launcher.prepare(home, cwd)["notes"]
    alias = launcher.link_today(cwd, notes, datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc))
    assert alias == cwd / "mm-today.md"
    assert alias.resolve() == (notes / "2026-01-02.md").resolve()
    assert alias.read_text(encoding="utf-8") == ""


def test_link_today_refuses_an_existing_day_file(launcher, run):
    from datetime import datetime, timezone

    home, cwd, _ = run
    notes = launcher.prepare(home, cwd)["notes"]
    (notes / "2026-01-02.md").write_text("seeded", encoding="utf-8")
    with pytest.raises(FileExistsError):
        launcher.link_today(cwd, notes, datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert (notes / "2026-01-02.md").read_text(encoding="utf-8") == "seeded"
