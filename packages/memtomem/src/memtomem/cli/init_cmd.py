"""memtomem init — interactive setup wizard."""

from __future__ import annotations

import json
import shutil
import subprocess
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


def _step_memory_dir(state: dict) -> None:
    step_header(2, "Memory Directory")
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


def _step_storage(state: dict) -> None:
    step_header(3, "Storage")
    config_dir = Path("~/.memtomem").expanduser()
    db_default = str(config_dir / "memtomem.db")
    state["db_path"] = nav_prompt("  SQLite DB path", default=db_default)
    click.echo()


def _step_namespace(state: dict) -> None:
    step_header(4, "Namespace")
    click.echo("  Organize memories into scoped groups (work, personal, project).")
    state["enable_auto_ns"] = nav_confirm(
        "  Auto-assign namespace from folder name? (~/docs → 'docs')", default=False
    )
    state["default_ns"] = nav_prompt("  Default namespace", default="default")
    click.echo()


def _step_search(state: dict) -> None:
    step_header(5, "Search")
    state["top_k"] = nav_prompt("  Results per search", type=click.INT, default=10)
    state["decay_enabled"] = nav_confirm(
        "  Enable time-decay? (older memories rank lower)", default=False
    )
    click.echo()


def _step_language(state: dict) -> None:
    step_header(6, "Language")
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
    step_header(7, "Claude Code Hooks")

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
    step_header(8, "Connect to AI Editor")
    click.echo("  How do you want to connect memtomem?")
    click.echo("    [1] Claude Code (run 'claude mcp add' automatically)")
    click.echo("    [2] Generate .mcp.json (for Cursor, Windsurf, etc.)")
    click.echo("    [3] Skip — I'll configure it manually")
    state["mcp_choice"] = nav_prompt("  Select", type=click.IntRange(1, 3), default=1)
    click.echo()


# ── Write config & summary ────────────────────────────────────────────


def _write_config_and_summary(state: dict, base_dir: Path | None = None) -> None:
    """Write config files and show summary (runs after all steps)."""
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

    # Read existing config as merge base (preserves post-init user edits)
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass

    # Build init-target fields only
    init_data: dict = {
        "embedding": {
            "provider": state["provider"],
            "model": state["model"],
            "dimension": state["dimension"],
        },
        "storage": {"backend": "sqlite", "sqlite_path": state["db_path"]},
        "indexing": {"memory_dirs": [state["memory_dir"]]},
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

    # Merge: init fields overwrite, non-init sections/fields preserved
    for section, fields in init_data.items():
        if section not in existing:
            existing[section] = {}
        if isinstance(fields, dict) and isinstance(existing[section], dict):
            existing[section].update(fields)
        else:
            existing[section] = fields

    config_path.write_text(json.dumps(existing, indent=2, default=str), encoding="utf-8")
    click.echo(f"  Config: {config_path}")

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
                click.echo("  MCP config: .mcp.json")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            click.echo("  Claude Code: 'claude' not found. Use .mcp.json instead.")
            _write_mcp_json(server_cmd, server_args, mcp_env)
            click.echo("  MCP config: .mcp.json")
    elif mcp_choice == 2:
        _write_mcp_json(server_cmd, server_args, mcp_env)
        click.echo("  MCP config: .mcp.json")

    # Summary
    click.echo()
    click.secho("  Setup complete!", fg="green", bold=True)
    click.echo()
    if state["provider"] == "none":
        click.echo("  Provider:   none (BM25 keyword search only)")
    else:
        click.echo(f"  Provider:   {state['provider']}/{state['model']} ({state['dimension']}d)")
    click.echo(f"  Storage:    {state['db_path']}")
    click.echo(f"  Memory:     {state['memory_dir']}")
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

        state.update(
            {
                "provider": _provider,
                "model": _model,
                "dimension": _dimension,
                "api_key": api_key or "",
                "memory_dir": _memory_dir,
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
            _step_memory_dir,
            _step_storage,
            _step_namespace,
            _step_search,
            _step_language,
            _step_mcp,
            _step_settings,
        ]
        run_steps(steps, state)

    _write_config_and_summary(state)
