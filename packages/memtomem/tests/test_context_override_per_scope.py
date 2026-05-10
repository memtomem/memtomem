"""ADR-0011 PR-E per-scope override resolution.

Layout PRESERVED: ``<canonical>/<asset_type>/<name>/overrides/<vendor>.<ext>``
per scope. Narrow→broad lookup with first-hit (project_local > project_shared
> user). Default ``scope=None`` searches all three; explicit scope limits.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.context.override import resolve
from .helpers import set_home


VENDOR_EXT = "md"  # claude/agents → .md per OVERRIDE_FORMATS


def _write_override(
    project_root: Path,
    *,
    scope_dir_name: str,  # "agents" or "agents.local"
    name: str,
    vendor: str,
    body: str,
    user_base: Path | None = None,
) -> Path:
    """Helper: create an override file under the appropriate canonical tree."""
    if scope_dir_name.startswith("user:"):
        # Encode user-tier writes via separate user_base param
        assert user_base is not None
        artifact = scope_dir_name.split(":", 1)[1]
        base = user_base / artifact
    else:
        base = project_root / ".memtomem" / scope_dir_name
    out = base / name / "overrides" / f"{vendor}.{VENDOR_EXT}"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body)
    return out


# ---------------------------------------------------------------------------
# Single-scope explicit lookups — exact-tier matching
# ---------------------------------------------------------------------------


def test_explicit_project_shared_scope(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    written = _write_override(
        project, scope_dir_name="agents", name="foo", vendor="claude", body="shared body"
    )
    out = resolve(project, "agents", "foo", "claude", scope="project_shared")
    assert out == written


def test_explicit_project_local_scope(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    written = _write_override(
        project, scope_dir_name="agents.local", name="foo", vendor="claude", body="local body"
    )
    out = resolve(project, "agents", "foo", "claude", scope="project_local")
    assert out == written


def test_explicit_project_shared_misses_when_only_local_exists(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_override(
        project, scope_dir_name="agents.local", name="foo", vendor="claude", body="local"
    )
    # Explicit project_shared — must not see project_local override
    assert resolve(project, "agents", "foo", "claude", scope="project_shared") is None


# ---------------------------------------------------------------------------
# Default scope=None: narrow→broad lookup with first-hit
# ---------------------------------------------------------------------------


def test_narrow_wins_local_over_shared(tmp_path: Path) -> None:
    """project_local override AND project_shared override → project_local wins."""
    project = tmp_path / "proj"
    local = _write_override(
        project, scope_dir_name="agents.local", name="foo", vendor="claude", body="local"
    )
    _write_override(project, scope_dir_name="agents", name="foo", vendor="claude", body="shared")
    # Default scope=None
    out = resolve(project, "agents", "foo", "claude")
    assert out == local


def test_narrow_wins_shared_over_user_when_local_absent(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    user_base = tmp_path / "home" / ".memtomem"
    set_home(monkeypatch, str(tmp_path / "home"))
    _write_override(
        project,
        scope_dir_name="user:agents",
        name="foo",
        vendor="claude",
        body="user",
        user_base=user_base,
    )
    shared = _write_override(
        project, scope_dir_name="agents", name="foo", vendor="claude", body="shared"
    )
    out = resolve(project, "agents", "foo", "claude")
    assert out == shared  # narrow-wins: shared beats user


# ---------------------------------------------------------------------------
# Broad-only fallback — only user override exists, must resolve to user.
# (Tie-break-only test would miss this path.)
# ---------------------------------------------------------------------------


def test_broad_only_user_fallback(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    user_base = tmp_path / "home" / ".memtomem"
    set_home(monkeypatch, str(tmp_path / "home"))
    user_override = _write_override(
        project,
        scope_dir_name="user:agents",
        name="foo",
        vendor="claude",
        body="user only",
        user_base=user_base,
    )
    out = resolve(project, "agents", "foo", "claude")
    assert out == user_override


def test_no_override_anywhere_returns_none(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    assert resolve(project, "agents", "foo", "claude") is None


# ---------------------------------------------------------------------------
# Asset type filtering — agents override doesn't satisfy skills lookup
# ---------------------------------------------------------------------------


def test_asset_type_filter(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    _write_override(
        project, scope_dir_name="agents", name="foo", vendor="claude", body="agents body"
    )
    assert resolve(project, "agents", "foo", "claude") is not None
    assert resolve(project, "skills", "foo", "claude") is None
    assert resolve(project, "commands", "foo", "claude") is None
