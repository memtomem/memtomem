"""Tools: context_detect, context_init, context_generate, context_sync, context_diff, context_memory_migrate."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import click

from memtomem.config import TargetScope
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
    return scope  # type: ignore[return-value]


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


def _validate_on_drop(on_drop: str) -> str:
    """Validate the ``on_drop`` severity, mirroring the CLI ``--on-drop`` Choice.

    The CLI exposes three drop severities (``ignore`` / ``warn`` / ``error``)
    via ``click.Choice(ON_DROP_LEVELS)`` in ``cli/context_cmd.py``, with
    ``--strict`` as a legacy alias for ``error``. The MCP tools only exposed
    the boolean ``strict``, leaving ``warn`` unreachable — an MCP caller had
    no way to request "report dropped fields but still write". This validator
    lets ``mem_context_generate`` / ``mem_context_sync`` accept the same
    vocabulary and reject bad values up front instead of silently downgrading
    to ``ignore`` downstream.
    """
    from memtomem.context.agents import ON_DROP_LEVELS

    if on_drop not in ON_DROP_LEVELS:
        raise ValueError(f"Unknown on_drop value '{on_drop}'. Supported: {sorted(ON_DROP_LEVELS)}")
    return on_drop


def _settings_dup_tier_warnings(root: Path, active_scope: str) -> list[str]:
    """Cross-tier duplicate-hook warning lines for the settings axis.

    Mirrors the CLI's ``_print_duplicate_tier_warnings`` (ADR-0010 §4), which
    fires inside the real generate / diff / sync workflow rather than behind a
    separate ``settings-doctor`` command. The MCP settings branches dropped
    these warnings entirely, so an MCP caller never learned that a
    memtomem-managed hook was duplicated in a non-active tier. Surface them
    here so the MCP and CLI settings surfaces agree. Non-blocking — duplicates
    are informational.
    """
    from memtomem.context.settings_doctor import detect_duplicate_tiers, format_warning

    return [
        f"  warning: {format_warning(dup, active_scope=active_scope)}"
        for dup in detect_duplicate_tiers(root, active_scope=active_scope)
    ]


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_init(
    include: str = "",
    overwrite: bool = False,
    overwrite_context_md: bool = False,
    scope: str = "",
    confirm_project_shared: bool = False,
    force_unsafe_import: bool = False,
    ctx: CtxType = None,
) -> str:
    """Seed canonical context artifact directories.

    Args:
        include: Comma-separated runtime artifact kinds to import into
            canonical storage (``skills``, ``agents``, ``commands``).
            ``settings`` is accepted for parity with other context tools
            but has no init-time import action.
        overwrite: Overwrite existing canonical entries during runtime
            import. Does **not** govern ``.memtomem/context.md`` rewrite
            — see ``overwrite_context_md``.
        overwrite_context_md: Allow rewriting an existing
            ``.memtomem/context.md`` from detected agent files. Kept
            separate from ``overwrite`` so artifact-import refresh cannot
            silently clobber hand-edited project memory. Mirrors the
            CLI's separate confirmation prompt at
            ``cli/context_cmd.py:789-798`` (which defaults to "No").
        scope: Artifact storage scope: ``project_shared`` (default),
            ``user``, or ``project_local``.
        confirm_project_shared: Required when ``scope="project_shared"``
            is explicitly supplied; MCP cannot prompt interactively, so a
            missing confirmation returns a ``needs confirmation`` message.
        force_unsafe_import: Bypass Gate A on existing runtime files for
            ``user`` / ``project_local`` imports. ``project_shared`` still
            hard-refuses unsafe imports.
    """
    from memtomem.context import _skip_reasons as skip_codes
    from memtomem.context.agents import (
        canonical_agent_name,
        extract_agents_to_canonical,
    )
    from memtomem.context.commands import extract_commands_to_canonical
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import extract_sections_from_agent_file
    from memtomem.context.parser import CONTEXT_FILENAME, sections_to_markdown
    from memtomem.context.privacy_scan import PrivacyScanError
    from memtomem.context.scope_resolver import canonical_artifact_dir
    from memtomem.context.skills import extract_skills_to_canonical

    # Reuse the CLI helper so the marker text and idempotency stay pinned
    # to one implementation.
    from memtomem.cli.context_cmd import _append_gitignore_marker

    inc = _parse_include(include)
    root = _find_project_root()
    scope_explicit = bool(scope.strip())
    artifact_scope = _resolve_artifact_mcp_scope(scope)
    has_project_signal = (root / ".git").exists() or (root / "pyproject.toml").exists()

    # EXPLICIT scope=project_* requires a real project context, mirroring
    # the CLI gate at cli/context_cmd.py:744. Implicit default (no scope=)
    # preserves pre-PR-E2 backward compatibility — falls through to the
    # warning + seed-here path below, the same way the CLI does.
    if scope_explicit and artifact_scope != "user" and not has_project_signal:
        return (
            f"--scope={artifact_scope} requires a project root "
            "(with .git or pyproject.toml). Use scope='user' from outside a project."
        )

    if scope_explicit and artifact_scope == "project_shared" and not confirm_project_shared:
        return (
            "needs confirmation: scope='project_shared' writes to git-tracked "
            f"{root / '.memtomem'}. Re-call with confirm_project_shared=True to proceed."
        )

    results: list[str] = []

    if not scope_explicit and not has_project_signal:
        results.append(
            f"warning: no .git or pyproject.toml in {root} — creating .memtomem/ here. "
            "Use scope='user' for cross-project artifacts."
        )

    artifact_only_scope = scope_explicit and artifact_scope in ("user", "project_local")
    write_context_md = has_project_signal and not artifact_only_scope
    ctx_path = root / CONTEXT_FILENAME

    if write_context_md and ctx_path.exists() and not overwrite_context_md:
        results.append(
            f"skipped {CONTEXT_FILENAME} rewrite (already exists; "
            "pass overwrite_context_md=True to replace)"
        )
        write_context_md = False

    if write_context_md:
        files = detect_agent_files(root)
        if not files:
            results.append("No agent files found. Creating empty context template.")
            sections: dict[str, str] = {
                "Project": "- Name: \n- Language: \n- Package manager: ",
                "Commands": "- Build: \n- Test: \n- Lint: ",
                "Architecture": "",
                "Rules": "",
                "Style": "",
            }
        else:
            best = max(files, key=lambda f: f.size)
            results.append(f"Extracting from {best.agent}: {best.path.name} ({best.size} bytes)")
            content = await asyncio.to_thread(best.path.read_text, encoding="utf-8")
            sections = extract_sections_from_agent_file(content, source=best.agent)
            for f in files:
                if f.path == best.path:
                    continue
                other_content = await asyncio.to_thread(f.path.read_text, encoding="utf-8")
                other_sections = extract_sections_from_agent_file(other_content, source=f.agent)
                for key, val in other_sections.items():
                    if key not in sections and val.strip():
                        sections[key] = val

        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            ctx_path.write_text,
            sections_to_markdown(sections),
            encoding="utf-8",
        )
        results.append(f"Created {CONTEXT_FILENAME}")
        results.append(f"  Sections: {', '.join(sections.keys())}")

    for kind in ("agents", "skills", "commands"):
        d = canonical_artifact_dir(kind, artifact_scope, root)
        d.mkdir(parents=True, exist_ok=True)
        results.append(f"Created {d}")

    if artifact_scope == "project_local":
        wrote, msg = _append_gitignore_marker(root)
        if wrote:
            results.append("Appended .gitignore marker for project_local artifacts")
        elif msg == "already_present":
            results.append(".gitignore marker already present")
        elif msg == "no_git_repo_pyproject_only":
            results.append(
                "warning: project root resolved via pyproject.toml but .git is missing; "
                ".gitignore not appended"
            )
        elif msg == "no_project_signal":
            results.append("warning: no project signal; .gitignore append skipped")

    def _skip_line(name: str, reason: str, code: str | None) -> str:
        prefix = (
            "blocked"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "skipped"
        )
        return f"  {prefix} {name}: {reason}"

    if "skills" in inc:
        try:
            skill_result = extract_skills_to_canonical(
                root,
                overwrite=overwrite,
                scope=artifact_scope,
                force_unsafe_import=force_unsafe_import,
            )
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        except click.ClickException as exc:
            # apply_gate_a hard-aborts project_shared privacy hits via
            # click.ClickException (_gate_a.py:171). Surface its message
            # so MCP callers see the actionable block, not "internal error".
            return f"privacy block: {exc.message}"
        results.append(f"Imported skills: {len(skill_result.imported)}")
        for path in skill_result.imported:
            results.append(f"  {path.name}")
        for name, reason, code in skill_result.skipped:
            results.append(_skip_line(name, reason, code))

    if "agents" in inc:
        try:
            agent_result = extract_agents_to_canonical(
                root,
                overwrite=overwrite,
                scope=artifact_scope,
                force_unsafe_import=force_unsafe_import,
            )
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        except click.ClickException as exc:
            return f"privacy block: {exc.message}"
        results.append(f"Imported sub-agents: {len(agent_result.imported)}")
        for path, layout in agent_result.imported:
            results.append(f"  {canonical_agent_name(path, layout)}")
        for name, reason, code in agent_result.skipped:
            results.append(_skip_line(name, reason, code))

    if "commands" in inc:
        try:
            command_result = extract_commands_to_canonical(
                root,
                overwrite=overwrite,
                scope=artifact_scope,
                force_unsafe_import=force_unsafe_import,
            )
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        except click.ClickException as exc:
            return f"privacy block: {exc.message}"
        results.append(f"Imported commands: {len(command_result.imported)}")
        for path, layout in command_result.imported:
            display = path.parent.name if layout == "dir" else path.stem
            results.append(f"  {display}")
        for name, reason, code in command_result.skipped:
            results.append(_skip_line(name, reason, code))

    if "settings" in inc:
        results.append("settings: no init-time import action")

    return "Initialized:\n" + "\n".join(results)


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_detect(
    include: str = "",
    ctx: CtxType = None,
) -> str:
    """Detect agent configuration files in the current project.

    Scans for CLAUDE.md, .cursorrules, GEMINI.md, AGENTS.md,
    and .github/copilot-instructions.md. Pass
    ``include="skills,agents,commands"`` to also list runtime skill
    directories, sub-agent files, and slash-command files.
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
    elif not inc:
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

    return "\n".join(lines) if lines else "Nothing detected."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_generate(
    agent: str = "all",
    include: str = "",
    strict: bool = False,
    on_drop: str = "ignore",
    scope: str = "",
    allow_host_writes: bool = False,
    ctx: CtxType = None,
) -> str:
    """Generate agent configuration files from .memtomem/context.md.

    Args:
        agent: Agent name (claude, cursor, gemini, codex, copilot) or "all".
        include: Comma-separated extra artifact kinds
            (``skills``, ``agents``, ``commands``, ``settings``).
        strict: Legacy alias for ``on_drop="error"``. Promotes dropped-field
            warnings to errors when converting sub-agents or slash commands.
            When both are supplied, ``on_drop`` wins unless it is still the
            default ``"ignore"`` (mirrors ``generate_all_agents``).
        on_drop: Severity when sub-agent / command fields are dropped during
            conversion: ``ignore`` (default, silent), ``warn`` (report but
            still write), or ``error`` (abort the kind). Mirrors the CLI
            ``--on-drop`` option; ``warn`` was previously unreachable via MCP.
        scope: ADR-0011 canonical artifact tier for skills / agents /
            commands fan-out: ``project_shared`` (default), ``user``, or
            ``project_local``. The same value is also forwarded as the
            ADR-0010 host-write target-scope override for ``settings``
            (mirrors the CLI at ``cli/context_cmd.py:963-987``).
        allow_host_writes: When ``include="settings"`` writes a settings
            file outside the project root (today only
            ``~/.claude/settings.json``), refuse with a
            ``needs confirmation`` line unless this is ``True``. Re-call
            with ``allow_host_writes=True`` after surfacing the host
            paths to the user.
    """
    from memtomem.context.agents import StrictDropError, generate_all_agents
    from memtomem.context.commands import (
        StrictDropError as CommandStrictDropError,
        generate_all_commands,
    )
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context
    from memtomem.context.privacy_scan import PrivacyScanError
    from memtomem.context.skills import generate_all_skills

    inc = _parse_include(include)
    on_drop = _validate_on_drop(on_drop)
    root = _find_project_root()
    artifact_scope = _resolve_artifact_mcp_scope(scope)
    ctx_path = root / CONTEXT_FILENAME

    results: list[str] = []

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        if sections:
            targets = list(GENERATORS.keys()) if agent == "all" else [agent]
            for name in targets:
                if name not in GENERATORS:
                    results.append(f"Unknown agent: {name}")
                    continue
                gen = GENERATORS[name]
                content = gen.generate(sections)
                out_path = root / gen.output_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(out_path.write_text, content, encoding="utf-8")
                results.append(f"{name}: {gen.output_path}")
        else:
            results.append(f"{CONTEXT_FILENAME} is empty.")
    elif not inc:
        return f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."
    else:
        results.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        try:
            skill_result = generate_all_skills(root, scope=artifact_scope)
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        if skill_result.generated:
            results.append("")
            results.append(f"Skills fan-out: {len(skill_result.generated)}")
            for runtime, path in skill_result.generated:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason, _code in skill_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")

    if "agents" in inc:
        try:
            agent_result = generate_all_agents(
                root, strict=strict, on_drop=on_drop, scope=artifact_scope
            )
        except StrictDropError as exc:
            return f"strict error: {exc}"
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        if agent_result.generated:
            results.append("")
            results.append(f"Sub-agent fan-out: {len(agent_result.generated)}")
            for runtime, path in agent_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason, _code in agent_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, agent_name, dropped in agent_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{agent_name}'")

    if "commands" in inc:
        try:
            command_result = generate_all_commands(
                root, strict=strict, on_drop=on_drop, scope=artifact_scope
            )
        except CommandStrictDropError as exc:
            return f"strict error: {exc}"
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        if command_result.generated:
            results.append("")
            results.append(f"Command fan-out: {len(command_result.generated)}")
            for runtime, path in command_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason, _code in command_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, cmd_name, dropped in command_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{cmd_name}'")

    if "settings" in inc:
        from memtomem.context.settings import generate_all_settings

        # Resolve settings scope lazily: _resolve_mcp_scope builds
        # Mem2MemConfig and applies env/file overrides, which can fail
        # on unrelated misconfiguration. Artifact-only callers must not
        # pay that cost or see that failure.
        settings_scope = _resolve_mcp_scope(scope.strip() or None)
        # Surface cross-tier duplicate-hook warnings, matching the CLI
        # ``_print_settings_generate`` which prints them before the results.
        results.extend(_settings_dup_tier_warnings(root, settings_scope))
        settings_results = generate_all_settings(
            root, scope=settings_scope, allow_host_writes=allow_host_writes
        )
        for sname, sr in settings_results.items():
            if sr.status == "ok":
                results.append(f"\nSettings: {sname} → {sr.target}")
                for w in sr.warnings:
                    results.append(f"  warning: {w}")
            elif sr.status == "skipped":
                results.append(f"  skipped {sname}: {sr.reason}")
            elif sr.status == "needs_confirmation":
                results.append(f"  needs confirmation {sname}: {sr.reason}")
            elif sr.status in ("error", "aborted"):
                results.append(f"  {sr.status} {sname}: {sr.reason}")

    return "Generated:\n" + "\n".join(results)


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
            ADR-0010 host-write target-scope override for ``settings``,
            mirroring ``mem_context_generate`` / ``mem_context_sync``
            (``cli/context_cmd.py:963-987``). MCP has no cwd to infer
            from, so callers must pass ``scope`` explicitly to target a
            non-default tier.
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
        rows = diff_skills(root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Skills:")
            for runtime, name, status in rows:
                lines.append(f"  {runtime}: {name} [{status}]")
        else:
            lines.append("No skills to compare.")

    if "agents" in inc:
        rows = diff_agents(root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Sub-agents:")
            for runtime, name, status in rows:
                lines.append(f"  {runtime}: {name} [{status}]")
        else:
            lines.append("No sub-agents to compare.")

    if "commands" in inc:
        rows = diff_commands(root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Commands:")
            for runtime, name, status in rows:
                lines.append(f"  {runtime}: {name} [{status}]")
        else:
            lines.append("No commands to compare.")

    if "settings" in inc:
        from memtomem.context.settings import diff_settings as _diff_settings

        # Resolve settings scope lazily: _resolve_mcp_scope builds
        # Mem2MemConfig and applies env/file overrides, which can fail
        # on unrelated misconfiguration. Artifact-only callers (no
        # ``settings`` in ``include``) must not pay that cost or see
        # that failure. Mirrors mem_context_generate's lazy resolve at
        # lines 520-524.
        settings_scope = _resolve_mcp_scope(scope.strip() or None)
        # Cross-tier duplicate-hook warnings, matching the CLI
        # ``_print_settings_diff`` which prints them before the rows.
        dup_warnings = _settings_dup_tier_warnings(root, settings_scope)
        settings_results = _diff_settings(root, scope=settings_scope)
        if settings_results or dup_warnings:
            if lines:
                lines.append("")
            lines.append("Settings:")
            lines.extend(dup_warnings)
            for sname, sr in settings_results.items():
                if sr.status in ("in sync", "out of sync", "missing target"):
                    lines.append(f"  {sname} [{sr.status}]")
                    for w in sr.warnings:
                        lines.append(f"    warning: {w}")
                elif sr.status == "skipped":
                    lines.append(f"  skipped {sname}: {sr.reason}")
                elif sr.status == "error":
                    lines.append(f"  error {sname}: {sr.reason}")

    return "\n".join(lines) if lines else "Nothing to compare."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_sync(
    include: str = "",
    strict: bool = False,
    on_drop: str = "ignore",
    scope: str = "",
    allow_host_writes: bool = False,
    ctx: CtxType = None,
) -> str:
    """Sync .memtomem/context.md to all detected agent files.

    Pass ``include="skills,agents,commands,settings"`` to also fan out
    ``.memtomem/skills/``, ``.memtomem/agents/``, ``.memtomem/commands/``,
    and ``.memtomem/settings.json`` to their runtime targets (Claude Code,
    Gemini CLI, Codex CLI).  ``on_drop`` controls the severity when
    sub-agent / command fields are dropped during conversion: ``ignore``
    (default), ``warn`` (report but still write), or ``error`` (abort the
    kind). ``strict=True`` is a legacy alias for ``on_drop="error"``; when
    both are supplied ``on_drop`` wins unless it is still the default.

    ``scope`` selects the ADR-0011 canonical artifact tier for
    ``skills``, ``agents``, and ``commands``: ``project_shared``
    (default), ``user``, or ``project_local``. For ``settings`` the same
    value is treated as the ADR-0010 host-write target-scope override.

    ``allow_host_writes`` defaults to ``False``: when ``include="settings"``
    would write to a file outside the project root (today only
    ``~/.claude/settings.json``), the tool returns a ``needs confirmation``
    line listing the host path instead of writing. Surface that to the
    user, then re-call with ``allow_host_writes=True`` to proceed.
    """
    from memtomem.context.agents import StrictDropError, generate_all_agents
    from memtomem.context.commands import (
        StrictDropError as CommandStrictDropError,
        generate_all_commands,
    )
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context
    from memtomem.context.privacy_scan import PrivacyScanError
    from memtomem.context.skills import generate_all_skills

    inc = _parse_include(include)
    on_drop = _validate_on_drop(on_drop)
    root = _find_project_root()
    artifact_scope = _resolve_artifact_mcp_scope(scope)
    ctx_path = root / CONTEXT_FILENAME

    results: list[str] = []

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        if not sections:
            # Mirror mem_context_generate's empty-guard. parse_context returns
            # {} for a content-but-no-``## Heading`` context.md; syncing it would
            # overwrite the user's existing agent files with header-only/empty
            # output (silent data loss). Refuse, matching the CLI sync guard.
            results.append(f"{CONTEXT_FILENAME} is empty — refusing to overwrite agent files.")
        else:
            files = detect_agent_files(root)

            if files:
                agents_synced: set[str] = set()
                for f in files:
                    if f.agent in agents_synced:
                        continue
                    gen = GENERATORS.get(f.agent)
                    if not gen:
                        continue
                    content = gen.generate(sections)
                    out_path = root / gen.output_path
                    await asyncio.to_thread(out_path.write_text, content, encoding="utf-8")
                    results.append(f"{f.agent}: {gen.output_path}")
                    agents_synced.add(f.agent)
            elif not inc:
                return "No agent files detected. Use mem_context_generate to create them."
    elif not inc:
        return f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."
    else:
        results.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        try:
            skill_result = generate_all_skills(root, scope=artifact_scope)
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        if skill_result.generated:
            if results:
                results.append("")
            results.append(f"Skills fan-out: {len(skill_result.generated)}")
            for runtime, path in skill_result.generated:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason, _code in skill_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")

    if "agents" in inc:
        try:
            agent_result = generate_all_agents(
                root, strict=strict, on_drop=on_drop, scope=artifact_scope
            )
        except StrictDropError as exc:
            return f"strict error: {exc}"
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        if agent_result.generated:
            if results:
                results.append("")
            results.append(f"Sub-agent fan-out: {len(agent_result.generated)}")
            for runtime, path in agent_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason, _code in agent_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, agent_name, dropped in agent_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{agent_name}'")

    if "commands" in inc:
        try:
            command_result = generate_all_commands(
                root, strict=strict, on_drop=on_drop, scope=artifact_scope
            )
        except CommandStrictDropError as exc:
            return f"strict error: {exc}"
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        if command_result.generated:
            if results:
                results.append("")
            results.append(f"Command fan-out: {len(command_result.generated)}")
            for runtime, path in command_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason, _code in command_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, cmd_name, dropped in command_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{cmd_name}'")

    if "settings" in inc:
        from memtomem.context.settings import generate_all_settings

        # Resolve settings scope lazily (see mem_context_generate note):
        # _resolve_mcp_scope builds Mem2MemConfig and applies env/file
        # overrides — artifact-only callers must not pay that cost or
        # see that failure.
        settings_scope = _resolve_mcp_scope(scope.strip() or None)
        # Surface cross-tier duplicate-hook warnings, matching the CLI
        # ``_print_settings_generate`` which prints them before the results.
        results.extend(_settings_dup_tier_warnings(root, settings_scope))
        settings_results = generate_all_settings(
            root, scope=settings_scope, allow_host_writes=allow_host_writes
        )
        for sname, sr in settings_results.items():
            if sr.status == "ok":
                if results:
                    results.append("")
                results.append(f"Settings: {sname} → {sr.target}")
                for w in sr.warnings:
                    results.append(f"  warning: {w}")
            elif sr.status == "skipped":
                results.append(f"  skipped {sname}: {sr.reason}")
            elif sr.status == "needs_confirmation":
                if results:
                    results.append("")
                results.append(f"  needs confirmation {sname}: {sr.reason}")
            elif sr.status in ("error", "aborted"):
                results.append(f"  {sr.status} {sname}: {sr.reason}")

    return "Synced:\n" + "\n".join(results) if results else "Nothing to sync."


_KNOWN_MEMORY_SCOPES: frozenset[str] = frozenset({"user", "project_shared", "project_local"})


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_memory_migrate(
    source: str,
    from_scope: str,
    to_scope: str,
    apply: bool = False,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Move markdown memory file(s) between ADR-0011 memory scope tiers.

    Renamed from ``mem_context_migrate`` in #1147 (B5-2): this tool only
    ever covered *memory*-tier migration, but the bare name implied it
    mirrored the full CLI ``mm context migrate`` (which also does artifact
    flat→dir and scope-tier moves — see ``mem_context_artifact_migrate``).
    The old name remains as a deprecated alias.

    Mirrors the CLI ``mm context memory-migrate <SOURCE> --from <scope>
    --to <scope> [--apply] [--confirm-project-shared]``
    (``cli/context_cmd.py:2574-2659``). Chunk-id-stable single-DB rename:
    the source file moves on disk to the target tier's canonical
    directory and the ``chunks`` table is UPDATEd in place via
    ``update_chunks_scope_for_source`` so chunk UUIDs and the
    ``chunk_links`` lineage are preserved. No re-index is triggered.

    Cross-DB migration is out of scope — deferred per ADR-0012 / #911.

    Args:
        source: Single existing markdown file path OR a glob pattern
            (e.g. ``"~/.memtomem/memories/**/*.md"``). For globs the
            pre-flight pass (privacy scan + lockfile probe) runs over
            every match before any FS move; on per-file DB failure
            mid-batch, that file's FS move is reverted, the remaining
            files are left untouched, and the call exits with the
            partial-progress message in the returned text.
        from_scope: Source memory tier — ``user``, ``project_shared``,
            or ``project_local``.
        to_scope: Target memory tier (same vocabulary). Must differ
            from ``from_scope``.
        apply: Execute the migration. Default ``False`` returns a
            dry-run preview (the same plan output the CLI shows above
            its "Run with --apply to execute." footer).
        confirm_project_shared: Required when ``to_scope="project_shared"``;
            MCP cannot prompt interactively, so a missing confirmation
            returns a ``needs confirmation`` message instead of touching
            disk. Mirrors ``mem_context_init`` / ``mem_context_generate``
            project_shared gating.

    Privacy: when ``to_scope="project_shared"`` the file content is
    re-scanned by ``privacy.enforce_write_guard`` both at pre-flight and
    at apply time (the latter catches in-flight edits during the
    confirmation window). Secret hits reject the migration with no
    force bypass — git history is forever (ADR-0011 §5). Rejections
    surface as ``privacy block: ...`` in the returned text.
    """
    from memtomem.cli.context_cmd import (
        _memory_migrate_run,
        _resolve_memory_migrate_sources,
    )
    from memtomem.context.privacy_scan import PrivacyScanError

    # Scope vocabulary validation up-front so callers get a clean error
    # instead of a downstream MemoryScopeError stringification.
    for label, value in (("from_scope", from_scope), ("to_scope", to_scope)):
        if value not in _KNOWN_MEMORY_SCOPES:
            return f"error: Unknown {label}='{value}'. Supported: {sorted(_KNOWN_MEMORY_SCOPES)}"

    if from_scope == to_scope:
        return "error: --from and --to must differ."

    # Gate B: project_shared writes go to the git-tracked memory tier.
    # MCP has no interactive prompt, so refuse early and tell the caller
    # how to opt in. Mirrors mem_context_init project_shared gating at
    # ``tools/context.py:151-155``.
    if to_scope == "project_shared" and not confirm_project_shared:
        return (
            "needs confirmation: to_scope='project_shared' writes to the "
            "git-tracked memory tier. Re-call with confirm_project_shared=True "
            "to proceed."
        )

    try:
        sources = _resolve_memory_migrate_sources(source)
    except click.ClickException as exc:
        return f"error: {exc.message}"

    # Per-call output buffers passed into ``_memory_migrate_run``.
    # We deliberately do NOT use ``contextlib.redirect_stdout`` /
    # ``redirect_stderr``: both swap ``sys.stdout`` / ``sys.stderr``
    # process-globally for the duration of the ``with`` block, and the
    # block awaits the heavy helper. While this coroutine is suspended
    # on I/O, any other concurrent MCP tool call that prints (or that
    # internally ``click.echo``s) would land in our buffer — and our
    # output would land in theirs. Routing through per-call lists keeps
    # each call's output strictly local; the helper falls back to its
    # original ``click.secho`` behaviour when both buffers are ``None``
    # (CLI path). Codex review-pass on PR #926.
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    try:
        await _memory_migrate_run(
            sources,
            cast(TargetScope, from_scope),
            cast(TargetScope, to_scope),
            apply,
            # ``yes=True`` because the MCP wrapper has already gated
            # Gate B above via the explicit ``confirm_project_shared``
            # argument. ``yes`` only suppresses the ``click.confirm``
            # prompt that the CLI falls through to when
            # ``confirm_project_shared`` is missing — irrelevant for
            # MCP, which has no TTY.
            yes=True,
            confirm_project_shared=confirm_project_shared,
            stdout_buf=stdout_lines,
            stderr_buf=stderr_lines,
        )
    except PrivacyScanError as exc:
        return f"privacy block: {exc.message}"
    except click.exceptions.Exit:
        # Gate A privacy block — the helper recorded the rejection text
        # in ``stderr_lines`` before exiting. Surface as a privacy
        # block so the MCP caller can branch on the prefix.
        stderr_text = "\n".join(stderr_lines).strip()
        if "Gate A:" in stderr_text:
            return f"privacy block: {stderr_text}"
        return f"error: {stderr_text or 'migration exited unexpectedly'}"
    except click.ClickException as exc:
        # Mid-batch failures (DB UPDATE failed, double-failure path)
        # record per-file partial-progress lines in ``stderr_lines``
        # BEFORE raising ``ClickException``. Append those so the MCP
        # caller sees the K-of-N batch state, not just the bare error
        # message — Codex flagged this on PR #926.
        stderr_tail = "\n".join(stderr_lines).strip()
        if stderr_tail:
            return f"error: {exc.message}\n{stderr_tail}"
        return f"error: {exc.message}"

    stdout_text = "\n".join(stdout_lines).rstrip()
    # Helper paths that reach here without raising have nothing in
    # ``stderr_lines`` today; defensively concatenate so a future
    # warning-but-don't-raise path is not silently dropped.
    stderr_tail = "\n".join(stderr_lines).strip()
    if stderr_tail:
        stdout_text = f"{stdout_text}\n{stderr_tail}" if stdout_text else stderr_tail
    return stdout_text or "Nothing to migrate."


@mcp.tool()
@tool_handler
async def mem_context_migrate(
    source: str,
    from_scope: str,
    to_scope: str,
    apply: bool = False,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """DEPRECATED alias for ``mem_context_memory_migrate``.

    Renamed in #1147 (B5-2): ``mem_context_migrate`` only ever covered
    *memory*-tier migration, but its bare name implied parity with the
    full CLI ``mm context migrate`` (which also does artifact flat→dir and
    scope-tier moves, now exposed as ``mem_context_artifact_migrate``).
    Use ``mem_context_memory_migrate`` instead; this alias forwards every
    argument unchanged and will be removed in a future major release.

    The explicit signature is repeated (rather than ``**kwargs``) because
    the MCP schema is built by inspecting the function signature — a
    ``**kwargs`` alias would publish an empty parameter schema. Not
    routed through ``mem_do`` directly: the registry alias
    ``"context_migrate" → "context_memory_migrate"`` (``tools/meta.py``)
    keeps the old ``mem_do`` action name working.
    """
    return await mem_context_memory_migrate(
        source=source,
        from_scope=from_scope,
        to_scope=to_scope,
        apply=apply,
        confirm_project_shared=confirm_project_shared,
        ctx=ctx,
    )
