"""Tools: context_detect, context_init, context_generate, context_sync, context_diff, context_memory_migrate, context_artifact_migrate, context_artifact_transfer."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

import click

from memtomem.config import TargetScope
from memtomem.context import versioning
from memtomem.context.scope_resolver import find_project_root
from memtomem.server import mcp
from memtomem.server.context import CtxType
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

if TYPE_CHECKING:
    from memtomem.context._names import Layout
    from memtomem.context.mcp_servers_copy import McpServerCopyResult
    from memtomem.context.migrate import MigrateRow, MigrateScopeResult
    from memtomem.context.scope_resolver import ArtifactKind
    from memtomem.context.transfer import TransferMode, TransferResult
    from memtomem.context.versioning import VersionsManifest

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


# Kinds whose fan-out honors a version label (ADR-0022 v1 scope: agents +
# commands). Mirrors ``cli.context_cmd._LABEL_ELIGIBLE_KINDS``.
_LABEL_ELIGIBLE_KINDS: frozenset[str] = frozenset({"agents", "commands"})


def _normalize_label(label: str) -> str | None:
    """MCP ``label`` arg (``str``, default ``""``) → ``generate_all_*``'s
    ``str | None``. Empty/whitespace → ``None`` (== working file, today's
    behavior); ``"latest"`` is passed through and also resolves to the working
    file at the engine boundary (ADR-0022 invariant 2)."""
    label = label.strip()
    return label or None


def _label_ineligible_notes(label: str | None, inc: set[str]) -> list[str]:
    """MCP analog of cli ``_warn_label_ineligible_kinds`` (ADR-0022 invariant 10).

    ``label`` governs only agents/commands; ineligible included kinds
    (skills/settings/project-memory) run label-less, and a label with no
    eligible kind in ``include`` is a warned no-op (never an error). Returns
    note lines for the tool output instead of ``click.secho``-ing them.
    """
    if label is None or label == "latest":
        return []
    notes: list[str] = []
    ineligible = sorted(inc - _LABEL_ELIGIBLE_KINDS)
    if ineligible:
        notes.append(
            f"  note: label does not apply to {', '.join(ineligible)} "
            "(only agents/commands are versioned); they sync from the working file."
        )
    if not (inc & _LABEL_ELIGIBLE_KINDS):
        notes.append(
            f"  note: label={label} had no effect — no versioned kind (agents/commands) in include."
        )
    return notes


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
    from memtomem.context._atomic import atomic_write_text
    from memtomem.context.agents import (
        canonical_agent_name,
        extract_agents_to_canonical,
    )
    from memtomem.context.commands import extract_commands_to_canonical
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import extract_sections_from_agent_file, preamble_source
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
            sections = extract_sections_from_agent_file(
                content, source=preamble_source(best.agent, best.path)
            )
            for f in files:
                if f.path == best.path:
                    continue
                other_content = await asyncio.to_thread(f.path.read_text, encoding="utf-8")
                other_sections = extract_sections_from_agent_file(
                    other_content, source=preamble_source(f.agent, f.path)
                )
                for key, val in other_sections.items():
                    if key not in sections and val.strip():
                        sections[key] = val

        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write (#1247 id 19): a crash mid-write must not truncate
        # the canonical context.md — unlike the runtime fan-out targets it
        # is NOT regenerable. 0o644: project file meant to be read (and
        # committed) by other tools, not a 0o600 state file.
        await asyncio.to_thread(
            atomic_write_text,
            ctx_path,
            sections_to_markdown(sections),
            0o644,
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
            # Thread offload (#1247 id 18): the skills import engine blocks
            # on the destination sidecar flock (budget-bounded) — keep that
            # wait off the event loop.
            skill_result = await asyncio.to_thread(
                extract_skills_to_canonical,
                root,
                overwrite=overwrite,
                scope=artifact_scope,
                force_unsafe_import=force_unsafe_import,
                surface="mcp_context_init",
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
                surface="mcp_context_init",
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
                surface="mcp_context_init",
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
    include_runtimes: bool = False,
    ctx: CtxType = None,
) -> str:
    """Detect agent configuration files in the current project.

    Scans for CLAUDE.md, .cursorrules, GEMINI.md, AGENTS.md,
    and .github/copilot-instructions.md. Pass
    ``include="skills,agents,commands"`` to also list runtime skill
    directories, sub-agent files, and slash-command files.

    Pass ``include_runtimes=True`` to also report read-only provider-client
    registration status (Claude / Antigravity / Codex / Kimi; ADR-0021 §B).
    This is a separate boolean — it is intentionally NOT part of the ``include``
    set, so the shared sync/generate/init/diff include contract is never
    widened (``mem_context_sync(include="runtimes")`` still rejects).
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
async def mem_context_generate(
    agent: str = "all",
    include: str = "",
    strict: bool = False,
    on_drop: str = "ignore",
    scope: str = "",
    allow_host_writes: bool = False,
    label: str = "",
    ctx: CtxType = None,
) -> str:
    """Generate agent configuration files from .memtomem/context.md.

    Args:
        agent: Agent name (claude, cursor, gemini, codex, copilot) or "all".
        include: Comma-separated extra artifact kinds
            (``skills``, ``agents``, ``commands``, ``settings``).
        label: ADR-0022 — fan out the frozen version at this label
            (e.g. ``"production"``) or a bare version tag (``"v2"``) for the
            versioned kinds (``agents`` / ``commands``) instead of the working
            canonical. ``""`` (default) / ``"latest"`` means the working file
            (today's behavior, unchanged). Ineligible included kinds run
            label-less with a note (invariant 10); an unknown/dangling label is
            isolated as a per-artifact skip, not a whole-run error. Mirrors the
            CLI ``mm context generate --label``.
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
    from memtomem.context._atomic import atomic_write_text
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
    label_norm = _normalize_label(label)
    root = _find_project_root()
    artifact_scope = _resolve_artifact_mcp_scope(scope)
    ctx_path = root / CONTEXT_FILENAME

    results: list[str] = _label_ineligible_notes(label_norm, inc)

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
                # Atomic write (#1247 id 19): crash mid-write must not
                # truncate the user's CLAUDE.md / GEMINI.md / etc.
                await asyncio.to_thread(atomic_write_text, out_path, content, 0o644)
                results.append(f"{name}: {gen.output_path}")
        else:
            results.append(f"{CONTEXT_FILENAME} is empty.")
    elif not inc:
        # Carry any label note through this nothing-to-generate exit (CLI parity).
        msg = f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."
        return "\n".join([*results, msg]) if results else msg
    else:
        results.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        try:
            # Thread offload: ``generate_all_skills`` blocks on destination
            # sidecar flocks since #1229 — keep the (budget-bounded) wait
            # off the event loop (#1247 id 18 sweep).
            skill_result = await asyncio.to_thread(
                generate_all_skills, root, scope=artifact_scope, surface="mcp_context_generate"
            )
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
                root,
                strict=strict,
                on_drop=on_drop,
                scope=artifact_scope,
                label=label_norm,
                surface="mcp_context_generate",
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
                root,
                strict=strict,
                on_drop=on_drop,
                scope=artifact_scope,
                label=label_norm,
                surface="mcp_context_generate",
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
            for row in rows:
                runtime, name, status = row
                reason = getattr(row, "reason", None)
                suffix = f" — {reason}" if reason else ""
                lines.append(f"  {runtime}: {name} [{status}]{suffix}")
        else:
            lines.append("No skills to compare.")

    if "agents" in inc:
        rows = diff_agents(root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Sub-agents:")
            for row in rows:
                runtime, name, status = row
                reason = getattr(row, "reason", None)
                suffix = f" — {reason}" if reason else ""
                lines.append(f"  {runtime}: {name} [{status}]{suffix}")
        else:
            lines.append("No sub-agents to compare.")

    if "commands" in inc:
        rows = diff_commands(root, scope=artifact_scope)
        if rows:
            if lines:
                lines.append("")
            lines.append("Commands:")
            for row in rows:
                runtime, name, status = row
                reason = getattr(row, "reason", None)
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
    label: str = "",
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

    ``label`` (ADR-0022) deploys a frozen version instead of the working
    canonical for the versioned kinds (``agents`` / ``commands``): pass a label
    (e.g. ``"production"``) or a bare version tag (``"v2"``) created via
    ``mem_context_version`` / ``mem_context_promote``. ``""`` (default) /
    ``"latest"`` fans out the working file (byte-for-byte today's behavior).
    The label applies only to agents/commands — ineligible included kinds run
    label-less with a note (invariant 10) — and an unknown/dangling label or a
    flat-layout artifact is isolated as a per-artifact ``skipped`` row, never a
    whole-run error. Mirrors the CLI ``mm context sync --label``.
    """
    from memtomem.context._atomic import atomic_write_text
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
    label_norm = _normalize_label(label)
    root = _find_project_root()
    artifact_scope = _resolve_artifact_mcp_scope(scope)
    ctx_path = root / CONTEXT_FILENAME

    results: list[str] = _label_ineligible_notes(label_norm, inc)

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
                    # Atomic write (#1247 id 19): crash mid-write must not
                    # truncate the user's CLAUDE.md / GEMINI.md / etc.
                    await asyncio.to_thread(atomic_write_text, out_path, content, 0o644)
                    results.append(f"{f.agent}: {gen.output_path}")
                    agents_synced.add(f.agent)
            elif not inc:
                # Carry any label note (e.g. "label had no effect") through this
                # nothing-to-sync exit so a labeled call still warns, matching the
                # CLI which prints the warning before this terminal message.
                msg = "No agent files detected. Use mem_context_generate to create them."
                return "\n".join([*results, msg]) if results else msg
    elif not inc:
        msg = f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."
        return "\n".join([*results, msg]) if results else msg
    else:
        results.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        try:
            # Thread offload: same rationale as mem_context_generate.
            skill_result = await asyncio.to_thread(
                generate_all_skills, root, scope=artifact_scope, surface="mcp_context_sync"
            )
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
                root,
                strict=strict,
                on_drop=on_drop,
                scope=artifact_scope,
                label=label_norm,
                surface="mcp_context_sync",
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
                root,
                strict=strict,
                on_drop=on_drop,
                scope=artifact_scope,
                label=label_norm,
                surface="mcp_context_sync",
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


# ── Artifact migration (flat→dir + scope-tier) ──────────────────────────────


def _format_artifact_flat_preview(rows: list[MigrateRow], *, skills_section: bool) -> str:
    """Plain-text dry-run preview for flat→dir migration (mirrors the CLI's
    ``_print_migrate_preview`` content without click colour)."""
    from memtomem.cli.context_cmd import _summarize_migrate_rows

    lines = ["Will migrate (review; re-call with apply=True to execute):"]
    last_type: str | None = None
    for row in rows:
        if row.asset_type != last_type:
            lines.append(f"\n{row.asset_type}")
            last_type = row.asset_type
        lines.append(f"  {row.name}  [{row.state}]  ({row.reason})")
    if skills_section:
        lines.append("\nskills\n  (always directory layout — no migration needed.)")
    lines.append(f"\nSummary: {_summarize_migrate_rows(rows)}.")
    lines.append("\nRe-call with apply=True to execute.")
    if any(r.state == "refuse_dirty" for r in rows):
        lines.append(
            "Dirty/collision assets need apply=True + force=True (creates a .bak per dirty file)."
        )
    return "\n".join(lines)


def _run_artifact_flat_apply(project_root: Path, rows: list[MigrateRow], *, force: bool) -> str:
    """Execute each row via ``migrate_one`` and format a plain-text summary."""
    from memtomem.context.migrate import migrate_one

    out: list[str] = []
    successes = failures = skipped = 0
    for row in rows:
        if row.state in {"noop", "skip_manual", "skip_orphan"}:
            out.append(f"  - {row.asset_type}/{row.name}: {row.reason}")
            skipped += 1
            continue
        if row.state == "refuse_dirty" and not force:
            out.append(f"  x {row.asset_type}/{row.name}: dirty without force=True")
            failures += 1
            continue
        result = migrate_one(project_root, row, force=force)
        if result.ok:
            tag = "migrated"
            if row.state == "cleanup_flat" or (row.state == "refuse_dirty" and row.dir_exists):
                tag = "flat removed (dir wins)"
            bak = f" (.bak: {result.bak_path.name})" if result.bak_path is not None else ""
            out.append(f"  ✓ {row.asset_type}/{row.name}: {tag}{bak}")
            successes += 1
        else:
            out.append(f"  x {row.asset_type}/{row.name}: {result.error}")
            failures += 1

    parts: list[str] = []
    if successes:
        parts.append(f"{successes} migrated")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failures:
        parts.append(f"{failures} failed")
    summary = ", ".join(parts) if parts else "0 actions"
    return "\n".join(out) + f"\n\nSummary: {summary}."


def _format_artifact_scope_result(result: MigrateScopeResult, *, apply_: bool) -> str:
    """Plain-text summary for one scope-tier move (mirrors the CLI's
    ``_print_migrate_scope_result``)."""
    layout_note = " (flat layout)" if result.layout == "flat" else ""
    lines = [
        f"Plan: migrate {result.kind}/{result.name}{layout_note}",
        f"  from {result.from_scope}: {result.src_path}",
        f"  to   {result.to_scope}: {result.dst_path}",
    ]
    if not apply_:
        if result.fanout_planned:
            lines.append(
                f"  will remove {len(result.fanout_planned)} stale runtime "
                f"fan-out target(s) at scope='{result.from_scope}' (content "
                f"that diverges from the canonical render is snapshotted to "
                f"a .bak first):"
            )
            lines.extend(f"    - {path}" for path in result.fanout_planned)
        lines.append("\nRe-call with apply=True to execute.")
        lines.append(
            f"After apply, run mem_context_sync(scope='{result.to_scope}') "
            "to refresh runtime fan-out."
        )
        return "\n".join(lines)

    lines.append(f"\n✓ moved {result.kind}/{result.name}: {result.from_scope} → {result.to_scope}")
    if result.fanout_cleaned:
        lines.append(f"  cleaned {len(result.fanout_cleaned)} stale runtime fan-out target(s):")
        lines.extend(f"    - {path}" for path in result.fanout_cleaned)
    if result.fanout_backed_up:
        lines.append(
            f"  {len(result.fanout_backed_up)} target(s) diverged from the "
            f"canonical render — snapshotted before removal (review and "
            f"delete manually):"
        )
        lines.extend(f"    - {path}" for path in result.fanout_backed_up)
    lines.append(
        f"\nNext: run mem_context_sync(scope='{result.to_scope}') "
        "to regenerate runtime fan-out at the new tier."
    )
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_artifact_migrate(
    asset_type: str = "",
    name: str = "",
    from_scope: str = "",
    to_scope: str = "",
    apply: bool = False,
    force: bool = False,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Migrate canonical context artifacts (agents / commands / skills).

    Mirrors the CLI ``mm context migrate`` verb's two modes, selected by
    whether ``to_scope`` is set (``cli/context_cmd.py:migrate_cmd``). Reuses the
    same pure functions (``classify_migrate`` / ``migrate_one`` /
    ``migrate_scope``) and the SAME gate semantics as the CLI — no logic is
    re-implemented. For *memory*-tier migration use ``mem_context_memory_migrate``.

    Two modes:

    * **Flat→dir layout** (``to_scope`` omitted) — converts pre-PR-C
      ``agents/<name>.md`` to ``agents/<name>/agent.md``. ``asset_type=""``
      (the default) batches agents + commands; ``"skills"`` is an informational
      no-op (skills are always directory layout). ``force=True`` (apply only)
      migrates dirty flat files, writing a ``.bak`` sibling each.
    * **Scope-tier move** (``to_scope`` set) — moves the canonical between
      ADR-0011 tiers (``user`` / ``project_shared`` / ``project_local``).
      Requires ``asset_type`` and ``name``. ``from_scope`` is auto-detected when
      omitted. Destinations always refuse on conflict (``force`` does not apply).

    Args:
        asset_type: ``agents`` | ``commands`` | ``skills``. Empty batches
            agents + commands (flat→dir mode only).
        name: Artifact name. Required for a scope-tier move; an optional filter
            for flat→dir.
        from_scope: Source tier for a scope-tier move (auto-detected if empty).
        to_scope: Target tier. Empty selects flat→dir mode; set selects
            scope-tier mode.
        apply: Execute the migration. Default ``False`` returns a dry-run
            preview, matching the context-tool family (``mem_context_sync`` /
            ``mem_context_memory_migrate``) rather than ``mem_ingest``'s
            ``dry_run`` convention.
        force: Flat→dir only — migrate dirty flat files (apply only). Rejected
            together with ``to_scope`` (scope-tier always refuses on conflict).
        confirm_project_shared: Required when ``to_scope='project_shared'`` and
            ``apply=True``; MCP cannot prompt, so a missing confirmation returns
            a ``needs confirmation`` message instead of touching disk.

    Refusals are prefixed (``error:`` / ``needs confirmation:`` / ``refused:`` /
    ``privacy block:``) so callers can branch on the prefix.
    """
    from memtomem.context.migrate import (
        MigratePartialError,
        SCOPE_MIGRATABLE_KINDS,
        classify_migrate,
        migrate_scope,
    )
    from memtomem.context.privacy_scan import PrivacyScanError

    asset = asset_type or None
    nm = name or None
    frm = from_scope or None
    to = to_scope or None

    # ── Shared validation gates (mirror cli migrate_cmd, including the
    #    flat-mode gates so scope inputs are never silently ignored). ──
    if force and not apply:
        return "error: force=True is only valid with apply=True."
    if nm is not None and asset is None:
        return "error: name requires asset_type."
    if asset == "memory":
        return (
            "error: memory migration is not handled here — use "
            "mem_context_memory_migrate(source=..., from_scope=..., to_scope=...)."
        )
    for label, value in (("from_scope", frm), ("to_scope", to)):
        if value is not None and value not in _KNOWN_ARTIFACT_SCOPES:
            return f"error: Unknown {label}='{value}'. Supported: {sorted(_KNOWN_ARTIFACT_SCOPES)}"

    project_root = await asyncio.to_thread(_find_project_root)

    # ── Scope-tier mode (to_scope set) ──
    if to is not None:
        if asset is None:
            return "error: to_scope requires asset_type."
        if asset not in SCOPE_MIGRATABLE_KINDS:
            return (
                f"error: to_scope is not supported for asset_type='{asset}' "
                f"(use one of {list(SCOPE_MIGRATABLE_KINDS)})."
            )
        if nm is None:
            return "error: name is required with to_scope."
        if force:
            return (
                "error: force does not apply to scope-tier moves "
                "(destinations always refuse on conflict)."
            )
        # Gate B: project_shared opt-in (apply only — dry-run never mutates).
        if to == "project_shared" and apply and not confirm_project_shared:
            return (
                "needs confirmation: to_scope='project_shared' moves the canonical "
                "into the git-tracked tier. Re-call with confirm_project_shared=True "
                "to proceed."
            )
        try:
            result = await asyncio.to_thread(
                migrate_scope,
                cast("ArtifactKind", asset),
                nm,
                from_scope=cast("TargetScope | None", frm),
                to_scope=cast(TargetScope, to),
                project_root=project_root,
                apply_=apply,
                surface="mcp_context_artifact_migrate",
            )
        except (FileNotFoundError, ValueError) as exc:
            return f"error: {exc}"
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"
        except MigratePartialError as exc:
            return f"error: {exc.message}"
        except click.ClickException as exc:
            return f"error: {exc.message}"

        out = _format_artifact_scope_result(result, apply_=apply)
        # project_local first-landing needs the gitignore marker (parity with
        # the CLI dispatch) so the local tier is not accidentally committed.
        # Surface the non-write states too: a caller MUST learn when .gitignore
        # protection was skipped, since project_local is meant to stay local.
        if apply and to == "project_local":
            from memtomem.cli.context_cmd import _append_gitignore_marker

            wrote, msg = await asyncio.to_thread(_append_gitignore_marker, project_root)
            if wrote:
                out += "\n  Appended .gitignore marker (.memtomem/*.local/, .memtomem/.staging/)."
            elif msg == "no_git_repo_pyproject_only":
                out += (
                    "\n  warning: project root resolved via pyproject.toml but `.git` "
                    "missing — .gitignore not appended. Run `git init` first to "
                    "git-protect the local tier."
                )
            elif msg == "no_project_signal":
                out += (
                    "\n  warning: no .git and no pyproject.toml in project root — "
                    ".gitignore append skipped; the project_local tier is not "
                    "git-protected."
                )
            # ``already_present`` is silent (marker already in place).
        return out

    # ── Flat→dir mode (to_scope omitted) ──
    if frm is not None:
        return "error: from_scope requires to_scope (scope-tier mode)."
    if confirm_project_shared:
        return "error: confirm_project_shared requires to_scope."
    if asset == "skills":
        return "skills are always directory layout (Agent Skills spec) — no migration needed."

    try:
        rows = await asyncio.to_thread(classify_migrate, project_root, asset, nm)
    except (FileNotFoundError, ValueError) as exc:
        return f"error: {exc}"

    skills_section = asset is None
    if not rows:
        note = (
            "\n(skills are always directory layout — no migration needed.)"
            if skills_section
            else ""
        )
        if nm is not None:
            return f"No matching asset to migrate (checked {asset}/{nm}).{note}"
        return f"No flat-layout assets to migrate.{note}"

    needs_force = [r for r in rows if r.state == "refuse_dirty"]
    actionable = [r for r in rows if r.state in {"migrate", "cleanup_flat"}]

    if not apply:
        return _format_artifact_flat_preview(rows, skills_section=skills_section)

    if needs_force and not force:
        return (
            f"refused: {len(needs_force)} entry(ies) have local edits since install; "
            "re-call with force=True to migrate (each dirty flat file gets a .bak sibling). "
            "No entry was written."
        )
    if not actionable and not (force and needs_force):
        return "Nothing to migrate."

    return await asyncio.to_thread(_run_artifact_flat_apply, project_root, rows, force=force)


# ── Cross-project / cross-tier transfer (ADR-0023, A-13 #1283) ──────────────
#
# Headless-agent parity for ``mm context copy`` / ``mm context move``
# (``cli/context_cmd.py:_transfer_dispatch``) and the web
# ``POST /context/{kind}/{name}/transfer`` route
# (``web/routes/context_transfer.py``). One ``@register`` action wrapping the
# A-2 transfer engine; ``asset_type="mcp-servers"`` rides the A-12 copy
# adapter exactly like the sibling surfaces. Destination projects are
# restricted to the registered discovery set, matched by ``scope_id`` — the
# typed-path consent valve is CLI-only (ADR-0023 §10: a human typing a path is
# consent; an agent passing one is not), and eligibility takes the web's
# stricter ``sync_eligible`` line for the same no-human-at-the-keyboard
# reason (the CLI lets a selected scan-only scope through as consent).

#: Whole-call pair-lock acquisition budget forwarded to the engine. Same
#: budget as the web route's ``_TRANSFER_LOCK_BUDGET_S``: an MCP agent cannot
#: Ctrl-C a stuck cross-process lock wait the way a CLI user can, so the
#: engine self-aborts (committing nothing on the lock-wait path) instead of
#: hanging the tool call.
_TRANSFER_LOCK_BUDGET_S = 30.0


def _format_transfer_result(result: TransferResult | McpServerCopyResult, *, apply_: bool) -> str:
    """Plain-text summary for one copy/move transfer.

    Mirrors the CLI's ``_print_transfer_result`` content: mode verb,
    copy-rename note, fan-out lists, the A-4 provenance carry outcome,
    engine ``notes``, and the engine's exact follow-up sync command
    (``sync_command``, else the prose ``sync_hint`` for results with no
    runnable command — mcp-servers fan-out is web-only). The dry-run
    footer additionally names the confirmation flag(s) the destination
    tier will require at apply time, so an agent learns the full
    re-call shape from the preview.
    """
    layout_note = (
        " (flat layout)" if result.layout == "flat" and result.kind != "mcp-servers" else ""
    )
    rename_note = f" as {result.dst_name}" if result.dst_name != result.name else ""
    lines = [
        f"Plan: {result.mode} {result.kind}/{result.name}{rename_note}{layout_note}",
        f"  from {result.from_scope}: {result.src_path}",
        f"  to   {result.to_scope}: {result.dst_path}",
    ]
    if not apply_:
        if result.fanout_planned:
            lines.append(
                f"  will remove {len(result.fanout_planned)} stale runtime "
                f"fan-out target(s) at scope='{result.from_scope}' (content "
                f"that diverges from the canonical render is snapshotted to "
                f"a .bak first):"
            )
            lines.extend(f"    - {path}" for path in result.fanout_planned)
        if result.provenance == "carried":
            lines.append(
                "  will carry the wiki install provenance (lock.json entry) to the destination"
            )
        elif result.provenance == "not_carried":
            lines.append(
                f"  install provenance will not be carried — the artifact "
                f"lands untracked: {result.provenance_reason}"
            )
        confirm_note = ""
        if result.to_scope == "project_shared":
            confirm_note = " and confirm_project_shared=True"
        elif result.to_scope == "user":
            confirm_note = " and allow_host_writes=True"
        lines.append(f"\nRe-call with apply=True{confirm_note} to execute.")
        if result.needs_sync and result.sync_command:
            lines.append(f"After apply, run `{result.sync_command}` to refresh runtime fan-out.")
        elif result.needs_sync and result.sync_hint:
            lines.append(f"After apply, {result.sync_hint}")
        return "\n".join(lines)

    verb = "moved" if result.mode == "move" else "copied"
    lines.append(
        f"\n✓ {verb} {result.kind}/{result.name}: "
        f"{result.from_scope} → {result.to_scope}{rename_note}"
    )
    if result.fanout_cleaned:
        lines.append(
            f"  cleaned {len(result.fanout_cleaned)} stale runtime fan-out "
            f"target(s) at scope='{result.from_scope}':"
        )
        lines.extend(f"    - {path}" for path in result.fanout_cleaned)
    if result.fanout_backed_up:
        lines.append(
            f"  {len(result.fanout_backed_up)} target(s) diverged from the "
            f"canonical render — snapshotted before removal (review and "
            f"delete manually):"
        )
        lines.extend(f"    - {path}" for path in result.fanout_backed_up)
    if result.provenance == "carried":
        lines.append("  carried the wiki install provenance (lock.json entry) to the destination")
    elif result.provenance == "not_carried":
        lines.append(
            f"  install provenance not carried — the artifact lands "
            f"untracked at the destination: {result.provenance_reason}"
        )
    lines.extend(f"  note: {note}" for note in result.notes)
    if result.needs_sync and result.sync_command:
        lines.append(
            f"\nNext: run `{result.sync_command}` to generate runtime fan-out at the destination."
        )
    elif result.needs_sync and result.sync_hint:
        lines.append(f"\nNext: {result.sync_hint}")
    return "\n".join(lines)


def _resolve_transfer_destination(to_project: str, src_root: Path) -> tuple[Path | None, str]:
    """Resolve ``to_project_scope_id`` against the registered discovery set.

    Returns ``(dst_root, "")`` on success or ``(None, refusal_text)`` —
    the caller returns the refusal verbatim. Runs in a worker thread
    (discovery walks the filesystem). Anchors discovery at the source
    project root, the A-9 subdir-anchor convention shared with the CLI
    ``--all-projects`` batch and the web lifespan.

    Eligibility is the web route's ``sync_eligible`` rule
    (``_reject_ineligible_destination``): both a paused and a
    never-enrolled (discovery-only) destination refuse, each with the
    remediation its state actually needs. The paused prose mirrors the
    CLI dispatch verbatim (sibling trust-UX wording).
    """
    from memtomem.cli.context_cmd import _projects_discover, _projects_gateway_cfg

    scope = next(
        (
            s
            for s in _projects_discover(_projects_gateway_cfg(), cwd=src_root)
            if s.scope_id == to_project
        ),
        None,
    )
    if scope is None:
        return None, (
            f"error: unknown to_project_scope_id: '{to_project}'. Destinations "
            f"are restricted to registered projects — list them with "
            f"`mm context projects list`."
        )
    if scope.root is None or scope.missing:
        return None, (f"error: scope '{to_project}' is registered but its root is missing.")
    if not scope.sync_eligible:
        if "known-projects" in scope.sources:
            return None, (
                f"refused: destination project {scope.scope_id} ({scope.root}) is "
                f"paused — sync enrollment is disabled, so the transferred "
                f"artifact would not fan out there. Run `mm context projects "
                f"resume {scope.scope_id}` first, or pick another destination."
            )
        return None, (
            f"refused: destination project {scope.scope_id} ({scope.root}) is "
            f"discovery-only (never enrolled for sync), so the transferred "
            f"artifact would not fan out there. Enroll it first with "
            f"`mm context projects add {scope.root}`, or pick another destination."
        )
    return scope.root, ""


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_artifact_transfer(
    asset_type: str = "",
    name: str = "",
    mode: str = "",
    from_scope: str = "",
    to_scope: str = "",
    to_project_scope_id: str = "",
    as_name: str = "",
    apply: bool = False,
    confirm_project_shared: bool = False,
    allow_host_writes: bool = False,
    ctx: CtxType = None,
) -> str:
    """Move or copy one canonical artifact between tiers and/or projects.

    Headless parity for ``mm context copy`` / ``mm context move`` and the
    web transfer endpoint — the same
    :func:`memtomem.context.transfer.transfer_artifact` engine call with
    the same gate semantics; no logic is re-implemented. The three verbs
    in one view: **move** consumes the source and cleans its stale
    runtime fan-out (destination fan-out is NOT generated — run the
    follow-up sync command the result prints); **copy** never touches
    the source and supports a renamed copy via ``as_name``;
    ``mem_context_artifact_migrate`` stays the within-project legacy
    verb (flat→dir layout adoption + tier moves).

    ``asset_type="mcp-servers"`` copies one MCP server definition to
    another project (A-12 adapter): copy-only, cross-project-only
    (``to_project_scope_id`` required), no ``as_name``, tiers pinned to
    ``project_shared``.

    Destination projects are restricted to the registered discovery set
    and matched by ``scope_id`` (``mm context projects list``) — the
    typed-path escape hatch is CLI-only, and paused or never-enrolled
    destinations refuse. Destination collisions always refuse (no force
    valve). A ``project_shared`` landing runs the privacy scan (Gate A)
    and requires ``confirm_project_shared=True`` with ``apply=True``
    (Gate B); a ``user``-tier landing is a host write outside any
    project root and requires ``allow_host_writes=True`` with
    ``apply=True``.

    Args:
        asset_type: ``agents`` | ``commands`` | ``skills`` |
            ``mcp-servers``. Required.
        name: Source artifact name. Required.
        mode: ``copy`` or ``move``. Required (``mcp-servers``: copy only).
        from_scope: Source tier (``user`` / ``project_shared`` /
            ``project_local``); auto-detected when omitted — pass it to
            disambiguate when the same name lives in multiple tiers.
        to_scope: Destination tier. Omitted: keeps the source tier (the
            common cross-project case). At least one of ``to_scope`` /
            ``to_project_scope_id`` is required.
        to_project_scope_id: Destination project — a ``p-<sha12>``
            scope_id from ``mm context projects list``. Omitted: the
            current project. Cannot be combined with ``to_scope='user'``
            (the user tier is global, not per-project).
        as_name: Name for the copy at the destination (copy only). The
            staged manifest's frontmatter ``name:`` is rewritten to
            match; overrides/ and frozen versions/ snapshots travel
            verbatim.
        apply: Execute the transfer. Default ``False`` returns a dry-run
            preview, matching the context-tool family.
        confirm_project_shared: Required when the destination tier is
            ``project_shared`` and ``apply=True``; MCP cannot prompt, so
            a missing confirmation returns a ``needs confirmation``
            message instead of touching disk.
        allow_host_writes: Required when the destination tier is
            ``user`` and ``apply=True`` — the canonical lands at a host
            path outside any project root (``~/.memtomem``). Re-call
            with ``allow_host_writes=True`` after surfacing the host
            path to the user.

    Refusals are prefixed (``error:`` for bad input, ``refused:`` for a
    legitimate state that stops the transfer — fix the state and
    re-call, ``needs confirmation:`` for a missing opt-in flag,
    ``privacy block:`` for Gate A hits) so callers can branch on the
    prefix.
    """
    from memtomem.context._names import InvalidNameError, validate_name
    from memtomem.context.mcp_servers import McpServerParseError
    from memtomem.context.mcp_servers_copy import copy_mcp_server
    from memtomem.context.migrate import (
        SCOPE_MIGRATABLE_KINDS,
        ArtifactNotFoundError,
        MigratePartialError,
        _detect_source_scope,
    )
    from memtomem.context.privacy_scan import PrivacyScanError
    from memtomem.context.transfer import TransferCollisionError, transfer_artifact

    asset = asset_type or None
    nm = name or None
    frm = from_scope or None
    to = to_scope or None
    to_project = to_project_scope_id or None
    new_name = as_name or None

    if mode not in ("copy", "move"):
        return "error: mode must be 'copy' or 'move'."
    if asset is None:
        return "error: asset_type is required (agents | commands | skills | mcp-servers)."
    if nm is None:
        return "error: name is required."

    # ── mcp-servers branch gates (A-12) — BEFORE the generic combinators so
    #    every refusal speaks mcp vocabulary (mirrors the CLI dispatch). ──
    is_mcp = asset == "mcp-servers"
    if is_mcp:
        if mode != "copy":
            return (
                "error: mcp-servers support copy only: the canonical is "
                "single-tier (project_shared), and a cross-project move would "
                "orphan the source project's .mcp.json fan-out. Copy it, then "
                "delete the source definition from its web panel if you meant "
                "move."
            )
        if new_name is not None:
            return (
                "error: as_name is not supported for mcp-servers; the copy "
                "keeps the server name (#1282 scope)."
            )
        for label, value in (("from_scope", frm), ("to_scope", to)):
            if value is not None and value != "project_shared":
                return (
                    f"error: {label}='{value}' is not valid for mcp-servers: "
                    f"the canonical is single-tier (project_shared) by design."
                )
        if to_project is None:
            return (
                "error: mcp-servers copy is cross-project only: pass "
                "to_project_scope_id (within one project the canonical "
                "already exists; there is no second tier to copy to)."
            )
        to = "project_shared"
    else:
        if asset not in SCOPE_MIGRATABLE_KINDS:
            return (
                f"error: unsupported asset_type='{asset}' for transfer "
                f"(use one of {list(SCOPE_MIGRATABLE_KINDS)} or 'mcp-servers')."
            )
        for label, value in (("from_scope", frm), ("to_scope", to)):
            if value is not None and value not in _KNOWN_ARTIFACT_SCOPES:
                return (
                    f"error: Unknown {label}='{value}'. Supported: {sorted(_KNOWN_ARTIFACT_SCOPES)}"
                )

    # ── Shared option-combination gates (mirror the CLI dispatch). ──
    if to is None and to_project is None:
        return (
            f"error: nothing to do: pass to_scope and/or to_project_scope_id "
            f"(a same-tier, same-project {mode} is a no-op)."
        )
    if to == "user" and to_project is not None:
        return (
            "error: to_project_scope_id cannot be combined with "
            "to_scope='user': the user tier is global (~/.memtomem), not "
            "per-project."
        )
    if new_name is not None and mode == "move":
        return "error: as_name is only valid with mode='copy' (renamed copy)."

    # Validate names BEFORE any path construction (A-3 Codex fold): the
    # to_scope-default pre-probe below builds candidate paths from the raw
    # name, and a traversal shape like ``../x`` must never reach a
    # filesystem probe.
    try:
        name_kind = "MCP server" if is_mcp else f"{asset[:-1]} name"
        validate_name(nm, kind=name_kind)
        if new_name is not None:
            validate_name(new_name, kind=name_kind)
    except InvalidNameError as exc:
        return f"error: {exc}"

    src_root = await asyncio.to_thread(_find_project_root)

    if to_project is not None:
        dst_root, refusal = await asyncio.to_thread(
            _resolve_transfer_destination, to_project, src_root
        )
        if dst_root is None:
            return refusal
    else:
        dst_root = src_root

    if is_mcp and dst_root.resolve() == src_root.resolve():
        return (
            "error: to_project_scope_id resolves to the source project; "
            "mcp-servers copy is cross-project only."
        )

    if to is None:
        # Tier default: keep the source tier. The engine re-detects under
        # its own lock; this pre-probe only resolves the default (and
        # shares the engine's missing/ambiguous error wording).
        try:
            detected, _src_path, _layout = await asyncio.to_thread(
                _detect_source_scope,
                cast("ArtifactKind", asset),
                nm,
                src_root,
                cast("TargetScope | None", frm),
            )
        except click.ClickException as exc:
            return f"error: {exc.message}"
        to = detected
        if to == "user" and to_project is not None:
            return (
                f"error: {asset}/{nm} lives at the user tier, which is global "
                f"— pass to_scope='project_shared' or to_scope='project_local' "
                f"to choose the tier it should land in inside the destination "
                f"project."
            )

    # #1274 parity: a cross-project destination must already be a memtomem
    # project — don't seed a half-initialized store into an arbitrary
    # registered directory. Within-project transfers keep migrate's
    # implicit-store behavior.
    if to_project is not None and to != "user" and not (dst_root / ".memtomem").is_dir():
        return (
            f"refused: destination project has no .memtomem/ store: {dst_root}. "
            f"Initialize it first: cd {dst_root} && mm context init"
        )

    # ── Tier-keyed confirmation gates (apply only — dry-run never gates and
    #    never writes). Gate B mirrors mem_context_artifact_migrate; the
    #    host-write gate mirrors the settings surfaces' allow_host_writes
    #    and the web transfer route's user-tier gate. ──
    if to == "project_shared" and apply and not confirm_project_shared:
        verb = "copies" if mode == "copy" else "moves"
        return (
            f"needs confirmation: to_scope='project_shared' {verb} the "
            f"canonical into the git-tracked tier. Re-call with "
            f"confirm_project_shared=True to proceed."
        )
    if to == "user" and apply and not allow_host_writes:
        return (
            "needs confirmation: to_scope='user' writes the canonical to a "
            "host path outside any project root (~/.memtomem). Re-call with "
            "allow_host_writes=True after surfacing the host path to the user."
        )

    try:
        result: TransferResult | McpServerCopyResult
        if is_mcp:
            result = await asyncio.to_thread(
                copy_mcp_server,
                nm,
                src_project_root=src_root,
                dst_project_root=dst_root,
                apply_=apply,
                surface="mcp_context_artifact_transfer",
                lock_timeout=_TRANSFER_LOCK_BUDGET_S,
            )
        else:
            result = await asyncio.to_thread(
                transfer_artifact,
                cast("ArtifactKind", asset),
                nm,
                src_project_root=src_root,
                from_scope=cast("TargetScope | None", frm),
                dst_project_root=None if to == "user" else dst_root,
                to_scope=cast(TargetScope, to),
                mode=cast("TransferMode", mode),
                apply_=apply,
                surface="mcp_context_artifact_transfer",
                new_name=new_name,
                lock_timeout=_TRANSFER_LOCK_BUDGET_S,
            )
    except TimeoutError:
        # Engine lock budget expired — commits nothing by construction.
        return (
            "error: transfer timed out waiting for the artifact lock — "
            "another sync or transfer may be in progress; nothing was "
            "written. Retry once it finishes."
        )
    except ArtifactNotFoundError as exc:
        return f"error: {exc.message}"
    except TransferCollisionError as exc:
        return f"refused: {exc.message}"
    except PrivacyScanError as exc:
        return f"privacy block: {exc.message}"
    except MigratePartialError as exc:
        return f"error: {exc.message}"
    except McpServerParseError as exc:
        return f"error: {exc}"
    except (FileNotFoundError, InvalidNameError, ValueError) as exc:
        return f"error: {exc}"
    except click.ClickException as exc:
        return f"error: {exc.message}"

    out = _format_transfer_result(result, apply_=apply)
    # project_local first-landing needs the gitignore marker at the
    # DESTINATION project (parity with the CLI dispatch, cross-project
    # aware). Surface the non-write states too — a caller MUST learn when
    # .gitignore protection was skipped.
    if apply and to == "project_local":
        from memtomem.cli.context_cmd import _append_gitignore_marker

        wrote, msg = await asyncio.to_thread(_append_gitignore_marker, dst_root)
        if wrote:
            out += "\n  Appended .gitignore marker (.memtomem/*.local/, .memtomem/.staging/)."
        elif msg == "no_git_repo_pyproject_only":
            out += (
                "\n  warning: project root resolved via pyproject.toml but `.git` "
                "missing — .gitignore not appended. Run `git init` first to "
                "git-protect the local tier."
            )
        elif msg == "no_project_signal":
            out += (
                "\n  warning: no .git and no pyproject.toml in project root — "
                ".gitignore append skipped; the project_local tier is not "
                "git-protected."
            )
        # ``already_present`` is silent (marker already in place).
    return out


# ── Version snapshots + label pointers (ADR-0022) ───────────────────────────
#
# Headless-agent parity for the ``mm context version`` CLI group
# (``cli/context_cmd.py``) and the web ``/context/{type}/{name}/versions`` +
# ``/labels/{label}`` routes (``web/routes/context_versions.py``). A *version*
# is an immutable snapshot of one artifact's working canonical; a *label*
# (``production`` / ``staging`` / …) is a movable pointer over versions, where a
# promote doubles as a rollback. Both tools reuse the SAME pure-filesystem
# ``context/versioning.py`` store the CLI and web routes use — no logic is
# re-implemented. agents + commands only (skills are tree-snapshot artifacts,
# deferred per ADR-0022 invariant 7).


def _resolve_version_artifact(
    artifact_type: str,
    project_root: Path,
    raw_name: str,
    scope: TargetScope,
) -> tuple[str, Path, Layout]:
    """Resolve ``(artifact_type, name, scope)`` → ``(name, working_file, layout)``.

    Mirrors the web router's ``_resolve_versionable`` (agents + commands only;
    skills / unknown types rejected — ADR-0022 invariant 7). Raises
    ``ValueError`` with a clean, path-free message for an unsupported type, an
    invalid name (``validate_name``'s ``InvalidNameError`` is a ``ValueError``),
    or a missing artifact — the caller catches it and returns ``error: …``.
    """
    from memtomem.context._names import validate_name
    from memtomem.context.agents import resolve_canonical_agent
    from memtomem.context.commands import resolve_canonical_command

    resolvers = {
        "agents": (resolve_canonical_agent, "agent"),
        "commands": (resolve_canonical_command, "command"),
    }
    entry = resolvers.get(artifact_type)
    if entry is None:
        raise ValueError(
            f"Versioning is not supported for {artifact_type!r} "
            f"(agents and commands only — skills are deferred, ADR-0022 invariant 7)."
        )
    resolver, kind = entry
    name = validate_name(raw_name, kind=kind)
    resolved = resolver(project_root, name, scope=scope)
    if resolved is None:
        raise ValueError(f"{kind} {name!r} not found")
    working_file, layout = resolved
    return name, working_file, layout


def _flat_layout_hint(artifact_type: str, name: str) -> str:
    """The shared "no version store on flat layout" remediation line (inv 3)."""
    return (
        f"{artifact_type}/{name} uses flat layout, which has no per-artifact version "
        f"store. Run mem_context_artifact_migrate(asset_type='{artifact_type}', "
        f"name='{name}') to convert it to directory layout first."
    )


def _format_version_list(
    artifact_type: str, name: str, scope: str, manifest: VersionsManifest
) -> str:
    """Plain-text version listing (newest first), mirroring the web GET shape
    and the CLI ``version list`` content."""
    if not manifest.versions:
        return f"{artifact_type}/{name} [{scope}]: no versions yet."
    # Reverse the label map so each version line shows the pointers that land
    # on it (tag → [labels]).
    labels_by_tag: dict[str, list[str]] = {}
    for label, tag in manifest.labels.items():
        labels_by_tag.setdefault(tag, []).append(label)
    lines = [f"{artifact_type}/{name} [{scope}] versions (newest first):"]
    for tag in sorted(manifest.versions, key=lambda t: int(t[1:]), reverse=True):
        rec = manifest.versions[tag]
        pointers = labels_by_tag.get(tag, [])
        suffix = f"  [{', '.join(sorted(pointers))}]" if pointers else ""
        note = f"  — {rec.note}" if rec.note else ""
        lines.append(f"  {tag:6s} {rec.created_at}{suffix}{note}")
    return "\n".join(lines)


def _format_label_result(headline: str, manifest: VersionsManifest) -> str:
    """Append the post-mutation label map to *headline* so the caller sees the
    full pointer state after a promote / delete (mirrors the web routes echoing
    ``labels`` in their response)."""
    if not manifest.labels:
        return f"{headline}\n  (no labels)"
    body = "\n".join(f"  {label} → {manifest.labels[label]}" for label in sorted(manifest.labels))
    return f"{headline}\nLabels:\n{body}"


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_version(
    artifact_type: str,
    name: str,
    action: str = "list",
    note: str = "",
    scope: str = "",
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """List or freeze per-artifact version snapshots (ADR-0022).

    Headless-agent parity for the ``mm context version list|create`` CLI
    commands and the web ``GET/POST /context/{type}/{name}/versions`` routes:
    freeze a known-good working canonical into an immutable ``versions/vN.md``
    snapshot, then point a label at it with ``mem_context_promote`` — so
    editing the canonical and deploying it become two acts with instant
    rollback. Covers ``agents`` and ``commands`` only (skills are directory-tree
    artifacts, deferred per ADR-0022 invariant 7).

    Args:
        artifact_type: ``agents`` or ``commands``. Any other type (including
            ``skills``) is rejected.
        name: Canonical artifact name (the directory under
            ``.memtomem/<type>/``).
        action: ``list`` (default, read-only) to show versions + label
            pointers, or ``create`` to freeze the current working canonical
            into a new ``vN`` snapshot.
        note: Optional annotation stored with a ``create`` snapshot; ignored
            for ``list``.
        scope: ADR-0011 canonical residency tier — ``project_shared``
            (default), ``user``, or ``project_local``. The user-tier and
            project_shared ``name`` have independent version histories and
            label maps (ADR-0022 Decision b); there is no cross-tier lookup.
        confirm_project_shared: Required for ``action="create"`` when
            ``scope="project_shared"`` is passed explicitly — the snapshot
            lands in the git-tracked tree and MCP cannot prompt, so a missing
            confirmation returns a ``needs confirmation`` line. The implicit
            default scope does not require it (mirrors ``mem_context_init``).

    A flat-layout artifact has no per-artifact ``versions/`` store (ADR-0022
    invariant 3): ``list`` returns a benign migrate-required hint and
    ``create`` refuses with a "run mem_context_artifact_migrate first" error.

    On ``create`` the snapshot bytes are privacy-scanned (Gate A, ADR-0011
    trust boundary): for ``project_shared`` a secret hard-refuses with a
    ``privacy block:`` line before ``versions/vN.md`` lands in git-tracked
    storage; ``user`` / ``project_local`` are permissive (the working file
    already holds the content locally). Mirrors the CLI ``version create``.
    Refusals are prefixed (``error:`` / ``needs confirmation:`` /
    ``privacy block:``) so callers can branch on the prefix.
    """
    action = (action or "").strip().lower()
    if action not in ("list", "create"):
        return f"error: unknown action {action!r}. Supported: list, create."

    root = await asyncio.to_thread(_find_project_root)
    scope_explicit = bool(scope.strip())
    try:
        artifact_scope = _resolve_artifact_mcp_scope(scope)
        name, working_file, layout = await asyncio.to_thread(
            _resolve_version_artifact, artifact_type, root, name, artifact_scope
        )
    except ValueError as exc:
        return f"error: {exc}"

    artifact_dir = working_file.parent

    if action == "list":
        if layout != "dir":
            # Benign hint, not an error — parity with the web read route's
            # ``migrate_required`` flag (the UI hints instead of erroring).
            return f"{_flat_layout_hint(artifact_type, name)} (no versions yet — migrate to start.)"
        try:
            manifest = await asyncio.to_thread(versioning.load_manifest, artifact_dir)
        except versioning.VersionError as exc:
            return f"error: {exc}"
        return _format_version_list(artifact_type, name, artifact_scope, manifest)

    # ── action == "create" ──
    # Gate B: an explicit project_shared write into the git-tracked tree needs
    # confirmation MCP cannot prompt for (mirrors mem_context_init / migrate).
    if scope_explicit and artifact_scope == "project_shared" and not confirm_project_shared:
        return (
            "needs confirmation: scope='project_shared' freezes a snapshot into the "
            "git-tracked tree. Re-call with confirm_project_shared=True to proceed."
        )
    if layout != "dir":
        return f"error: {_flat_layout_hint(artifact_type, name)}"

    # Gate A on the snapshot bytes (mirror cli version_create_cmd): read the
    # working canonical ONCE and snapshot the SAME bytes via ``source_bytes=`` so
    # a concurrent edit between scan and write cannot slip unscanned bytes into
    # versions/vN.md. ``raise_or_collect`` hard-refuses project_shared on a hit
    # and returns a (ignored) skip tuple for user / project_local — permissive
    # because the working file already holds the content locally.
    from memtomem.context.privacy_scan import (
        PrivacyScanError,
        raise_or_collect,
        scan_text_content,
    )

    try:
        snapshot_bytes = await asyncio.to_thread(working_file.read_bytes)
    except OSError as exc:
        return f"error: cannot read working canonical {working_file}: {exc}"
    file_scan = await asyncio.to_thread(
        lambda: scan_text_content(
            snapshot_bytes.decode("utf-8", errors="replace"),
            source_path=working_file,
            surface="mcp_context_version_create",
            scope=artifact_scope,
            project_root=root,
        )
    )
    if file_scan.decision in ("blocked", "blocked_project_shared"):
        try:
            raise_or_collect(
                file_scan, scope=artifact_scope, kind=artifact_type[:-1], artifact_name=name
            )
        except PrivacyScanError as exc:
            return f"privacy block: {exc.message}"

    try:
        record = await asyncio.to_thread(
            versioning.create_version,
            artifact_dir,
            working_file,
            note=note,
            source_bytes=snapshot_bytes,
        )
    except versioning.VersionError as exc:
        return f"error: {exc}"

    return (
        f"Created {artifact_type}/{name} version {record.tag} "
        f"[{artifact_scope}] at {record.created_at}" + (f" — {record.note}" if record.note else "")
    )


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_promote(
    artifact_type: str,
    name: str,
    label: str,
    version: str = "",
    delete: bool = False,
    scope: str = "",
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Move or drop a label pointer over an artifact's versions (ADR-0022).

    Headless-agent parity for ``mm context version promote`` and the web
    ``PUT/DELETE /context/{type}/{name}/labels/{label}`` routes. A *label*
    (``production`` / ``staging`` / …) is a movable pointer over the immutable
    versions created by ``mem_context_version``. Promote and rollback are the
    same act — both just move the pointer; pass ``delete=True`` to drop a label
    entirely. Covers ``agents`` and ``commands`` only.

    Moving a pointer only updates the manifest — it does NOT fan out to the
    runtimes by itself. Deploy the pointed-at version with
    ``mem_context_sync(include="agents", label="<label>", scope=...)`` (or the
    CLI ``mm context sync --label <label>``).

    Args:
        artifact_type: ``agents`` or ``commands``.
        name: Canonical artifact name.
        label: The label to move or drop (e.g. ``production``). The reserved
            ``latest`` (always the working file) and version-shaped names
            (``v1`` — reserved for direct version addressing) are rejected.
        version: Version tag to point ``label`` at (e.g. ``v2``). Required
            unless ``delete=True``.
        delete: Drop ``label`` from the manifest instead of moving it. A
            ``delete`` of an absent label is a no-op (still succeeds). Cannot
            be combined with ``version``.
        scope: ADR-0011 canonical residency tier — ``project_shared``
            (default), ``user``, or ``project_local`` (independent label maps
            per tier, ADR-0022 Decision b).
        confirm_project_shared: Required when ``scope="project_shared"`` is
            passed explicitly — the label map (``versions.json``) is
            git-tracked and MCP cannot prompt. The implicit default scope does
            not require it (mirrors ``mem_context_init``).

    A flat-layout artifact has no version store (ADR-0022 invariant 3) and is
    refused. Refusals are prefixed (``error:`` / ``needs confirmation:``) so
    callers can branch on the prefix.
    """
    if delete and version.strip():
        return "error: delete=True drops the label and takes no version."
    if not delete and not version.strip():
        return (
            "error: version is required to promote (e.g. version='v2'), "
            "or pass delete=True to drop the label."
        )

    root = await asyncio.to_thread(_find_project_root)
    scope_explicit = bool(scope.strip())
    # Resolve scope + artifact BEFORE the confirm gate (matches
    # mem_context_version's order) — telling the caller the artifact is missing
    # or flat is more useful than asking them to confirm a write that cannot
    # land. Neither resolution step mutates disk.
    try:
        artifact_scope = _resolve_artifact_mcp_scope(scope)
        name, working_file, layout = await asyncio.to_thread(
            _resolve_version_artifact, artifact_type, root, name, artifact_scope
        )
    except ValueError as exc:
        return f"error: {exc}"

    # Gate B: explicit project_shared write into the git-tracked label map.
    if scope_explicit and artifact_scope == "project_shared" and not confirm_project_shared:
        return (
            "needs confirmation: scope='project_shared' moves a pointer in the "
            "git-tracked label map. Re-call with confirm_project_shared=True to proceed."
        )

    if layout != "dir":
        return f"error: {_flat_layout_hint(artifact_type, name)}"
    artifact_dir = working_file.parent

    try:
        if delete:
            await asyncio.to_thread(versioning.delete_label, artifact_dir, label)
            manifest = await asyncio.to_thread(versioning.load_manifest, artifact_dir)
            return _format_label_result(
                f"Dropped label {label!r} from {artifact_type}/{name} [{artifact_scope}]",
                manifest,
            )
        await asyncio.to_thread(versioning.promote_label, artifact_dir, label, version)
        manifest = await asyncio.to_thread(versioning.load_manifest, artifact_dir)
        return _format_label_result(
            f"Promoted {artifact_type}/{name} [{artifact_scope}]: {label} → {version}",
            manifest,
        )
    except versioning.VersionError as exc:
        return f"error: {exc}"
