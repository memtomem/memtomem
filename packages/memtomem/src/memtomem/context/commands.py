"""Canonical ⇄ runtime slash/custom command fan-out.

Phase 3 of the "memtomem as canonical context gateway" plan. A slash command
lives at ``.memtomem/commands/<name>.md`` with YAML frontmatter (Claude
Code-compatible superset) and a Markdown body that acts as the prompt
template. From that single canonical source we fan out to **two** runtimes:

* ``.claude/commands/<name>.md`` — Claude Code (Markdown + YAML, pass-through)
* ``.gemini/commands/<name>.toml`` — Gemini CLI (TOML: ``prompt`` + ``description``)

Codex commands are **not** fanned out: :data:`COMMAND_GENERATORS` registers
only Claude + Gemini, and Codex custom prompts (``~/.codex/prompts/*.md``) are
*upstream-deprecated*. OpenAI recommends migrating command-like workflows to
**skills**, which memtomem already fans out to Codex via ``.agents/skills/``
(Phase 1). The runtime fan-out table reserves a ``("commands", "codex", "user")``
slot so a future ``CodexCommandsGenerator`` can land without churn, but none is
registered today.

Placeholder normalization
-------------------------
Claude's ``$ARGUMENTS`` placeholder and Gemini's ``{{args}}`` placeholder have
the same semantics — both substitute the entire user-supplied argument string.
When fanning out Claude-flavoured canonical → Gemini TOML we rewrite
``$ARGUMENTS`` → ``{{args}}``; the reverse import rewrites it back.
Codex custom prompts are not generated today, so Codex placeholder syntax is
documented here only as migration context. ``!{...}`` shell injection and
``@{...}`` file embed syntax are Gemini-only advanced features and remain out
of scope — users who need them can hand-edit ``.gemini/commands/*.toml``
directly.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._atomic import atomic_write_text
from memtomem.context._atomic_reverse import (
    canonical_artifact_name,
    diff_atomic_artifact,
    import_passthrough_runtime,
    list_canonical_artifacts,
    resolve_artifact_extract_target,
    resolve_artifact_under_root,
)
from memtomem.context._gate_a import GateABlocked, apply_gate_a
from memtomem.config import TargetScope
from memtomem.context._names import InvalidNameError, Layout, validate_name
from memtomem.context._runtime_targets import runtime_fanout_root
from memtomem.context._sync_atomic import (
    ON_DROP_LEVELS,
    AtomicSyncAdapter,
    AtomicSyncResult,
    StrictDropError as _EngineStrictDropError,
    sync_atomic_artifact,
)
from memtomem.context.agents import (
    _FRONT_MATTER_RE,
    _parse_flat_yaml,
    _toml_scalar,
)
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.versioning import make_label_resolver

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
    return canonical_artifact_name(path, layout)


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


def _parse_canonical_command_text(
    content: str,
    *,
    source: Path,
    layout: Layout = "flat",
) -> SlashCommand:
    """Parse already-loaded canonical command text. Used by both the path-based
    :func:`parse_canonical_command` (back-compat) and the sync flow that
    captures bytes once to close the scan→write TOCTOU window
    (PR-E3 Codex review fold).
    """
    default_name = source.parent.name if layout == "dir" else source.stem

    # Share agents.py's BOM + CRLF normalization — the shared
    # ``_FRONT_MATTER_RE`` anchors at position 0 and on ``\n`` only, so a
    # BOM-prefixed or CRLF file would otherwise parse as "no frontmatter" and
    # silently fall through to the filename-based default, dropping
    # description/model/allowed-tools while diff still reported "in sync"
    # (#1229). Exactly one leading BOM is stripped (``utf-8-sig`` semantics).
    content = content.removeprefix("﻿").replace("\r\n", "\n")
    m = _FRONT_MATTER_RE.match(content)
    if m is None:
        # Commands without frontmatter are tolerated — treat the whole file
        # as the prompt body with a filename-derived name.
        body = content.lstrip("\n").rstrip() + "\n"
        try:
            stem = validate_name(default_name, kind="command name")
        except InvalidNameError as exc:
            raise CommandParseError(f"{exc} (source: {source})") from exc
        return SlashCommand(name=stem, description="", body=body)

    frontmatter = _parse_flat_yaml(m.group(1))
    body = content[m.end() :].lstrip("\n").rstrip() + "\n"

    name = str(frontmatter.get("name") or default_name)
    try:
        name = validate_name(name, kind="command name")
    except InvalidNameError as exc:
        raise CommandParseError(f"{exc} (source: {source})") from exc
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


def parse_canonical_command(path: Path, *, layout: Layout = "flat") -> SlashCommand:
    """Parse a canonical command file into a :class:`SlashCommand`.

    ``layout`` selects the default-name fallback when the frontmatter omits
    ``name``: ``"flat"`` (legacy ``commands/<name>.md``) uses ``path.stem``;
    ``"dir"`` (ADR-0008 ``commands/<name>/command.md``) uses
    ``path.parent.name``.
    """
    content = path.read_text(encoding="utf-8")
    return _parse_canonical_command_text(content, source=path, layout=layout)


def resolve_canonical_command(
    project_root: Path, name: str, *, scope: TargetScope = "project_shared"
) -> tuple[Path, Layout] | None:
    """Return the canonical ``(path, layout)`` for ``name`` if it exists.

    Directory layout wins when both the legacy flat file and ADR-0008
    directory layout are present. Name validation is intentionally left to
    callers so existing 400 behavior remains at the route/CLI boundary.

    ``scope`` selects the canonical residency tier (ADR-0016). Default
    ``project_shared`` preserves pre-#940 behavior.
    """
    return resolve_artifact_under_root(
        canonical_artifact_dir("commands", scope, project_root),
        name,
        artifact_label="commands",
        dir_filename=COMMAND_DIR_FILENAME,
        logger=logger,
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
    return list_canonical_artifacts(
        project_root,
        artifact_label="commands",
        dir_filename=COMMAND_DIR_FILENAME,
        logger=logger,
        scope=scope,
    )


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


# Sister subclass (issue #900) — see the matching comment in
# :mod:`memtomem.context.agents`. Distinct class so identity stays
# module-specific (``AgentSyncResult is not CommandSyncResult``).
@dataclass
class CommandSyncResult(AtomicSyncResult):
    """Module-specific result subclass — see :class:`AtomicSyncResult`."""


class StrictDropError(_EngineStrictDropError):
    """Module-specific strict-drop error — see :class:`_EngineStrictDropError`."""


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


# Issue #900 extraction — see the matching adapter in
# :mod:`memtomem.context.agents` for the design rationale.
# Per-runtime file suffix for commands fan-out. Used by ``diff_commands``
# (via the adapter's ``runtime_suffixes``) to delegate to
# ``runtime_artifact_names``.
_COMMAND_RUNTIME_SUFFIX: dict[str, str] = {
    "claude": ".md",
    "gemini": ".toml",
    # Codex: project-tier has no fan-out (RUNTIME_FANOUT_TABLE returns None);
    # user-tier prompts use ``.md`` per Codex docs.
    "codex": ".md",
}


_COMMAND_ADAPTER: AtomicSyncAdapter[SlashCommand] = AtomicSyncAdapter(
    kind="command",
    artifact_label="commands",
    list_canonical=list_canonical_commands,
    parse_canonical_text=_parse_canonical_command_text,
    parse_error_type=CommandParseError,
    name_of=lambda c: c.name,
    generators=COMMAND_GENERATORS,
    result_type=CommandSyncResult,
    strict_drop_error_type=StrictDropError,
    logger=logger,
    runtime_suffixes=_COMMAND_RUNTIME_SUFFIX,
)


def generate_all_commands(
    project_root: Path,
    runtimes: list[str] | None = None,
    strict: bool = False,
    on_drop: str = "ignore",
    *,
    scope: TargetScope = "project_shared",
    label: str | None = None,
    surface: str = "cli_context_sync",
    force_unsafe: bool = False,
) -> CommandSyncResult:
    """Fan out every canonical command to the requested runtimes.

    Thin wrapper that binds the command-specific adapter and delegates to
    :func:`memtomem.context._sync_atomic.sync_atomic_artifact` — see that
    function for the full Phase 1 / Phase 2 contract.

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
        label: ADR-0022 — when set to a non-``latest`` value, fan out the
            version pointed at by this label (or a bare ``vN`` tag) instead of
            the working ``command.md``. ``None`` / ``"latest"`` preserve
            today's behavior byte-for-byte. Per-artifact resolution failures
            isolate as skips.
        surface: Gate A audit identifier, forwarded verbatim to the
            engine's privacy scans. The CLI relies on the default
            ``"cli_context_sync"``; the Web sync route passes
            ``"web_context_commands_sync"`` and the MCP tools pass
            ``"mcp_context_generate"`` / ``"mcp_context_sync"`` (#1246).
        force_unsafe: Reviewed Gate A bypass (ADR-0011 §5) forwarded to
            the engine. ``True`` lets a reviewed false positive fan out
            to ``user`` / ``project_local``; ``project_shared`` stays
            hard-refused regardless. Default ``False``.
    """
    adapter = _COMMAND_ADAPTER
    if label is not None and label != "latest":
        adapter = replace(_COMMAND_ADAPTER, resolve_canonical_bytes=make_label_resolver(label))
    # See note on the matching ``cast`` in agents.py.
    return cast(
        "CommandSyncResult",
        sync_atomic_artifact(
            adapter,
            project_root,
            runtimes,
            strict=strict,
            on_drop=on_drop,
            scope=scope,
            surface=surface,
            force_unsafe=force_unsafe,
        ),
    )


# ── Reverse: runtime → canonical ────────────────────────────────────


_CANONICAL_DESC_LINE = re.compile(r"^description\s*:\s*(.*)$", re.MULTILINE)


def _gemini_toml_to_canonical(toml_path: Path) -> str:
    """Render a canonical Markdown+YAML file from a Gemini TOML command."""
    # ``utf-8-sig`` so a BOM-prefixed Windows-authored .toml imports instead
    # of skipping with a TOML parse error (tomllib rejects a raw BOM); it is
    # a byte-for-byte no-op for BOM-less files.
    data = tomllib.loads(toml_path.read_text(encoding="utf-8-sig"))
    prompt = str(data.get("prompt", ""))
    description = str(data.get("description", ""))
    body = _gemini_to_claude_body(prompt).rstrip() + "\n"
    # TOML strings are multi-line-capable but the canonical frontmatter value
    # is one line — collapse line breaks to spaces BEFORE interpolating.
    # Interpolating raw let a description like "helper\nmodel: gpt-4" inject
    # arbitrary frontmatter keys into the canonical (which then fan out to
    # every runtime), and a "---" line terminate the frontmatter early
    # (#1229). The flat-YAML parser reads quoted scalars without decoding
    # escapes, so quoting instead of collapsing would corrupt the value.
    description = " ".join(description.split())
    if description:
        return f"---\ndescription: {description}\n---\n\n{body}"
    # No description — frontmatter-less canonical (parser tolerates this).
    return body


def extract_commands_to_canonical(
    project_root: Path,
    overwrite: bool = False,
    only_name: str | None = None,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
    dry_run: bool = False,
    surface: str = "cli_context_init",
) -> ExtractResult:
    """Import existing Claude/Gemini command files into the scoped canonical dir.

    ``dry_run`` (rank-10 import preview) runs the full scan + validation +
    Gate A walk + cross-runtime dedup + canonical-exists check on both the
    Claude and Gemini branches, then **skips only the write**: ``imported``
    lists the destinations that *would* be written and ``skipped`` carries the
    same reasons a real run would, with nothing touching disk.

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

    No ``source_scope`` parameter by design (#1520 item 5): the decoupled
    project-runtime → user-library import (the web ``/import-to-user``
    route) is skills-only — see ``extract_skills_to_canonical``'s
    ``source_scope`` docstring. Here the runtime source root always
    follows ``scope``.

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

    def _claude_audit_context(src: Path, dst: Path, cmd_name: str) -> dict[str, object]:
        # Mirror agents.py audit_context shape — SOC pipelines grep both
        # ``source=`` and ``target=`` for incident triage; commands' earlier
        # omission was a sibling-parity gap (PR #889 review D1).
        return {
            "source": str(src),
            "target": str(dst),
            "kind": "commands",
            "runtime": "claude",
            "command_name": cmd_name,
        }

    # ── Claude branch — byte-level passthrough (Markdown + YAML) ──
    import_passthrough_runtime(
        "claude",
        artifact_label="commands",
        dir_filename=COMMAND_DIR_FILENAME,
        name_kind="command name",
        message_kind="command",
        audit_context=_claude_audit_context,
        canonical_root=canonical_root,
        project_root=project_root,
        overwrite=overwrite,
        scope=scope,
        force_unsafe_import=force_unsafe_import,
        dry_run=dry_run,
        surface=surface,
        only_name=only_name,
        imported=imported,
        skipped=skipped,
        seen=seen,
        logger=logger,
    )

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
            dst, layout = resolve_artifact_extract_target(
                canonical_root,
                cmd_name,
                artifact_label="commands",
                dir_filename=COMMAND_DIR_FILENAME,
                logger=logger,
            )
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
                surface=surface,
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
            # ``dry_run`` records the would-import target but skips the write
            # so the preview never mutates disk (rank-10).
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(dst, canonical_content)
            imported.append((dst, layout))
            seen[cmd_name] = gemini_label

    # Codex prompts intentionally not imported (see docstring rationale).
    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ──────────────────────────────────────


def diff_commands(
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> list[tuple[str, str, str]]:
    """Compare canonical commands against every registered runtime.

    Returns ``(runtime, command_name, status)`` where status is one of
    ``"in sync"``, ``"out of sync"``, ``"missing target"``,
    ``"missing canonical"``, ``"parse error"``, or ``"invalid name"``.

    ADR-0011 PR-E3: ``scope`` selects both the canonical source and the
    runtime fan-out roots. Default ``project_shared`` preserves
    pre-PR-E3 behavior.
    """
    return diff_atomic_artifact(_COMMAND_ADAPTER, project_root, scope=scope)


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
    "ON_DROP_LEVELS",
    "SlashCommand",
    "StrictDropError",
    "canonical_command_name",
    "diff_commands",
    "extract_commands_to_canonical",
    "generate_all_commands",
    "list_canonical_commands",
    "parse_canonical_command",
    "resolve_canonical_command",
]
