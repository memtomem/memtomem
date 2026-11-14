"""MCP context tools: ``mem_context_detect`` and ``mem_context_diff`` only.

Both read; neither writes. Every context operation that writes (init, generate,
sync, pull, memory and artifact migration, transfer, versions and labels) has
no MCP tool; it runs from the CLI (``mm context ...``), and some also from the
web UI. Those writes reach the agents' own configuration (hooks, skills,
instruction files), and over MCP the caller is an agent: a consent flag it
passes as a tool argument (``allow_host_writes``, ``confirm_project_shared``,
``apply``) is not consent from the person. The CLI puts the same operation
behind the client's own command approval (ADR-0044).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from memtomem._runtime_paths import scrub_text
from memtomem.config import TargetScope
from memtomem.context.error_redact import (
    redact_engine_reason,
    scrub_residual_absolute_paths,
)
from memtomem.context.scope_resolver import find_project_root
from memtomem.server import mcp
from memtomem.server.context import CtxType
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

# Known --include values (mirrors cli.context_cmd._KNOWN_INCLUDES).
_KNOWN_INCLUDES: frozenset[str] = frozenset({"skills", "agents", "commands", "settings"})
_KNOWN_ARTIFACT_SCOPES: frozenset[str] = frozenset({"user", "project_shared", "project_local"})


def _find_project_root() -> Path:
    """Walk up from cwd to find project root.

    Thin wrapper over the shared ``scope_resolver.find_project_root`` so the
    MCP context tools, the CLI, and the web app share one definition.
    """
    return find_project_root()


def _redact_reason_unescaped(reason: str | None, *roots: Path) -> str:
    """Path-redact a raw engine reason, WITHOUT escaping control characters.

    The redaction half of :func:`_redact_reason`, split out because the two
    halves do not commute. :func:`scrub_text` spells a control character as
    ``\\xNN``, and the absolute-path regexes treat a backslash as a path
    separator, so an escaped ``\\x1b[2J\\x1b[H`` reads as a two-segment
    absolute path: scrubbing first makes the later path scrub swallow the
    escape *and* the remediation text that follows it (reproduced on a Pull
    reason that composed a second scrub on top, before ADR-0044 removed it).

    Every caller that renders text finishes with :func:`scrub_text`; this
    function exists so a caller that composes another path scrub can do so
    before that final escape. Do not print its result directly.
    """
    return scrub_residual_absolute_paths(redact_engine_reason(reason, *roots) or "")


def _redact_reason(reason: str | None, *roots: Path) -> str:
    """Strip absolute host paths from a raw engine diff/skip ``reason``.

    Thin wrapper over ``context.error_redact.redact_engine_reason`` (the neutral
    twin of the web ``sanitize_diff_reason``) that coalesces the ``None``
    (empty-reason) return to ``""`` so a skip line renders ``skipped x: ``
    rather than ``skipped x: None``; diff-row callers keep their ``if reason``
    suffix guard on the returned string. The MCP tool result flows to the
    calling agent's transcript / model provider, so it gets the same wire-
    boundary sanitization the loopback dashboard applies.

    Composes :func:`scrub_residual_absolute_paths` — the absolute-ONLY scrub,
    NOT the web's :func:`scrub_absolute_paths`. ``redact_engine_reason`` strips
    only the roots it is handed plus the import-frozen ``$HOME``, so a runtime
    dir symlinked onto a shared volume kept its full path on this wire (PR
    review, reproduced). The web twin's scrub cannot be reused: it also eats
    the RELATIVE remainder, which here is the remediation
    (``blocked foo: privacy hits in .claude/agents/foo.md``) and, in the
    success-path formatters, the intended ``~``-collapsed output. Two postures,
    two scrubs — see :mod:`memtomem.context.error_redact`.

    Finishes with :func:`scrub_text`, the display-safe escape. Engine reasons
    and warnings quote settings text — hook events, matchers, commands — which
    is untrusted and can carry terminal control sequences, and this result is
    rendered in the calling agent's transcript. It is the outermost step
    because escaping is not reversible and the path redactions above must see
    the original bytes (#2478); callers that compose a further path scrub take
    :func:`_redact_reason_unescaped` and apply the escape themselves.
    """
    return scrub_text(_redact_reason_unescaped(reason, *roots))


def _resolve_mcp_scope(override: str | None = None) -> str:
    """Return the resolved ``hooks.target_scope`` for an MCP tool call.

    A per-call override wins. Otherwise this builds a fresh config and
    applies user-level overrides with ``migrate=False``. Scope
    resolution is read-only, and the same MCP tool dispatcher is shared
    by read-only entry points (mem_context_detect, mem_context_diff)
    where a disk-write side effect would be wrong.
    """
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    if override is not None:
        if override not in _KNOWN_ARTIFACT_SCOPES:
            raise ValueError(
                f"Unknown scope value '{override}'. Supported: {sorted(_KNOWN_ARTIFACT_SCOPES)}"
            )
        return override
    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg, migrate=False)
    return cfg.hooks.target_scope


def _resolve_artifact_mcp_scope(scope: str | None) -> TargetScope:
    """Resolve the ADR-0011 artifact scope axis for MCP context tools."""
    if scope is None or not scope.strip():
        return "project_shared"
    scope = scope.strip()
    if scope not in _KNOWN_ARTIFACT_SCOPES:
        raise ValueError(
            f"Unknown scope value '{scope}'. Supported: {sorted(_KNOWN_ARTIFACT_SCOPES)}"
        )
    # Narrowed against _KNOWN_ARTIFACT_SCOPES above; cast expresses that to mypy.
    return cast(TargetScope, scope)


def _parse_include(include: str) -> set[str]:
    """Parse a comma-separated ``include`` argument coming from an MCP caller."""
    values: set[str] = set()
    for token in include.split(","):
        token = token.strip()
        if not token:
            continue
        if token not in _KNOWN_INCLUDES:
            raise ValueError(
                f"Unknown include value '{token}'. Supported: {sorted(_KNOWN_INCLUDES)}"
            )
        values.add(token)
    return values


def _settings_dup_tier_warnings(root: Path, active_scope: str) -> list[str]:
    """Cross-tier duplicate + malformed-matcher warning lines for settings.

    Mirrors the CLI's ``_print_duplicate_tier_warnings`` (ADR-0010 §4), which
    fires inside the real generate / diff / sync workflow rather than behind a
    separate ``settings-doctor`` command. The MCP settings branches dropped
    these warnings entirely, so an MCP caller never learned that a
    memtomem-managed hook was duplicated in a non-active tier. Surface them
    here so the MCP and CLI settings surfaces agree. Non-blocking — duplicates
    are informational.

    ``format_warning`` embeds the tier path — absolute ``$HOME`` for a
    user-tier duplicate — so redact the PATH before formatting (#1550, the
    dup-tier leg the #1539 sweep missed). Redacting the formatted line instead
    would hit the 200-char ``redact_message`` cap and truncate the migrate
    hint; the path-only substitution keeps the CLI-parity wording whole. The
    malformed-matcher leg (#1987) embeds a path the same way and gets the same
    per-finding substitution.

    These substitutions use :func:`_redact_reason_unescaped`, not
    :func:`_redact_reason`: every formatter below already finishes its own
    fields with ``scrub_text``, and the secret-shape checks here
    (``redact_secret_value``, ``redact_unportable_command_fields``) have to
    read the original text rather than an escaped copy of it (#2477).
    """
    from dataclasses import replace

    from memtomem.context.error_redact import SECRET_REDACTED_MARKER, redact_secret_value
    from memtomem.context.settings_doctor import (
        detect_duplicate_tiers,
        find_malformed_matchers,
        find_unportable_hook_commands,
        find_unscanned_settings_files,
        format_malformed_warning,
        format_unportable_command_warning,
        format_unscanned_settings_warning,
        format_warning,
        redact_unportable_command_fields,
    )

    lines: list[str] = []
    for dup in detect_duplicate_tiers(root, active_scope=active_scope):
        redacted_dup = replace(dup, path=Path(_redact_reason_unescaped(str(dup.path), root)))
        lines.append(f"  warning: {format_warning(redacted_dup, active_scope=active_scope)}")
    for finding in find_malformed_matchers(root):
        redacted_finding = replace(
            finding,
            path=Path(_redact_reason_unescaped(str(finding.path), root)),
            event=_redact_reason_unescaped(finding.event, root),
        )
        lines.append(f"  warning: {format_malformed_warning(redacted_finding)}")
    for unportable_finding in find_unportable_hook_commands(root):
        command_redacted = redact_secret_value(
            _redact_reason_unescaped(unportable_finding.command, root)
        )
        safe_cmd, safe_lit = redact_unportable_command_fields(
            unportable_finding.command, unportable_finding.unportable_literal
        )
        if safe_cmd == SECRET_REDACTED_MARKER:
            command_redacted = SECRET_REDACTED_MARKER
            literal_redacted = SECRET_REDACTED_MARKER
        else:
            literal_redacted = _redact_reason_unescaped(safe_lit, root)
        redacted_unportable = replace(
            unportable_finding,
            path=Path(_redact_reason_unescaped(str(unportable_finding.path), root)),
            event=_redact_reason_unescaped(unportable_finding.event, root),
            unportable_literal=literal_redacted,
            command=command_redacted,
        )
        lines.append(f"  warning: {format_unportable_command_warning(redacted_unportable)}")
    for unscanned in find_unscanned_settings_files(root):
        redacted_unscanned = replace(
            unscanned, path=Path(_redact_reason_unescaped(str(unscanned.path), root))
        )
        lines.append(f"  warning: {format_unscanned_settings_warning(redacted_unscanned)}")
    return lines


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_detect(
    include: str = "",
    include_runtimes: bool = False,
    ctx: CtxType = None,
) -> str:
    """Detect agent configuration files in the current project.

    Scans for CLAUDE.md, .cursorrules, GEMINI.md, AGENTS.md,
    and .github/copilot-instructions.md. Pass
    ``include="skills,agents,commands"`` to also list runtime skill
    directories, sub-agent files, and slash-command files.

    Args:
        include: Comma list of extra kinds to report — ``skills``,
            ``agents``, ``commands``, ``settings``. ``settings`` lists the
            resolved-scope settings file of each available runtime (Claude,
            Codex, Gemini, Kimi), including ones not yet created. Empty
            (default) reports agent files only.
        include_runtimes: Also report read-only provider-client registration
            status (Claude / Antigravity / Codex / Kimi; ADR-0021 §B). A
            separate boolean on purpose — it is NOT part of the ``include``
            set, so the include contract shared with ``mem_context_diff``
            and ``mm context sync`` / ``generate`` / ``init`` is never
            widened (``mm context sync --include=runtimes`` still rejects).
    """
    from memtomem.context.detector import (
        detect_agent_dirs,
        detect_agent_files,
        detect_command_dirs,
        detect_skill_dirs,
    )

    inc = _parse_include(include)
    root = _find_project_root()
    files = detect_agent_files(root)

    lines: list[str] = []
    if files:
        lines.append(f"Found {len(files)} agent file(s):\n")
        for f in files:
            rel = f.path.relative_to(root) if f.path.is_relative_to(root) else f.path
            lines.append(f"  {f.agent}: {rel} ({f.size} bytes)")
    elif not inc and not include_runtimes:
        return "No agent configuration files found."

    if "skills" in inc:
        skills = detect_skill_dirs(root)
        if lines:
            lines.append("")
        if skills:
            lines.append(f"{len(skills)} skill(s):")
            for s in skills:
                rel = s.path.relative_to(root) if s.path.is_relative_to(root) else s.path
                lines.append(f"  {s.agent}: {rel} ({s.size} bytes)")
        else:
            lines.append("No skill directories found.")

    if "agents" in inc:
        agents = detect_agent_dirs(root)
        if lines:
            lines.append("")
        if agents:
            lines.append(f"{len(agents)} sub-agent file(s):")
            for a in agents:
                rel = a.path.relative_to(root) if a.path.is_relative_to(root) else a.path
                lines.append(f"  {a.agent}: {rel} ({a.size} bytes)")
        else:
            lines.append("No sub-agent files found.")

    if "commands" in inc:
        cmds = detect_command_dirs(root)
        if lines:
            lines.append("")
        if cmds:
            lines.append(f"{len(cmds)} slash-command file(s):")
            for c in cmds:
                rel = c.path.relative_to(root) if c.path.is_relative_to(root) else c.path
                lines.append(f"  {c.agent}: {rel} ({c.size} bytes)")
        else:
            lines.append("No slash-command files found.")

    if "settings" in inc:
        from memtomem.context.detector import detect_settings_files

        settings = detect_settings_files(root, _resolve_mcp_scope())
        if lines:
            lines.append("")
        if settings:
            lines.append(f"{len(settings)} settings file(s):")
            for s in settings:
                status = f"({s.size} bytes)" if s.size else "(not yet created)"
                lines.append(f"  {s.agent}: {s.path} {status}")
        else:
            lines.append("No settings files detected.")

    if include_runtimes:
        from memtomem.context.runtime_registry import probe_all_runtimes

        if lines:
            lines.append("")
        lines.append("Provider-client registration:")
        for st in probe_all_runtimes(root):
            if st.memtomem_registered or st.mms_registered:
                ids = "+".join(
                    n
                    for n, on in (("memtomem", st.memtomem_registered), ("mms", st.mms_registered))
                    if on
                )
                locs = f" [{', '.join(st.registered_locations)}]" if st.registered_locations else ""
                state = f"{ids} registered{locs}"
            elif st.installed:
                state = "installed, not registered"
            else:
                state = "not detected"
            suffix = f" (error: {st.error_kind})" if st.error_kind else ""
            lines.append(f"  {st.name}: {state}{suffix}")

    return "\n".join(lines) if lines else "Nothing detected."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_diff(
    include: str = "",
    scope: str = "",
    ctx: CtxType = None,
) -> str:
    """Show sync status between context.md and agent files.

    Pass ``include="skills,agents,commands"`` to also compare canonical
    skills, sub-agents, and slash commands against their runtime counterparts.

    Args:
        include: Comma-separated extra artifact kinds
            (``skills``, ``agents``, ``commands``, ``settings``).
        scope: ADR-0011 canonical artifact tier for the skills / agents /
            commands diff (``project_shared`` default, ``user``, or
            ``project_local``). The same value is also forwarded as the
            ADR-0010 target-scope override for the ``settings`` comparison,
            as ``mm context generate`` / ``sync`` resolve it. MCP has no cwd
            to infer from, so callers must pass ``scope`` explicitly to
            target a non-default tier.
    """
    from memtomem.context.agents import diff_agents
    from memtomem.context.commands import diff_commands
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context
    from memtomem.context.skills import diff_skills

    inc = _parse_include(include)
    root = _find_project_root()
    artifact_scope = _resolve_artifact_mcp_scope(scope)
    ctx_path = root / CONTEXT_FILENAME

    lines: list[str] = []

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        files = detect_agent_files(root)

        if files:
            for f in files:
                gen = GENERATORS.get(f.agent)
                if not gen:
                    continue
                current = (await asyncio.to_thread(f.path.read_text, encoding="utf-8")).strip()
                expected = gen.generate(sections).strip()
                status = "in sync" if current == expected else "out of sync"
                lines.append(f"{f.agent}: {f.path.name} [{status}]")
        elif not inc:
            return "No agent files to compare."
    elif not inc:
        return f"{CONTEXT_FILENAME} not found."
    else:
        lines.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        # Diff engines scan canonical + N runtime dirs per kind — off the
        # event loop like the per-file reads above (#1518 uniformity).
        rows = await asyncio.to_thread(diff_skills, root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Skills:")
            for row in rows:
                runtime, name, status = row
                reason = _redact_reason(getattr(row, "reason", None), root)
                suffix = f" — {reason}" if reason else ""
                lines.append(f"  {runtime}: {name} [{status}]{suffix}")
        else:
            lines.append("No skills to compare.")

    if "agents" in inc:
        rows = await asyncio.to_thread(diff_agents, root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Sub-agents:")
            for row in rows:
                runtime, name, status = row
                reason = _redact_reason(getattr(row, "reason", None), root)
                suffix = f" — {reason}" if reason else ""
                lines.append(f"  {runtime}: {name} [{status}]{suffix}")
        else:
            lines.append("No sub-agents to compare.")

    if "commands" in inc:
        rows = await asyncio.to_thread(diff_commands, root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Commands:")
            for row in rows:
                runtime, name, status = row
                reason = _redact_reason(getattr(row, "reason", None), root)
                suffix = f" — {reason}" if reason else ""
                lines.append(f"  {runtime}: {name} [{status}]{suffix}")
        else:
            lines.append("No commands to compare.")

    if "settings" in inc:
        from memtomem.context.settings import diff_settings as _diff_settings

        # Resolve settings scope lazily: _resolve_mcp_scope builds
        # Mem2MemConfig and applies env/file overrides, which can fail
        # on unrelated misconfiguration. Artifact-only callers (no
        # ``settings`` in ``include``) must not pay that cost or see
        # that failure.
        settings_scope = _resolve_mcp_scope(scope.strip() or None)
        # Cross-tier duplicate-hook warnings, matching the CLI
        # ``_print_settings_diff`` which prints them before the rows.
        dup_warnings = _settings_dup_tier_warnings(root, settings_scope)
        settings_results = await asyncio.to_thread(_diff_settings, root, scope=settings_scope)
        if settings_results or dup_warnings:
            if lines:
                lines.append("")
            lines.append("Settings:")
            lines.extend(dup_warnings)
            # Same MCP wire-boundary redaction as the generate/sync settings
            # loops — reasons embed absolute canonical/target paths.
            for sname, sr in settings_results.items():
                if sr.status in ("in sync", "out of sync", "missing target"):
                    lines.append(f"  {sname} [{sr.status}]")
                    for w in sr.warnings:
                        lines.append(f"    warning: {_redact_reason(w, root)}")
                elif sr.status == "skipped":
                    lines.append(f"  skipped {sname}: {_redact_reason(sr.reason, root)}")
                elif sr.status == "error":
                    lines.append(f"  error {sname}: {_redact_reason(sr.reason, root)}")

    return "\n".join(lines) if lines else "Nothing to compare."
