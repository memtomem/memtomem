"""Canonical ⇄ runtime sub-agent fan-out.

Phase 2 of the "memtomem as canonical context gateway" plan. A sub-agent lives
at ``.memtomem/agents/<name>.md`` with YAML frontmatter (Claude Code-compatible
superset) and a Markdown body that acts as the system prompt. From that single
canonical source we fan out to:

* ``.claude/agents/<name>.md`` — Claude Code (project-scope)
* ``.gemini/agents/<name>.md`` — Gemini CLI (project-scope; experimental in 2026-03)
* ``~/.codex/agents/<name>.toml`` — OpenAI Codex CLI (**user-scope only**)

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

logger = logging.getLogger(__name__)

CANONICAL_AGENT_ROOT = ".memtomem/agents"

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


def parse_canonical_agent(path: Path) -> SubAgent:
    """Parse a canonical agent file into a :class:`SubAgent`."""
    content = path.read_text(encoding="utf-8")
    m = _FRONT_MATTER_RE.match(content)
    if not m:
        raise AgentParseError(f"missing YAML frontmatter: {path}")
    frontmatter = _parse_flat_yaml(m.group(1))
    body = content[m.end() :].lstrip("\n").rstrip() + "\n"

    name = frontmatter.get("name") or path.stem
    description = frontmatter.get("description") or ""
    return SubAgent(
        name=str(name),
        description=str(description),
        body=body,
        tools=_coerce_list(frontmatter.get("tools")),
        model=(str(frontmatter["model"]) if frontmatter.get("model") else None),
        skills=_coerce_list(frontmatter.get("skills")),
        isolation=(str(frontmatter["isolation"]) if frontmatter.get("isolation") else None),
        kind=(str(frontmatter["kind"]) if frontmatter.get("kind") else None),
        temperature=_coerce_float(frontmatter.get("temperature")),
    )


def list_canonical_agents(project_root: Path) -> list[Path]:
    root = project_root / CANONICAL_AGENT_ROOT
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.md") if p.is_file())


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
    # TOML basic strings: " delimited, \\, \", control chars need escaping.
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        if "\n" in value:
            # Triple-quoted multi-line basic string. Any stray \"\"\" is
            # escaped by breaking one of the quotes.
            escaped = value.replace("\\", "\\\\").replace('"""', '""\\"')
            return f'"""\n{escaped}"""'
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
    """Protocol for runtime-specific sub-agent generators."""

    name: str
    scope: str  # "project" or "user"
    default: bool  # included when scope filter is None (backward compat)

    def target_file(self, project_root: Path, agent_name: str) -> Path:
        """Return the file that should hold the rendered agent."""
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
    scope: str = "project"
    default: bool = True

    def target_file(self, project_root: Path, agent_name: str) -> Path:
        return project_root / self.output_root / f"{agent_name}.md"

    def render(self, agent: SubAgent) -> tuple[str, list[str]]:
        return _subagent_to_claude_md(agent)


@dataclass
class GeminiAgentsGenerator:
    name: str = "gemini_agents"
    output_root: str = ".gemini/agents"
    scope: str = "project"
    default: bool = True

    def target_file(self, project_root: Path, agent_name: str) -> Path:
        return project_root / self.output_root / f"{agent_name}.md"

    def render(self, agent: SubAgent) -> tuple[str, list[str]]:
        return _subagent_to_gemini_md(agent)


@dataclass
class CodexAgentsGenerator:
    name: str = "codex_agents"
    # Display-only — Codex is user-scope, so the real path is resolved from
    # ``Path.home()`` inside ``target_file``. We keep a visible root string for
    # CLI / MCP output consistency.
    output_root: str = "~/.codex/agents"
    scope: str = "user"
    default: bool = True

    def target_file(self, project_root: Path, agent_name: str) -> Path:
        # project_root is intentionally ignored — Codex stores custom agents
        # under the user's home directory.
        return Path.home() / ".codex/agents" / f"{agent_name}.toml"

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
    skipped: list[tuple[str, str]]  # (runtime_or_agent, reason)


@dataclass
class ExtractResult:
    """Result of a reverse (runtime → canonical) import."""

    imported: list[Path]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (item_name, reason)


class StrictDropError(ValueError):
    """Raised under ``strict=True`` / ``on_drop="error"`` when a conversion would drop fields."""


# Valid severity levels for the ``on_drop`` parameter.
ON_DROP_LEVELS = ("ignore", "warn", "error")


def _select_generators(
    generators: dict[str, AgentGenerator],
    scope: str | None,
) -> list[str]:
    """Return generator names matching *scope*.

    - ``None`` — default generators only (backward-compatible).
    - ``"all"`` — every registered generator.
    - ``"project"`` / ``"user"`` — only matching scope.
    """
    if scope is None:
        return [n for n, g in generators.items() if g.default]
    if scope == "all":
        return list(generators.keys())
    return [n for n, g in generators.items() if g.scope == scope]


def generate_all_agents(
    project_root: Path,
    runtimes: list[str] | None = None,
    strict: bool = False,
    on_drop: str = "ignore",
    scope: str | None = None,
) -> AgentSyncResult:
    """Fan out every canonical sub-agent to the requested runtimes.

    Args:
        on_drop: Severity when fields are dropped during conversion.
            ``"ignore"`` (default) — silently record in ``result.dropped``.
            ``"warn"``  — log a warning per dropped-field set.
            ``"error"`` — raise :class:`StrictDropError` immediately.
        strict: Legacy alias for ``on_drop="error"``. If *both* are supplied,
            ``on_drop`` takes precedence unless it is still the default.
        scope: ``"project"`` / ``"user"`` / ``"all"`` / ``None``.
            ``None`` (default) runs only ``default=True`` generators.
    """
    # Resolve legacy ``strict`` flag.
    effective_drop = on_drop if on_drop != "ignore" or not strict else "error"

    generated: list[tuple[str, Path]] = []
    dropped: list[tuple[str, str, list[str]]] = []
    skipped: list[tuple[str, str]] = []

    canonicals = list_canonical_agents(project_root)
    if not canonicals:
        return AgentSyncResult(generated=[], dropped=[], skipped=[("<all>", "no canonical agents")])

    targets = runtimes if runtimes is not None else _select_generators(AGENT_GENERATORS, scope)
    for target in targets:
        gen = AGENT_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime"))
            continue
        for agent_path in canonicals:
            try:
                agent = parse_canonical_agent(agent_path)
            except AgentParseError as exc:
                skipped.append((agent_path.name, f"parse error: {exc}"))
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
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            generated.append((target, out_path))
            if dropped_fields:
                dropped.append((target, agent.name, dropped_fields))

    return AgentSyncResult(generated=generated, dropped=dropped, skipped=skipped)


# ── Reverse: runtime → canonical ────────────────────────────────────


def extract_agents_to_canonical(
    project_root: Path,
    overwrite: bool = False,
) -> ExtractResult:
    """Import existing Claude / Gemini agent files into ``.memtomem/agents/``.

    Codex TOML is **not** imported (one-way conversion; too lossy to round-trip
    without reconstructing fields we dropped on the way out). First occurrence
    wins across runtimes (Claude before Gemini — deterministic order).

    Returns an :class:`ExtractResult` with both imported paths and skipped
    items so the caller can warn the user about silent deduplication.
    """
    canonical_root = project_root / CANONICAL_AGENT_ROOT
    imported: list[Path] = []
    skipped: list[tuple[str, str]] = []
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
            if agent_name in seen:
                reason = f"already imported from {seen[agent_name]}"
                skipped.append((agent_name, reason))
                logger.warning("skip %s from %s: %s", agent_name, runtime_label, reason)
                continue
            dst = canonical_root / f"{agent_name}.md"
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((agent_name, reason))
                logger.warning("skip %s from %s: %s", agent_name, runtime_label, reason)
                seen[agent_name] = runtime_label
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(md_file.read_bytes())
            imported.append(dst)
            seen[agent_name] = runtime_label

    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ──────────────────────────────────────


def _runtime_agent_names(gen_name: str, project_root: Path) -> set[str]:
    if gen_name == "codex_agents":
        runtime_root = Path.home() / ".codex/agents"
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


def diff_agents(
    project_root: Path,
    scope: str | None = None,
) -> list[tuple[str, str, str]]:
    """Compare canonical agents against registered runtimes.

    Returns a list of ``(runtime, agent_name, status)`` where status is one of
    ``"in sync"``, ``"out of sync"``, ``"missing target"``, ``"missing canonical"``,
    ``"parse error"``.
    """
    results: list[tuple[str, str, str]] = []
    canonical_names = {p.stem for p in list_canonical_agents(project_root)}
    selected = _select_generators(AGENT_GENERATORS, scope)

    for gen_name, gen in AGENT_GENERATORS.items():
        if gen_name not in selected:
            continue
        runtime_names = _runtime_agent_names(gen_name, project_root)
        for name in sorted(canonical_names | runtime_names):
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
                continue
            if name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
                continue

            src = project_root / CANONICAL_AGENT_ROOT / f"{name}.md"
            try:
                agent = parse_canonical_agent(src)
            except AgentParseError:
                results.append((gen_name, name, "parse error"))
                continue
            expected, _ = gen.render(agent)
            target = gen.target_file(project_root, name)
            actual = target.read_text(encoding="utf-8") if target.is_file() else ""
            if expected.strip() == actual.strip():
                results.append((gen_name, name, "in sync"))
            else:
                results.append((gen_name, name, "out of sync"))

    return results


__all__ = [
    "AGENT_GENERATORS",
    "AgentGenerator",
    "AgentParseError",
    "AgentSyncResult",
    "CANONICAL_AGENT_ROOT",
    "ExtractResult",
    "ClaudeAgentsGenerator",
    "CodexAgentsGenerator",
    "GeminiAgentsGenerator",
    "ON_DROP_LEVELS",
    "StrictDropError",
    "SubAgent",
    "diff_agents",
    "extract_agents_to_canonical",
    "generate_all_agents",
    "list_canonical_agents",
    "parse_canonical_agent",
]
