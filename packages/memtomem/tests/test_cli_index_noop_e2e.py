"""End-to-end pins: fresh ``mm init --provider none`` → ``mm index`` → search.

Before the fix, a fresh ``--provider none`` install did not create the
``chunks_vec`` virtual table (NoopEmbedder dim=0), and every subsequent
``upsert_chunks`` crashed with ``no such table: chunks_vec``. These tests
exercise the whole user journey end-to-end so any regression of the
unconditional write paths is caught immediately.

Two variants are kept intentionally:

* **inline** — ``CliRunner`` invocations share the process, so the fix
  is observed directly without subprocess overhead.
* **subprocess** — ``sys.executable -m memtomem`` round-trip covers the
  process boundary (``HOME`` / ``XDG_CONFIG_HOME`` / CWD plumbing) that
  in-process tests can't surface.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


def _make_memory_dir(home: str) -> str:
    mem_dir = os.path.join(home, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "test.md"), "w", encoding="utf-8") as f:
        f.write("# memo\n\nhello world this is a bm25 smoke test\n")
    return mem_dir


class TestFreshNoopIndexInline:
    def test_init_index_search_via_cli_runner(self, tmp_path, monkeypatch):
        """``CliRunner`` round-trip: init → index → search must all succeed.

        Three-layer isolation needed in-process:

        1. ``HOME`` env override — caught by ``Path.home()`` calls that run
           inside command functions (e.g. ``init_cmd.py`` config writer).
        2. Patch ``_bootstrap._CONFIG_PATH`` — that module-level constant is
           bound at import time, so ``monkeypatch.setenv`` alone leaves the
           ``cli_components`` existence check pointing at the real home.
           Previously masked locally by a pre-existing real ``~/.memtomem/
           config.json`` but exposed in CI (no leaked state).
        3. Strip ``MEMTOMEM_*`` env overrides — pydantic-settings binds
           any ``MEMTOMEM_<SECTION>__<KEY>`` from the parent shell into
           the freshly built config, so a developer's
           ``MEMTOMEM_SEARCH__ENABLE_BM25=false`` (or any indexing/storage
           override) leaks into the test and the search assertion below
           comes back as ``0 BM25 + 0 dense → 0 results``. Filter on the
           full prefix so a future config section is covered automatically.
        """
        from memtomem.cli import _bootstrap

        for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
            monkeypatch.delenv(var, raising=False)

        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")

        mem_dir = _make_memory_dir(str(home))

        runner = CliRunner()

        r = runner.invoke(
            cli,
            [
                "init",
                "-y",
                "--provider",
                "none",
                "--memory-dir",
                mem_dir,
                "--mcp",
                "skip",
            ],
        )
        assert r.exit_code == 0, f"init failed: {r.output}"

        r = runner.invoke(cli, ["index", mem_dir])
        assert r.exit_code == 0, f"index failed: {r.output}"
        # Pre-fix: "no such table: chunks_vec". Post-fix: "1 new".
        assert "no such table" not in r.output
        assert "1 new" in r.output or "1 file" in r.output

        r = runner.invoke(cli, ["search", "hello"])
        assert r.exit_code == 0, f"search failed: {r.output}"
        assert "hello world" in r.output


class TestFreshNoopIndexSubprocess:
    def test_init_index_search_via_subprocess(self, tmp_path):
        """Out-of-process variant: catches regressions that only manifest
        across the HOME / XDG boundary (e.g. config.json path resolution).

        Uses the ``mm`` script installed by ``uv pip install -e`` (co-located
        with ``sys.executable``) rather than ``python -m memtomem`` — the
        package has no ``__main__`` module, and the installed entry point is
        what real users hit.
        """
        # ``shutil.which`` adds the platform-correct suffix (``.exe`` on
        # Windows via PATHEXT, none on POSIX), so the same lookup works
        # against both ``.venv/bin/mm`` and ``.venv/Scripts/mm.exe``.
        bin_dir = os.path.dirname(sys.executable)
        mm_bin = shutil.which("mm", path=bin_dir)
        # Fail loudly instead of pytest.skip — any valid test environment
        # (``uv run pytest`` or ``uv pip install -e``) must provide the
        # ``mm`` entry point. A silent skip here would turn this subprocess
        # regression guard into CI false-green if the editable install is
        # ever dropped.
        if mm_bin is None:
            pytest.fail(
                f"mm binary not found in {bin_dir}. "
                "Run `uv pip install -e packages/memtomem[all]` before testing."
            )

        home = tmp_path / "home"
        home.mkdir()
        mem_dir = _make_memory_dir(str(home))

        env = os.environ.copy()
        # Strip developer ``MEMTOMEM_*`` overrides — ``HOME`` only
        # isolates ``~/.memtomem/config.json`` reads, but
        # pydantic-settings still applies env-var overrides from the
        # parent shell (e.g. ``MEMTOMEM_INDEXING__MEMORY_DIRS``
        # pointing at a real memory dir, or
        # ``MEMTOMEM_SEARCH__ENABLE_BM25=false`` disabling the BM25
        # path the assertions below rely on) which would
        # un-hermeticize the subprocess. Filter on the full
        # ``MEMTOMEM_`` prefix rather than a hand-curated list so any
        # new top-level config section's env binding is covered
        # automatically.
        for var in [k for k in env if k.startswith("MEMTOMEM_")]:
            env.pop(var, None)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows ``Path.home()`` priority
        env["XDG_CONFIG_HOME"] = str(home / ".config")

        def _run(*args: str) -> subprocess.CompletedProcess:
            # ``encoding="utf-8"`` is required: ``text=True`` alone falls
            # back to ``locale.getpreferredencoding(False)``, which is
            # ``cp949`` on Korean Windows. The CLI emits UTF-8 (em-dashes,
            # box-drawing) so the reader thread crashes mid-decode and
            # ``r.stdout`` / ``r.stderr`` come back as ``None``, surfacing
            # later as ``"argument of type 'NoneType' is not iterable"``
            # on the assertion below (#759).
            return subprocess.run(
                [mm_bin, *args],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )

        r = _run(
            "init",
            "-y",
            "--provider",
            "none",
            "--memory-dir",
            mem_dir,
            "--mcp",
            "skip",
        )
        assert r.returncode == 0, f"init failed:\nstdout={r.stdout}\nstderr={r.stderr}"

        r = _run("index", mem_dir)
        assert r.returncode == 0, f"index failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        assert "no such table" not in (r.stdout + r.stderr)
        assert "1 new" in r.stdout or "1 file" in r.stdout

        r = _run("search", "hello")
        assert r.returncode == 0, f"search failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        assert "hello world" in r.stdout


def test_force_unsafe_rejected_with_debounce_modes():
    """ADR-0006 PR-A: ``--force-unsafe`` only applies to direct indexing; the
    debounce queue does not thread it, so combining it with
    ``--flush`` / ``--status`` / ``--debounce-window`` must error rather than
    silently ignore the flag.
    """
    runner = CliRunner()
    r = runner.invoke(cli, ["index", "--force-unsafe", "--flush"])
    assert r.exit_code != 0
    assert "only applies to direct indexing" in r.output


def test_debounce_flush_surfaces_blocked_file_as_error(tmp_path, monkeypatch):
    """A secret-bearing file enqueued via ``--debounce-window`` and drained
    via ``--flush`` must not be reported as ``Indexed`` and silently dropped
    from the queue — it must surface as an ``Errors`` entry and stay queued
    for retry, matching ``mm index``'s direct-run behavior. Before the fix,
    ``_make_indexer`` discarded the ``IndexingStats`` return value entirely,
    so a redaction-blocked (or any other per-file-failed) file was reported
    as indexed with no way for a hook caller to ever learn it was skipped.
    """
    from memtomem.cli import _bootstrap

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
    # ``debounce.queue_path()`` falls back to a module-level constant bound at
    # import time; ``set_home`` alone won't isolate it from a real
    # ``~/.memtomem/index_debounce_queue.json``.
    monkeypatch.setenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", str(tmp_path / "debounce_queue.json"))

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    # HuggingFace-token shape assembled at runtime so GitHub push-protection
    # does not flag this file (mirrors test_index_privacy_block_surfaces.py).
    secret = "hf" + "_FAKEfake0123456789FAKEfake01234567"
    leak = mem_dir / "leak.md"
    leak.write_text(f"# Leak\n\napi token: {secret}\n")

    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
    )
    assert r.exit_code == 0, f"init failed: {r.output}"

    # Enqueue only — a huge window never elapses on this call.
    r = runner.invoke(cli, ["index", "--debounce-window", "999999", str(leak)])
    assert r.exit_code == 0, f"enqueue failed: {r.output}"

    # Force-drain regardless of window.
    r = runner.invoke(cli, ["index", "--flush"])
    assert r.exit_code == 0, f"flush failed: {r.output}"
    assert "Indexed: 0" in r.output
    assert "Errors: 1" in r.output
    assert "redaction_blocked" in r.output
    assert "Remaining in queue: 1" in r.output

    # ``--json`` shape: same outcome, still queued for the next retry. The
    # redaction gate's ``logger.warning`` calls share stdout/stderr with
    # CliRunner, so the JSON dict — the last thing ``_print_drain_result``
    # emits — is the last line, not necessarily the whole output.
    r = runner.invoke(cli, ["index", "--flush", "--json"])
    assert r.exit_code == 0, f"flush --json failed: {r.output}"
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["indexed"] == []
    assert len(payload["errors"]) == 1
    assert "redaction_blocked" in payload["errors"][0]["message"]
    assert payload["remaining"] == 1


def test_debounce_flush_drops_poison_entry_after_cap(tmp_path, monkeypatch):
    """A queue entry that fails on every drain is dropped after
    ``_MAX_DRAIN_ATTEMPTS`` flushes, loudly: the human output and the
    ``--json`` payload both carry a ``dropped`` record and the queue is
    empty afterwards (#1574 item 3). Uses a redaction-blocked file as the
    deterministic failure — the recorded decision is that blocked files
    are capped too, since exempting them reintroduces unbounded retry.
    """
    from memtomem.cli import _bootstrap
    from memtomem.indexing.debounce import _MAX_DRAIN_ATTEMPTS

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
    monkeypatch.setenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", str(tmp_path / "debounce_queue.json"))

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    secret = "hf" + "_FAKEfake0123456789FAKEfake01234567"
    leak = mem_dir / "leak.md"
    leak.write_text(f"# Leak\n\napi token: {secret}\n")

    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
    )
    assert r.exit_code == 0, f"init failed: {r.output}"
    r = runner.invoke(cli, ["index", "--debounce-window", "999999", str(leak)])
    assert r.exit_code == 0, f"enqueue failed: {r.output}"

    for i in range(_MAX_DRAIN_ATTEMPTS - 1):
        r = runner.invoke(cli, ["index", "--flush"])
        assert r.exit_code == 0, f"flush {i + 1} failed: {r.output}"
        assert "Remaining in queue: 1" in r.output, f"flush {i + 1}: {r.output}"

    r = runner.invoke(cli, ["index", "--flush", "--json"])
    assert r.exit_code == 0, f"final flush failed: {r.output}"
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["errors"] == []
    assert len(payload["dropped"]) == 1
    assert payload["dropped"][0]["path"] == str(leak)
    assert payload["remaining"] == 0

    # Genuinely gone — the next flush sees an empty queue.
    r = runner.invoke(cli, ["index", "--flush", "--json"])
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["indexed"] == [] and payload["dropped"] == [] and payload["remaining"] == 0


def test_debounce_flush_drains_terminal_skip_without_livelock(tmp_path, monkeypatch):
    """A terminal, non-security skip (binary / too-large file) enqueued via the
    hook path must still **drain** — it must not stick in the queue and
    re-error on every subsequent flush. The redaction fix raises only on
    ``blocked_files`` (the security case); ``stats.errors`` for a binary file —
    which can never succeed on retry — must NOT pin the entry. This guards
    against re-broadening the raise to ``stats.errors`` and reintroducing a
    permanent queue livelock for un-indexable assets (the pre-fix silent-drop
    was correct for these).
    """
    from memtomem.cli import _bootstrap

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
    monkeypatch.setenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", str(tmp_path / "debounce_queue.json"))

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    # Null bytes → the engine flags it "binary file detected" (a terminal skip
    # that populates stats.errors but NOT stats.blocked_files). NUL is valid
    # UTF-8, so read_text succeeds and the binary heuristic is what trips.
    binfile = mem_dir / "asset.md"
    binfile.write_bytes(b"# Title\n\n\x00\x00 binary noise \x00\n")

    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
    )
    assert r.exit_code == 0, f"init failed: {r.output}"

    r = runner.invoke(cli, ["index", "--debounce-window", "999999", str(binfile)])
    assert r.exit_code == 0, f"enqueue failed: {r.output}"

    r = runner.invoke(cli, ["index", "--flush"])
    assert r.exit_code == 0, f"flush failed: {r.output}"
    # Drained, not stuck: the terminal skip leaves nothing queued and is not
    # reported as a retryable error. (Pre-fix-broad behavior would have raised →
    # "Errors: 1" / "Remaining in queue: 1" and livelocked.)
    assert "Indexed: 1" in r.output
    assert "Remaining in queue: 0" in r.output

    # And it does not re-appear on the next flush (queue is genuinely empty).
    r = runner.invoke(cli, ["index", "--flush", "--json"])
    assert r.exit_code == 0, f"second flush failed: {r.output}"
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["indexed"] == []
    assert payload["errors"] == []
    assert payload["remaining"] == 0
