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
    find_project_root,
    list_artifact_scopes_present,
    project_root_from_artifact_path,
)


ARTIFACTS = ("agents", "skills", "commands")


# ---------------------------------------------------------------------------
# find_project_root — shared CLI/MCP/web root detection (M4)
# ---------------------------------------------------------------------------


class TestFindProjectRoot:
    def test_returns_cwd_when_marker_at_cwd(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        assert find_project_root() == tmp_path

    def test_walks_up_to_git_ancestor_from_subdir(self, tmp_path: Path, monkeypatch) -> None:
        """The M4 bug scenario: launched from a project subdirectory, the root
        must resolve to the repo root (so web/CLI/MCP target one .memtomem)."""
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "packages" / "foo"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        assert find_project_root() == tmp_path

    def test_walks_up_to_pyproject_ancestor(self, tmp_path: Path, monkeypatch) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        subdir = tmp_path / "src"
        subdir.mkdir()
        monkeypatch.chdir(subdir)
        assert find_project_root() == tmp_path

    def test_falls_back_to_origin_when_no_marker(self, tmp_path: Path, monkeypatch) -> None:
        subdir = tmp_path / "a" / "b"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        # No .git/pyproject.toml anywhere up the (tmp) tree → original cwd.
        assert find_project_root() == subdir

    def test_explicit_start_argument(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "x"
        subdir.mkdir()
        assert find_project_root(start=subdir) == tmp_path

    def test_cli_and_mcp_helpers_delegate_to_shared(self, tmp_path: Path, monkeypatch) -> None:
        """CLI and MCP ``_find_project_root`` now share one definition (dedup)."""
        from memtomem.cli.context_cmd import _find_project_root as cli_root
        from memtomem.server.tools.context import _find_project_root as mcp_root

        (tmp_path / ".git").mkdir()
        subdir = tmp_path / "deep" / "nested"
        subdir.mkdir(parents=True)
        monkeypatch.chdir(subdir)
        assert cli_root() == tmp_path
        assert mcp_root() == tmp_path

    def test_web_lifespan_resolves_root_via_shared_walk(self) -> None:
        """Pin the actual M4 fix SITE, not just the helper.

        ``find_project_root`` being correct does not prove the web app uses it:
        a regression reverting ``app.state.project_root`` back to ``Path.cwd()``
        would still pass the behavioral cases above and silently re-introduce
        the subdir split-brain. The real lifespan is too heavy to run here
        (``create_components`` + FileWatcher + embedding sync), so this is a
        source-level pin in the same spirit as the static asserts elsewhere in
        the web suite: the lifespan MUST resolve the root through the shared
        walk and MUST NOT pin the bare cwd.
        """
        import inspect

        from memtomem.web.app import _lifespan

        src = inspect.getsource(_lifespan)
        assert "find_project_root()" in src, "web lifespan no longer uses the shared walk"
        assert "project_root = Path.cwd()" not in src, (
            "web lifespan pins the bare cwd again — subdir launches will write to "
            "<subdir>/.memtomem instead of the repo root (M4 regression)"
        )


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
