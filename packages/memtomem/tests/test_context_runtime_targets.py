"""Tests for ADR-0011 PR-E ``context._runtime_targets`` runtime-side table.

Pins the full RUNTIME_FANOUT_TABLE so coverage is mechanical and any
future runtime addition forces a deliberate test update (no silent gaps).
"""

from __future__ import annotations

from itertools import product
from pathlib import Path

import pytest

from memtomem.context._runtime_targets import (
    KNOWN_RUNTIMES,
    RUNTIME_FANOUT_TABLE,
    runtime_artifact_listing,
    runtime_artifact_names,
    runtime_fanout_root,
)

from .helpers import set_home
from memtomem.context.scope_resolver import ArtifactKind


ARTIFACTS: tuple[ArtifactKind, ...] = ("agents", "skills", "commands")
SCOPES = ("user", "project_shared", "project_local")


# ---------------------------------------------------------------------------
# Table shape — every (artifact, runtime, scope) tuple is populated
# ---------------------------------------------------------------------------


def test_table_covers_full_cross_product() -> None:
    """Table must contain every (artifact, runtime, scope) tuple — no gaps."""
    expected = set(product(ARTIFACTS, KNOWN_RUNTIMES, SCOPES))
    actual = set(RUNTIME_FANOUT_TABLE.keys())
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"missing tuples: {missing}"
    assert not extra, f"extra tuples not in cross-product: {extra}"


def test_all_project_local_entries_are_none() -> None:
    """ADR §3: project_local has no runtime fan-out — every entry None."""
    for (artifact, runtime, scope), value in RUNTIME_FANOUT_TABLE.items():
        if scope == "project_local":
            assert value is None, f"{(artifact, runtime, scope)} should be None (ADR §3)"


def test_codex_commands_user_only() -> None:
    """Codex CLI prompts are user-tier only by design (commands.py:5)."""
    assert RUNTIME_FANOUT_TABLE[("commands", "codex", "user")] is not None
    assert RUNTIME_FANOUT_TABLE[("commands", "codex", "project_shared")] is None
    assert RUNTIME_FANOUT_TABLE[("commands", "codex", "project_local")] is None


# ---------------------------------------------------------------------------
# runtime_fanout_root — happy path resolution per scope
# ---------------------------------------------------------------------------


def test_user_scope_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_home(monkeypatch, str(tmp_path))
    out = runtime_fanout_root("agents", "claude", "user", project_root=None)
    assert out == (tmp_path / ".claude" / "agents").resolve()


def test_user_scope_codex_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    set_home(monkeypatch, str(tmp_path))
    out = runtime_fanout_root("commands", "codex", "user", project_root=None)
    assert out == (tmp_path / ".codex" / "prompts").resolve()


def test_project_shared_joins_root(tmp_path: Path) -> None:
    out = runtime_fanout_root("agents", "gemini", "project_shared", project_root=tmp_path)
    assert out == (tmp_path / ".gemini" / "agents").resolve()


def test_codex_skills_project_uses_dot_agents(tmp_path: Path) -> None:
    """Codex skills project-scope path is .agents/skills, NOT .codex/skills."""
    out = runtime_fanout_root("skills", "codex", "project_shared", project_root=tmp_path)
    assert out == (tmp_path / ".agents" / "skills").resolve()


def test_project_local_returns_none(tmp_path: Path) -> None:
    """Every project_local lookup returns None (no fan-out)."""
    for artifact, runtime in product(ARTIFACTS, KNOWN_RUNTIMES):
        out = runtime_fanout_root(artifact, runtime, "project_local", project_root=tmp_path)
        assert out is None, f"({artifact}, {runtime}, project_local) should be None"


def test_codex_commands_project_shared_returns_none(tmp_path: Path) -> None:
    out = runtime_fanout_root("commands", "codex", "project_shared", project_root=tmp_path)
    assert out is None


# ---------------------------------------------------------------------------
# Fail-loud — no silent fallback
# ---------------------------------------------------------------------------


def test_unknown_runtime_raises_keyerror(tmp_path: Path) -> None:
    """An unknown runtime is a programming error — KeyError, not None."""
    with pytest.raises(KeyError):
        runtime_fanout_root("agents", "imaginary_runtime", "user", project_root=tmp_path)


def test_unknown_artifact_raises_keyerror(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        runtime_fanout_root("imaginary_artifact", "claude", "user", project_root=tmp_path)  # type: ignore[arg-type]


def test_project_scope_without_root_raises_valueerror() -> None:
    """ValueError is preferred over silent None for missing project_root."""
    with pytest.raises(ValueError, match="requires project_root"):
        runtime_fanout_root("agents", "claude", "project_shared", project_root=None)


def test_project_local_without_root_returns_none_first() -> None:
    """project_local entries are None — that branch wins before the
    project_root check, so passing None is harmless for project_local."""
    assert runtime_fanout_root("agents", "claude", "project_local", project_root=None) is None


def test_runtime_artifact_names_skips_invalid_file_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / ".claude" / "agents"
    root.mkdir(parents=True)
    (root / "ok.md").write_text("", encoding="utf-8")
    (root / "-bad.md").write_text("", encoding="utf-8")

    names = runtime_artifact_names(
        "agents", "claude", tmp_path, "project_shared", file_suffix=".md"
    )

    assert names == {"ok"}
    assert any(
        record.message == "Skipping invalid runtime artifact name"
        and record.name == "memtomem.context._runtime_targets"
        and getattr(record, "artifact") == "agents"
        and getattr(record, "runtime") == "claude"
        and getattr(record, "scope") == "project_shared"
        and getattr(record, "artifact_name") == "-bad"
        for record in caplog.records
    )


def test_runtime_artifact_names_skips_invalid_manifest_dirs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good = tmp_path / ".agents" / "skills" / "ok"
    bad = tmp_path / ".agents" / "skills" / "-bad"
    good.mkdir(parents=True)
    bad.mkdir()
    (good / "SKILL.md").write_text("", encoding="utf-8")
    (bad / "SKILL.md").write_text("", encoding="utf-8")

    names = runtime_artifact_names(
        "skills", "codex", tmp_path, "project_shared", dir_manifest="SKILL.md"
    )

    assert names == {"ok"}
    assert any(
        record.message == "Skipping invalid runtime artifact name"
        and getattr(record, "artifact") == "skills"
        and getattr(record, "runtime") == "codex"
        and getattr(record, "artifact_name") == "-bad"
        for record in caplog.records
    )


def test_runtime_artifact_names_skips_internal_staging_dirs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Crash-leftover staging/move-aside trees carry a SKILL.md mirror and
    pass validate_name — the dir_manifest branch must drop them SILENTLY
    (they are our own artifacts, not user content) so diff never shows a
    phantom 'missing canonical' row (#1229)."""
    good = tmp_path / ".claude" / "skills" / "ok"
    good.mkdir(parents=True)
    (good / "SKILL.md").write_text("", encoding="utf-8")
    for leftover in (".staging-ok-99999-abc123.tmp", ".old-ok-99999-abc123.tmp"):
        d = tmp_path / ".claude" / "skills" / leftover
        d.mkdir()
        (d / "SKILL.md").write_text("", encoding="utf-8")

    names = runtime_artifact_names(
        "skills", "claude", tmp_path, "project_shared", dir_manifest="SKILL.md"
    )

    assert names == {"ok"}
    # Silent skip — contrast with the InvalidNameError warning path above.
    assert not any(
        record.message == "Skipping invalid runtime artifact name" for record in caplog.records
    )


def test_runtime_artifact_listing_returns_invalid_names(tmp_path: Path) -> None:
    """#1229: the listing variant exposes invalid raw names so diff can emit
    a dedicated 'invalid name' row; the names-only wrapper stays valid-only."""
    root = tmp_path / ".claude" / "agents"
    root.mkdir(parents=True)
    (root / "ok.md").write_text("", encoding="utf-8")
    (root / "-bad.md").write_text("", encoding="utf-8")
    (root / "bad name.md").write_text("", encoding="utf-8")

    names, invalid = runtime_artifact_listing(
        "agents", "claude", tmp_path, "project_shared", file_suffix=".md"
    )
    assert names == {"ok"}
    # (raw_name, reason) pairs — the reason rides the diff row (#1229 U7).
    assert [n for n, _ in invalid] == ["-bad", "bad name"]
    assert all("invalid agent name" in r for _, r in invalid)

    assert runtime_artifact_names(
        "agents", "claude", tmp_path, "project_shared", file_suffix=".md"
    ) == {"ok"}
