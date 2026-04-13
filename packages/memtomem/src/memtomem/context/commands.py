"""Canonical ⇄ runtime slash/custom command fan-out.

Phase 3 (+ Phase 3.5) of the "memtomem as canonical context gateway" plan. A
slash command lives at ``.memtomem/commands/<name>.md`` with YAML frontmatter
(Claude Code-compatible superset) and a Markdown body that acts as the prompt
template. From that single canonical source we fan out to three runtimes:

* ``.claude/commands/<name>.md`` — Claude Code (Markdown + YAML, pass-through)
* ``.gemini/commands/<name>.toml`` — Gemini CLI (TOML: ``prompt`` + ``description``)
* ``~/.codex/prompts/<name>.md`` — OpenAI Codex CLI (**user-scope**, Markdown +
  YAML superset minus ``allowed-tools`` / ``model``)

Codex custom prompts are *upstream-deprecated* — OpenAI recommends migrating
command-like workflows to **skills** (which memtomem already fans out to Codex
via ``.agents/skills/`` in Phase 1). Phase 3.5 still provides fan-out for
parity with the Claude + Gemini pipeline; new workflows should prefer skills.

Placeholder normalization
-------------------------
Claude's ``$ARGUMENTS`` placeholder and Gemini's ``{{args}}`` placeholder have
the same semantics — both substitute the entire user-supplied argument string.
When fanning out Claude-flavoured canonical → Gemini TOML we rewrite
``$ARGUMENTS`` → ``{{args}}``; the reverse import rewrites it back.
Codex natively supports ``$ARGUMENTS``, ``$1``..``$9``, ``$NAME``, and ``$$``
(verbatim to Claude's surface), so the canonical body passes through unchanged
for the Codex target — **no rewrite**. ``!{...}`` shell injection and
``@{...}`` file embed syntax are Gemini-only advanced features and remain out
of scope — users who need them can hand-edit ``.gemini/commands/*.toml``
directly.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from memtomem.context.agents import (
    _FRONT_MATTER_RE,
    _parse_flat_yaml,
    _toml_scalar,
)

logger = logging.getLogger(__name__)

CANONICAL_COMMAND_ROOT = ".memtomem/commands"


# ── Canonical dataclass ──────────────────────────────────────────────


@dataclass
class SlashCommand:
    """In-memory canonical representation of a slash / custom command."""

    name: str
    description: str
    body: str  # prompt template, with $ARGUMENTS as the canonical placeholder
    argument_hint: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    model: str | None = None


class CommandParseError(ValueError):
    """Raised when a canonical command file cannot be parsed."""


def parse_canonical_command(path: Path) -> SlashCommand:
    """Parse a canonical command file into a :class:`SlashCommand`."""
    content = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER_RE.match(content)
    if m is None:
        # Commands without frontmatter are tolerated — treat the whole file
        # as the prompt body with a filename-derived name.
        body = content.lstrip("\n").rstrip() + "\n"
        return SlashCommand(name=path.stem, description="", body=body)

    frontmatter = _parse_flat_yaml(m.group(1))
    body = content[m.end() :].lstrip("\n").rstrip() + "\n"

    name = str(frontmatter.get("name") or path.stem)
    description = str(frontmatter.get("description") or "")
    argument_hint_raw = frontmatter.get("argument-hint") or frontmatter.get("argument_hint")
    allowed_tools_raw = frontmatter.get("allowed-tools") or frontmatter.get("allowed_tools")

    # Claude's argument-hint is a free-form string rendered to the user (e.g.
    # ``[file-path]`` or ``[issue-number] [priority]``). The flat-YAML parser
    # sometimes misreads a single-token bracket form like ``[file-path]`` as an
    # inline list, so we rebuild the original bracket notation when that happens.
    if isinstance(argument_hint_raw, list):
        argument_hint: str | None = "[" + ", ".join(str(t) for t in argument_hint_raw) + "]"
    elif argument_hint_raw:
        argument_hint = str(argument_hint_raw)
    else:
        argument_hint = None

    if isinstance(allowed_tools_raw, list):
        allowed_tools = [str(t) for t in allowed_tools_raw if str(t).strip()]
    elif allowed_tools_raw:
        allowed_tools = [str(allowed_tools_raw).strip()]
    else:
        allowed_tools = []

    return SlashCommand(
        name=name,
        description=description,
        body=body,
        argument_hint=argument_hint,
        allowed_tools=allowed_tools,
        model=(str(frontmatter["model"]) if frontmatter.get("model") else None),
    )


def list_canonical_commands(project_root: Path) -> list[Path]:
    root = project_root / CANONICAL_COMMAND_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.md") if p.is_file())


# ── Placeholder rewriting ────────────────────────────────────────────

_CLAUDE_PLACEHOLDER = "$ARGUMENTS"
_GEMINI_PLACEHOLDER = "{{args}}"


def _claude_to_gemini_body(body: str) -> str:
    return body.replace(_CLAUDE_PLACEHOLDER, _GEMINI_PLACEHOLDER)


def _gemini_to_claude_body(body: str) -> str:
    return body.replace(_GEMINI_PLACEHOLDER, _CLAUDE_PLACEHOLDER)


# ── Renderers ────────────────────────────────────────────────────────


def _yaml_inline_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def _subcommand_to_claude_md(cmd: SlashCommand) -> tuple[str, list[str]]:
    """Render for ``.claude/commands/<name>.md`` — pass-through."""
    lines: list[str] = []
    if cmd.description:
        lines.append(f"description: {cmd.description}")
    if cmd.argument_hint:
        lines.append(f"argument-hint: {cmd.argument_hint}")
    if cmd.allowed_tools:
        lines.append(f"allowed-tools: {_yaml_inline_list(cmd.allowed_tools)}")
    if cmd.model:
        lines.append(f"model: {cmd.model}")

    body = cmd.body if cmd.body.endswith("\n") else cmd.body + "\n"
    if lines:
        frontmatter = "\n".join(lines)
        return f"---\n{frontmatter}\n---\n\n{body}", []
    # No frontmatter at all — still legal for Claude slash commands.
    return body, []


def _subcommand_to_gemini_toml(cmd: SlashCommand) -> tuple[str, list[str]]:
    """Render for ``.gemini/commands/<name>.toml``.

    Drops ``argument-hint``, ``allowed-tools``, ``model`` (no Gemini
    equivalents). Rewrites ``$ARGUMENTS`` → ``{{args}}`` in the body.
    """
    dropped: list[str] = []
    if cmd.argument_hint:
        dropped.append("argument-hint")
    if cmd.allowed_tools:
        dropped.append("allowed-tools")
    if cmd.model:
        dropped.append("model")

    prompt = _claude_to_gemini_body(cmd.body.rstrip())
    parts: list[str] = []
    if cmd.description:
        parts.append(f"description = {_toml_scalar(cmd.description)}")
    parts.append(f"prompt = {_toml_scalar(prompt)}")
    return "\n".join(parts) + "\n", dropped


# ── Generator registry ───────────────────────────────────────────────


class CommandGenerator(Protocol):
    """Protocol for runtime-specific command generators."""

    name: str
    scope: str  # "project" or "user"
    default: bool  # included when scope filter is None (backward compat)

    def target_file(self, project_root: Path, command_name: str) -> Path:
        """Return the file that should hold the rendered command."""
        ...

    def render(self, cmd: SlashCommand) -> tuple[str, list[str]]:
        """Return ``(file_content, dropped_field_names)``."""
        ...


COMMAND_GENERATORS: dict[str, CommandGenerator] = {}


def _register(gen: CommandGenerator) -> CommandGenerator:
    COMMAND_GENERATORS[gen.name] = gen
    return gen


@dataclass
class ClaudeCommandsGenerator:
    name: str = "claude_commands"
    output_root: str = ".claude/commands"
    scope: str = "project"
    default: bool = True

    def target_file(self, project_root: Path, command_name: str) -> Path:
        return project_root / self.output_root / f"{command_name}.md"

    def render(self, cmd: SlashCommand) -> tuple[str, list[str]]:
        return _subcommand_to_claude_md(cmd)


@dataclass
class GeminiCommandsGenerator:
    name: str = "gemini_commands"
    output_root: str = ".gemini/commands"
    scope: str = "project"
    default: bool = True

    def target_file(self, project_root: Path, command_name: str) -> Path:
        return project_root / self.output_root / f"{command_name}.toml"

    def render(self, cmd: SlashCommand) -> tuple[str, list[str]]:
        return _subcommand_to_gemini_toml(cmd)


_register(ClaudeCommandsGenerator())
_register(GeminiCommandsGenerator())


# ── Fan-out: canonical → runtimes ───────────────────────────────────


@dataclass
class CommandSyncResult:
    generated: list[tuple[str, Path]]  # (runtime, target_file)
    dropped: list[tuple[str, str, list[str]]]  # (runtime, command_name, dropped_fields)
    skipped: list[tuple[str, str]]  # (runtime_or_command, reason)


@dataclass
class ExtractResult:
    """Result of a reverse (runtime → canonical) import."""

    imported: list[Path]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (item_name, reason)


class StrictDropError(ValueError):
    """Raised under ``strict=True`` / ``on_drop="error"`` when a conversion would drop fields."""


def _select_generators(
    generators: dict[str, CommandGenerator],
    scope: str | None,
) -> list[str]:
    """Return generator names matching *scope*."""
    if scope is None:
        return [n for n, g in generators.items() if g.default]
    if scope == "all":
        return list(generators.keys())
    return [n for n, g in generators.items() if g.scope == scope]


def generate_all_commands(
    project_root: Path,
    runtimes: list[str] | None = None,
    strict: bool = False,
    on_drop: str = "ignore",
    scope: str | None = None,
) -> CommandSyncResult:
    """Fan out every canonical command to the requested runtimes.

    Args:
        on_drop: Severity when fields are dropped during conversion.
            ``"ignore"`` (default) — silently record in ``result.dropped``.
            ``"warn"``  — log a warning per dropped-field set.
            ``"error"`` — raise :class:`StrictDropError` immediately.
        strict: Legacy alias for ``on_drop="error"``. If *both* are supplied,
            ``on_drop`` takes precedence unless it is still the default.
        scope: ``"project"`` / ``"user"`` / ``"all"`` / ``None``.
    """
    effective_drop = on_drop if on_drop != "ignore" or not strict else "error"

    generated: list[tuple[str, Path]] = []
    dropped: list[tuple[str, str, list[str]]] = []
    skipped: list[tuple[str, str]] = []

    canonicals = list_canonical_commands(project_root)
    if not canonicals:
        return CommandSyncResult(
            generated=[], dropped=[], skipped=[("<all>", "no canonical commands")]
        )

    targets = runtimes if runtimes is not None else _select_generators(COMMAND_GENERATORS, scope)
    for target in targets:
        gen = COMMAND_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime"))
            continue
        for cmd_path in canonicals:
            try:
                cmd = parse_canonical_command(cmd_path)
            except CommandParseError as exc:
                skipped.append((cmd_path.name, f"parse error: {exc}"))
                continue
            content, dropped_fields = gen.render(cmd)
            if dropped_fields:
                if effective_drop == "error":
                    raise StrictDropError(
                        f"strict mode: {target} would drop {dropped_fields} from '{cmd.name}'"
                    )
                if effective_drop == "warn":
                    logger.warning("%s dropped %s from '%s'", target, dropped_fields, cmd.name)
            out_path = gen.target_file(project_root, cmd.name)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            generated.append((target, out_path))
            if dropped_fields:
                dropped.append((target, cmd.name, dropped_fields))

    return CommandSyncResult(generated=generated, dropped=dropped, skipped=skipped)


# ── Reverse: runtime → canonical ────────────────────────────────────


_CANONICAL_DESC_LINE = re.compile(r"^description\s*:\s*(.*)$", re.MULTILINE)


def _gemini_toml_to_canonical(toml_path: Path) -> str:
    """Render a canonical Markdown+YAML file from a Gemini TOML command."""
    data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    prompt = str(data.get("prompt", ""))
    description = str(data.get("description", ""))
    body = _gemini_to_claude_body(prompt).rstrip() + "\n"
    if description:
        return f"---\ndescription: {description}\n---\n\n{body}"
    # No description — frontmatter-less canonical (parser tolerates this).
    return body


def extract_commands_to_canonical(
    project_root: Path,
    overwrite: bool = False,
) -> ExtractResult:
    """Import existing Claude/Gemini command files into ``.memtomem/commands/``.

    Phase 3's conversion is lossless in both directions (only two TOML fields,
    placeholder rewrite is reversible), so Gemini commands can be round-tripped
    back into canonical form — unlike Phase 2 Codex TOML.

    Codex prompts (``~/.codex/prompts/*.md``) are intentionally **not**
    imported even though the format is byte-compatible with Claude — the
    user-scope path spans projects, which would break the "import runtime
    files from *this* project" semantic (matching the Phase 2 Codex sub-agent
    policy). Use ``.memtomem/commands/`` as the single authoring surface and
    let ``generate_all_commands`` populate Codex.

    First occurrence wins: Claude runtime first, then Gemini.  Returns an
    :class:`ExtractResult` with both imported paths and skipped items so the
    caller can warn the user about silent deduplication.
    """
    canonical_root = project_root / CANONICAL_COMMAND_ROOT
    imported: list[Path] = []
    skipped: list[tuple[str, str]] = []
    seen: dict[str, str] = {}  # cmd_name → first runtime label

    # Claude — direct copy (both sides are Markdown+YAML frontmatter).
    claude_dir = project_root / ".claude/commands"
    if claude_dir.is_dir():
        for md_file in sorted(claude_dir.glob("*.md")):
            cmd_name = md_file.stem
            if cmd_name in seen:
                reason = f"already imported from {seen[cmd_name]}"
                skipped.append((cmd_name, reason))
                logger.warning("skip %s from .claude/commands: %s", cmd_name, reason)
                continue
            dst = canonical_root / f"{cmd_name}.md"
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((cmd_name, reason))
                logger.warning("skip %s from .claude/commands: %s", cmd_name, reason)
                seen[cmd_name] = ".claude/commands"
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(md_file.read_bytes())
            imported.append(dst)
            seen[cmd_name] = ".claude/commands"

    # Gemini — TOML → canonical Markdown conversion.
    gemini_dir = project_root / ".gemini/commands"
    if gemini_dir.is_dir():
        for toml_file in sorted(gemini_dir.glob("*.toml")):
            cmd_name = toml_file.stem
            if cmd_name in seen:
                reason = f"already imported from {seen[cmd_name]}"
                skipped.append((cmd_name, reason))
                logger.warning("skip %s from .gemini/commands: %s", cmd_name, reason)
                continue
            dst = canonical_root / f"{cmd_name}.md"
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((cmd_name, reason))
                logger.warning("skip %s from .gemini/commands: %s", cmd_name, reason)
                seen[cmd_name] = ".gemini/commands"
                continue
            try:
                canonical_content = _gemini_toml_to_canonical(toml_file)
            except (tomllib.TOMLDecodeError, OSError):
                skipped.append((cmd_name, "TOML parse error"))
                logger.warning("skip %s from .gemini/commands: TOML parse error", cmd_name)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(canonical_content, encoding="utf-8")
            imported.append(dst)
            seen[cmd_name] = ".gemini/commands"

    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ──────────────────────────────────────


def _runtime_command_names(gen_name: str, project_root: Path) -> set[str]:
    """Return the set of command ``stem`` names present on disk for a runtime."""
    if gen_name == "claude_commands":
        runtime_root = project_root / ".claude/commands"
        suffix = ".md"
    elif gen_name == "gemini_commands":
        runtime_root = project_root / ".gemini/commands"
        suffix = ".toml"
    else:
        return set()
    if not runtime_root.is_dir():
        return set()
    return {p.stem for p in runtime_root.iterdir() if p.is_file() and p.suffix == suffix}


def diff_commands(
    project_root: Path,
    scope: str | None = None,
) -> list[tuple[str, str, str]]:
    """Compare canonical commands against registered runtimes.

    Returns ``(runtime, command_name, status)`` where status is one of
    ``"in sync"``, ``"out of sync"``, ``"missing target"``,
    ``"missing canonical"``, or ``"parse error"``.
    """
    results: list[tuple[str, str, str]] = []
    canonical_root = project_root / CANONICAL_COMMAND_ROOT
    canonical_names = {p.stem for p in list_canonical_commands(project_root)}
    selected = _select_generators(COMMAND_GENERATORS, scope)

    for gen_name, gen in COMMAND_GENERATORS.items():
        if gen_name not in selected:
            continue
        runtime_names = _runtime_command_names(gen_name, project_root)

        for name in sorted(canonical_names | runtime_names):
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
                continue
            if name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
                continue

            src = canonical_root / f"{name}.md"
            try:
                cmd = parse_canonical_command(src)
            except CommandParseError:
                results.append((gen_name, name, "parse error"))
                continue
            expected, _ = gen.render(cmd)
            target = gen.target_file(project_root, name)
            actual = target.read_text(encoding="utf-8") if target.is_file() else ""
            if expected.strip() == actual.strip():
                results.append((gen_name, name, "in sync"))
            else:
                results.append((gen_name, name, "out of sync"))

    return results


__all__ = [
    "CANONICAL_COMMAND_ROOT",
    "COMMAND_GENERATORS",
    "ClaudeCommandsGenerator",
    "CommandGenerator",
    "CommandParseError",
    "CommandSyncResult",
    "ExtractResult",
    "GeminiCommandsGenerator",
    "SlashCommand",
    "StrictDropError",
    "diff_commands",
    "extract_commands_to_canonical",
    "generate_all_commands",
    "list_canonical_commands",
    "parse_canonical_command",
]
