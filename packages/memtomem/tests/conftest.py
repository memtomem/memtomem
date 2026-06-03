"""Shared fixtures for memtomem tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure tests/ directory is importable for helpers.py
sys.path.insert(0, str(Path(__file__).parent))

import httpx
import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components, create_components, close_components

# Re-export wiki fixtures so any test in this directory can request
# ``git_identity`` / ``wiki_root`` as a parameter without per-file imports.
# Keeping the definitions in ``_wiki_fixtures.py`` avoids bloating this
# already-heavy conftest with unrelated git-env plumbing.
from _wiki_fixtures import git_identity, wiki_root  # noqa: F401


def _ollama_available() -> bool:
    """Check if Ollama is reachable at localhost:11434."""
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _can_create_symlink() -> bool:
    """Probe whether the test runner can create filesystem symlinks.

    On Windows, ``os.symlink`` requires either Developer Mode or
    administrator privileges; without them every symlink-touching test
    raises ``OSError: [WinError 1314]`` ("client does not hold the
    required privilege"). CI is Linux-only, so this probe exists purely
    to keep the suite runnable for contributors on a locked-down Windows
    shell — they get a tidy SKIPPED row instead of a hard error in
    fixture setup.

    Both file *and* directory symlinks are probed: historically Windows
    treated them as separate privilege classes, and ``TestFsList.fs_tree``
    needs ``symlink_to(..., target_is_directory=True)`` specifically.
    Note: the probe runs in ``tempfile.gettempdir()``, which is what
    ``tmp_path`` defaults to — users who pass ``pytest --basetemp=...``
    pointed at a filesystem with different symlink semantics (e.g. FAT32,
    certain network mounts) may still see marked tests fail despite the
    probe passing.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        # File-to-file symlink.
        file_target = Path(td) / "probe-file-target"
        file_link = Path(td) / "probe-file-link"
        file_target.touch()
        try:
            file_link.symlink_to(file_target)
        except (OSError, NotImplementedError):
            return False
        # Directory symlink — separate Windows privilege historically.
        dir_target = Path(td) / "probe-dir-target"
        dir_target.mkdir()
        try:
            (Path(td) / "probe-dir-link").symlink_to(dir_target, target_is_directory=True)
        except (OSError, NotImplementedError):
            return False
    return True


def _playwright_browser_available() -> bool:
    """Probe whether ``pytest-playwright`` *and* a usable Chromium are
    present.

    Two failure modes are folded together because the symptom — a
    ``@pytest.mark.browser`` test exploding in fixture setup — is the
    same: ``pytest-playwright`` not installed (most contributor
    laptops), or installed but ``playwright install chromium`` was
    never run (the binary download is ~150 MB and is opt-in for that
    reason). Either way, auto-skipping is the right behaviour.

    The probe imports the sync API and launches headless chromium with
    a short timeout; any failure means the marker should skip. Result
    is cached in ``_PLAYWRIGHT_OK`` so the launch cost (a few hundred
    ms) is paid once per session, not per item.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, timeout=2_000)
            browser.close()
        return True
    except Exception:
        return False


_OLLAMA_UP = _ollama_available()
_CAN_SYMLINK = _can_create_symlink()
_PLAYWRIGHT_OK = _playwright_browser_available()


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests whose marker prerequisites aren't met:

    - ``@pytest.mark.ollama`` when Ollama isn't reachable.
    - ``@pytest.mark.requires_symlinks`` when the filesystem can't make
      symlinks (Windows without Developer Mode / admin shell).
    - ``@pytest.mark.browser`` when ``pytest-playwright`` or Chromium
      isn't installed (the harness in ``tests/web/`` needs both).
    """
    skip_ollama = pytest.mark.skip(reason="Ollama not running")
    skip_symlink = pytest.mark.skip(
        reason="Filesystem cannot create symlinks (Windows without Developer Mode/admin)"
    )
    skip_browser = pytest.mark.skip(
        reason="pytest-playwright + Chromium not available "
        "(install via `uv sync && uv run playwright install chromium`)"
    )
    for item in items:
        if not _OLLAMA_UP and "ollama" in item.keywords:
            item.add_marker(skip_ollama)
        if not _CAN_SYMLINK and "requires_symlinks" in item.keywords:
            item.add_marker(skip_symlink)
        if not _PLAYWRIGHT_OK and "browser" in item.keywords:
            item.add_marker(skip_browser)


@pytest.fixture(autouse=True)
def _csrf_observe_only_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default CSRF enforcement to OFF for the test suite.

    Production defaults to enforce. Most route tests build TestClients
    via ``create_app`` and exercise route logic, not the middleware —
    threading the per-process token + a loopback Host/Origin into every
    request would be churn for zero security value. The middleware is
    exhaustively tested in ``tests/test_web_csrf_middleware.py``.

    Tests that need to exercise the production posture either build their
    own FastAPI app and flip ``app.state.csrf_enforce = True``, or use
    ``monkeypatch.setenv("MEMTOMEM_WEB__CSRF_ENFORCE", ...)`` directly —
    both override this autouse default cleanly.
    """
    monkeypatch.setenv("MEMTOMEM_WEB__CSRF_ENFORCE", "0")


@pytest.fixture(autouse=True)
def _isolate_claude_projects_scan(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the ``~/.claude/projects`` scan at a nonexistent dir suite-wide.

    ``auto_display_configured_projects`` is on by default in production, so
    ``discover_project_scopes`` now scans ``~/.claude/projects/`` (filtered to
    roots with a runtime marker) unless gated. Without this isolation any test
    that lists project scopes — directly or via a route — would pick up the
    developer's real Claude project history and break exact-count assertions.
    ``_CLAUDE_PROJECTS_DIR`` is captured at import time, so sandboxing HOME does
    not cover it; we patch the module attribute directly. Tests that exercise
    the scan monkeypatch ``_CLAUDE_PROJECTS_DIR`` themselves, which overrides
    this default cleanly (and reverts in LIFO order at teardown).
    """
    import memtomem.context.projects as _proj

    monkeypatch.setattr(_proj, "_CLAUDE_PROJECTS_DIR", tmp_path / "no-claude-projects")


@pytest.fixture
async def components(tmp_path):
    """Create components with a temporary DB for isolated testing."""
    import json
    import os

    db_path = str(tmp_path / "test.db")
    mem_dir = str(tmp_path / "memories")
    # exist_ok: pytest reuses tmp_path roots; on Windows a previous session's
    # SQLite/ONNX handle leak can leave this dir behind and trip WinError 183.
    # See #206.
    (tmp_path / "memories").mkdir(exist_ok=True)

    os.environ["MEMTOMEM_STORAGE__SQLITE_PATH"] = db_path
    os.environ["MEMTOMEM_INDEXING__MEMORY_DIRS"] = json.dumps([mem_dir])
    os.environ["MEMTOMEM_EMBEDDING__MODEL"] = "bge-m3"
    os.environ["MEMTOMEM_EMBEDDING__DIMENSION"] = "1024"

    # Prevent ~/.memtomem/config.json from overriding test settings
    config = Mem2MemConfig()
    # Apply env vars directly (bypass load_config_overrides)
    config.storage.sqlite_path = Path(db_path)
    config.embedding.model = "bge-m3"
    config.embedding.dimension = 1024
    config.indexing.memory_dirs = [Path(mem_dir)]

    # Monkey-patch to skip config.json loading
    import memtomem.config as _cfg

    _orig = _cfg.load_config_overrides
    _cfg.load_config_overrides = lambda c: None
    comp = await create_components(config)
    _cfg.load_config_overrides = _orig
    yield comp
    await close_components(comp)

    for key in (
        "MEMTOMEM_STORAGE__SQLITE_PATH",
        "MEMTOMEM_INDEXING__MEMORY_DIRS",
        "MEMTOMEM_EMBEDDING__MODEL",
        "MEMTOMEM_EMBEDDING__DIMENSION",
    ):
        os.environ.pop(key, None)


@pytest.fixture
def storage(components: Components):
    return components.storage


@pytest.fixture
def pipeline(components: Components):
    return components.search_pipeline


@pytest.fixture
def engine(components: Components):
    return components.index_engine


@pytest.fixture
def memory_dir(components: Components):
    return Path(components.config.indexing.memory_dirs[0]).expanduser().resolve()


@pytest.fixture
async def bm25_only_components(tmp_path, monkeypatch):
    """Real BM25-only component stack with a tmp DB + memory_dir.

    Hermetic against ``~/.memtomem/config.json`` and developer
    ``MEMTOMEM_*`` env vars. Dense search is off so we don't pull an
    embedder; ``chunks_vec`` still needs a non-zero dimension to satisfy
    ``upsert_chunks``. Yields ``(components, memory_dir)``.

    Shared between MCP-tool integration tests that need real storage but
    no embedding model — see ``test_multi_agent_integration.py`` and
    ``test_batch_add_tag_isolation.py``.
    """
    db_path = tmp_path / "bm25.db"
    mem_dir = tmp_path / "memories"
    # exist_ok: see ``components`` fixture above — Windows tmp_path reuse. #206.
    mem_dir.mkdir(exist_ok=True)

    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.dimension = 1024
    config.search.enable_dense = False  # BM25-only — no embedder needed

    comp = await create_components(config)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)


@pytest.fixture
async def onnx_components(tmp_path, monkeypatch):
    """Component stack with ONNX multilingual MiniLM-L12 against a tmp DB.

    Hermetic — bypasses ``~/.memtomem/config.json`` and any developer-set
    ``MEMTOMEM_EMBEDDING__*`` / ``MEMTOMEM_STORAGE__*`` env vars.

    Shared between ``test_golden_path.py`` (2-chunk sanity) and
    ``test_multilingual_regression.py`` (~80-chunk quality floors).  Both files
    expect the fastembed model to be cached by the ``test-golden-path`` CI job.
    Determinism env vars (``PYTHONHASHSEED``, ``OMP_NUM_THREADS``) are set at
    the CI job level; this fixture does not re-set them in-process.
    """
    db_path = tmp_path / "golden.db"
    mem_dir = tmp_path / "memories"
    # exist_ok: see ``components`` fixture above — Windows tmp_path reuse. #206.
    mem_dir.mkdir(exist_ok=True)

    for var in (
        "MEMTOMEM_EMBEDDING__PROVIDER",
        "MEMTOMEM_EMBEDDING__MODEL",
        "MEMTOMEM_EMBEDDING__DIMENSION",
        "MEMTOMEM_STORAGE__SQLITE_PATH",
        "MEMTOMEM_INDEXING__MEMORY_DIRS",
    ):
        monkeypatch.delenv(var, raising=False)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    config.embedding.dimension = 384

    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)

    comp = await create_components(config)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)
