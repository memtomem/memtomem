"""``mm mem init`` — project memory tier init/register verb (#1700).

Pins the ADR-0011 opt-in contract:

1. The verb creates ``<project>/.memtomem/memories[.local]`` and registers
   it in ``indexing.project_memory_dirs`` via a locked, atomic
   read-modify-write on ``config.json``.
2. Registration is an explicit trust operation: a project marker
   (``.git`` / ``pyproject.toml``) is hard-required, ``project_shared``
   passes Gate B, and for ``project_local`` the ``.gitignore`` guard is
   established *before* registration (a failed write aborts, leaving
   nothing registered).
3. ``register_project_memory_dir`` survives concurrent registrations
   (cross-process lock) and preserves fragment-contributed entries
   (config.json is REPLACE-on-load, so the aggregate list is pinned).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem import config as _cfg
from memtomem.config import (
    Mem2MemConfig,
    load_config_d,
    load_config_overrides,
    register_project_memory_dir,
)
from memtomem.memory_scope import is_project_tier_registered

from .helpers import set_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME + ``_override_path`` pointing into it."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    override = home / ".memtomem" / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: override)
    return home


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A marker-bearing project root, with cwd inside it."""
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    return root


def _invoke(args: list[str], **kwargs):
    from memtomem.cli.mem_cmd import mem

    return CliRunner().invoke(mem, args, **kwargs)


def _loaded_config() -> Mem2MemConfig:
    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg, migrate=False)
    return cfg


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_init_project_local_creates_registers_and_gitignores(home: Path, project: Path) -> None:
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output

    tier = project / ".memtomem" / "memories.local"
    assert tier.is_dir()

    # Registered — round-trip through the real loader, and the exact
    # membership check every write surface uses.
    cfg = _loaded_config()
    assert is_project_tier_registered(tier, cfg.indexing.project_memory_dirs)

    # .gitignore guard block landed (before registration — see ordering test).
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert ".memtomem/*.local/" in gitignore

    # Under HOME the persisted entry is tilde-relativized (portable-paths
    # parity with ``memory_dirs``) — but this project lives OUTSIDE the
    # isolated HOME, so the raw entry stays absolute here; parity is pinned
    # separately in test_register_relativizes_under_home.
    assert "restart" in result.output


def test_init_is_idempotent(home: Path, project: Path) -> None:
    assert _invoke(["init"]).exit_code == 0
    rerun = _invoke(["init"])
    assert rerun.exit_code == 0
    assert "Already initialized" in rerun.output

    cfg = _loaded_config()
    entries = [str(d) for d in cfg.indexing.project_memory_dirs]
    assert len(entries) == len(set(entries)) == 1


def test_init_requires_project_marker(home: Path, tmp_path: Path, monkeypatch) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    result = _invoke(["init"])
    assert result.exit_code != 0
    assert "git init" in result.output
    assert not (bare / ".memtomem").exists()
    assert _loaded_config().indexing.project_memory_dirs == []


def test_init_project_shared_gate_b(home: Path, project: Path) -> None:
    # Declined interactive confirm → abort, nothing created or registered.
    refused = _invoke(["init", "--scope", "project_shared"], input="n\n")
    assert refused.exit_code != 0
    assert not (project / ".memtomem" / "memories").exists()
    assert _loaded_config().indexing.project_memory_dirs == []

    # Explicit flag → proceeds.
    ok = _invoke(["init", "--scope", "project_shared", "--confirm-project-shared"])
    assert ok.exit_code == 0, ok.output
    tier = project / ".memtomem" / "memories"
    assert tier.is_dir()
    assert is_project_tier_registered(tier, _loaded_config().indexing.project_memory_dirs)


def test_init_gitignore_failure_aborts_before_registration(home: Path, project: Path) -> None:
    """Codex design-gate Major: git protection FIRST, registration last.

    ``.gitignore`` as a directory makes the append raise ``OSError``
    deterministically (no chmod, works under root CI). The verb must
    abort with the local tier neither created nor registered.
    """
    (project / ".gitignore").mkdir()
    result = _invoke(["init"])
    assert result.exit_code != 0
    assert "Aborting before registration" in result.output
    assert not (project / ".memtomem" / "memories.local").exists()
    assert _loaded_config().indexing.project_memory_dirs == []


def test_init_pyproject_only_discloses_unprotected(home: Path, tmp_path, monkeypatch) -> None:
    root = tmp_path / "pyproj"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    monkeypatch.chdir(root)
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    assert "NOT git-protected" in result.output
    # Still registers — disclosure, not refusal.
    tier = root / ".memtomem" / "memories.local"
    assert is_project_tier_registered(tier, _loaded_config().indexing.project_memory_dirs)


def test_init_resolves_write_surface_project_context(home: Path, project: Path) -> None:
    """The exact seam ``mem_add`` uses: after init, the cwd resolves to the
    project root via the registered dirs (no server restart needed for
    fresh-process CLI writes)."""
    from memtomem.server.tools.search import _resolve_project_context_from_dirs

    assert _invoke(["init"]).exit_code == 0
    cfg = _loaded_config()
    resolved = _resolve_project_context_from_dirs(cfg.indexing.project_memory_dirs)
    assert resolved is not None
    assert resolved == project.resolve()


# ---------------------------------------------------------------------------
# register_project_memory_dir — unit + persistence contracts
# ---------------------------------------------------------------------------


def test_register_rejects_non_tier_paths(home: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a project memory tier"):
        register_project_memory_dir(tmp_path / "random_dir")
    with pytest.raises(ValueError, match="not a project memory tier"):
        # Right leaf name, wrong parent.
        register_project_memory_dir(tmp_path / "memories")


def test_register_returns_false_when_already_present(home: Path, tmp_path: Path) -> None:
    tier = tmp_path / "p" / ".memtomem" / "memories"
    assert register_project_memory_dir(tier) is True
    assert register_project_memory_dir(tier) is False
    raw = json.loads(_cfg._override_path().read_text(encoding="utf-8"))
    assert len(raw["indexing"]["project_memory_dirs"]) == 1


def test_register_relativizes_under_home(home: Path) -> None:
    tier = home / "work" / "proj" / ".memtomem" / "memories.local"
    assert register_project_memory_dir(tier) is True
    raw = json.loads(_cfg._override_path().read_text(encoding="utf-8"))
    assert raw["indexing"]["project_memory_dirs"] == ["~/work/proj/.memtomem/memories.local"]


def test_register_preserves_fragment_entries(home: Path, tmp_path: Path) -> None:
    """config.json REPLACES the fragment-appended list on load, so the
    helper must persist the aggregate — otherwise the write would mask
    fragment registrations on the next load (Codex design-gate item)."""
    frag_tier = tmp_path / "frag_proj" / ".memtomem" / "memories"
    config_d = home / ".memtomem" / "config.d"
    config_d.mkdir(parents=True)
    (config_d / "10-frag.json").write_text(
        json.dumps({"indexing": {"project_memory_dirs": [str(frag_tier)]}}),
        encoding="utf-8",
    )

    # Registering the fragment's own entry is a no-op: already effective.
    assert register_project_memory_dir(frag_tier) is False
    assert not _cfg._override_path().exists()

    # Registering a NEW entry pins the aggregate (fragment + new).
    new_tier = tmp_path / "new_proj" / ".memtomem" / "memories.local"
    assert register_project_memory_dir(new_tier) is True
    cfg = _loaded_config()
    resolved = {Path(d).expanduser().resolve() for d in cfg.indexing.project_memory_dirs}
    assert frag_tier.resolve() in resolved
    assert new_tier.resolve() in resolved


def test_register_preserves_unrelated_config_keys(home: Path, tmp_path: Path) -> None:
    override = _cfg._override_path()
    override.parent.mkdir(parents=True)
    override.write_text(
        json.dumps({"indexing": {"memory_dirs": ["~/memories"]}, "decay": {"enabled": False}}),
        encoding="utf-8",
    )
    register_project_memory_dir(tmp_path / "p" / ".memtomem" / "memories")
    raw = json.loads(override.read_text(encoding="utf-8"))
    assert raw["indexing"]["memory_dirs"] == ["~/memories"]
    assert raw["decay"] == {"enabled": False}


def test_register_concurrent_processes_lose_nothing(home: Path, tmp_path: Path) -> None:
    """Codex design-gate Major: the whole read→append→write runs inside
    the config write lock, so N concurrent registrations (distinct
    projects, separate processes — the lock is cross-process) must all
    survive. A naive read-outside-lock implementation loses entries."""
    n = 4
    override = _cfg._override_path()
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from memtomem.config import register_project_memory_dir\n"
        "register_project_memory_dir(Path(sys.argv[1]), config_path=Path(sys.argv[2]))\n"
    )
    tiers = [tmp_path / f"proj{i}" / ".memtomem" / "memories" for i in range(n)]
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(t), str(override)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for t in tiers
    ]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()

    raw = json.loads(override.read_text(encoding="utf-8"))
    persisted = {Path(d).expanduser().resolve() for d in raw["indexing"]["project_memory_dirs"]}
    assert persisted == {t.resolve() for t in tiers}


def test_save_config_overrides_does_not_clobber_registration(home: Path, tmp_path: Path) -> None:
    """``project_memory_dirs`` stays OUT of ``_EXTRA_MUTATION_FIELDS``: a
    long-running process (``mm web``) that loaded config *before* a
    registration would otherwise pop the key on its next unrelated save
    (stale live == comparand). Generic saves must leave the key alone."""
    from memtomem.config import save_config_overrides

    # A process loads its config BEFORE the registration happens.
    stale_cfg = _loaded_config()
    assert stale_cfg.indexing.project_memory_dirs == []

    tier = tmp_path / "p" / ".memtomem" / "memories"
    register_project_memory_dir(tier)

    # The stale process saves an unrelated override.
    stale_cfg.search.default_top_k = 99
    save_config_overrides(stale_cfg)

    raw = json.loads(_cfg._override_path().read_text(encoding="utf-8"))
    assert raw["indexing"]["project_memory_dirs"], "registration was clobbered by a generic save"
    assert raw["search"] == {"default_top_k": 99}
