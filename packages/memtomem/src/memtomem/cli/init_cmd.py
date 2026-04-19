"""memtomem init — interactive setup wizard."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import time
from pathlib import Path

import click

from memtomem.cli.wizard import nav_confirm, nav_prompt, run_steps, step_header


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a command and return the result."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ollama_available() -> bool:
    return shutil.which("ollama") is not None


def _ollama_running() -> bool:
    try:
        return _run(["ollama", "list"], timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _ollama_has_model(model: str) -> bool:
    try:
        return model in _run(["ollama", "list"], timeout=5).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _test_openai_key(api_key: str) -> bool:
    try:
        import httpx

        resp = httpx.post(
            "https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"input": "test", "model": "text-embedding-3-small"},
            timeout=10.0,
        )
        return resp.status_code == 200
    except Exception:
        return False


def _is_source_install() -> bool:
    check = Path.cwd()
    for _ in range(5):
        if (check / "pyproject.toml").exists() and (check / "packages").exists():
            return True
        check = check.parent
    return False


def _detect_source_dir() -> str | None:
    check = Path.cwd()
    for _ in range(5):
        if (check / "pyproject.toml").exists() and (check / "packages").exists():
            return str(check)
        check = check.parent
    return None


def _is_project_install() -> bool:
    """Detect project-scoped install (uv add memtomem)."""
    check = Path.cwd()
    for _ in range(5):
        if (check / "pyproject.toml").exists() and not (check / "packages").exists():
            return True
        check = check.parent
    return False


def _detect_project_dir() -> str | None:
    """Find project root for project-scoped install."""
    check = Path.cwd()
    for _ in range(5):
        if (check / "pyproject.toml").exists() and not (check / "packages").exists():
            return str(check)
        check = check.parent
    return None


# ── Step functions ────────────────────────────────────────────────────


def _step_embedding(state: dict) -> None:
    step_header(1, "Embedding Provider")
    click.echo("  Choose how to generate embeddings:")
    click.echo("    [1] Quick start — keyword search only, no setup needed (recommended)")
    click.echo("    [2] Local ONNX — dense search, no server needed (pip install memtomem[onnx])")
    click.echo("    [3] Ollama — local dense embeddings, needs GPU")
    click.echo("    [4] OpenAI — cloud dense embeddings, needs API key")
    choice = nav_prompt("  Select", type=click.IntRange(1, 4), default=1)
    click.echo()

    api_key = ""

    if choice == 1:
        # BM25-only mode: no embeddings, no external dependencies
        provider = "none"
        model = ""
        dimension = 0
        click.secho("  BM25 keyword search — ready to go, no setup needed.", fg="green")
        click.echo("  You can add dense embeddings later by re-running 'mm init'.")

    elif choice == 2:
        provider = "onnx"
        _onnx_available = False
        try:
            import fastembed  # noqa: F401

            _onnx_available = True
        except ImportError:
            click.secho("  fastembed not installed.", fg="yellow")
            click.echo("  Install with: pip install memtomem[onnx]")
            click.echo("  Saving ONNX config now so you're ready after install.")
            click.echo()

        click.echo("  Available models:")
        click.echo("    [1] all-MiniLM-L6-v2 — English, fast, tiny (~22 MB, 384d)")
        click.echo("    [2] bge-small-en-v1.5 — English, better accuracy (~33 MB, 384d)")
        click.echo("    [3] bge-m3 — multilingual KR/EN/JP/CN (~1.2 GB, 1024d)")
        model_choice = nav_prompt("  Select", type=click.IntRange(1, 3), default=1)

        models = {
            1: ("all-MiniLM-L6-v2", 384),
            2: ("bge-small-en-v1.5", 384),
            3: ("bge-m3", 1024),
        }
        model, dimension = models[model_choice]

        if model_choice == 3:
            click.secho(
                "  Note: bge-m3 is ~1.2 GB (similar to Ollama models). "
                "For lightweight EN-only search, choose option 1 or 2.",
                fg="yellow",
            )

        if _onnx_available:
            click.secho(f"  Model '{model}' selected.", fg="green")
            click.echo("  It will be downloaded automatically on first indexing.")

    elif choice == 3:
        provider = "ollama"
        if not _ollama_available():
            click.secho("  Ollama not found.", fg="yellow")
            click.echo("  Install from https://ollama.com, then run 'mm index' to embed.")
            click.echo("  Saving Ollama config now so you're ready after install.")
            click.echo()
            # Record intent — config is written, embedding runs when Ollama is available.
            model = "nomic-embed-text"
            dimension = 768
        else:
            if not _ollama_running():
                click.secho("  Ollama not running. Starting 'ollama serve'...", fg="yellow")
                click.echo("  Run 'ollama serve' in another terminal if this fails.")
                click.echo()

            click.echo("  Available models:")
            click.echo("    [1] nomic-embed-text — English, fast (768d)")
            click.echo("    [2] bge-m3 — multilingual, higher accuracy (1024d)")
            model_choice = nav_prompt("  Select", type=click.IntRange(1, 2), default=1)

            models = {1: ("nomic-embed-text", 768), 2: ("bge-m3", 1024)}
            model, dimension = models[model_choice]

            if _ollama_has_model(model):
                click.secho(f"  Model '{model}' is ready.", fg="green")
            else:
                pull = nav_confirm(f"  Model '{model}' not found. Pull now?", default=True)
                if pull:
                    click.echo(f"  Pulling {model}... (this may take a few minutes)")
                    result = _run(["ollama", "pull", model], timeout=600)
                    if result.returncode == 0:
                        click.secho(f"  Model '{model}' pulled successfully.", fg="green")
                    else:
                        click.secho(f"  Pull failed: {result.stderr.strip()}", fg="red")
                        click.echo("  You can pull manually: ollama pull " + model)
                else:
                    click.echo(f"  Remember to run: ollama pull {model}")

    else:
        provider = "openai"
        click.echo("  Available models:")
        click.echo("    [1] text-embedding-3-small — balanced (1536d)")
        click.echo("    [2] text-embedding-3-large — highest accuracy (3072d)")
        model_choice = nav_prompt("  Select", type=click.IntRange(1, 2), default=1)

        models = {1: ("text-embedding-3-small", 1536), 2: ("text-embedding-3-large", 3072)}
        model, dimension = models[model_choice]

        api_key = nav_prompt("  OpenAI API key", hide_input=True)
        click.echo("  Testing API key...")

        if _test_openai_key(api_key):
            click.secho("  API key is valid.", fg="green")
        else:
            click.secho("  API key test failed. Check your key and try again.", fg="red")
            if not nav_confirm("  Continue anyway?", default=False):
                raise SystemExit(1)

    state["provider"] = provider
    state["model"] = model
    state["dimension"] = dimension
    state["api_key"] = api_key
    click.echo()


def _step_reranker(state: dict) -> None:
    step_header(2, "Reranker (optional)")
    click.echo("  Cross-encoder reranking sharpens search relevance after BM25+dense fusion.")
    click.echo("  Runs locally via fastembed ONNX — no API key, no server.")
    enabled = nav_confirm("  Enable reranker?", default=False)
    if not enabled:
        state["rerank_enabled"] = False
        click.echo()
        return

    click.echo()
    click.echo("  Available models:")
    click.echo("    [1] English (Xenova/ms-marco-MiniLM-L-6-v2) — 80 MB")
    click.echo("    [2] Multilingual (jinaai/jina-reranker-v2-base-multilingual) — 1.1 GB")
    click.echo("        Recommended for Korean/Chinese/Japanese/mixed content.")
    choice = nav_prompt("  Select", type=click.IntRange(1, 2), default=1)

    models = {
        1: "Xenova/ms-marco-MiniLM-L-6-v2",
        2: "jinaai/jina-reranker-v2-base-multilingual",
    }
    state["rerank_enabled"] = True
    state["rerank_model"] = models[choice]
    click.secho(f"  Reranker '{models[choice]}' selected.", fg="green")
    click.echo("  Model downloads on first search (~/.cache/fastembed/).")
    click.echo()


def _step_memory_dir(state: dict) -> None:
    step_header(3, "Memory Directory")
    click.echo("  Where are the files you want to index?")
    memory_dir = nav_prompt("  Directory", default="~/memories")
    memory_path = Path(memory_dir).expanduser()
    if not memory_path.exists():
        create = nav_confirm(f"  '{memory_dir}' doesn't exist. Create it?", default=True)
        if create:
            memory_path.mkdir(parents=True, exist_ok=True)
            click.secho(f"  Created {memory_path}", fg="green")
    state["memory_dir"] = memory_dir
    click.echo()


def _step_provider_dirs(state: dict) -> None:
    """Opt-in indexing of provider memory folders detected on this machine.

    Replaces the legacy silent ``ensure_auto_discovered_dirs`` runtime path.
    Per-category prompts (Claude Code memory, Claude plans, Codex memories)
    let the user pick exactly which surfaces to make searchable. Categories
    with zero detected directories are skipped silently — the step is a
    no-op on machines without any of these tools installed.
    """
    step_header(4, "Provider Memory Folders")
    from memtomem.config import _detect_provider_dirs

    grouped = _detect_provider_dirs()
    available = {cat: dirs for cat, dirs in grouped.items() if dirs}

    if not available:
        click.echo("  No AI tool memory folders detected on this machine.")
        click.echo("  (Skipping — re-run `mm init` after installing Claude Code or Codex CLI.)")
        state["provider_dirs"] = []
        click.echo()
        return

    click.echo("  Make memory from other AI tools searchable through memtomem?")
    click.echo("  Each option is opt-in; declined folders stay out of search.")
    click.echo()

    selected: list[Path] = []

    if "claude-memory" in available:
        n = len(available["claude-memory"])
        suffix = "project" if n == 1 else "projects"
        if nav_confirm(
            f"  Claude Code per-project memory ({n} {suffix} with .md content)?",
            default=False,
        ):
            selected.extend(available["claude-memory"])

    if "claude-plans" in available:
        if nav_confirm(
            "  Claude Code plans (~/.claude/plans/)?",
            default=False,
        ):
            selected.extend(available["claude-plans"])

    if "codex" in available:
        if nav_confirm(
            "  Codex CLI memories (~/.codex/memories/)?",
            default=False,
        ):
            selected.extend(available["codex"])

    state["provider_dirs"] = [str(p) for p in selected]
    if selected:
        click.secho(f"  Added {len(selected)} provider folder(s) to memory_dirs.", fg="green")
        click.echo("  New Claude Code projects created later won't be auto-indexed —")
        click.echo("  re-run `mm init` or use `mm config set indexing.memory_dirs` to add them.")
    click.echo()


def _step_storage(state: dict) -> None:
    step_header(5, "Storage")
    config_dir = Path("~/.memtomem").expanduser()
    db_default = str(config_dir / "memtomem.db")
    state["db_path"] = nav_prompt("  SQLite DB path", default=db_default)
    click.echo()


def _step_namespace(state: dict) -> None:
    step_header(6, "Namespace")
    click.echo("  Organize memories into scoped groups (work, personal, project).")
    state["enable_auto_ns"] = nav_confirm(
        "  Auto-assign namespace from folder name? (~/docs → 'docs')", default=False
    )
    state["default_ns"] = nav_prompt("  Default namespace", default="default")
    click.echo()


def _step_search(state: dict) -> None:
    step_header(7, "Search")
    state["top_k"] = nav_prompt("  Results per search", type=click.INT, default=10)
    state["decay_enabled"] = nav_confirm(
        "  Enable time-decay? (older memories rank lower)", default=False
    )
    click.echo()


def _step_language(state: dict) -> None:
    step_header(8, "Language")
    click.echo("  Search tokenizer:")
    click.echo("    [1] Unicode (default — English and most languages)")
    click.echo("    [2] Korean (kiwipiepy — better Korean word splitting)")
    lang_choice = nav_prompt("  Select", type=click.IntRange(1, 2), default=1)
    tokenizer = "unicode61" if lang_choice == 1 else "kiwipiepy"

    if tokenizer == "kiwipiepy":
        try:
            import kiwipiepy  # noqa: F401

            click.secho("  kiwipiepy is installed.", fg="green")
        except ImportError:
            click.secho("  kiwipiepy not installed. Run: pip install kiwipiepy", fg="yellow")
    state["tokenizer"] = tokenizer
    click.echo()


def _step_settings(state: dict) -> None:
    step_header(9, "Claude Code Hooks")

    # Skip entirely if Claude Code is not installed
    claude_dir = Path.home() / ".claude"
    if not claude_dir.is_dir():
        click.echo("  Claude Code not detected (~/.claude/ missing). Skipping.")
        state["settings_hooks"] = False
        click.echo()
        return

    click.echo("  memtomem can manage Claude Code hooks via .memtomem/settings.json.")
    click.echo("  Hooks are merged into ~/.claude/settings.json additively.")
    state["settings_hooks"] = nav_confirm(
        "  Configure Claude Code hooks via memtomem?", default=False
    )

    if state["settings_hooks"]:
        from memtomem.context.settings import CANONICAL_SETTINGS_FILE, generate_all_settings

        project_root = Path.cwd()
        canonical = project_root / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True, exist_ok=True)
        if not canonical.exists():
            canonical.write_text(
                json.dumps({"hooks": {}}, indent=2) + "\n",
                encoding="utf-8",
            )
            click.secho(f"  Created {CANONICAL_SETTINGS_FILE}", fg="green")

        results = generate_all_settings(project_root)
        for name, r in results.items():
            if r.status == "ok":
                click.secho(f"  Merged → {r.target}", fg="green")
            elif r.status == "skipped":
                click.secho(f"  skipped {name}: {r.reason}", fg="yellow")

        click.echo()
        click.echo("  Empty hooks file created. Add hooks to .memtomem/settings.json,")
        click.echo("  then run 'mm context sync --include=settings' to apply.")
    click.echo()


def _step_mcp(state: dict) -> None:
    step_header(10, "Connect to AI Editor")
    click.echo("  How do you want to connect memtomem?")
    click.echo("    [1] Claude Code (run 'claude mcp add' automatically)")
    click.echo("    [2] Generate .mcp.json here (Claude Code project scope;")
    click.echo("        copy into your editor's config file for Cursor / Windsurf / others)")
    click.echo("    [3] Skip — I'll configure it manually")
    state["mcp_choice"] = nav_prompt("  Select", type=click.IntRange(1, 3), default=1)
    click.echo()


def _emit_mcp_paste_hints() -> None:
    """Print per-editor paste targets for the generated ``.mcp.json``.

    Claude Code auto-loads a project-root ``.mcp.json``; other editors do not
    and expect their own config file. Shown after every path that writes the
    file so users know the generated JSON is a template, not a drop-in config
    for Cursor/Windsurf/Claude Desktop/Gemini CLI."""
    click.echo("    Cursor          → paste into ~/.cursor/mcp.json")
    click.echo("    Windsurf        → paste into ~/.codeium/windsurf/mcp_config.json")
    click.echo(
        "    Claude Desktop  → paste into "
        "~/Library/Application Support/Claude/claude_desktop_config.json"
    )
    click.echo("    Gemini CLI      → paste into ~/.gemini/settings.json")
    click.echo("  (Claude Code picks up ./.mcp.json in this project automatically.)")


# ── Write config & summary ────────────────────────────────────────────


def _flatten_config(d: dict, prefix: str = "") -> dict[str, object]:
    """Flatten nested dict into `"section.key": value` pairs (one level deep
    is enough for this codebase's config shape)."""
    out: dict[str, object] = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten_config(v, prefix=f"{key}."))
        else:
            out[key] = v
    return out


def _flatten_init_data_keys(init_data: dict) -> set[str]:
    """Flat `section.key` set for every field the wizard actively wrote this
    run — used to exclude them from the Preserved summary."""
    keys: set[str] = set()
    for section, fields in init_data.items():
        if isinstance(fields, dict):
            for k in fields:
                keys.add(f"{section}.{k}")
        else:
            keys.add(section)
    return keys


def _fmt_config_value(v: object) -> str:
    """Render a config value for the summary — keep bools lowercase so they
    match how they appear in config.json."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, str):
        return v
    return json.dumps(v, default=str)


# Canonical config keys that hold credentials, endpoints, or user-curated
# data the wizard does not ask about. ``--fresh`` MUST preserve these even
# when they're non-default and wizard-untouched, otherwise a single
# `mm init --fresh` would silently wipe API keys, custom endpoints, exclude
# patterns, namespace rules, etc.
#
# Derivation (re-runnable): grep for credential/endpoint/list patterns in
# ``packages/memtomem/src/memtomem/config.py`` (commit 0e61e7d):
#   credentials → ``api_key|secret|token|password|bearer|credential``
#   endpoints   → ``base_url|endpoint|_url|host``
#   user data   → list/dict fields that aren't pure tuning numbers
#
# Maintenance: when adding a new config field that holds credentials,
# endpoints, or user-curated data the wizard doesn't ask about, add it
# here. Verify with ``pytest -k fresh_preserves_user_data_keys``.
_FRESH_PRESERVE_KEYS: frozenset[str] = frozenset(
    {
        # Credentials
        "embedding.api_key",
        "rerank.api_key",
        "llm.api_key",
        "webhook.secret",
        # Endpoints
        "embedding.base_url",
        "llm.base_url",
        "webhook.url",
        # User-curated lists / rules (wizard doesn't ask)
        "indexing.exclude_patterns",
        "indexing.supported_extensions",
        "namespace.rules",
        "search.system_namespace_prefixes",
        "webhook.events",
    }
)


def _compute_fresh_drops(
    existing: dict,
    wizard_touched: set[str],
) -> list[tuple[str, object, object]]:
    """Return ``[(flat_key, current_value, default_value), ...]`` for the
    keys ``--fresh`` will reset to the built-in default.

    A key is a drop candidate iff ALL hold:
      - present in ``Mem2MemConfig().model_dump()`` flat-key set — keys
        outside this canonical shape are user custom extensions and are
        preserved unconditionally;
      - NOT in ``wizard_touched`` — those get overwritten by ``init_data``
        anyway, no need to drop them first;
      - NOT in :data:`_FRESH_PRESERVE_KEYS` — credentials, endpoints,
        user-curated data are never auto-dropped;
      - current value differs from the built-in default — already-default
        values are no-ops on disk after merge.
    """
    from memtomem.config import Mem2MemConfig

    defaults_flat = _flatten_config(Mem2MemConfig().model_dump(mode="json"))
    existing_flat = _flatten_config(existing)
    drops: list[tuple[str, object, object]] = []
    for key, value in existing_flat.items():
        if key not in defaults_flat:
            continue
        if key in wizard_touched:
            continue
        if key in _FRESH_PRESERVE_KEYS:
            continue
        default_val = defaults_flat[key]
        if default_val == value:
            continue
        drops.append((key, value, default_val))
    return drops


def _drop_flat_keys(existing: dict, drops: list[tuple[str, object, object]]) -> None:
    """Remove each flat key from the nested ``existing`` dict, then prune
    parent dicts that become empty."""
    for flat_key, _, _ in drops:
        parts = flat_key.split(".")
        path: list[tuple[dict, str]] = []
        cur: object = existing
        for p in parts[:-1]:
            if not isinstance(cur, dict) or p not in cur:
                cur = None
                break
            path.append((cur, p))
            cur = cur[p]
        if isinstance(cur, dict) and parts[-1] in cur:
            del cur[parts[-1]]
            for parent, key in reversed(path):
                if isinstance(parent[key], dict) and not parent[key]:
                    del parent[key]
                else:
                    break


def _emit_reset_block(
    drops: list[tuple[str, object, object]],
    backup_path: Path | None,
) -> None:
    """Print the ``--fresh`` outcome — which keys were reset to default,
    where the backup lives, and the web-UI restart caveat.

    With zero drops we still print one informational line so the user knows
    why ``--fresh`` produced no visible change."""
    if not drops:
        click.echo()
        click.secho(
            "  --fresh: no wizard-untouched leftovers to reset.",
            fg="cyan",
        )
        return

    click.echo()
    click.secho(
        "  Reset to default (--fresh dropped wizard-untouched leftovers):",
        fg="cyan",
    )
    for key, value, default_val in sorted(drops):
        click.secho(
            f"    [-] {key}: {_fmt_config_value(value)} → "
            f"{_fmt_config_value(default_val)} (default)",
            fg="cyan",
        )
    if backup_path is not None:
        click.echo()
        click.echo(f"  Backup saved to: {backup_path}")
    click.echo()
    click.secho(
        "  [!] If the web UI is running, restart it to pick up the new config.",
        fg="yellow",
    )
    click.secho(
        "      Otherwise a web save may restore the reset values.",
        fg="yellow",
    )


def _emit_preserved_block(
    existing_before: dict,
    written: dict,
    wizard_touched: set[str],
) -> None:
    """Flag values that (a) were already in the previous config, (b) the
    wizard did not touch this run, and (c) differ from the built-in default.

    Silent preservation is the main way Web UI dumps of the full mutable
    config (memory-dirs add/remove, section save, etc.) accumulate non-default
    values the user didn't consciously set. Surfacing them here is the
    cheapest way to close that loop."""
    from memtomem.config import Mem2MemConfig

    defaults_flat = _flatten_config(Mem2MemConfig().model_dump(mode="json"))
    before_flat = _flatten_config(existing_before)
    written_flat = _flatten_config(written)

    flagged: list[tuple[str, object, object]] = []
    for key, value in written_flat.items():
        if key in wizard_touched:
            continue
        if key not in before_flat:
            continue
        if before_flat[key] != value:
            continue
        default_val = defaults_flat.get(key)
        if default_val == value:
            continue
        flagged.append((key, value, default_val))

    if not flagged:
        return

    click.echo()
    click.secho(
        "  Preserved from existing config (wizard didn't ask about these):",
        fg="yellow",
    )
    for key, value, default_val in sorted(flagged):
        click.secho(
            f"    [!] {key} = {_fmt_config_value(value)}   "
            f"(built-in default: {_fmt_config_value(default_val)})",
            fg="yellow",
        )
    click.echo()
    click.echo("  If any of these are unexpected, edit ~/.memtomem/config.json")
    click.echo('  and remove the flagged keys (e.g. delete the "mmr" section')
    click.echo("  to restore mmr.enabled=false).")


def _write_config_and_summary(
    state: dict, base_dir: Path | None = None, fresh: bool = False
) -> None:
    """Write config files and show summary (runs after all steps).

    When ``fresh=True``, drop wizard-untouched canonical settings whose
    value differs from the built-in default before merging the wizard's
    choices. Credentials, endpoints, and user-curated lists in
    :data:`_FRESH_PRESERVE_KEYS` are preserved unconditionally; user-added
    custom keys outside the canonical ``Mem2MemConfig`` shape are also
    preserved. A timestamped backup is written first iff at least one key
    is going to be dropped — otherwise the run is a no-op on disk."""
    if base_dir is None:
        base_dir = Path.home()
    config_dir = base_dir / ".memtomem"
    config_dir.mkdir(parents=True, exist_ok=True)

    source_install = state.get("source_install", False)
    source_dir = state.get("source_dir")
    project_install = state.get("project_install", False)
    project_dir = state.get("project_dir")

    # Write ~/.memtomem/config.json (read-merge-write to preserve non-init fields)
    click.secho("Writing configuration...", fg="green")
    config_path = config_dir / "config.json"

    # Read existing config as merge base (preserves post-init user edits).
    # If the file is unreadable we save a timestamped backup and start fresh —
    # better than silently wiping what might be salvageable by hand.
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            backup_path = config_path.with_suffix(f".json.bak-{int(time.time())}")
            try:
                shutil.copy2(config_path, backup_path)
                click.secho(
                    f"  Note: {config_path.name} was unreadable ({exc.__class__.__name__}); "
                    f"backed up to {backup_path.name} and starting from empty.",
                    fg="yellow",
                )
            except OSError as backup_exc:
                click.secho(
                    f"  Warning: {config_path.name} unreadable and backup failed "
                    f"({backup_exc}); proceeding with empty base.",
                    fg="yellow",
                )
    # Snapshot for the preserved-values summary below — must happen BEFORE
    # merge so we can diff pre-merge vs post-write.
    existing_before = copy.deepcopy(existing)

    # Build init-target fields only. ``memory_dirs`` combines the user's
    # primary directory with any provider folders accepted in Step 4
    # (Claude memory, Claude plans, Codex memories), deduped while
    # preserving order so the primary dir always lists first.
    provider_dirs = state.get("provider_dirs", []) or []
    combined_dirs: list[str] = []
    seen: set[str] = set()
    for entry in [state["memory_dir"], *provider_dirs]:
        key = str(Path(entry).expanduser())
        if key in seen:
            continue
        seen.add(key)
        combined_dirs.append(entry)

    init_data: dict = {
        "embedding": {
            "provider": state["provider"],
            "model": state["model"],
            "dimension": state["dimension"],
        },
        "storage": {"backend": "sqlite", "sqlite_path": state["db_path"]},
        "indexing": {"memory_dirs": combined_dirs, "auto_discover": False},
        "namespace": {
            "enable_auto_ns": state["enable_auto_ns"],
            "default_namespace": state["default_ns"],
        },
        "search": {"default_top_k": state["top_k"], "tokenizer": state["tokenizer"]},
        "decay": {"enabled": state["decay_enabled"]},
    }
    if state["provider"] == "ollama":
        init_data["embedding"]["base_url"] = "http://localhost:11434"
    if state.get("api_key"):
        init_data["embedding"]["api_key"] = state["api_key"]
    if state.get("rerank_enabled"):
        init_data["rerank"] = {
            "enabled": True,
            "provider": "fastembed",
            "model": state["rerank_model"],
        }

    # Compute wizard-touched keys upfront — both --fresh drop logic and the
    # post-write Preserved block need them.
    wizard_touched_keys = _flatten_init_data_keys(init_data)

    # --fresh: drop wizard-untouched non-default canonical leftovers from
    # `existing` BEFORE merge so they don't survive the round-trip. Always
    # back up first, but only if there's at least one drop — otherwise we'd
    # litter ~/.memtomem/ with redundant `.bak-<ts>` files on every re-run.
    fresh_drops: list[tuple[str, object, object]] = []
    fresh_backup_path: Path | None = None
    if fresh:
        fresh_drops = _compute_fresh_drops(existing, wizard_touched_keys)
        if fresh_drops and config_path.exists():
            fresh_backup_path = config_path.with_suffix(f".json.bak-{int(time.time())}")
            try:
                shutil.copy2(config_path, fresh_backup_path)
            except OSError as exc:
                # --fresh is destructive; refuse to drop without a recovery
                # path. The user can re-run without --fresh, or fix the
                # backup target (disk full / permission) and retry.
                click.secho(
                    f"  Error: --fresh requires a successful backup but "
                    f"copy to {fresh_backup_path} failed ({exc}). "
                    "Aborting to avoid data loss.",
                    fg="red",
                )
                raise SystemExit(1) from exc
            _drop_flat_keys(existing, fresh_drops)

    # Merge: init fields overwrite, non-init sections/fields preserved
    for section, fields in init_data.items():
        if section not in existing:
            existing[section] = {}
        if isinstance(fields, dict) and isinstance(existing[section], dict):
            existing[section].update(fields)
        else:
            existing[section] = fields

    from memtomem.config import _atomic_write_json

    _atomic_write_json(config_path, existing)
    click.echo(f"  Config: {config_path}")

    if fresh:
        # Reset block replaces Preserved when --fresh was passed; the keys it
        # would have flagged are exactly what we just dropped (modulo the
        # preserve list).
        _emit_reset_block(fresh_drops, fresh_backup_path)
    else:
        # Flag non-default values preserved from the previous config that
        # the wizard never asked about — e.g. mmr.enabled=true left over
        # from the Web UI's full-config dump.
        try:
            written = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            written = existing  # unreachable in practice; stay safe
        _emit_preserved_block(existing_before, written, wizard_touched_keys)

    # Build MCP server command
    if source_install and source_dir:
        server_cmd = "uv"
        server_args = ["run", "--directory", source_dir, "memtomem-server"]
    elif project_install and project_dir:
        server_cmd = "uv"
        server_args = ["run", "--directory", project_dir, "memtomem-server"]
    else:
        server_cmd = "uvx"
        server_args = ["--from", "memtomem", "memtomem-server"]

    # All settings come from ~/.memtomem/config.json at startup via
    # load_config_overrides() — no env overrides are written into .mcp.json.
    # (Previously we wrote MEMTOMEM_INDEXING__MEMORY_DIRS here as a plain
    # string, but pydantic-settings parses list[Path] env vars as JSON arrays,
    # which caused the MCP server to crash on startup.)
    mcp_env: dict[str, str] = {}

    # MCP integration
    mcp_choice = state["mcp_choice"]
    if mcp_choice == 1:
        claude_cmd = ["claude", "mcp", "add", "memtomem", "-s", "user", "--"]
        claude_cmd.append(server_cmd)
        claude_cmd.extend(server_args)

        try:
            result = _run(claude_cmd, timeout=10)
            if result.returncode == 0:
                click.secho("  Claude Code: configured (user scope)", fg="green")
            else:
                click.echo("  Claude Code: 'claude' not found. Use .mcp.json instead.")
                _write_mcp_json(server_cmd, server_args, mcp_env)
                click.echo("  MCP config: wrote ./.mcp.json")
                _emit_mcp_paste_hints()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            click.echo("  Claude Code: 'claude' not found. Use .mcp.json instead.")
            _write_mcp_json(server_cmd, server_args, mcp_env)
            click.echo("  MCP config: wrote ./.mcp.json")
            _emit_mcp_paste_hints()
    elif mcp_choice == 2:
        _write_mcp_json(server_cmd, server_args, mcp_env)
        click.echo("  MCP config: wrote ./.mcp.json")
        _emit_mcp_paste_hints()

    # Summary
    click.echo()
    click.secho("  Setup complete!", fg="green", bold=True)
    click.echo()
    if state["provider"] == "none":
        click.echo("  Provider:   none (BM25 keyword search only)")
    else:
        click.echo(f"  Provider:   {state['provider']}/{state['model']} ({state['dimension']}d)")
    if state.get("rerank_enabled"):
        click.echo(f"  Reranker:   fastembed/{state['rerank_model']}")
    click.echo(f"  Storage:    {state['db_path']}")
    click.echo(f"  Memory:     {state['memory_dir']}")
    provider_dirs = state.get("provider_dirs", []) or []
    if provider_dirs:
        click.echo(f"  Providers:  {len(provider_dirs)} folder(s) added")
    ns_label = "auto" if state["enable_auto_ns"] else "manual"
    click.echo(f"  Namespace:  {ns_label} (default: {state['default_ns']})")
    click.echo(f"  Search:     top_k={state['top_k']}, tokenizer={state['tokenizer']}")
    click.echo(f"  Decay:      {'on' if state['decay_enabled'] else 'off'}")
    install_type = "source" if source_install else "project" if project_install else "PyPI"
    click.echo(f"  Install:    {install_type}")
    click.echo()
    click.echo(f"  Config:     {config_path}")
    click.echo()
    click.secho("  All settings are stored in ~/.memtomem/config.json.", dim=True)
    click.secho("  MCP config only contains the server command (no env overrides).", dim=True)
    click.echo()
    click.secho("  Next steps:", fg="cyan")
    run_prefix = "uv run " if source_install or project_install else ""
    click.echo(f"    1. {run_prefix}mm index {state['memory_dir']}")
    click.echo(f"    2. {run_prefix}mm search 'your first query'")

    # Web UI is behind the [web] extra (fastapi + uvicorn). If it isn't
    # installed, surface a hint here rather than letting `mm web` fail later.
    from memtomem.cli.web import _missing_web_deps, _web_install_hint

    if not source_install and not project_install and _missing_web_deps() is not None:
        click.echo("    3. mm web  (requires [web] extra — not included in base install)")
        click.echo(f"       → {_web_install_hint()}")
    else:
        click.echo(f"    3. {run_prefix}mm web  (browse & manage your memories)")
    click.echo()


def _write_mcp_json(server_cmd: str, server_args: list[str], mcp_env: dict[str, str]) -> None:
    """Write or update .mcp.json in current directory."""
    server_entry: dict = {
        "command": server_cmd,
        "args": server_args,
    }
    if mcp_env:
        server_entry["env"] = mcp_env
    mcp_config: dict = {"mcpServers": {"memtomem": server_entry}}
    mcp_path = Path(".mcp.json")
    if mcp_path.exists():
        existing = json.loads(mcp_path.read_text(encoding="utf-8"))
        existing.setdefault("mcpServers", {})["memtomem"] = mcp_config["mcpServers"]["memtomem"]
        mcp_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    else:
        mcp_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")


# ── CLI entry point ───────────────────────────────────────────────────


_MODEL_DIMS: dict[str, int] = {
    "all-MiniLM-L6-v2": 384,
    "bge-small-en-v1.5": 384,
    "nomic-embed-text": 768,
    "bge-m3": 1024,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}


@click.command("init")
@click.option(
    "-y", "--non-interactive", is_flag=True, help="Skip wizard, use defaults or provided options"
)
@click.option("--provider", type=click.Choice(["none", "onnx", "ollama", "openai"]), default=None)
@click.option("--model", default=None, help="Embedding model name")
@click.option("--memory-dir", default=None, help="Memory directory path")
@click.option("--db-path", default=None, help="SQLite DB path")
@click.option("--namespace", default=None, help="Default namespace")
@click.option("--auto-ns", is_flag=True, default=False, help="Auto-assign namespace from folder")
@click.option("--top-k", default=None, type=int, help="Results per search")
@click.option("--tokenizer", type=click.Choice(["unicode61", "kiwipiepy"]), default=None)
@click.option("--decay", is_flag=True, default=False, help="Enable time-decay")
@click.option("--api-key", default=None, help="OpenAI API key")
@click.option("--mcp", "mcp_mode", type=click.Choice(["claude", "json", "skip"]), default=None)
@click.option(
    "--include-provider",
    "include_providers",
    type=click.Choice(["claude-memory", "claude-plans", "codex"]),
    multiple=True,
    help=(
        "Include provider memory folders in indexing (repeatable). Without this "
        "flag, non-interactive runs add no provider folders. Options: "
        "claude-memory (~/.claude/projects/*/memory/), claude-plans "
        "(~/.claude/plans/), codex (~/.codex/memories/)."
    ),
)
@click.option(
    "--fresh",
    is_flag=True,
    default=False,
    help=(
        "Reset wizard-untouched canonical settings to their built-in defaults. "
        "Preserves user-added custom keys, credentials (api_key/secret), "
        "endpoints (base_url/url), and user-curated lists (exclude_patterns, "
        "namespace.rules, etc). Backs up the previous config.json to "
        "config.json.bak-<ts> only if at least one key is dropped. For "
        "fine-grained control, edit ~/.memtomem/config.json directly or use "
        "the web UI."
    ),
)
def init(
    non_interactive: bool,
    provider: str | None,
    model: str | None,
    memory_dir: str | None,
    db_path: str | None,
    namespace: str | None,
    auto_ns: bool,
    top_k: int | None,
    tokenizer: str | None,
    decay: bool,
    api_key: str | None,
    mcp_mode: str | None,
    include_providers: tuple[str, ...],
    fresh: bool,
) -> None:
    """Set up memtomem with an interactive wizard."""
    click.echo()
    click.secho("  memtomem init", fg="cyan", bold=True)
    click.secho("  ─────────────", fg="cyan")
    click.echo()

    source_install = _is_source_install()
    project_install = not source_install and _is_project_install()
    state: dict = {
        "source_install": source_install,
        "source_dir": _detect_source_dir() if source_install else None,
        "project_install": project_install,
        "project_dir": _detect_project_dir() if project_install else None,
    }

    if state["source_install"]:
        click.secho("  Detected: source install", fg="blue")
        click.echo(f"  Source directory: {state['source_dir']}")
        click.echo()
    elif state["project_install"]:
        click.secho("  Detected: project install", fg="blue")
        click.echo(f"  Project directory: {state['project_dir']}")
        click.echo()

    if non_interactive:
        _provider = provider or "none"
        if _provider == "none":
            _model = ""
            _dimension = 0
        elif _provider == "onnx":
            _model = model or "all-MiniLM-L6-v2"
            _dimension = _MODEL_DIMS.get(_model, 384)
        elif _provider == "ollama":
            _model = model or "nomic-embed-text"
            _dimension = _MODEL_DIMS.get(_model, 768)
        else:
            _model = model or "text-embedding-3-small"
            _dimension = _MODEL_DIMS.get(_model, 1536)
        _memory_dir = memory_dir or "~/memories"

        # Auto-create memory directory
        memory_path = Path(_memory_dir).expanduser()
        if not memory_path.exists():
            memory_path.mkdir(parents=True, exist_ok=True)

        # Resolve --include-provider into concrete dirs that exist on this machine.
        # Categories with no detected dirs are silently skipped — non-interactive
        # callers don't get an error if they ask for codex on a Claude-only box.
        from memtomem.config import _detect_provider_dirs

        grouped = _detect_provider_dirs()
        provider_dirs: list[str] = []
        for category in include_providers:
            for d in grouped.get(category, []):
                provider_dirs.append(str(d))

        state.update(
            {
                "provider": _provider,
                "model": _model,
                "dimension": _dimension,
                "api_key": api_key or "",
                "rerank_enabled": False,
                "memory_dir": _memory_dir,
                "provider_dirs": provider_dirs,
                "db_path": db_path or str(Path("~/.memtomem").expanduser() / "memtomem.db"),
                "enable_auto_ns": auto_ns,
                "default_ns": namespace or "default",
                "top_k": top_k or 10,
                "tokenizer": tokenizer or "unicode61",
                "decay_enabled": decay,
                "mcp_choice": {"claude": 1, "json": 2, "skip": 3}.get(mcp_mode or "skip", 3),
            }
        )
    else:
        steps = [
            _step_embedding,
            _step_reranker,
            _step_memory_dir,
            _step_provider_dirs,
            _step_storage,
            _step_namespace,
            _step_search,
            _step_language,
            _step_settings,
            _step_mcp,
        ]
        run_steps(steps, state)

    _write_config_and_summary(state, fresh=fresh)
