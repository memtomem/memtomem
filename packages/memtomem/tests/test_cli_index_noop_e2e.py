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

        # #1767: the JSON format carries score provenance per item.
        # ``--provider none`` keeps ``enable_dense`` on (the dense leg just
        # returns nothing), so fusion still runs and the scale is "rrf" —
        # observable in the score itself: 1/(rrf_k + 1) ≈ 0.0164, not a raw
        # BM25 magnitude.
        r = runner.invoke(cli, ["search", "--format", "json", "hello"])
        assert r.exit_code == 0, f"json search failed: {r.output}"
        items = json.loads(r.output)
        assert items and all(item["score_scale"] == "rrf" for item in items)


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


def test_debounce_flush_drops_blocked_file_as_permanent(tmp_path, monkeypatch):
    """A secret-bearing file enqueued via ``--debounce-window`` and drained
    via ``--flush`` must be reported loudly and removed immediately. A privacy
    refusal cannot resolve through blind retries, so it must not consume the
    transient retry budget (#2026).
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
    assert r.exit_code == 1, f"flush should report failure: {r.output}"
    assert "Indexed: 0" in r.output
    assert "Dropped (permanent): 1" in r.output
    assert "redaction_blocked" in r.output
    assert "fix the cause, then" in r.output
    assert "Remaining in queue: 0" in r.output

    # Enqueue the same still-blocked file again to pin the machine-readable
    # shape independently. The new write creates a fresh queue entry, but the
    # permanent classification still drops it on its first drain.
    r = runner.invoke(cli, ["index", "--debounce-window", "999999", str(leak)])
    assert r.exit_code == 0, f"re-enqueue failed: {r.output}"

    # The redaction gate's ``logger.warning`` calls share stdout/stderr with
    # CliRunner, so the JSON dict — the last thing ``_print_drain_result``
    # emits — is the last line, not necessarily the whole output.
    r = runner.invoke(cli, ["index", "--flush", "--json"])
    assert r.exit_code == 1, f"flush --json should report failure: {r.output}"
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["indexed"] == []
    assert payload["errors"] == []
    assert len(payload["dropped"]) == 1
    assert payload["dropped"][0]["path"] == str(leak)
    assert "redaction_blocked" in payload["dropped"][0]["message"]
    assert payload["retryable_dropped"] == []
    assert payload["remaining"] == 0


def test_debounce_flush_indexes_a_declared_exemption(tmp_path, monkeypatch):
    """#2076: the debounce queue carries only ``(path, namespace, force)`` and
    rejects ``--force-unsafe`` outright, so before the frontmatter declaration
    a pattern-documenting note could never drain — it dropped as permanent on
    every flush. The declaration reaches this path because it travels with the
    content the gate already reads.
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
    note = mem_dir / "redaction-notes.md"
    note.write_text(
        "---\nredaction: documents-patterns\n---\n\n"
        "# Notes\n\nThe guard matches `api_key=` on the keyword alone.\n"
    )

    runner = CliRunner()
    r = runner.invoke(
        cli,
        ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
    )
    assert r.exit_code == 0, f"init failed: {r.output}"

    r = runner.invoke(cli, ["index", "--debounce-window", "999999", str(note)])
    assert r.exit_code == 0, f"enqueue failed: {r.output}"

    r = runner.invoke(cli, ["index", "--flush"])
    assert r.exit_code == 0, f"flush should succeed: {r.output}"
    assert "Indexed: 1" in r.output
    # The drop line prints only when something dropped, so its absence is the
    # assertion: the file drained instead of being refused.
    assert "Dropped (permanent)" not in r.output
    assert "redaction_blocked" not in r.output
    # The exemption still leaves an audit line — a persistent bypass has to be
    # visible somewhere, and this path has no per-run summary of its own.
    assert "redaction exemption declared in file" in r.output
    assert "decision=exempted" in r.output


def test_debounce_flush_drops_binary_error_immediately(tmp_path, monkeypatch):
    """A deterministic binary-file refusal is permanent and drops on the
    first drain rather than spending the retry budget."""
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

    r = runner.invoke(cli, ["index", "--flush", "--json"])
    assert r.exit_code == 1, r.output
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["indexed"] == []
    assert payload["errors"] == []
    assert len(payload["dropped"]) == 1
    assert "binary file detected" in payload["dropped"][0]["message"]
    assert payload["retryable_dropped"] == []
    assert payload["remaining"] == 0
