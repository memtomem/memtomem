"""Tests for ADR-0011 PR-E ``context.scope_resolver`` canonical-side API.

The runtime fan-out side has its own test module
(``test_context_runtime_targets``); this file covers ``canonical_artifact_dir``,
``list_artifact_scopes_present``, and ``project_root_from_artifact_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import (
    ContextScopeError,
    canonical_artifact_dir,
    list_artifact_scopes_present,
    project_root_from_artifact_path,
)


ARTIFACTS = ("agents", "skills", "commands")


# ---------------------------------------------------------------------------
# canonical_artifact_dir — 9 happy cases (3 artifacts × 3 scopes) + errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_canonical_user_scope(tmp_path: Path, artifact: str) -> None:
    user_base = tmp_path / "home" / ".memtomem"
    out = canonical_artifact_dir(artifact, "user", project_root=None, user_base=user_base)  # type: ignore[arg-type]
    assert out == (user_base / artifact).resolve()


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_canonical_project_shared(tmp_path: Path, artifact: str) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    out = canonical_artifact_dir(artifact, "project_shared", project_root=project)  # type: ignore[arg-type]
    assert out == (project / ".memtomem" / artifact).resolve()


@pytest.mark.parametrize("artifact", ARTIFACTS)
def test_canonical_project_local(tmp_path: Path, artifact: str) -> None:
    project = tmp_path / "myproj"
    project.mkdir()
    out = canonical_artifact_dir(artifact, "project_local", project_root=project)  # type: ignore[arg-type]
    assert out == (project / ".memtomem" / f"{artifact}.local").resolve()


@pytest.mark.parametrize("scope", ["project_shared", "project_local"])
def test_canonical_project_tier_requires_root(scope: str) -> None:
    with pytest.raises(ContextScopeError, match="requires a project context"):
        canonical_artifact_dir("agents", scope, project_root=None)  # type: ignore[arg-type]


def test_canonical_unknown_scope(tmp_path: Path) -> None:
    # Pass a valid project_root so the ``project_root is None`` branch
    # doesn't intercept; we want the unsupported-scope branch.
    with pytest.raises(ContextScopeError, match="unsupported artifact scope"):
        canonical_artifact_dir("agents", "bogus", project_root=tmp_path)  # type: ignore[arg-type]


def test_canonical_user_base_default_expands_home() -> None:
    out = canonical_artifact_dir("agents", "user", project_root=None)
    # ``~/.memtomem`` resolves to absolute under user's home; just check
    # the expansion happened (no leading ``~``).
    assert "~" not in str(out)
    assert out.name == "agents"


# ---------------------------------------------------------------------------
# list_artifact_scopes_present — directory presence drives membership
# ---------------------------------------------------------------------------


def test_list_scopes_present_empty(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    user_base = tmp_path / "home" / ".memtomem"
    assert list_artifact_scopes_present("agents", project, user_base=user_base) == []


def test_list_scopes_present_user_only(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    user_base = tmp_path / "home" / ".memtomem"
    (user_base / "agents").mkdir(parents=True)
    assert list_artifact_scopes_present("agents", project, user_base=user_base) == ["user"]


def test_list_scopes_present_all_three(tmp_path: Path) -> None:
    project = tmp_path / "p"
    project.mkdir()
    (project / ".memtomem" / "agents").mkdir(parents=True)
    (project / ".memtomem" / "agents.local").mkdir(parents=True)
    user_base = tmp_path / "home" / ".memtomem"
    (user_base / "agents").mkdir(parents=True)
    out = list_artifact_scopes_present("agents", project, user_base=user_base)
    assert set(out) == {"user", "project_shared", "project_local"}


def test_list_scopes_present_isolates_artifacts(tmp_path: Path) -> None:
    """Skills directory presence does NOT count as agents-tier presence."""
    project = tmp_path / "p"
    project.mkdir()
    (project / ".memtomem" / "skills").mkdir(parents=True)
    user_base = tmp_path / "home" / ".memtomem"
    assert list_artifact_scopes_present("agents", project, user_base=user_base) == []
    assert list_artifact_scopes_present("skills", project, user_base=user_base) == [
        "project_shared"
    ]


# ---------------------------------------------------------------------------
# project_root_from_artifact_path — walk parents for .memtomem ancestor
# ---------------------------------------------------------------------------


def test_project_root_happy_path(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    (project / ".memtomem" / "agents").mkdir(parents=True)
    artifact = project / ".memtomem" / "agents" / "foo.md"
    artifact.touch()
    assert project_root_from_artifact_path(artifact) == project


def test_project_root_deeply_nested(tmp_path: Path) -> None:
    project = tmp_path / "myproj"
    nested = project / ".memtomem" / "skills" / "deep-skill" / "scripts"
    nested.mkdir(parents=True)
    leaf = nested / "run.sh"
    leaf.touch()
    assert project_root_from_artifact_path(leaf) == project


def test_project_root_no_ancestor_returns_none(tmp_path: Path) -> None:
    orphan = tmp_path / "elsewhere" / "file.md"
    orphan.parent.mkdir(parents=True)
    orphan.touch()
    assert project_root_from_artifact_path(orphan) is None


def test_project_root_input_is_memtomem_dir_returns_parent(tmp_path: Path) -> None:
    """When the input path itself IS ``.memtomem``, return its parent."""
    project = tmp_path / "myproj"
    memdir = project / ".memtomem"
    memdir.mkdir(parents=True)
    assert project_root_from_artifact_path(memdir) == project


def test_project_root_non_artifact_path_inside_project_returns_none(tmp_path: Path) -> None:
    """A source file under a project that contains ``.memtomem`` is NOT an
    artifact path. The walk must check ``ancestor.name == ".memtomem"``,
    not ``(ancestor / ".memtomem").is_dir()`` — otherwise any path under
    a memtomem-using repo would falsely map to the project root.
    """
    project = tmp_path / "myproj"
    (project / ".memtomem" / "agents").mkdir(parents=True)
    src_file = project / "src" / "foo.py"
    src_file.parent.mkdir(parents=True)
    src_file.touch()
    # src/foo.py is inside the project but has no .memtomem ancestor.
    assert project_root_from_artifact_path(src_file) is None


def test_project_root_path_under_project_root_itself(tmp_path: Path) -> None:
    """The project root itself isn't an artifact path; returns None."""
    project = tmp_path / "myproj"
    (project / ".memtomem").mkdir(parents=True)
    # project root has .memtomem as a child but not an ancestor.
    assert project_root_from_artifact_path(project) is None
