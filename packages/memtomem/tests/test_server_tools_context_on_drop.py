"""MCP parity pin: ``mem_context_generate`` / ``mem_context_sync`` accept the
full ``on_drop`` vocabulary (``ignore`` / ``warn`` / ``error``), mirroring the
CLI ``--on-drop`` option (#1123 B5-4).

Before this, the MCP tools only exposed the boolean ``strict``, so:

* ``warn`` was unreachable — there was no way to ask for "report dropped
  fields but still write the runtime files".
* an invalid value could not be rejected (the param did not exist), and the
  helper silently defaulted to ``ignore``.

The behavioural difference these tests pin is ``error`` (abort the kind) vs
``warn`` / ``ignore`` (write anyway). ``warn`` vs ``ignore`` differ only by a
logged warning, so they share the same returned text.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.server.tools.context import mem_context_generate, mem_context_sync

from .helpers import set_home

# A canonical sub-agent carrying both Gemini-only (``kind`` / ``temperature``)
# and Claude-only (``skills`` / ``isolation``) fields, so *some* field is
# dropped for every runtime — guaranteeing a drop under any fan-out order.
_FULL_AGENT = """---
name: code-reviewer
description: Reviews staged code for quality
tools: [Read, Grep, Glob]
model: sonnet
skills: [code-review]
isolation: worktree
kind: reviewer
temperature: 0.2
---

You are a meticulous code reviewer.
"""


def _project_with_dropping_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    set_home(monkeypatch, tmp_path / "home")
    agents_dir = canonical_artifact_dir("agents", "project_shared", project)
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "code-reviewer.md").write_text(_FULL_AGENT, encoding="utf-8")
    monkeypatch.chdir(project)
    return project


@pytest.mark.anyio
async def test_generate_rejects_unknown_on_drop(tmp_path, monkeypatch):
    """Bad ``on_drop`` is rejected up front (mirrors ``click.Choice``)."""
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_generate(include="agents", on_drop="bogus")
    assert out.startswith("Error:")
    assert "on_drop" in out


@pytest.mark.anyio
async def test_generate_on_drop_error_aborts(tmp_path, monkeypatch):
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_generate(include="agents", on_drop="error")
    assert out.startswith("strict error:")


@pytest.mark.anyio
async def test_generate_on_drop_warn_writes_and_reports(tmp_path, monkeypatch):
    """``warn`` must reach the helper without aborting — the previously
    unreachable level. The dropped-field summary still renders."""
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_generate(include="agents", on_drop="warn")
    assert "strict error:" not in out
    assert "Sub-agent fan-out:" in out
    assert "dropped" in out


@pytest.mark.anyio
async def test_generate_default_on_drop_ignore_does_not_abort(tmp_path, monkeypatch):
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_generate(include="agents")
    assert "strict error:" not in out
    assert "Sub-agent fan-out:" in out


@pytest.mark.anyio
async def test_generate_strict_alias_still_aborts(tmp_path, monkeypatch):
    """Legacy ``strict=True`` ≡ ``on_drop="error"`` is preserved."""
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_generate(include="agents", strict=True)
    assert out.startswith("strict error:")


@pytest.mark.anyio
async def test_sync_on_drop_error_aborts(tmp_path, monkeypatch):
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_sync(include="agents", on_drop="error")
    assert out.startswith("strict error:")


@pytest.mark.anyio
async def test_sync_rejects_unknown_on_drop(tmp_path, monkeypatch):
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_sync(include="agents", on_drop="bogus")
    assert out.startswith("Error:")
    assert "on_drop" in out


@pytest.mark.anyio
async def test_sync_on_drop_warn_writes_and_reports(tmp_path, monkeypatch):
    """Balance the sync coverage with generate: ``warn`` must reach the helper
    and write without aborting."""
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_sync(include="agents", on_drop="warn")
    assert "strict error:" not in out
    assert "Sub-agent fan-out:" in out
    assert "dropped" in out


# ── Precedence: explicit on_drop vs the legacy strict alias ──────────────────
# Engine rule (context/_sync_atomic.py): effective = on_drop if on_drop is not
# the default "ignore" else ("error" when strict else "ignore"). i.e. an
# explicit non-ignore on_drop WINS over strict; strict only bites when on_drop
# is still its default. ``test_generate_strict_alias_still_aborts`` already
# pins the (on_drop="ignore", strict=True) -> error direction; these pin the
# other direction, which the threading must preserve.


@pytest.mark.anyio
async def test_generate_on_drop_warn_overrides_strict(tmp_path, monkeypatch):
    """``on_drop="warn"`` + ``strict=True`` must NOT abort — explicit on_drop
    wins over the legacy alias. Regression guard: if a future change dropped
    the ``on_drop=`` threading and kept only ``strict=``, this would abort."""
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_generate(include="agents", on_drop="warn", strict=True)
    assert "strict error:" not in out
    assert "Sub-agent fan-out:" in out


@pytest.mark.anyio
async def test_sync_on_drop_warn_overrides_strict(tmp_path, monkeypatch):
    _project_with_dropping_agent(tmp_path, monkeypatch)
    out = await mem_context_sync(include="agents", on_drop="warn", strict=True)
    assert "strict error:" not in out
    assert "Sub-agent fan-out:" in out
