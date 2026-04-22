"""memtomem init — interactive setup wizard."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Literal

import click

from memtomem.cli.init_presets import PRESETS, _VALID_PRESETS, get_preset
from memtomem.cli.wizard import nav_confirm, nav_prompt, run_steps, step_header

InstallType = Literal["source", "project", "tool", "uvx"]


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
            # Use the install-type-aware hint so the inline message matches
            # what the summary would otherwise print, then mark the warning
            # as already surfaced so ``_collect_missing_extras`` skips it.
            click.echo(f"  Install with: {_extra_install_hint(['onnx'], state)}")
            click.echo("  Saving ONNX config now so you're ready after install.")
            click.echo()
            state.setdefault("_extras_warned_inline", set()).add("onnx")

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


# Namespace policy rule templates applied when the user accepts a provider
# category. Pairs with ``indexing.memory_dirs`` entries added in the same
# wizard step so auto_ns doesn't collapse ``~/.claude/projects/FOO/memory/**``
# to the generic ``default`` namespace (issue #296).
#
# ``claude-memory`` uses ``{ancestor:1}`` (the project-id folder above the
# generic ``memory`` basename); single-dir categories use literal namespaces.
# The flat ``claude-plans`` / ``codex`` labels are deliberately conservative:
# RFC #304 may migrate these into a vendor/product hierarchy later. Re-visit
# when that issue settles a wire format.
_PROVIDER_RULE_TEMPLATES: dict[str, tuple[str, str]] = {
    "claude-memory": ("~/.claude/projects/*/memory/**", "claude:{ancestor:1}"),
    "claude-plans": ("~/.claude/plans/**", "claude-plans"),
    "codex": ("~/.codex/memories/**", "codex"),
}

# Lock placeholders permitted in the preset table until RFC #304 settles the
# hierarchy format. New templates beyond ``{ancestor:1}`` should get an
# explicit review rather than a silent extension.
_VALID_PRESET_PLACEHOLDERS: frozenset[str] = frozenset({"{ancestor:1}"})

assert all(
    not any(tok in ns for tok in ("{", "}")) or any(tok in ns for tok in _VALID_PRESET_PLACEHOLDERS)
    for _, ns in _PROVIDER_RULE_TEMPLATES.values()
), "preset namespaces may only use _VALID_PRESET_PLACEHOLDERS until #304 decides the hierarchy"


def _expand_glob_for_compare(path_glob: str) -> str:
    """Return ``path_glob`` with ``~`` expanded for dedup comparison.

    Storage keeps the literal ``~`` form (matches what the user typed and
    what the current wizard writes), but dedup should treat
    ``~/.codex/memories/**`` and ``/Users/foo/.codex/memories/**`` as the
    same rule so re-running ``mm init`` is idempotent regardless of which
    form earlier runs or manual edits produced.
    """
    v = path_glob.strip()
    if v == "~" or v.startswith("~/"):
        v = str(Path(v).expanduser())
    return v


def _rule_matches_existing(new_path_glob: str, existing_rules: list[dict]) -> bool:
    """Return True if ``new_path_glob`` is already covered by an existing rule.

    Comparison is on ``path_glob`` only (after ``~`` expansion on both sides).
    When a user already has a rule for the same glob with a different
    namespace, the wizard respects that rule and skips the preset — the
    user's manual override wins. Returning True signals "skip the wizard
    rule"; the caller reports it in the banner so the skip isn't silent.
    """
    target = _expand_glob_for_compare(new_path_glob)
    for rule in existing_rules:
        existing_glob = rule.get("path_glob")
        if not isinstance(existing_glob, str):
            continue
        if _expand_glob_for_compare(existing_glob) == target:
            return True
    return False


def _proposed_rule_for_category(category: str) -> dict | None:
    """Return the ``{path_glob, namespace}`` dict for a category, or None."""
    template = _PROVIDER_RULE_TEMPLATES.get(category)
    if template is None:
        return None
    path_glob, namespace = template
    return {"path_glob": path_glob, "namespace": namespace}


def _emit_rules_banner(
    proposed: list[tuple[str, dict]],
    skipped: list[tuple[str, dict]],
) -> None:
    """Print the pre-write rules banner.

    ``proposed`` and ``skipped`` carry ``(category, rule_dict)`` pairs.
    Banner is emitted only when at least one rule was offered; an
    all-skipped run still prints a one-liner so the user knows the wizard
    looked at rules but decided existing ones covered them.
    """
    if not proposed and not skipped:
        return
    click.echo("  Namespace rules:")
    if not proposed and skipped:
        n = len(skipped)
        click.secho(
            f"    {n} rule(s) already managed, nothing to add.",
            fg="yellow",
        )
        click.echo()
        return
    for _, rule in proposed:
        line = f"    + {rule['path_glob']:<40} → {rule['namespace']}"
        click.secho(line, fg="green")
    for _, rule in skipped:
        line = f"    ⏭ {rule['path_glob']:<40} (existing rule kept)"
        click.secho(line, fg="yellow")
    click.echo()


def _step_provider_dirs(state: dict) -> None:
    """Opt-in indexing of provider memory folders detected on this machine.

    Replaces the legacy silent ``ensure_auto_discovered_dirs`` runtime path.
    Per-category prompts (Claude Code memory, Claude plans, Codex memories)
    let the user pick exactly which surfaces to make searchable. Categories
    with zero detected directories are skipped silently — the step is a
    no-op on machines without any of these tools installed.

    When a category is accepted and has a matching preset in
    ``_PROVIDER_RULE_TEMPLATES``, the corresponding ``NamespacePolicyRule``
    is collected into ``state['provider_rules']`` for the write step to
    merge into ``namespace.rules``. This addresses #296: auto_ns collapses
    to ``default`` for memory_dirs whose basename is non-discriminating
    (``memory`` / ``plans`` / ``memories``).
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
    accepted_categories: list[str] = []

    if "claude-memory" in available:
        n = len(available["claude-memory"])
        suffix = "project" if n == 1 else "projects"
        if nav_confirm(
            f"  Claude Code per-project memory ({n} {suffix} with .md content)?",
            default=False,
        ):
            selected.extend(available["claude-memory"])
            accepted_categories.append("claude-memory")

    if "claude-plans" in available:
        if nav_confirm(
            "  Claude Code plans (~/.claude/plans/)?",
            default=False,
        ):
            selected.extend(available["claude-plans"])
            accepted_categories.append("claude-plans")

    if "codex" in available:
        if nav_confirm(
            "  Codex CLI memories (~/.codex/memories/)?",
            default=False,
        ):
            selected.extend(available["codex"])
            accepted_categories.append("codex")

    state["provider_dirs"] = [str(p) for p in selected]
    state["provider_rules"] = [
        (cat, _proposed_rule_for_category(cat))
        for cat in accepted_categories
        if _proposed_rule_for_category(cat) is not None
    ]
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


def _claude_desktop_config_hint() -> str:
    """Return the Claude Desktop config path for the current OS.

    Claude Desktop is the only editor in ``_emit_mcp_paste_hints`` whose
    config location is OS-specific; Cursor / Windsurf / Gemini CLI all use
    a single ``~/<dot-dir>/...`` layout that works on every platform."""
    if sys.platform == "darwin":
        return "~/Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return r"%APPDATA%\Claude\claude_desktop_config.json"
    return "~/.config/Claude/claude_desktop_config.json"


def _emit_mcp_paste_hints() -> None:
    """Print per-editor paste targets for the generated ``.mcp.json``.

    Claude Code auto-loads a project-root ``.mcp.json``; other editors do not
    and expect their own config file. Shown after every path that writes the
    file so users know the generated JSON is a template, not a drop-in config
    for Cursor/Windsurf/Claude Desktop/Gemini CLI."""
    click.echo("    Cursor          → paste into ~/.cursor/mcp.json")
    click.echo("    Windsurf        → paste into ~/.codeium/windsurf/mcp_config.json")
    click.echo(f"    Claude Desktop  → paste into {_claude_desktop_config_hint()}")
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


def _maybe_offer_embedding_reset(state: dict, *, interactive: bool) -> None:
    """Reconcile an existing DB's embedding metadata with the new config.

    ``mm init`` rewrites ``~/.memtomem/config.json`` but leaves the SQLite DB
    untouched. If a previous install wrote ``chunks_vec`` with a different
    provider / dimension (classically ``provider=none`` → dim=0), the next
    MCP server startup fails fast with ``EmbeddingDimensionMismatchError``.
    Detect the mismatch here and offer to reset so users don't see an opaque
    crash on their first ``mm web`` / MCP launch.

    Interactive: prompt before the destructive reset (default=yes so the
    wizard's common case flows forward without extra typing). Non-interactive
    (``-y`` / piped stdin): print a loud warning pointing at the exact
    recovery command instead of guessing.
    """
    import asyncio

    db_path = Path(state["db_path"]).expanduser()
    if not db_path.exists():
        return

    try:
        asyncio.run(_check_embedding_mismatch(state, interactive=interactive))
    except Exception as exc:
        click.secho(
            f"  Warning: could not check DB embedding state ({exc.__class__.__name__}: {exc}). "
            f"Run 'mm embedding-reset --mode status' to verify manually.",
            fg="yellow",
        )


async def _check_embedding_mismatch(state: dict, *, interactive: bool) -> None:
    from memtomem.config import StorageConfig
    from memtomem.storage.sqlite_backend import SqliteBackend

    storage_cfg = StorageConfig(sqlite_path=Path(state["db_path"]).expanduser())
    storage = SqliteBackend(
        storage_cfg,
        dimension=state["dimension"],
        embedding_provider=state["provider"],
        embedding_model=state["model"],
        strict_dim_check=False,
    )
    await storage.initialize()
    try:
        mismatch = getattr(storage, "embedding_mismatch", None)
        if mismatch is None:
            return

        stored = mismatch["stored"]
        configured = mismatch["configured"]
        click.echo()
        click.secho("  Existing DB detected — embedding mismatch:", fg="yellow", bold=True)
        click.echo(
            f"    DB stored:  {stored['provider']}/{stored['model']} ({stored['dimension']}d)"
        )
        click.echo(
            f"    New config: {configured['provider']}/{configured['model']} "
            f"({configured['dimension']}d)"
        )
        click.echo()
        click.echo("  The MCP server will fail to start until this is resolved.")

        if not interactive:
            click.secho(
                "  Run 'mm embedding-reset --mode apply-current' to recreate the "
                "vector index with the new dimension.",
                fg="yellow",
            )
            return

        click.echo()
        if click.confirm(
            "  Reset vector index now? (chunks table preserved, re-index required)",
            default=True,
        ):
            await storage.reset_embedding_meta(
                dimension=state["dimension"],
                provider=state["provider"],
                model=state["model"],
            )
            click.secho(
                f"  Vector index reset to {state['provider']}/{state['model']} "
                f"({state['dimension']}d). Run 'mm index <path>' to re-embed.",
                fg="green",
            )
        else:
            click.secho(
                "  Skipped. Run 'mm embedding-reset --mode apply-current' before "
                "starting the MCP server.",
                fg="yellow",
            )
    finally:
        await storage.close()


# Canonical hint prefixes. Branching lives in ``_extra_install_hint``:
# source/project installs use the workspace ``uv sync`` path so the extras
# land in the same ``.venv`` that ``uv run mm`` will use; everything else
# (``uv tool install`` / PyPI) stays on the tool-env reinstall path that the
# global ``mm`` binary relies on.
_EXTRA_INSTALL_HINT_PREFIX: str = 'uv tool install --reinstall "memtomem'
_UV_SYNC_HINT_PREFIX: str = "uv sync --extra "

# Probe snippet for extras presence in a foreign Python. ``find_spec`` is
# cheaper than ``__import__`` and matches ``_collect_missing_extras``'s
# historical semantic.
_PROBE_EXTRAS_CODE: str = (
    "import json, importlib.util as u; "
    "print(json.dumps([n for n in ['fastembed','fastapi','uvicorn'] "
    "if u.find_spec(n) is not None]))"
)


def _workspace_python(state: dict) -> Path | None:
    """Return ``<source_dir|project_dir>/.venv/bin/python`` if the workspace
    venv exists, else ``None``.

    This is the reconcile hook between the cwd-filesystem axis
    (:func:`_is_source_install`) and the runtime-interpreter axis
    (``sys.executable``) — the Phase 1 fix for issue #360. When the user
    runs ``mm init`` from a source/project checkout via a global ``mm``
    binary (``uv tool install``), the wizard's own interpreter is the tool
    env (which may lack ``[all]``), but ``Next steps`` prints ``uv run mm``
    which will use the workspace venv. Probing the workspace venv here
    avoids warning the user to reinstall the tool env when the workspace
    already has the extras."""
    root = state.get("source_dir") or state.get("project_dir")
    if not root:
        return None
    py = Path(root) / ".venv" / "bin" / "python"
    return py if py.exists() else None


def _probe_workspace_extras(py: Path) -> set[str] | None:
    """Ask a foreign Python which of ``fastembed`` / ``fastapi`` / ``uvicorn``
    are importable. Returns the importable subset, or ``None`` on subprocess
    failure (missing binary, timeout, non-zero rc, bad JSON). Callers treat
    ``None`` as "unknown — fall back to the in-process probe"."""
    try:
        result = _run([str(py), "-c", _PROBE_EXTRAS_CODE], timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    return set(parsed)


def _inproc_have_extras() -> tuple[bool, bool]:
    """Return ``(have_fastembed, have_web)`` via in-process ``find_spec`` /
    :func:`memtomem.cli.web._missing_web_deps`. Used when not a
    source/project install, or when the workspace-python probe fails."""
    from importlib.util import find_spec

    from memtomem.cli.web import _missing_web_deps

    return (find_spec("fastembed") is not None, _missing_web_deps() is None)


def _workspace_needs_sync(state: dict) -> bool:
    """True iff source/project install detected but ``<dir>/.venv/`` is
    absent — a fresh clone / fresh worktree. The summary shows a single
    ``run uv sync first`` line in this case instead of a noisy missing-
    extras warning that would have probed the wrong interpreter."""
    source = state.get("source_install") or state.get("project_install")
    if not source:
        return False
    return _workspace_python(state) is None


def _extra_install_hint(extras: list[str], state: dict | None = None) -> str:
    """Return the install command for one or more missing extras, branched
    by install type.

    - source / project install → ``uv sync --extra <name>`` (single) or
      ``uv sync --extra all`` (two+). The workspace ``.venv`` is what
      ``uv run mm`` will use, so ``uv sync`` is the right install path.
    - PyPI / ``uv tool install`` (default) → ``uv tool install --reinstall
      "memtomem[<name>]"`` or ``[all]``. Matches how the global ``mm``
      binary is installed.

    Multi-extra case collapses to ``[all]`` instead of bracketed multiples
    (``memtomem[onnx,web]``) because ``[all]`` is the public, documented
    extra in ``pyproject.toml``; the user doesn't need to know the
    sub-bundle."""
    state = state or {}
    is_workspace = state.get("source_install") or state.get("project_install")
    name = extras[0] if len(extras) == 1 else "all"
    if is_workspace:
        return f"{_UV_SYNC_HINT_PREFIX}{name}"
    return f'{_EXTRA_INSTALL_HINT_PREFIX}[{name}]"'


def _install_extras(
    install_type: InstallType,
    extras: list[str],
    *,
    confirm: bool = False,
    workspace_dir: Path | None = None,
) -> bool:
    """Confirm-then-subprocess install for python package extras.

    Scope is intentionally narrow: python package extras only. Ollama model-
    pull and ``claude mcp add`` have different shapes (no ``install_type ×
    extras`` axis — a model name vs. a package-extras list) and stay inline
    at their current call sites. Generalizing this helper into an arbitrary
    "confirm + subprocess + fallback" runner would bloat it with call-site-
    specific prompt strings for zero reuse today.

    ``confirm`` is passed as ``default=`` to :func:`nav_confirm`: ``True``
    means Enter-is-Yes (ollama-style — the user likely wants the heavy
    model download), ``False`` means Enter-is-No (python-extras-style —
    the user has already typed through the wizard and a ``[all]`` reinstall
    is heavy).

    ``workspace_dir`` is REQUIRED when ``install_type`` is ``"source"`` or
    ``"project"`` (used as ``subprocess.run`` ``cwd=`` for ``uv sync``)
    and MUST be ``None`` for ``"tool"`` / ``"uvx"``. Callers are expected
    to branch on the install type before calling.

    Returns ``True`` only when a subprocess actually ran and exited 0. In
    every other case — empty ``extras``, missing ``workspace_dir`` for
    source/project, ``uvx`` branch (hint-only, no install semantic),
    user decline, ``FileNotFoundError``, ``TimeoutExpired``, or non-zero
    rc — returns ``False`` so the caller falls back to the Phase 1
    :func:`_emit_missing_extras_warning` hint path."""
    if not extras:
        return False
    name = extras[0] if len(extras) == 1 else "all"

    cwd: str | None
    if install_type in ("source", "project"):
        if workspace_dir is None:
            return False
        cmd = ["uv", "sync", "--extra", name]
        cwd = str(workspace_dir)
    elif install_type == "tool":
        cmd = ["uv", "tool", "install", "--reinstall", f"memtomem[{name}]"]
        cwd = None
    else:  # uvx — ephemeral env; a non-ephemeral install is meaningless
        click.echo(
            "  (uvx is ephemeral — re-invoke with "
            f'`uvx --from "memtomem[{name}]" memtomem ...` instead)'
        )
        return False

    prompt = f"  Install memtomem[{name}] now?"
    if not nav_confirm(prompt, default=confirm):
        return False

    click.echo(f"  Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _collect_missing_extras(state: dict) -> list[str]:
    """Return ordered list of missing extras the chosen config will need.

    Source/project install: probes the workspace ``.venv/bin/python`` — the
    interpreter ``uv run mm`` will use — so the warning matches what
    ``Next steps`` will actually run. If the workspace venv is absent the
    caller shows a ``run uv sync first`` one-liner instead, so this path
    only fires when the probe has authoritative state.

    Everything else (PyPI / ``uv tool install``) uses in-process
    ``find_spec`` as before — it matches the ``mm`` binary the wizard is
    running under, which is also what the un-prefixed ``mm index`` /
    ``mm web`` commands will use.

    Order: ``onnx`` before ``web`` so the extras appear in the same order
    the dependent commands fail (``mm index`` → ``mm web``). Entries in
    ``state['_extras_warned_inline']`` are filtered out so the interactive
    ``_step_embedding`` path doesn't double-print."""
    source = state.get("source_install") or state.get("project_install")
    ws_py = _workspace_python(state) if source else None

    if source and ws_py is not None:
        importable = _probe_workspace_extras(ws_py)
        if importable is not None:
            have_fastembed = "fastembed" in importable
            have_web = {"fastapi", "uvicorn"}.issubset(importable)
        else:
            have_fastembed, have_web = _inproc_have_extras()
    else:
        have_fastembed, have_web = _inproc_have_extras()

    missing: list[str] = []
    # [onnx] = fastembed. Both the embedder and the fastembed reranker share
    # this package — either on its own is enough to require the extra.
    needs_fastembed = state.get("provider") == "onnx" or (
        state.get("rerank_enabled") and state.get("rerank_model")
    )
    if needs_fastembed and not have_fastembed:
        missing.append("onnx")
    if not have_web:
        missing.append("web")

    warned = state.get("_extras_warned_inline") or set()
    return [x for x in missing if x not in warned]


def _runtime_under_workspace_venv(state: dict) -> bool:
    """True iff the current ``sys.executable`` is inside ``<workspace>/.venv/``.

    Used by :func:`_emit_cwd_runtime_mismatch_banner` to decide whether the
    user is running a global ``mm`` from inside a source/project checkout
    (mismatch) vs. ``uv run mm`` from the workspace venv (aligned).

    Compares raw path strings (no ``.resolve()``) on purpose: ``uv`` creates
    ``.venv/bin/python3`` as a symlink to its managed interpreter cache
    (``~/.local/share/uv/python/...``). Resolving the symlink would always
    land outside the workspace and fire the banner even when the runtime
    IS the workspace venv. The raw prefix comparison matches how Python
    itself reports ``sys.executable`` (it reports the symlink path, not
    the target)."""
    root = state.get("source_dir") or state.get("project_dir")
    if not root:
        return False
    try:
        return Path(sys.executable).is_relative_to(Path(root) / ".venv")
    except (OSError, ValueError):
        return False


def _emit_cwd_runtime_mismatch_banner(state: dict) -> None:
    """Info banner for ``source/project cwd + non-workspace runtime``.

    Phase 1 (#361) silenced the false missing-extras warning for this
    combination (the wizard's own interpreter is the tool env but Next-
    steps uses ``uv run mm`` which probes the workspace venv). Phase 2
    closes the UX loop by explaining the silence: users otherwise wonder
    whether the wizard did the right thing or swallowed an error.

    Trigger is orthogonal to extras presence — the mismatch is a property
    of the axes themselves, not of what's installed. Prints nothing when
    the runtime interpreter is inside the workspace venv, or when the
    install type is PyPI / tool (no cwd axis to disagree with)."""
    if not (state.get("source_install") or state.get("project_install")):
        return
    if _runtime_under_workspace_venv(state):
        return
    cwd_type = "source" if state.get("source_install") else "project"
    click.echo()
    click.secho(
        f"  i  cwd: {cwd_type} / runtime: uv tool — Next steps assume `uv run mm`.",
        fg="cyan",
    )
    click.secho(
        "     If you intend to run bare `mm`, install [all] into the tool env.",
        fg="cyan",
    )


def _emit_missing_extras_warning(missing: list[str], state: dict) -> None:
    """Print an actionable warning about missing extras, or nothing.

    Silent when ``missing`` is empty so the common "already installed" path
    adds no output noise. When non-empty, names the missing extras, lists
    which ``Next steps`` commands will fail without them, and prints a single
    install command (narrow per-extra hint when one missing, ``[all]`` when
    two+ — matches ``pyproject.toml``'s public extras). The hint is branched
    by install type: workspace installs get ``uv sync --extra …``, tool
    installs get ``uv tool install --reinstall …``."""
    if not missing:
        return
    click.echo()
    click.secho(
        f"  [!] Missing extras: {', '.join(missing)}",
        fg="yellow",
        bold=True,
    )
    if "onnx" in missing and "web" in missing:
        click.echo("      'mm index' (embeddings) and 'mm web' (UI) will fail until installed.")
    elif "onnx" in missing:
        click.echo("      'mm index' will fail to embed until installed.")
    elif "web" in missing:
        click.echo("      'mm web' will fail to start until installed.")
    click.echo(f"      → {_extra_install_hint(missing, state)}")


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

    # Namespace preset rules from accepted provider categories — append to
    # existing ``namespace.rules`` (dedup by ``path_glob``, user rules win
    # per the A-2 decision in #296's design thread). Mutates ``existing``
    # directly because ``init_data["namespace"]`` intentionally doesn't
    # carry ``rules`` (the merge loop would overwrite user rules via
    # ``update``). Banner is printed *before* write so a partial failure
    # surfaces what was attempted.
    proposed_rules: list[tuple[str, dict]] = state.get("provider_rules") or []
    if proposed_rules:
        existing_ns = existing.setdefault("namespace", {})
        raw_rules = existing_ns.get("rules")
        existing_rules: list[dict] = raw_rules if isinstance(raw_rules, list) else []
        to_append: list[tuple[str, dict]] = []
        to_skip: list[tuple[str, dict]] = []
        for cat, rule in proposed_rules:
            if _rule_matches_existing(rule["path_glob"], existing_rules):
                to_skip.append((cat, rule))
            else:
                to_append.append((cat, rule))
        _emit_rules_banner(to_append, to_skip)
        if to_append:
            merged_rules = list(existing_rules)
            for _, rule in to_append:
                merged_rules.append(dict(rule))
            existing_ns["rules"] = merged_rules
        # Mark as wizard-touched so the Preserved block doesn't flag the
        # merged-rule list as "leftover from a prior config" on a clean
        # re-run where every preset is already present.
        wizard_touched_keys.add("namespace.rules")

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

    # Warn about missing extras (fastembed for onnx, fastapi/uvicorn for web)
    # before Next Steps so users don't hit "fastembed required" or "Web UI
    # requires the [web] extra" after following the printed commands. The
    # interactive `_step_embedding` covers the onnx case inline, but preset
    # paths (minimal/english/korean) skip that step entirely — check here so
    # every path surfaces the gap.
    #
    # Source/project install + workspace .venv absent (fresh worktree) →
    # the missing-extras probe has no authoritative interpreter to query;
    # show a single ``run uv sync first`` line instead. Otherwise probe the
    # workspace venv (if present) or in-process and emit the regular
    # warning. Issue #360 Phase 1.
    #
    # Before either branch: cwd-vs-runtime mismatch banner (#360 Phase 2).
    # Source/project cwd + non-workspace runtime → explain why the wizard
    # stays quiet about the tool-env extras and which invocation shape the
    # printed Next steps assume.
    _emit_cwd_runtime_mismatch_banner(state)
    install_type_lit: InstallType = (
        "source" if source_install else "project" if project_install else "tool"
    )
    if _workspace_needs_sync(state):
        click.echo()
        click.secho(
            "  Workspace .venv not found — run `uv sync --extra all` first.",
            fg="yellow",
        )
    else:
        missing = _collect_missing_extras(state)
        if missing:
            ws: Path | None
            if source_install:
                ws = Path(state["source_dir"])
            elif project_install:
                ws = Path(state["project_dir"])
            else:
                ws = None
            installed = _install_extras(install_type_lit, missing, confirm=False, workspace_dir=ws)
            if installed:
                click.echo()
                click.secho(
                    f"  Installed missing extras: {', '.join(missing)}.",
                    fg="green",
                )
            else:
                _emit_missing_extras_warning(missing, state)

    click.echo()
    click.secho("  Next steps:", fg="cyan")
    run_prefix = "uv run " if source_install or project_install else ""
    click.echo(f"    1. {run_prefix}mm index {state['memory_dir']}")
    click.echo(f"    2. {run_prefix}mm search 'your first query'")
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


# ── Preset quick-setup helpers ────────────────────────────────────────


def _step_preset_picker(state: dict) -> None:
    """First step of the default interactive path — pick a preset or Advanced.

    Writes ``state["_preset_choice"]`` = one of ``"minimal" | "english" |
    "korean" | "advanced"``. The Advanced entry falls through to the full
    10-step wizard; the other three bundle embedding/reranker/tokenizer/
    namespace defaults via ``_apply_preset`` so the user only sees the
    essential memory-dir and MCP questions afterwards.
    """
    # First step — no prior step to return to, so only surface 'q: quit'.
    # 'b' still works (nav_prompt intercepts it before IntRange), but
    # run_steps treats back-on-step-0 as a no-op, which would confuse
    # users if advertised.
    click.secho("  Choose setup style:", fg="yellow", bold=True)
    click.echo(click.style("  (q: quit)", dim=True))
    click.echo()

    ordered: list[str] = ["minimal", "english", "korean"]
    for i, name in enumerate(ordered, start=1):
        spec = PRESETS[name]  # type: ignore[index]
        click.echo(f"    [{i}] {spec.label}")
        click.echo(f"        {spec.description}")
    advanced_idx = len(ordered) + 1
    click.echo(f"    [{advanced_idx}] Advanced — full 10-step wizard (all options)")
    click.echo()

    choice = nav_prompt("  Select", type=click.IntRange(1, advanced_idx), default=2)
    click.echo()

    if choice == advanced_idx:
        state["_preset_choice"] = "advanced"
    else:
        state["_preset_choice"] = ordered[choice - 1]


def _apply_preset(state: dict, preset_name: str) -> None:
    """Populate ``state`` with a preset's bundled defaults.

    Mirrors the flat keys individual ``_step_*`` functions write so the
    downstream ``_write_config_and_summary`` merge path is unchanged.
    Does NOT touch ``memory_dir`` or ``mcp_choice`` — those remain asked
    interactively (or come from explicit CLI flags).
    """
    spec = get_preset(preset_name)
    state["provider"] = spec.provider
    state["model"] = spec.model
    state["dimension"] = spec.dimension
    state["api_key"] = ""
    state["rerank_enabled"] = spec.rerank_enabled
    if spec.rerank_enabled and spec.rerank_model is not None:
        state["rerank_model"] = spec.rerank_model
    state["tokenizer"] = spec.tokenizer
    state["top_k"] = spec.default_top_k
    state["enable_auto_ns"] = spec.enable_auto_ns
    state["default_ns"] = spec.default_namespace
    state["decay_enabled"] = spec.decay_enabled
    state["db_path"] = str(Path("~/.memtomem").expanduser() / "memtomem.db")
    state["settings_hooks"] = False
    state["_preset_applied"] = preset_name


def _step_provider_dirs_auto(state: dict) -> None:
    """Auto-apply detected provider memory folders for preset 2/3.

    Respects the preset's ``autodetect_providers`` flag: when False (minimal),
    skips without scanning. When True, scans via ``_detect_provider_dirs``,
    adds every detected category, emits a banner listing what was added (or
    a one-line message when nothing was detected so the skip isn't silent).
    """
    preset_name = state.get("_preset_applied")
    if preset_name is None:
        state.setdefault("provider_dirs", [])
        state.setdefault("provider_rules", [])
        return

    spec = get_preset(preset_name)
    if not spec.autodetect_providers:
        state["provider_dirs"] = []
        state["provider_rules"] = []
        return

    from memtomem.config import _detect_provider_dirs

    grouped = _detect_provider_dirs()
    available = {cat: dirs for cat, dirs in grouped.items() if dirs}

    if not available:
        click.echo(
            "  No provider memory folders detected (checked: ~/.claude/projects, "
            "~/.claude/plans, ~/.codex/memories)."
        )
        click.echo("  Run 'mm init --advanced' later to add custom paths.")
        click.echo()
        state["provider_dirs"] = []
        state["provider_rules"] = []
        return

    selected: list[Path] = []
    accepted_categories: list[str] = []
    for cat in ("claude-memory", "claude-plans", "codex"):
        dirs_for_cat = available.get(cat, [])
        if dirs_for_cat:
            selected.extend(dirs_for_cat)
            accepted_categories.append(cat)

    state["provider_dirs"] = [str(p) for p in selected]
    state["provider_rules"] = [
        (cat, _proposed_rule_for_category(cat))
        for cat in accepted_categories
        if _proposed_rule_for_category(cat) is not None
    ]
    click.secho(
        f"  Auto-added {len(selected)} provider folder(s) "
        f"from {len(accepted_categories)} categor"
        f"{'y' if len(accepted_categories) == 1 else 'ies'}.",
        fg="green",
    )
    for cat in accepted_categories:
        n = len(available[cat])
        suffix = "dir" if n == 1 else "dirs"
        click.echo(f"    • {cat} ({n} {suffix})")
    click.echo()


def _resolve_provider_dirs_non_interactive(
    state: dict,
    preset_name: str,
    include_providers: tuple[str, ...],
) -> None:
    """Compute ``provider_dirs`` / ``provider_rules`` for the scripted path.

    Union of the preset's autodetect set (when ``autodetect_providers`` is
    True) and any categories named via ``--include-provider``. Categories
    without detected dirs are silently skipped — mirrors the legacy
    non-interactive policy (no error if a user asks for ``codex`` on a
    Claude-only box).
    """
    from memtomem.config import _detect_provider_dirs

    spec = get_preset(preset_name)
    grouped = _detect_provider_dirs()
    categories_to_add: set[str] = set()
    if spec.autodetect_providers:
        categories_to_add.update(cat for cat, dirs in grouped.items() if dirs)
    for explicit in include_providers:
        if grouped.get(explicit):
            categories_to_add.add(explicit)

    provider_dirs: list[str] = []
    provider_rules: list[tuple[str, dict]] = []
    for cat in ("claude-memory", "claude-plans", "codex"):
        if cat in categories_to_add:
            for d in grouped.get(cat, []):
                provider_dirs.append(str(d))
            rule = _proposed_rule_for_category(cat)
            if rule is not None:
                provider_rules.append((cat, rule))
    state["provider_dirs"] = provider_dirs
    state["provider_rules"] = provider_rules


def _override_from_flags(
    state: dict,
    *,
    provider: str | None,
    model: str | None,
    tokenizer: str | None,
    memory_dir: str | None,
    db_path: str | None,
    namespace: str | None,
    auto_ns: bool,
    top_k: int | None,
    decay: bool,
    api_key: str | None,
    mcp_mode: str | None,
) -> None:
    """Apply explicit CLI flag values over preset-populated state.

    Called after ``_apply_preset`` in every preset branch (non-interactive,
    explicit ``--preset``, interactive picker) so flag precedence is
    consistent: preset sets the baseline, explicit flags win. ``--provider``
    also refreshes ``dimension`` via ``_MODEL_DIMS`` to match the
    non-interactive block's behavior.
    """
    if provider is not None:
        # Only reset the preset-populated model/dimension when the user
        # actually switches to a different provider AND hasn't supplied
        # their own --model. `mm init --preset korean --provider onnx`
        # must keep korean's bge-m3 (same provider); `--preset korean
        # --provider ollama` must fall back to ollama's default model.
        provider_changed = provider != state.get("provider")
        state["provider"] = provider
        if provider_changed and model is None:
            if provider == "none":
                state["model"] = ""
                state["dimension"] = 0
            else:
                defaults = {
                    "onnx": ("all-MiniLM-L6-v2", 384),
                    "ollama": ("nomic-embed-text", 768),
                    "openai": ("text-embedding-3-small", 1536),
                }
                if provider in defaults:
                    default_model, default_dim = defaults[provider]
                    state["model"] = default_model
                    state["dimension"] = default_dim
    if model is not None:
        state["model"] = model
        state["dimension"] = _MODEL_DIMS.get(model, state.get("dimension", 0))
    if tokenizer is not None:
        state["tokenizer"] = tokenizer
    if memory_dir is not None:
        state["memory_dir"] = memory_dir
    if db_path is not None:
        state["db_path"] = db_path
    if namespace is not None:
        state["default_ns"] = namespace
    if auto_ns:
        state["enable_auto_ns"] = True
    if top_k is not None:
        state["top_k"] = top_k
    if decay:
        state["decay_enabled"] = True
    if api_key is not None:
        state["api_key"] = api_key
    if mcp_mode is not None:
        state["mcp_choice"] = {"claude": 1, "json": 2, "skip": 3}.get(mcp_mode, 3)


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
@click.option(
    "--preset",
    type=click.Choice(sorted(_VALID_PRESETS)),
    default=None,
    help=(
        "Apply a preset bundle (minimal | english | korean) of embedding, "
        "reranker, tokenizer, and namespace defaults. Without this flag, the "
        "interactive picker asks at startup; `mm init -y` alone behaves as "
        "`--preset minimal -y`. Mutually exclusive with --advanced."
    ),
)
@click.option(
    "--advanced",
    is_flag=True,
    default=False,
    help=(
        "Force the full 10-step wizard (skip the preset picker). Useful for "
        "fine-grained control over every step. Mutually exclusive with --preset."
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
    preset: str | None,
    advanced: bool,
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

    if preset and advanced:
        raise click.UsageError("--preset and --advanced are mutually exclusive")

    advanced_steps = [
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

    if non_interactive:
        # `mm init -y` alone behaves as `--preset minimal -y`; minimal's defaults
        # match the previous non-interactive block's (provider="none", no rerank,
        # unicode61, auto_ns=False, top_k=10). Explicit flags then override the
        # preset baseline, preserving the prior `-y --provider onnx --model X`
        # contract.
        effective_preset = preset or "minimal"
        _apply_preset(state, effective_preset)
        _override_from_flags(
            state,
            provider=provider,
            model=model,
            tokenizer=tokenizer,
            memory_dir=memory_dir,
            db_path=db_path,
            namespace=namespace,
            auto_ns=auto_ns,
            top_k=top_k,
            decay=decay,
            api_key=api_key,
            mcp_mode=mcp_mode,
        )
        if "memory_dir" not in state:
            state["memory_dir"] = "~/memories"
        if "mcp_choice" not in state:
            state["mcp_choice"] = 3  # skip — scripted runs don't touch Claude

        memory_path = Path(state["memory_dir"]).expanduser()
        if not memory_path.exists():
            memory_path.mkdir(parents=True, exist_ok=True)

        _resolve_provider_dirs_non_interactive(state, effective_preset, include_providers)
    elif advanced:
        run_steps(advanced_steps, state)
    elif preset:
        _apply_preset(state, preset)
        _override_from_flags(
            state,
            provider=provider,
            model=model,
            tokenizer=tokenizer,
            memory_dir=memory_dir,
            db_path=db_path,
            namespace=namespace,
            auto_ns=auto_ns,
            top_k=top_k,
            decay=decay,
            api_key=api_key,
            mcp_mode=mcp_mode,
        )
        run_steps([_step_memory_dir, _step_provider_dirs_auto, _step_mcp], state)
    else:
        # Default interactive path: show the preset picker, dispatch on choice.
        if not sys.stdin.isatty():
            raise click.UsageError("Non-interactive terminal detected. Pass --preset <name> or -y.")
        run_steps([_step_preset_picker], state)
        if state["_preset_choice"] == "advanced":
            run_steps(advanced_steps, state)
        else:
            _apply_preset(state, state["_preset_choice"])
            _override_from_flags(
                state,
                provider=provider,
                model=model,
                tokenizer=tokenizer,
                memory_dir=memory_dir,
                db_path=db_path,
                namespace=namespace,
                auto_ns=auto_ns,
                top_k=top_k,
                decay=decay,
                api_key=api_key,
                mcp_mode=mcp_mode,
            )
            run_steps([_step_memory_dir, _step_provider_dirs_auto, _step_mcp], state)

    _write_config_and_summary(state, fresh=fresh)
    _maybe_offer_embedding_reset(state, interactive=not non_interactive)
