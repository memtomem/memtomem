"""Tests for ``memtomem.wiki.override.render_seed_bytes`` — vendor-aware seed bytes.

PR-D C1a lifts the skills-only ``NotImplementedError`` so agents and commands
seed via vendor generators (path (b): ``parse_canonical_*`` + generator
``render``). The codex commands row in :data:`OVERRIDE_FORMATS` is a
permanent placeholder (no ``codex_commands`` generator); seeding raises
``NotImplementedError`` with a diagnostic message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context._names import InvalidNameError
from memtomem.wiki.override import render_seed_bytes
from memtomem.wiki.store import WikiStore

# ``wiki_root`` fixture is registered globally via ``conftest.py`` (which
# re-exports from ``_wiki_fixtures.py``). No per-file import needed.


_AGENT_CANONICAL = """---
name: bar
description: a test agent
---

Body of the agent.
"""

_COMMAND_CANONICAL = """---
description: a test command
---

Command body.
"""


def _initialized_wiki() -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def test_render_seed_bytes_for_agents_uses_vendor_generator(wiki_root: Path) -> None:
    """Agents seed routes through the vendor generator.

    Codex agents emit TOML — pin format identity (TOML keys, no Markdown
    frontmatter delimiter) so the seed bytes match what ``.codex/agents/``
    would receive at fan-out time.
    """
    store = _initialized_wiki()
    agent_dir = wiki_root / "agents" / "bar"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text(_AGENT_CANONICAL, encoding="utf-8")

    seed, dropped = render_seed_bytes(store, "agents", "bar", "codex")

    text = seed.decode("utf-8")
    assert "name = " in text, "TOML name key absent — generator did not emit TOML?"
    assert "Body of the agent." in text
    assert not text.lstrip().startswith("---"), (
        "seed starts with Markdown frontmatter — codex generator returned Markdown?"
    )
    # Minimal canonical (no skills/isolation/temperature/kind) → codex drops nothing.
    assert dropped == []


def test_render_seed_bytes_for_commands_uses_vendor_generator(wiki_root: Path) -> None:
    """Commands seed routes through the vendor generator.

    Gemini commands emit TOML — pin format identity.
    """
    store = _initialized_wiki()
    cmd_dir = wiki_root / "commands" / "baz"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.md").write_text(_COMMAND_CANONICAL, encoding="utf-8")

    seed, dropped = render_seed_bytes(store, "commands", "baz", "gemini")

    text = seed.decode("utf-8")
    assert "prompt = " in text, "TOML prompt key absent — generator did not emit TOML?"
    assert "Command body." in text
    assert not text.lstrip().startswith("---"), (
        "seed starts with Markdown frontmatter — gemini generator returned Markdown?"
    )
    # Minimal canonical (no argument-hint/allowed-tools/model) → gemini drops nothing.
    assert dropped == []


def test_render_seed_bytes_codex_commands_raises_not_implemented(
    wiki_root: Path,
) -> None:
    """``("commands", "codex")`` is a permanent placeholder — no
    ``codex_commands`` generator. Seeding raises ``NotImplementedError`` with
    a diagnostic message rather than silently failing on a dict KeyError.
    """
    store = _initialized_wiki()
    cmd_dir = wiki_root / "commands" / "baz"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "command.md").write_text(_COMMAND_CANONICAL, encoding="utf-8")

    with pytest.raises(NotImplementedError, match="commands not yet supported"):
        render_seed_bytes(store, "commands", "baz", "codex")


def test_render_seed_bytes_rejects_traversal_name(wiki_root: Path) -> None:
    """Defense-in-depth: ``render_seed_bytes`` validates ``name`` itself.

    ``seed_override`` (the usual caller) already validates, but
    ``render_seed_bytes`` is in ``__all__`` so direct callers should not
    have to remember to pre-validate. A traversal-shaped name in the
    ``store.root / asset_type / name / ...`` path would otherwise escape
    the wiki root.
    """
    store = _initialized_wiki()

    with pytest.raises(InvalidNameError):
        render_seed_bytes(store, "agents", "../etc/passwd", "claude")
    with pytest.raises(InvalidNameError):
        render_seed_bytes(store, "commands", "../../escape", "claude")
    with pytest.raises(InvalidNameError):
        render_seed_bytes(store, "skills", "../../escape", "claude")


def test_save_bak_on_legacy_wiki_stays_out_of_git_status(wiki_root: Path) -> None:
    """Editor Save on a wiki whose history lacks the scaffold .gitignore:
    the edited canonical shows in status (uncommitted edit — correct), but
    the ``.bak`` recovery sibling must not (T2-7 retrofit via the write-path
    ``ensure_bak_excluded`` hook)."""
    import subprocess

    from memtomem.wiki.override import write_canonical

    store = _initialized_wiki()
    subprocess.run(
        ["git", "rm", "-q", ".gitignore"], cwd=wiki_root, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-qm", "drop scaffold gitignore"],
        cwd=wiki_root,
        check=True,
        capture_output=True,
    )
    agent_dir = wiki_root / "agents" / "bar"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text(_AGENT_CANONICAL, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=wiki_root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "add bar"], cwd=wiki_root, check=True, capture_output=True
    )

    write_canonical(store, "agents", "bar", b"---\nname: bar\ndescription: d2\n---\n\nv2\n")

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=wiki_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "agents/bar/agent.md" in status  # the real uncommitted edit
    assert ".bak" not in status  # the recovery sibling is excluded
    assert (agent_dir / "agent.md.bak").read_text(encoding="utf-8") == _AGENT_CANONICAL
