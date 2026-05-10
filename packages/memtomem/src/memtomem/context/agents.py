"""Canonical ⇄ runtime sub-agent fan-out.

Phase 2 of the "memtomem as canonical context gateway" plan. A sub-agent lives
at ``.memtomem/agents/<name>.md`` with YAML frontmatter (Claude Code-compatible
superset) and a Markdown body that acts as the system prompt. From that single
canonical source we fan out to:

* ``.claude/agents/<name>.md`` — Claude Code (project-scope)
* ``.gemini/agents/<name>.md`` — Gemini CLI (project-scope)
* ``.codex/agents/<name>.toml`` — OpenAI Codex CLI (project-scope)

Codex CLI accepts both ``~/.codex/agents/`` (user-scope) and ``.codex/agents/``
(project-scope) per the official subagents docs. memtomem fans out to the
project-scope path so a single repository's `.memtomem/agents/` source tree
stays contained within the project — no host-home pollution, worktrees isolate
naturally, and the layout matches Claude / Gemini.

Unlike Phase 1 skills, sub-agents have genuine format divergence:

* Claude and Gemini share Markdown + YAML frontmatter but disagree on fields
  (Gemini has no ``isolation``/``skills``, Claude has no ``kind``/``temperature``).
* Codex uses a TOML schema (``name``, ``description``, ``developer_instructions``,
  ``model``, ...) — our Markdown body becomes ``developer_instructions``. Tools
  are dropped because Codex models capabilities through ``mcp_servers`` +
  ``skills.config`` rather than a flat tool list.

Every conversion reports its ``dropped`` fields so the user can see what was
lost. ``--strict`` promotes any drop to an error. Nested Claude fields
(``hooks``, ``codex.*`` overrides, full ``mcp_servers`` tables) are out of
scope for Phase 2 — the canonical frontmatter is intentionally flat.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._atomic import atomic_write_bytes, atomic_write_text
from memtomem.config import TargetScope
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, Layout, validate_name
from memtomem.context._runtime_targets import runtime_fanout_root

logger = logging.getLogger(__name__)

CANONICAL_AGENT_ROOT = ".memtomem/agents"
AGENT_DIR_FILENAME = "agent.md"


def canonical_agent_name(path: Path, layout: Layout) -> str:
    """Single source of truth for agent path → name dispatch.

    Used by :func:`list_canonical_agents` consumers, the web routes import
    handler, and any other place that needs the canonical name without
    re-implementing the layout fallback. The brittle
    ``path.name == "agent.md"`` heuristic is intentionally avoided —
    callers must pass the layout tag they got from
    :func:`list_canonical_agents` or :func:`extract_agents_to_canonical`.
    """
    return path.parent.name if layout == "dir" else path.stem


# Reuse the same frontmatter regex used by the markdown chunker so canonical
# agent files parse consistently with the rest of memtomem.
_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)
_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$")


# ── Canonical dataclass ──────────────────────────────────────────────


@dataclass
class SubAgent:
    """In-memory canonical representation of a sub-agent.

    Fields mirror the intersection/union of Claude Code and Gemini CLI
    sub-agent schemas; Codex-specific keys are derived at render time.
    """

    name: str
    description: str
    body: str  # system prompt (markdown)
    tools: list[str] = field(default_factory=list)
    model: str | None = None
    skills: list[str] = field(default_factory=list)
    isolation: str | None = None
    kind: str | None = None
    temperature: float | None = None


class AgentParseError(ValueError):
    """Raised when a canonical agent file cannot be parsed."""


# ── Minimal flat-YAML parser ─────────────────────────────────────────


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_flat_yaml(text: str) -> dict[str, Any]:
    """Parse a minimal flat YAML subset.

    Supported forms:

    * ``key: value`` (string / number / bool)
    * ``key: [a, b, c]`` (inline list)
    * ``key:`` followed by indented ``  - item`` lines (block list)

    Nested dicts, anchors, multi-doc separators, and other advanced YAML
    features are **not** supported — unsupported lines are silently skipped.
    That is intentional for Phase 2 so we don't take a pyyaml dependency.
    """
    result: dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        m = _KEY_VALUE_RE.match(line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()

        if value == "":
            # Possibly a block list.
            block_items: list[str] = []
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip().startswith("- "):
                    block_items.append(_strip_quotes(nxt.strip()[2:].strip()))
                    j += 1
                elif nxt.strip() == "":
                    j += 1
                    continue
                else:
                    break
            if block_items:
                result[key] = block_items
                i = j
                continue
            result[key] = None
            i += 1
            continue

        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            items = [_strip_quotes(tok.strip()) for tok in inner.split(",") if tok.strip()]
            result[key] = items
            i += 1
            continue

        result[key] = _strip_quotes(value)
        i += 1
    return result


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


_KNOWN_AGENT_KEYS = frozenset(
    {"name", "description", "tools", "model", "skills", "isolation", "kind", "temperature"}
)


def parse_canonical_agent(path: Path, *, layout: Layout = "flat") -> SubAgent:
    """Parse a canonical agent file into a :class:`SubAgent`.

    ``layout`` selects the default-name fallback when the frontmatter omits
    ``name``: ``"flat"`` (legacy ``agents/<name>.md``) uses ``path.stem``;
    ``"dir"`` (ADR-0008 ``agents/<name>/agent.md``) uses
    ``path.parent.name``. Callers normally get ``layout`` from
    :func:`list_canonical_agents`.
    """
    content = path.read_text(encoding="utf-8")
    # Normalize CRLF → LF so ``_FRONT_MATTER_RE`` (which anchors on ``\n``) matches
    # files authored on Windows or by editors that emit CRLF.
    content = content.replace("\r\n", "\n")
    m = _FRONT_MATTER_RE.match(content)
    if not m:
        raise AgentParseError(f"missing YAML frontmatter: {path}")
    frontmatter = _parse_flat_yaml(m.group(1))

    unknown = sorted(set(frontmatter) - _KNOWN_AGENT_KEYS)
    if unknown:
        logger.warning("unknown frontmatter keys %s in %s (ignored)", unknown, path)

    body = content[m.end() :].lstrip("\n").rstrip() + "\n"

    default_name = path.parent.name if layout == "dir" else path.stem
    name = frontmatter.get("name") or default_name
    try:
        name = validate_name(str(name), kind="agent name")
    except InvalidNameError as exc:
        raise AgentParseError(f"{exc} (source: {path})") from exc
    description = frontmatter.get("description") or ""
    return SubAgent(
        name=name,
        description=str(description),
        body=body,
        tools=_coerce_list(frontmatter.get("tools")),
        model=(str(frontmatter["model"]) if frontmatter.get("model") else None),
        skills=_coerce_list(frontmatter.get("skills")),
        isolation=(str(frontmatter["isolation"]) if frontmatter.get("isolation") else None),
        kind=(str(frontmatter["kind"]) if frontmatter.get("kind") else None),
        temperature=_coerce_float(frontmatter.get("temperature")),
    )


def list_canonical_agents(project_root: Path) -> list[tuple[Path, Layout]]:
    """Enumerate canonical agents in both flat and directory layouts.

    Flat layout (legacy): ``agents/<name>.md``. Directory layout (ADR-0008
    PR-C+): ``agents/<name>/agent.md``. When the same name has both forms,
    the directory layout wins and a WARNING is logged so the silent flat
    file is visible. ``mm context migrate`` (PR-D) is the supported way
    to consolidate.
    """
    root = project_root / CANONICAL_AGENT_ROOT
    if not root.is_dir():
        return []

    flat: dict[str, Path] = {p.stem: p for p in sorted(root.glob("*.md")) if p.is_file()}
    dirs: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            agent_md = entry / AGENT_DIR_FILENAME
            if agent_md.is_file():
                dirs[entry.name] = agent_md

    for name in sorted(set(flat) & set(dirs)):
        logger.warning(
            "agents/%s: both flat (%s.md) and dir (%s/agent.md) layouts present; "
            "using dir. Remove the flat file or run `mm context migrate` (PR-D).",
            name,
            name,
            name,
        )

    merged_paths = {**flat, **dirs}  # dir overrides flat on collision
    layouts: dict[str, Layout] = {**dict.fromkeys(flat, "flat"), **dict.fromkeys(dirs, "dir")}
    return [(merged_paths[k], layouts[k]) for k in sorted(merged_paths)]


# ── Renderers ────────────────────────────────────────────────────────


def _yaml_inline_list(items: list[str]) -> str:
    return "[" + ", ".join(items) + "]"


def _render_markdown_agent(
    agent: SubAgent,
    include_fields: list[str],
) -> str:
    """Render an agent as Markdown + YAML frontmatter, emitting only the
    frontmatter keys listed in ``include_fields`` (in order)."""
    lines: list[str] = [f"name: {agent.name}", f"description: {agent.description}"]
    for key in include_fields:
        if key in ("name", "description"):
            continue
        if key == "tools" and agent.tools:
            lines.append(f"tools: {_yaml_inline_list(agent.tools)}")
        elif key == "model" and agent.model:
            lines.append(f"model: {agent.model}")
        elif key == "skills" and agent.skills:
            lines.append(f"skills: {_yaml_inline_list(agent.skills)}")
        elif key == "isolation" and agent.isolation:
            lines.append(f"isolation: {agent.isolation}")
        elif key == "kind" and agent.kind:
            lines.append(f"kind: {agent.kind}")
        elif key == "temperature" and agent.temperature is not None:
            lines.append(f"temperature: {agent.temperature}")
    frontmatter = "\n".join(lines)
    body = agent.body if agent.body.endswith("\n") else agent.body + "\n"
    return f"---\n{frontmatter}\n---\n\n{body}"


_CLAUDE_FIELDS = ["tools", "model", "skills", "isolation"]
_GEMINI_FIELDS = ["tools", "model", "kind", "temperature"]


def _subagent_to_claude_md(agent: SubAgent) -> tuple[str, list[str]]:
    dropped: list[str] = []
    if agent.kind is not None:
        dropped.append("kind")
    if agent.temperature is not None:
        dropped.append("temperature")
    return _render_markdown_agent(agent, _CLAUDE_FIELDS), dropped


def _subagent_to_gemini_md(agent: SubAgent) -> tuple[str, list[str]]:
    dropped: list[str] = []
    if agent.skills:
        dropped.append("skills")
    if agent.isolation is not None:
        dropped.append("isolation")
    return _render_markdown_agent(agent, _GEMINI_FIELDS), dropped


# ── TOML writer (hand-rolled, no pyyaml / tomli-w dependency) ────────


def _toml_escape_basic_string(s: str) -> str:
    """Escape ``s`` for a TOML basic (single-line, ``"``-delimited) string.

    TOML basic strings require ``\\b \\t \\n \\f \\r \\" \\\\`` for those
    characters and ``\\uXXXX`` for any other C0 control or DEL. Leaving raw
    control chars produces TOML that ``tomllib.loads`` rejects.
    """
    out: list[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        else:
            code = ord(ch)
            if code < 0x20 or code == 0x7F:
                out.append(f"\\u{code:04x}")
            else:
                out.append(ch)
    return "".join(out)


def _toml_escape_multiline_string(s: str) -> str:
    """Escape ``s`` for a TOML multi-line basic (``\"\"\"``-delimited) string.

    Literal newlines and tabs are permitted; ``\\r`` and other C0 controls
    still need escaping, and any stray ``\"\"\"`` must be broken up.
    """
    out: list[str] = []
    for ch in s:
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n" or ch == "\t":
            out.append(ch)
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch == "\r":
            out.append("\\r")
        else:
            code = ord(ch)
            if code < 0x20 or code == 0x7F:
                out.append(f"\\u{code:04x}")
            else:
                out.append(ch)
    return "".join(out).replace('"""', '""\\"')


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if "\n" in value:
            return f'"""\n{_toml_escape_multiline_string(value)}"""'
        return f'"{_toml_escape_basic_string(value)}"'
    raise TypeError(f"unsupported TOML scalar: {type(value).__name__}")


def _subagent_to_codex_toml(agent: SubAgent) -> tuple[str, list[str]]:
    dropped: list[str] = []
    if agent.tools:
        dropped.append("tools")
    if agent.skills:
        dropped.append("skills")
    if agent.isolation is not None:
        dropped.append("isolation")
    if agent.kind is not None:
        dropped.append("kind")
    if agent.temperature is not None:
        dropped.append("temperature")

    parts: list[str] = [
        f"name = {_toml_scalar(agent.name)}",
        f"description = {_toml_scalar(agent.description)}",
        f"developer_instructions = {_toml_scalar(agent.body.rstrip())}",
    ]
    if agent.model:
        parts.append(f"model = {_toml_scalar(agent.model)}")
    return "\n".join(parts) + "\n", dropped


# ── Generator registry ───────────────────────────────────────────────


class AgentGenerator(Protocol):
    """Protocol for runtime-specific sub-agent generators.

    ``target_file`` accepts an ADR-0011 ``scope`` keyword (default
    ``project_shared`` preserves pre-PR-E behavior). Returns ``None``
    when the (runtime, scope) tuple has no fan-out by design — see
    :data:`memtomem.context._runtime_targets.RUNTIME_FANOUT_TABLE`.
    Callers must handle ``None`` and emit
    ``skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME``.
    """

    name: str

    def target_file(
        self,
        project_root: Path,
        agent_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        """Return the file that should hold the rendered agent (or ``None``)."""
        ...

    def render(self, agent: SubAgent) -> tuple[str, list[str]]:
        """Return ``(file_content, dropped_field_names)``."""
        ...


AGENT_GENERATORS: dict[str, AgentGenerator] = {}


def _register(gen: AgentGenerator) -> AgentGenerator:
    AGENT_GENERATORS[gen.name] = gen
    return gen


@dataclass
class ClaudeAgentsGenerator:
    name: str = "claude_agents"
    output_root: str = ".claude/agents"

    def target_file(
        self,
        project_root: Path,
        agent_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("agents", "claude", scope, project_root)
        return None if root is None else root / f"{agent_name}.md"

    def render(self, agent: SubAgent) -> tuple[str, list[str]]:
        return _subagent_to_claude_md(agent)


@dataclass
class GeminiAgentsGenerator:
    name: str = "gemini_agents"
    output_root: str = ".gemini/agents"

    def target_file(
        self,
        project_root: Path,
        agent_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("agents", "gemini", scope, project_root)
        return None if root is None else root / f"{agent_name}.md"

    def render(self, agent: SubAgent) -> tuple[str, list[str]]:
        return _subagent_to_gemini_md(agent)


@dataclass
class CodexAgentsGenerator:
    name: str = "codex_agents"
    output_root: str = ".codex/agents"

    def target_file(
        self,
        project_root: Path,
        agent_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("agents", "codex", scope, project_root)
        return None if root is None else root / f"{agent_name}.toml"

    def render(self, agent: SubAgent) -> tuple[str, list[str]]:
        return _subagent_to_codex_toml(agent)


_register(ClaudeAgentsGenerator())
_register(GeminiAgentsGenerator())
_register(CodexAgentsGenerator())


# ── Fan-out: canonical → runtimes ───────────────────────────────────


@dataclass
class AgentSyncResult:
    generated: list[tuple[str, Path]]  # (runtime, target_file)
    dropped: list[tuple[str, str, list[str]]]  # (runtime, agent_name, dropped_fields)
    # (runtime_or_agent, human_reason, reason_code) — see :mod:`memtomem.context._skip_reasons`.
    skipped: list[tuple[str, str, skip_codes.SkipCode]]


@dataclass
class ExtractResult:
    """Result of a reverse (runtime → canonical) import.

    Each entry in ``imported`` is ``(path, layout)`` so consumers can use
    :func:`canonical_agent_name` without re-deriving the layout from the
    path. ``layout`` is whichever form the canonical now lives in on disk
    (preserving an existing flat file or writing a new dir entry — see
    :func:`_resolve_agent_extract_target`).
    """

    imported: list[tuple[Path, Layout]]
    # (item_name, human_reason, reason_code) — see :mod:`memtomem.context._skip_reasons`.
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = field(default_factory=list)


class StrictDropError(ValueError):
    """Raised under ``strict=True`` / ``on_drop="error"`` when a conversion would drop fields."""


# Valid severity levels for the ``on_drop`` parameter.
ON_DROP_LEVELS = ("ignore", "warn", "error")


def generate_all_agents(
    project_root: Path,
    runtimes: list[str] | None = None,
    strict: bool = False,
    on_drop: str = "ignore",
) -> AgentSyncResult:
    """Fan out every canonical sub-agent to the requested runtimes.

    Args:
        on_drop: Severity when fields are dropped during conversion.
            ``"ignore"`` (default) — silently record in ``result.dropped``.
            ``"warn"``  — log a warning per dropped-field set.
            ``"error"`` — raise :class:`StrictDropError` immediately.
        strict: Legacy alias for ``on_drop="error"``. If *both* are supplied,
            ``on_drop`` takes precedence unless it is still the default.
    """
    # Resolve legacy ``strict`` flag.
    effective_drop = on_drop if on_drop != "ignore" or not strict else "error"

    generated: list[tuple[str, Path]] = []
    dropped: list[tuple[str, str, list[str]]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []

    canonicals = list_canonical_agents(project_root)
    if not canonicals:
        return AgentSyncResult(
            generated=[],
            dropped=[],
            skipped=[("<all>", "no canonical agents", skip_codes.NO_CANONICAL_ROOT)],
        )

    targets = runtimes if runtimes is not None else list(AGENT_GENERATORS.keys())
    for target in targets:
        gen = AGENT_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime", skip_codes.UNKNOWN_RUNTIME))
            continue
        for agent_path, layout in canonicals:
            try:
                agent = parse_canonical_agent(agent_path, layout=layout)
            except AgentParseError as exc:
                skipped.append((agent_path.name, f"parse error: {exc}", skip_codes.PARSE_ERROR))
                continue
            content, dropped_fields = gen.render(agent)
            if dropped_fields:
                if effective_drop == "error":
                    raise StrictDropError(
                        f"strict mode: {target} would drop {dropped_fields} from '{agent.name}'"
                    )
                if effective_drop == "warn":
                    logger.warning("%s dropped %s from '%s'", target, dropped_fields, agent.name)
            out_path = gen.target_file(project_root, agent.name)
            # ADR-0011 PR-E: target_file may return None for scopes with no
            # fan-out by design. Default kwarg is project_shared (existing
            # behavior), which never returns None — so this branch is
            # currently unreachable, but the assertion makes the contract
            # explicit for E2/E3 callers that pass scope= kwargs.
            assert out_path is not None, (
                f"{target} target_file returned None for default project_shared scope"
            )
            atomic_write_text(out_path, content)
            # ADR-0008 Invariant 4: per-vendor override replaces the runtime file.
            # Race: see PR-D' for the unified write path that closes the
            # canonical→override window. Same pattern as skills.py:213-220.
            vendor = GENERATOR_VENDOR.get(target)
            if vendor is not None:
                # ADR-0011 PR-E: pin scope=project_shared so default fan-out
                # never picks up a draft project_local override (narrow→broad
                # is intended for explicit cross-tier reads, not the default
                # project_shared sync surface). E3 will thread the resolved
                # scope through when sync becomes scope-aware.
                override_path = _override.resolve(
                    project_root, "agents", agent.name, vendor, scope="project_shared"
                )
                if override_path is not None:
                    atomic_write_bytes(out_path, override_path.read_bytes())
            generated.append((target, out_path))
            if dropped_fields:
                dropped.append((target, agent.name, dropped_fields))

    return AgentSyncResult(generated=generated, dropped=dropped, skipped=skipped)


# ── Reverse: runtime → canonical ────────────────────────────────────


def _resolve_agent_extract_target(canonical_root: Path, agent_name: str) -> tuple[Path, Layout]:
    """Decide where reverse-sync writes the canonical for ``agent_name``.

    Truth table (ADR-0008 PR-C):
      dir+flat both → dir wins, flat is silently divergent → WARN
      dir only      → dir
      flat only     → flat (preserve existing layout; PR-C does not migrate)
      neither       → dir (ADR-0008 layout for new agents)
    """
    dir_target = canonical_root / agent_name / AGENT_DIR_FILENAME
    flat_target = canonical_root / f"{agent_name}.md"
    has_dir = dir_target.is_file()
    has_flat = flat_target.is_file()
    if has_dir and has_flat:
        logger.warning(
            "agents/%s: reverse-sync updates dir layout (%s/agent.md); the flat "
            "file (%s.md) is now silently divergent. Remove it or run "
            "`mm context migrate` (PR-D).",
            agent_name,
            agent_name,
            agent_name,
        )
        return dir_target, "dir"
    if has_dir:
        return dir_target, "dir"
    if has_flat:
        return flat_target, "flat"
    return dir_target, "dir"


def extract_agents_to_canonical(
    project_root: Path,
    overwrite: bool = False,
    only_name: str | None = None,
) -> ExtractResult:
    """Import existing Claude / Gemini agent files into ``.memtomem/agents/``.

    Codex TOML is **not** imported (one-way conversion; too lossy to round-trip
    without reconstructing fields we dropped on the way out). First occurrence
    wins across runtimes (Claude before Gemini — deterministic order).

    Returns an :class:`ExtractResult` with both imported paths and skipped
    items so the caller can warn the user about silent deduplication.

    When ``only_name`` is set, every runtime file with a different stem is
    silently skipped before any validation/dedupe work. Callers (e.g. the
    single-item import route) can detect "no such runtime artifact" by
    inspecting an empty ``imported`` + ``skipped``.

    Layout policy: new agents (no existing canonical) land in directory
    layout per ADR-0008. Existing flat-layout entries are preserved by
    PR-C — migration to directory layout is a separate command (PR-D).
    """
    canonical_root = project_root / CANONICAL_AGENT_ROOT
    imported: list[tuple[Path, Layout]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []
    seen: dict[str, str] = {}  # agent_name → first runtime label

    for runtime_dir in (
        project_root / ".claude/agents",
        project_root / ".gemini/agents",
    ):
        if not runtime_dir.is_dir():
            continue
        runtime_label = runtime_dir.relative_to(project_root).as_posix()
        for md_file in sorted(runtime_dir.glob("*.md")):
            agent_name = md_file.stem
            if only_name is not None and agent_name != only_name:
                continue
            try:
                validate_name(agent_name, kind="agent name")
            except InvalidNameError as exc:
                skipped.append((agent_name, f"invalid name: {exc}", skip_codes.INVALID_NAME))
                logger.warning(
                    "skip %r from %s: invalid name",
                    agent_name,
                    runtime_label,
                )
                continue
            if agent_name in seen:
                reason = f"already imported from {seen[agent_name]}"
                skipped.append((agent_name, reason, skip_codes.ALREADY_IMPORTED))
                logger.warning("skip %s from %s: %s", agent_name, runtime_label, reason)
                continue
            dst, layout = _resolve_agent_extract_target(canonical_root, agent_name)
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((agent_name, reason, skip_codes.CANONICAL_EXISTS))
                logger.warning("skip %s from %s: %s", agent_name, runtime_label, reason)
                seen[agent_name] = runtime_label
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(dst, md_file.read_bytes())
            imported.append((dst, layout))
            seen[agent_name] = runtime_label

    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ──────────────────────────────────────


def _runtime_agent_names(gen_name: str, project_root: Path) -> set[str]:
    if gen_name == "codex_agents":
        runtime_root = project_root / ".codex/agents"
        suffix = ".toml"
    elif gen_name == "claude_agents":
        runtime_root = project_root / ".claude/agents"
        suffix = ".md"
    elif gen_name == "gemini_agents":
        runtime_root = project_root / ".gemini/agents"
        suffix = ".md"
    else:
        return set()
    if not runtime_root.is_dir():
        return set()
    return {p.stem for p in runtime_root.iterdir() if p.is_file() and p.suffix == suffix}


def diff_agents(project_root: Path) -> list[tuple[str, str, str]]:
    """Compare canonical agents against every registered runtime.

    Returns a list of ``(runtime, agent_name, status)`` where status is one of
    ``"in sync"``, ``"out of sync"``, ``"missing target"``, ``"missing canonical"``,
    ``"parse error"``.
    """
    results: list[tuple[str, str, str]] = []
    canonical_index = {
        path.parent.name if layout == "dir" else path.stem: (path, layout)
        for path, layout in list_canonical_agents(project_root)
    }
    canonical_names = set(canonical_index)

    for gen_name, gen in AGENT_GENERATORS.items():
        runtime_names = _runtime_agent_names(gen_name, project_root)
        for name in sorted(canonical_names | runtime_names):
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
                continue
            if name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
                continue

            src, layout = canonical_index[name]
            try:
                agent = parse_canonical_agent(src, layout=layout)
            except AgentParseError:
                results.append((gen_name, name, "parse error"))
                continue
            expected, _ = gen.render(agent)
            target = gen.target_file(project_root, name)
            assert target is not None  # ADR-0011 PR-E: default scope=project_shared never None
            actual = target.read_text(encoding="utf-8") if target.is_file() else ""
            if expected.strip() == actual.strip():
                results.append((gen_name, name, "in sync"))
            else:
                results.append((gen_name, name, "out of sync"))

    return results


__all__ = [
    "AGENT_DIR_FILENAME",
    "AGENT_GENERATORS",
    "AgentGenerator",
    "AgentParseError",
    "AgentSyncResult",
    "CANONICAL_AGENT_ROOT",
    "ExtractResult",
    "ClaudeAgentsGenerator",
    "CodexAgentsGenerator",
    "GeminiAgentsGenerator",
    "Layout",
    "ON_DROP_LEVELS",
    "StrictDropError",
    "SubAgent",
    "canonical_agent_name",
    "diff_agents",
    "extract_agents_to_canonical",
    "generate_all_agents",
    "list_canonical_agents",
    "parse_canonical_agent",
]
