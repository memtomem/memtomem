"""Backward-compatible enumeration / extraction across agent canonical layouts.

ADR-0008 commits to ``agents/<name>/agent.md`` (directory layout) but the
legacy form ``agents/<name>.md`` (flat) still exists in PR-B users' trees.
PR-C must enumerate both, parse both, and preserve the existing layout
during reverse-sync; new agents land in the directory form.
"""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem.context.agents import (
    CANONICAL_AGENT_ROOT,
    extract_agents_to_canonical,
    list_canonical_agents,
    parse_canonical_agent,
)


SAMPLE_FRONTMATTER = """---
name: helper
description: Generic helper
---

Help with things.
"""


def _write_flat_agent(project_root: Path, name: str) -> Path:
    root = project_root / CANONICAL_AGENT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{name}.md"
    target.write_text(SAMPLE_FRONTMATTER.replace("name: helper", f"name: {name}"))
    return target


def _write_dir_agent(project_root: Path, name: str) -> Path:
    root = project_root / CANONICAL_AGENT_ROOT / name
    root.mkdir(parents=True, exist_ok=True)
    target = root / "agent.md"
    target.write_text(SAMPLE_FRONTMATTER.replace("name: helper", f"name: {name}"))
    return target


# ── list_canonical_agents enumeration ───────────────────────────────────


def test_list_agents_flat_only_unchanged(tmp_path: Path) -> None:
    _write_flat_agent(tmp_path, "alpha")
    _write_flat_agent(tmp_path, "beta")
    result = list_canonical_agents(tmp_path)
    names = [p.stem for p, _ in result]
    layouts = [layout for _, layout in result]
    assert names == ["alpha", "beta"]
    assert layouts == ["flat", "flat"]


def test_list_agents_dir_only_enumerated(tmp_path: Path) -> None:
    _write_dir_agent(tmp_path, "gamma")
    _write_dir_agent(tmp_path, "delta")
    result = list_canonical_agents(tmp_path)
    names = [p.parent.name for p, _ in result]
    layouts = [layout for _, layout in result]
    assert names == ["delta", "gamma"]
    assert layouts == ["dir", "dir"]


def test_list_returns_layout_tag(tmp_path: Path) -> None:
    _write_flat_agent(tmp_path, "alpha")
    _write_dir_agent(tmp_path, "beta")
    result = list_canonical_agents(tmp_path)
    by_name = {(p.parent.name if layout == "dir" else p.stem): layout for p, layout in result}
    assert by_name == {"alpha": "flat", "beta": "dir"}


def test_list_agents_both_layouts_dir_wins(
    tmp_path: Path,
    caplog: "logging.LogCaptureFixture",
) -> None:
    _write_flat_agent(tmp_path, "shared")
    _write_dir_agent(tmp_path, "shared")
    with caplog.at_level(logging.WARNING):
        result = list_canonical_agents(tmp_path)
    # Dir wins on collision.
    assert len(result) == 1
    path, layout = result[0]
    assert layout == "dir"
    assert path.name == "agent.md"
    # WARN-level log emitted with the conflict shape.
    assert any(
        "both flat" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records
    )


# ── parse_canonical_agent layout dispatch ───────────────────────────────


def test_parse_dir_layout_uses_parent_name(tmp_path: Path) -> None:
    """Frontmatter without ``name`` falls back to ``path.parent.name`` for
    dir layout (avoids the ``path.stem == "agent"`` heuristic)."""
    target = tmp_path / CANONICAL_AGENT_ROOT / "foo" / "agent.md"
    target.parent.mkdir(parents=True)
    # Frontmatter intentionally omits ``name``.
    target.write_text("---\ndescription: x\n---\n\nbody\n")
    agent = parse_canonical_agent(target, layout="dir")
    assert agent.name == "foo"


def test_parse_flat_layout_with_filename_agent(tmp_path: Path) -> None:
    """Heuristic-free guard: a flat file literally named ``agent.md`` must NOT
    be misclassified as dir form. The layout tag is the source of truth."""
    target = tmp_path / CANONICAL_AGENT_ROOT / "agent.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ndescription: x\n---\n\nbody\n")
    agent = parse_canonical_agent(target, layout="flat")
    assert agent.name == "agent"


# ── extract_agents_to_canonical layout policy ───────────────────────────


def test_extract_preserves_flat_layout_when_only_flat_exists(
    tmp_path: Path,
) -> None:
    _write_flat_agent(tmp_path, "legacy")
    runtime = tmp_path / ".claude/agents"
    runtime.mkdir(parents=True)
    runtime_md = runtime / "legacy.md"
    runtime_md.write_text(
        SAMPLE_FRONTMATTER.replace("name: helper", "name: legacy").replace(
            "Generic helper", "UPDATED"
        )
    )
    extract_agents_to_canonical(tmp_path, overwrite=True)
    # Flat preserved; no dir layout created.
    flat_path = tmp_path / CANONICAL_AGENT_ROOT / "legacy.md"
    assert flat_path.is_file()
    assert "UPDATED" in flat_path.read_text()
    assert not (tmp_path / CANONICAL_AGENT_ROOT / "legacy" / "agent.md").exists()


def test_extract_writes_dir_layout_for_new_agent(tmp_path: Path) -> None:
    runtime = tmp_path / ".claude/agents"
    runtime.mkdir(parents=True)
    (runtime / "fresh.md").write_text(SAMPLE_FRONTMATTER.replace("name: helper", "name: fresh"))
    extract_agents_to_canonical(tmp_path)
    # New agent → dir layout.
    assert (tmp_path / CANONICAL_AGENT_ROOT / "fresh" / "agent.md").is_file()
    assert not (tmp_path / CANONICAL_AGENT_ROOT / "fresh.md").exists()


def test_extract_warns_when_both_layouts_present(
    tmp_path: Path,
    caplog: "logging.LogCaptureFixture",
) -> None:
    """Dir+flat coexist → reverse-sync updates dir, flat goes silently
    divergent. Emit a separate WARNING from the list-time warning so the
    user sees the silent-divergence shape during sync, not just on list."""
    _write_flat_agent(tmp_path, "shared")
    _write_dir_agent(tmp_path, "shared")
    runtime = tmp_path / ".claude/agents"
    runtime.mkdir(parents=True)
    (runtime / "shared.md").write_text(
        SAMPLE_FRONTMATTER.replace("name: helper", "name: shared").replace(
            "Generic helper", "FROM RUNTIME"
        )
    )
    with caplog.at_level(logging.WARNING):
        extract_agents_to_canonical(tmp_path, overwrite=True)
    # Dir updated.
    dir_path = tmp_path / CANONICAL_AGENT_ROOT / "shared" / "agent.md"
    assert "FROM RUNTIME" in dir_path.read_text()
    # Flat file stays at its old contents (silent divergence).
    flat_path = tmp_path / CANONICAL_AGENT_ROOT / "shared.md"
    assert flat_path.is_file()
    assert "FROM RUNTIME" not in flat_path.read_text()
    # WARN-level message about silent divergence in extract.
    assert any(
        "silently divergent" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )
