"""Shared helper to query runtime coverage details (available, installed, registered) across projects."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def compute_runtime_coverage(project_root: Path) -> list[dict[str, object]]:
    """Probe each known runtime for any on-disk fan-out surface and registration status.

    Returns one entry per runtime in :data:`KNOWN_RUNTIMES` with an
    ``available`` flag that is the OR across:
      * top-level agent file (``CLAUDE.md`` / ``GEMINI.md`` / ``AGENTS.md``),
      * non-import runtime marker file (for example Kimi ``.kimi/config.toml``),
      * project-scope skill dir (``.claude/skills`` etc.),
      * project-scope sub-agent dir (``.claude/agents`` etc.),
      * project-scope command dir (``.claude/commands`` etc., Codex omitted),
      * settings-generator availability (ADR-0009 §1: settings is the one
        ``is_available()``-probed surface because its targets are not
        project-scoped).

    The settings probe is home-OR-project (ADR-0010 §3), so ``available`` can
    be true from machine-level runtime presence alone (``~/.codex`` exists,
    project untouched); ``installed`` stays the registry's separate
    machine-axis signal.
    """
    from memtomem.context._runtime_targets import KNOWN_RUNTIMES
    from memtomem.context.detector import (
        AGENT_DIRS,
        AGENT_FILES,
        COMMAND_DIRS,
        RUNTIME_MARKER_FILES,
        SKILL_DIRS,
    )
    from memtomem.context.runtime_registry import RUNTIME_TO_CLIENT, probe_all_runtimes
    from memtomem.context.settings import SETTINGS_GENERATORS

    try:
        statuses = {s.name: s for s in probe_all_runtimes(project_root)}
    except Exception:
        logger.exception("probe_all_runtimes failed during runtime_coverage calculation")
        statuses = {}

    out: list[dict[str, object]] = []
    for rt in KNOWN_RUNTIMES:
        probes: list[bool] = []
        for rel in AGENT_FILES.get(rt, []):
            probes.append((project_root / rel).exists())
        for rel in RUNTIME_MARKER_FILES.get(rt, []):
            probes.append((project_root / rel).exists())
        for rel in SKILL_DIRS.get(f"{rt}_skills", []):
            probes.append((project_root / rel).is_dir())
        for rel in AGENT_DIRS.get(f"{rt}_agents", []):
            probes.append((project_root / rel).is_dir())
        cmd = COMMAND_DIRS.get(f"{rt}_commands")
        if cmd is not None:
            probes.append((project_root / cmd[0]).is_dir())
        settings_gen = SETTINGS_GENERATORS.get(f"{rt}_settings")
        if settings_gen is not None:
            probes.append(settings_gen.is_available(project_root))
        entry: dict[str, object] = {"name": rt, "available": any(probes)}
        st = statuses.get(RUNTIME_TO_CLIENT.get(rt, ""))
        if st is not None:
            entry["installed"] = st.installed
            entry["memtomem_registered"] = st.memtomem_registered
        out.append(entry)
    return out
