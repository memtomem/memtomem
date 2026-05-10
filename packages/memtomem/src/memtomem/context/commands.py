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

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._atomic import atomic_write_bytes, atomic_write_text
from memtomem.context._gate_a import GateABlocked, apply_gate_a
from memtomem.config import TargetScope
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, Layout, validate_name
from memtomem.context._runtime_targets import runtime_artifact_names, runtime_fanout_root
from memtomem.context.agents import (
    _FRONT_MATTER_RE,
    _parse_flat_yaml,
    _toml_scalar,
)
from memtomem.context.scope_resolver import canonical_artifact_dir

logger = logging.getLogger(__name__)

CANONICAL_COMMAND_ROOT = ".memtomem/commands"
COMMAND_DIR_FILENAME = "command.md"


def canonical_command_name(path: Path, layout: Layout) -> str:
    """Single source of truth for command path → name dispatch.

    Mirror of :func:`memtomem.context.agents.canonical_agent_name`. Avoids
    the brittle ``path.name == "command.md"`` heuristic — callers must
    pass the layout tag from :func:`list_canonical_commands` or
    :func:`extract_commands_to_canonical`.
    """
    return path.parent.name if layout == "dir" else path.stem


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


def parse_canonical_command(path: Path, *, layout: Layout = "flat") -> SlashCommand:
    """Parse a canonical command file into a :class:`SlashCommand`.

    ``layout`` selects the default-name fallback when the frontmatter omits
    ``name``: ``"flat"`` (legacy ``commands/<name>.md``) uses ``path.stem``;
    ``"dir"`` (ADR-0008 ``commands/<name>/command.md``) uses
    ``path.parent.name``.
    """
    default_name = path.parent.name if layout == "dir" else path.stem

    content = path.read_text(encoding="utf-8")
    # Share agents.py's CRLF normalization — the shared ``_FRONT_MATTER_RE``
    # anchors on ``\n`` only, so a CRLF file would otherwise parse as "no
    # frontmatter" and silently fall through to the filename-based default.
    content = content.replace("\r\n", "\n")
    m = _FRONT_MATTER_RE.match(content)
    if m is None:
        # Commands without frontmatter are tolerated — treat the whole file
        # as the prompt body with a filename-derived name.
        body = content.lstrip("\n").rstrip() + "\n"
        try:
            stem = validate_name(default_name, kind="command name")
        except InvalidNameError as exc:
            raise CommandParseError(f"{exc} (source: {path})") from exc
        return SlashCommand(name=stem, description="", body=body)

    frontmatter = _parse_flat_yaml(m.group(1))
    body = content[m.end() :].lstrip("\n").rstrip() + "\n"

    name = str(frontmatter.get("name") or default_name)
    try:
        name = validate_name(name, kind="command name")
    except InvalidNameError as exc:
        raise CommandParseError(f"{exc} (source: {path})") from exc
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


def list_canonical_commands(
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> list[tuple[Path, Layout]]:
    """Enumerate canonical commands in both flat and directory layouts.

    Flat layout (legacy): ``commands/<name>.md``. Directory layout (ADR-0008
    PR-C+): ``commands/<name>/command.md``. When the same name has both
    forms, the directory layout wins and a WARNING is logged so the silent
    flat file is visible.

    ADR-0011 PR-E3: ``scope`` selects the canonical root via
    :func:`canonical_artifact_dir` (default ``project_shared`` preserves
    pre-PR-E3 behavior).
    """
    root = canonical_artifact_dir("commands", scope, project_root)
    if not root.is_dir():
        return []

    flat: dict[str, Path] = {p.stem: p for p in sorted(root.glob("*.md")) if p.is_file()}
    dirs: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            cmd_md = entry / COMMAND_DIR_FILENAME
            if cmd_md.is_file():
                dirs[entry.name] = cmd_md

    for name in sorted(set(flat) & set(dirs)):
        logger.warning(
            "commands/%s: both flat (%s.md) and dir (%s/command.md) layouts "
            "present; using dir. Remove the flat file or run "
            "`mm context migrate` (PR-D).",
            name,
            name,
            name,
        )

    merged_paths = {**flat, **dirs}  # dir overrides flat on collision
    layouts: dict[str, Layout] = {**dict.fromkeys(flat, "flat"), **dict.fromkeys(dirs, "dir")}
    return [(merged_paths[k], layouts[k]) for k in sorted(merged_paths)]


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
    """Protocol for runtime-specific command generators.

    ADR-0011 PR-E: ``target_file`` accepts a ``scope`` keyword (default
    ``project_shared``). Returns ``None`` when no fan-out by design.
    """

    name: str

    def target_file(
        self,
        project_root: Path,
        command_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        """Return the file that should hold the rendered command (or ``None``)."""
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

    def target_file(
        self,
        project_root: Path,
        command_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("commands", "claude", scope, project_root)
        return None if root is None else root / f"{command_name}.md"

    def render(self, cmd: SlashCommand) -> tuple[str, list[str]]:
        return _subcommand_to_claude_md(cmd)


@dataclass
class GeminiCommandsGenerator:
    name: str = "gemini_commands"
    output_root: str = ".gemini/commands"

    def target_file(
        self,
        project_root: Path,
        command_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("commands", "gemini", scope, project_root)
        return None if root is None else root / f"{command_name}.toml"

    def render(self, cmd: SlashCommand) -> tuple[str, list[str]]:
        return _subcommand_to_gemini_toml(cmd)


_register(ClaudeCommandsGenerator())
_register(GeminiCommandsGenerator())


# ── Fan-out: canonical → runtimes ───────────────────────────────────


@dataclass
class CommandSyncResult:
    generated: list[tuple[str, Path]]  # (runtime, target_file)
    dropped: list[tuple[str, str, list[str]]]  # (runtime, command_name, dropped_fields)
    # (runtime_or_command, human_reason, reason_code) — see :mod:`memtomem.context._skip_reasons`.
    skipped: list[tuple[str, str, skip_codes.SkipCode]]


@dataclass
class ExtractResult:
    """Result of a reverse (runtime → canonical) import.

    Each entry in ``imported`` is ``(path, layout)`` so consumers can use
    :func:`canonical_command_name` without re-deriving the layout from
    the path.
    """

    imported: list[tuple[Path, Layout]]
    # (item_name, human_reason, reason_code) — see :mod:`memtomem.context._skip_reasons`.
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = field(default_factory=list)


class StrictDropError(ValueError):
    """Raised under ``strict=True`` / ``on_drop="error"`` when a conversion would drop fields."""


def generate_all_commands(
    project_root: Path,
    runtimes: list[str] | None = None,
    strict: bool = False,
    on_drop: str = "ignore",
    *,
    scope: TargetScope = "project_shared",
) -> CommandSyncResult:
    """Fan out every canonical command to the requested runtimes.

    Args:
        on_drop: Severity when fields are dropped during conversion.
            ``"ignore"`` (default) — silently record in ``result.dropped``.
            ``"warn"``  — log a warning per dropped-field set.
            ``"error"`` — raise :class:`StrictDropError` immediately.
        strict: Legacy alias for ``on_drop="error"``. If *both* are supplied,
            ``on_drop`` takes precedence unless it is still the default.
        scope: ADR-0011 PR-E3 — selects canonical root and runtime
            fan-out destination. Default ``project_shared`` preserves
            pre-PR-E3 behavior.
    """
    effective_drop = on_drop if on_drop != "ignore" or not strict else "error"

    generated: list[tuple[str, Path]] = []
    dropped: list[tuple[str, str, list[str]]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []

    canonicals = list_canonical_commands(project_root, scope=scope)
    if not canonicals:
        return CommandSyncResult(
            generated=[],
            dropped=[],
            skipped=[("<all>", "no canonical commands", skip_codes.NO_CANONICAL_ROOT)],
        )

    targets = runtimes if runtimes is not None else list(COMMAND_GENERATORS.keys())
    for target in targets:
        gen = COMMAND_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime", skip_codes.UNKNOWN_RUNTIME))
            continue
        for cmd_path, layout in canonicals:
            try:
                cmd = parse_canonical_command(cmd_path, layout=layout)
            except CommandParseError as exc:
                skipped.append((cmd_path.name, f"parse error: {exc}", skip_codes.PARSE_ERROR))
                continue
            # ADR-0011 PR-E (#891): resolve the runtime target BEFORE render
            # + dropped-field handling. ``None`` means NO_FANOUT per
            # ``_runtime_targets.RUNTIME_FANOUT_TABLE``; emit a typed skip
            # without invoking ``render`` so a strict caller doesn't raise
            # ``StrictDropError`` for a runtime that has no fan-out by
            # design (the fail-quiet contract).
            out_path = gen.target_file(project_root, cmd.name, scope=scope)
            if out_path is None:
                skipped.append(
                    (
                        cmd.name,
                        f"no fan-out for runtime {target} at this scope",
                        skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME,
                    )
                )
                continue
            content, dropped_fields = gen.render(cmd)
            if dropped_fields:
                if effective_drop == "error":
                    raise StrictDropError(
                        f"strict mode: {target} would drop {dropped_fields} from '{cmd.name}'"
                    )
                if effective_drop == "warn":
                    logger.warning("%s dropped %s from '%s'", target, dropped_fields, cmd.name)
            atomic_write_text(out_path, content)
            # ADR-0008 Invariant 4: per-vendor override replaces the runtime file.
            # Race: see PR-D' for the unified write path that closes the
            # canonical→override window. Same pattern as skills.py:213-220.
            vendor = GENERATOR_VENDOR.get(target)
            if vendor is not None:
                # ADR-0011 PR-E3: thread the resolved sync ``scope`` through
                # to override resolution (same-tier-only lookup; see
                # agents.py for the mirrored rationale).
                override_path = _override.resolve(
                    project_root, "commands", cmd.name, vendor, scope=scope
                )
                if override_path is not None:
                    atomic_write_bytes(out_path, override_path.read_bytes())
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


def _resolve_command_extract_target(canonical_root: Path, cmd_name: str) -> tuple[Path, Layout]:
    """Decide where reverse-sync writes the canonical for ``cmd_name``.

    Truth table mirrors :func:`memtomem.context.agents._resolve_agent_extract_target`:
    dir+flat both → dir wins (silent flat divergence WARNed);
    dir only → dir; flat only → flat (preserve); neither → dir (ADR layout).
    """
    dir_target = canonical_root / cmd_name / COMMAND_DIR_FILENAME
    flat_target = canonical_root / f"{cmd_name}.md"
    has_dir = dir_target.is_file()
    has_flat = flat_target.is_file()
    if has_dir and has_flat:
        logger.warning(
            "commands/%s: reverse-sync updates dir layout (%s/command.md); the "
            "flat file (%s.md) is now silently divergent. Remove it or run "
            "`mm context migrate` (PR-D).",
            cmd_name,
            cmd_name,
            cmd_name,
        )
        return dir_target, "dir"
    if has_dir:
        return dir_target, "dir"
    if has_flat:
        return flat_target, "flat"
    return dir_target, "dir"


def extract_commands_to_canonical(
    project_root: Path,
    overwrite: bool = False,
    only_name: str | None = None,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
) -> ExtractResult:
    """Import existing Claude/Gemini command files into the scoped canonical dir.

    Phase 3's conversion is lossless in both directions (only two TOML fields,
    placeholder rewrite is reversible), so Gemini commands can be round-tripped
    back into canonical form — unlike Phase 2 Codex TOML.

    Codex prompts (``~/.codex/prompts/*.md``) are intentionally **not**
    imported even though the format is byte-compatible with Claude. The
    Codex CLI's prompt directory is user-scope (cross-project) and our
    runtime fan-out table reserves a ``("commands", "codex", "user")``
    slot for future symmetry, but the import side keeps the existing
    "use ``.memtomem/commands/`` as the single authoring surface and let
    ``generate_all_commands`` populate Codex" semantic.

    ADR-0011 PR-E2: ``scope`` selects both the canonical destination and
    the source runtime root (per-scope import). ``project_local`` has no
    runtime fan-out by design and short-circuits to an empty result.

    Each branch (Claude bytes-passthrough, Gemini TOML→Markdown) applies
    Gate A separately. The Gemini branch scans the **converted Markdown**
    — that is what gets persisted, and the converted body inherits any
    secret embedded in the source ``prompt`` field.

    First occurrence wins: Claude runtime first, then Gemini.

    When ``only_name`` is set, every runtime file with a different stem is
    silently skipped before any validation/dedupe work.

    Layout policy: new commands (no existing canonical) land in directory
    layout per ADR-0008. Existing flat-layout entries are preserved by
    PR-C — migration to directory layout is a separate command (PR-D).
    """
    if scope == "project_local":
        return ExtractResult(
            imported=[],
            skipped=[
                (
                    "<all>",
                    "project_local has no runtime fan-out (ADR-0011 §3)",
                    skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME,
                )
            ],
        )

    canonical_root = canonical_artifact_dir("commands", scope, project_root)
    imported: list[tuple[Path, Layout]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []
    seen: dict[str, str] = {}  # cmd_name → first runtime label

    # ── Claude branch — byte-level passthrough (Markdown + YAML) ──
    try:
        claude_dir = runtime_fanout_root("commands", "claude", scope, project_root)
    except KeyError:
        claude_dir = None
    if claude_dir is not None and claude_dir.is_dir():
        claude_label = f"claude ({claude_dir})"
        for md_file in sorted(claude_dir.glob("*.md")):
            cmd_name = md_file.stem
            if only_name is not None and cmd_name != only_name:
                continue
            try:
                validate_name(cmd_name, kind="command name")
            except InvalidNameError as exc:
                skipped.append((cmd_name, f"invalid name: {exc}", skip_codes.INVALID_NAME))
                logger.warning("skip %r from %s: invalid name", cmd_name, claude_label)
                continue
            if cmd_name in seen:
                reason = f"already imported from {seen[cmd_name]}"
                skipped.append((cmd_name, reason, skip_codes.ALREADY_IMPORTED))
                logger.warning("skip %s from %s: %s", cmd_name, claude_label, reason)
                continue
            dst, layout = _resolve_command_extract_target(canonical_root, cmd_name)
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((cmd_name, reason, skip_codes.CANONICAL_EXISTS))
                logger.warning("skip %s from %s: %s", cmd_name, claude_label, reason)
                seen[cmd_name] = claude_label
                continue
            try:
                content_bytes = md_file.read_bytes()
            except OSError as exc:
                skipped.append((cmd_name, f"unreadable: {exc}", skip_codes.PARSE_ERROR))
                continue
            content_text = content_bytes.decode("utf-8", errors="replace")
            outcome = apply_gate_a(
                content_text=content_text,
                src=md_file,
                scope=scope,
                force_unsafe_import=force_unsafe_import,
                # Mirror agents.py audit_context shape — SOC pipelines grep
                # both ``source=`` and ``target=`` for incident triage;
                # commands' earlier omission was a sibling-parity gap
                # (PR #889 review D1).
                audit_context={
                    "source": str(md_file),
                    "target": str(dst),
                    "kind": "commands",
                    "runtime": "claude",
                    "command_name": cmd_name,
                },
                message_kind="command",
                imported_so_far=len(imported),
            )
            if isinstance(outcome, GateABlocked):
                skipped.append(
                    (
                        cmd_name,
                        f"blocked: {outcome.hits_count} privacy pattern hit(s){outcome.hint}",
                        outcome.code,
                    )
                )
                seen[cmd_name] = claude_label
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(dst, content_bytes)
            imported.append((dst, layout))
            seen[cmd_name] = claude_label

    # ── Gemini branch — TOML → canonical Markdown conversion ──
    try:
        gemini_dir = runtime_fanout_root("commands", "gemini", scope, project_root)
    except KeyError:
        gemini_dir = None
    if gemini_dir is not None and gemini_dir.is_dir():
        gemini_label = f"gemini ({gemini_dir})"
        for toml_file in sorted(gemini_dir.glob("*.toml")):
            cmd_name = toml_file.stem
            if only_name is not None and cmd_name != only_name:
                continue
            try:
                validate_name(cmd_name, kind="command name")
            except InvalidNameError as exc:
                skipped.append((cmd_name, f"invalid name: {exc}", skip_codes.INVALID_NAME))
                logger.warning("skip %r from %s: invalid name", cmd_name, gemini_label)
                continue
            if cmd_name in seen:
                reason = f"already imported from {seen[cmd_name]}"
                skipped.append((cmd_name, reason, skip_codes.ALREADY_IMPORTED))
                logger.warning("skip %s from %s: %s", cmd_name, gemini_label, reason)
                continue
            dst, layout = _resolve_command_extract_target(canonical_root, cmd_name)
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((cmd_name, reason, skip_codes.CANONICAL_EXISTS))
                logger.warning("skip %s from %s: %s", cmd_name, gemini_label, reason)
                seen[cmd_name] = gemini_label
                continue
            try:
                canonical_content = _gemini_toml_to_canonical(toml_file)
            except (tomllib.TOMLDecodeError, OSError):
                skipped.append((cmd_name, "TOML parse error", skip_codes.TOML_PARSE_ERROR))
                logger.warning("skip %s from %s: TOML parse error", cmd_name, gemini_label)
                continue
            # Scan the CONVERTED Markdown — that's what gets persisted.
            # A secret in the source `prompt = "..."` field flows into
            # the body, so this catches it without re-scanning the raw TOML.
            outcome = apply_gate_a(
                content_text=canonical_content,
                src=toml_file,
                scope=scope,
                force_unsafe_import=force_unsafe_import,
                audit_context={
                    "source": str(toml_file),
                    "target": str(dst),
                    "kind": "commands",
                    "runtime": "gemini",
                    "command_name": cmd_name,
                },
                message_kind="command",
                imported_so_far=len(imported),
            )
            if isinstance(outcome, GateABlocked):
                skipped.append(
                    (
                        cmd_name,
                        f"blocked: {outcome.hits_count} privacy pattern hit(s){outcome.hint}",
                        outcome.code,
                    )
                )
                seen[cmd_name] = gemini_label
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(dst, canonical_content)
            imported.append((dst, layout))
            seen[cmd_name] = gemini_label

    # Codex prompts intentionally not imported (see docstring rationale).
    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ──────────────────────────────────────


# Per-runtime file suffix for commands fan-out. Used by ``diff_commands``
# to delegate to ``runtime_artifact_names``.
_COMMAND_RUNTIME_SUFFIX: dict[str, str] = {
    "claude": ".md",
    "gemini": ".toml",
    # Codex: project-tier has no fan-out (RUNTIME_FANOUT_TABLE returns None);
    # user-tier prompts use ``.md`` per Codex docs.
    "codex": ".md",
}


def diff_commands(
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> list[tuple[str, str, str]]:
    """Compare canonical commands against every registered runtime.

    Returns ``(runtime, command_name, status)`` where status is one of
    ``"in sync"``, ``"out of sync"``, ``"missing target"``,
    ``"missing canonical"``, or ``"parse error"``.

    ADR-0011 PR-E3: ``scope`` selects both the canonical source and the
    runtime fan-out roots. Default ``project_shared`` preserves
    pre-PR-E3 behavior.
    """
    results: list[tuple[str, str, str]] = []
    canonical_index = {
        path.parent.name if layout == "dir" else path.stem: (path, layout)
        for path, layout in list_canonical_commands(project_root, scope=scope)
    }
    canonical_names = set(canonical_index)

    for gen_name, gen in COMMAND_GENERATORS.items():
        # ADR-0011 PR-E3 cleanup item #1: query the table directly via
        # ``runtime_fanout_root``. Earlier code probed with a fixed command
        # name (``__probe_891__``) which leaked the table-shape assumption
        # into the call shape — call-shape fragility, not name-independence.
        runtime = gen_name.split("_", 1)[0]
        if runtime_fanout_root("commands", runtime, scope, project_root) is None:
            continue
        suffix = _COMMAND_RUNTIME_SUFFIX.get(runtime, ".md")
        runtime_names = runtime_artifact_names(
            "commands", runtime, project_root, scope, file_suffix=suffix
        )

        for name in sorted(canonical_names | runtime_names):
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
                continue
            if name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
                continue

            src, layout = canonical_index[name]
            try:
                cmd = parse_canonical_command(src, layout=layout)
            except CommandParseError:
                results.append((gen_name, name, "parse error"))
                continue
            expected, _ = gen.render(cmd)
            # Cleanup item #2: the upstream ``runtime_fanout_root`` guard
            # above guarantees this runtime+scope has a fan-out root, so
            # ``gen.target_file`` cannot return ``None`` for any name.
            # Earlier defensive ``if target is None: continue`` removed.
            target = gen.target_file(project_root, name, scope=scope)
            assert target is not None  # narrowed by upstream NO_FANOUT guard
            actual = target.read_text(encoding="utf-8") if target.is_file() else ""
            if expected.strip() == actual.strip():
                results.append((gen_name, name, "in sync"))
            else:
                results.append((gen_name, name, "out of sync"))

    return results


__all__ = [
    "CANONICAL_COMMAND_ROOT",
    "COMMAND_DIR_FILENAME",
    "COMMAND_GENERATORS",
    "ClaudeCommandsGenerator",
    "CommandGenerator",
    "CommandParseError",
    "CommandSyncResult",
    "ExtractResult",
    "GeminiCommandsGenerator",
    "SlashCommand",
    "StrictDropError",
    "canonical_command_name",
    "diff_commands",
    "extract_commands_to_canonical",
    "generate_all_commands",
    "list_canonical_commands",
    "parse_canonical_command",
]
