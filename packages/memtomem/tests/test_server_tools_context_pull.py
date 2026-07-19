"""Real-FS integration pins for the ``mem_context_pull`` MCP tool (ADR-0030 PR-H).

Headless parity for ``mm context pull`` and the web Pull route — the SAME
result-coded ``context.pull_apply`` / ``context.pull_preview`` engine, the same
source-selectable §5 semantics and Gate A/B gates. The engine's own behavior is
pinned by ``test_context_pull_apply.py`` / ``test_context_pull_preview.py``;
these tests pin the MCP *translation*:

* registry classification: ``@register("context")`` → routed by ``mem_do``, NOT
  one of the frozen core-9;
* ``apply=False`` (default) previews and never mutates;
* prefix-coded refusals (``error:`` / ``refused:`` / ``needs confirmation:`` /
  ``privacy block:``) matching the web ``_finalize_pull`` status routing — every
  ``PullApplyStatus`` maps to a rendering branch (parity pin);
* the ``skills_overwrite_unsupported`` refusal surfaces cleanly as ``refused:``
  text today (the PR-G independence guard — MCP inherits it, no PR-G needed);
* consent gates return ``needs confirmation`` (MCP cannot prompt) only once a
  write is imminent, and redact the disclosed host path;
* ``force_unsafe_import`` opens the Gate A valve for a LITERAL ``True`` only —
  a ``mem_do`` raw string ``"true"`` must NOT bypass;
* Gate A audit attribution: ``surface="mcp_context_pull"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.context import mem_context_pull
from memtomem.server.tools.meta import mem_do

from .helpers import seed_multi_runtime

_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS-key shape — caught by the privacy scan


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One initialized project root + isolated HOME, cwd at the project."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem").mkdir()
    monkeypatch.chdir(p)
    return p


def _agent(name: str, marker: str) -> str:
    return f"---\nname: {name}\ndescription: t\n---\n{marker}\n"


def _canonical_agent_text(proj: Path, name: str, scope: str = "project_shared") -> str:
    d = canonical_artifact_dir("agents", scope, proj) / name
    return (d / "agent.md").read_text(encoding="utf-8")


def _store_exists(proj: Path, kind: str, name: str, scope: str = "project_shared") -> bool:
    return (canonical_artifact_dir(kind, scope, proj) / name).exists()


# ── registry classification ───────────────────────────────────────────────────


def test_registered_and_routed_by_mem_do() -> None:
    assert "context_pull" in ACTIONS
    from memtomem.server import _CORE_TOOLS

    assert "mem_context_pull" not in _CORE_TOOLS


async def test_mem_do_routes_the_action(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    out = await mem_do(action="context_pull", params={"kind": "agents", "name": "a"})
    assert "Pull preview: agents/a" in out


# ── dry-run preview (default) ─────────────────────────────────────────────────


async def test_preview_default_no_writes(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "c"), "gemini": _agent("a", "g")}
    )
    out = await mem_context_pull(kind="agents", name="a")
    assert "ambiguous" in out
    assert "Re-call with apply=True" in out
    assert not _store_exists(proj, "agents", "a")


async def test_preview_from_narrows_to_source(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "C"), "gemini": _agent("a", "G")}
    )
    out = await mem_context_pull(kind="agents", name="a", from_runtime="gemini")
    assert "gemini" in out
    assert "claude" not in out  # narrowed out
    assert "source: gemini" in out


# ── input validation (error:) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"kind": "settings", "name": "a"}, "no Pull sources"),
        ({"kind": "", "name": "a"}, "no Pull sources"),
        ({"kind": "agents", "name": "a", "scope": "project_local"}, "project_local"),
        ({"kind": "agents", "name": "a", "scope": "bogus"}, "Unknown scope"),
        ({"kind": "agents", "name": "bad name!"}, "name"),
        ({"kind": "agents", "name": "a", "from_runtime": "bogus"}, "bogus"),
    ],
)
async def test_input_validation_errors(proj: Path, kwargs: dict, needle: str) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    out = await mem_context_pull(**kwargs)
    assert out.startswith("error:")
    assert needle in out


async def test_from_codex_agents_is_export_only(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    out = await mem_context_pull(kind="agents", name="a", from_runtime="codex")
    assert out.startswith("error:")
    assert "codex" in out.lower()


# ── apply ─────────────────────────────────────────────────────────────────────


async def test_apply_single_candidate_writes(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "only")})
    out = await mem_context_pull(
        kind="agents", name="a", scope="project_shared", apply=True, confirm_project_shared=True
    )
    assert "Pulled agents/a from claude" in out
    assert "only" in _canonical_agent_text(proj, "a")


async def test_apply_from_lands_chosen(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "CLAUDE"), "gemini": _agent("a", "GEM")}
    )
    out = await mem_context_pull(
        kind="agents",
        name="a",
        from_runtime="gemini",
        scope="project_shared",
        apply=True,
        confirm_project_shared=True,
    )
    assert "Pulled agents/a from gemini" in out
    assert "GEM" in _canonical_agent_text(proj, "a")


async def test_apply_source_conflict_refuses(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "c"), "gemini": _agent("a", "g")}
    )
    out = await mem_context_pull(
        kind="agents", name="a", scope="project_shared", apply=True, confirm_project_shared=True
    )
    assert out.startswith("refused:")
    # Engine reason names the divergent sources + the disambiguation flag
    # (verbatim, shared with the web picker — CLI-flavored "--from" wording).
    assert "distinct" in out
    assert "claude" in out and "gemini" in out
    assert not _store_exists(proj, "agents", "a")


async def test_apply_inferred_scope_refused(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    out = await mem_context_pull(kind="agents", name="a", apply=True)
    assert out.startswith("error:")
    assert "explicit scope='project_shared'" in out
    assert not _store_exists(proj, "agents", "a")


async def test_apply_overwrite_refused_without_flag(proj: Path) -> None:
    d = canonical_artifact_dir("agents", "project_shared", proj) / "a"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(_agent("a", "STORE"), encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "RUNTIME")})
    out = await mem_context_pull(
        kind="agents", name="a", scope="project_shared", apply=True, confirm_project_shared=True
    )
    assert out.startswith("refused:")
    assert "STORE" in _canonical_agent_text(proj, "a")  # untouched


async def test_apply_identical_noop(proj: Path) -> None:
    body = _agent("a", "same")
    d = canonical_artifact_dir("agents", "project_shared", proj) / "a"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(body, encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": body})
    out = await mem_context_pull(
        kind="agents",
        name="a",
        scope="project_shared",
        apply=True,
        overwrite=True,
        confirm_project_shared=True,
    )
    assert "identical" in out.lower()
    assert not out.startswith(("error:", "refused:"))


# ── skills overwrite — the PR-G independence guard ─────────────────────────────


async def test_skills_overwrite_refused_cleanly(proj: Path) -> None:
    """A skills Pull over an existing Store entry is a clean ``refused:`` today
    (``skills_overwrite_unsupported``) — MCP inherits it with no PR-G work."""
    d = canonical_artifact_dir("skills", "project_shared", proj) / "s"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s\n---\nold\n", encoding="utf-8")
    seed_multi_runtime(proj, "skills", "s", {"claude": "---\nname: s\n---\nnew\n"})
    out = await mem_context_pull(
        kind="skills",
        name="s",
        scope="project_shared",
        apply=True,
        overwrite=True,
        confirm_project_shared=True,
    )
    assert out.startswith("refused:")
    assert "not yet supported" in out or "delete the canonical" in out


# ── consent gates (needs confirmation:) ───────────────────────────────────────


async def test_project_shared_needs_confirmation_then_writes(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    blocked = await mem_context_pull(kind="agents", name="a", scope="project_shared", apply=True)
    assert blocked.startswith("needs confirmation:")
    assert "confirm_project_shared" in blocked
    assert not _store_exists(proj, "agents", "a")

    ok = await mem_context_pull(
        kind="agents", name="a", scope="project_shared", apply=True, confirm_project_shared=True
    )
    assert "Pulled agents/a" in ok
    assert _store_exists(proj, "agents", "a")


async def test_user_tier_needs_allow_host_writes_and_redacts_path(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")}, scope="user")
    blocked = await mem_context_pull(kind="agents", name="a", scope="user", apply=True)
    assert blocked.startswith("needs confirmation:")
    assert "allow_host_writes" in blocked
    # Host path disclosed HOME-relative (username / $HOME never leaks to the
    # agent transcript — canonical-path-leak discipline).
    assert "~/.memtomem" in blocked
    assert str(Path.home()) not in blocked
    assert not _store_exists(proj, "agents", "a", scope="user")

    ok = await mem_context_pull(
        kind="agents", name="a", scope="user", apply=True, allow_host_writes=True
    )
    assert "Pulled agents/a" in ok
    assert _store_exists(proj, "agents", "a", scope="user")


# ── Gate A (privacy block:) + force valve literal-true ─────────────────────────


async def test_project_shared_secret_hard_refused_even_with_force(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")})
    out = await mem_context_pull(
        kind="agents",
        name="a",
        scope="project_shared",
        apply=True,
        confirm_project_shared=True,
        force_unsafe_import=True,
    )
    assert out.startswith("privacy block:")
    assert not _store_exists(proj, "agents", "a")


async def test_user_tier_secret_bypassable_with_literal_true(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")}, scope="user")
    # Without the valve: bypassable Gate A blocks and offers the valve.
    blocked = await mem_context_pull(
        kind="agents", name="a", scope="user", apply=True, allow_host_writes=True
    )
    assert blocked.startswith("privacy block:")
    assert "force_unsafe_import=True" in blocked
    assert not _store_exists(proj, "agents", "a", scope="user")

    # Literal True opens the valve (user tier is bypassable).
    ok = await mem_context_pull(
        kind="agents",
        name="a",
        scope="user",
        apply=True,
        allow_host_writes=True,
        force_unsafe_import=True,
    )
    assert "Pulled agents/a" in ok
    assert _store_exists(proj, "agents", "a", scope="user")


@pytest.mark.parametrize(
    "flag",
    ["overwrite", "apply", "force_unsafe_import", "allow_host_writes", "confirm_project_shared"],
)
@pytest.mark.parametrize("bad", ["false", "true", 1, 0, "yes", None])
async def test_stringified_booleans_are_refused_via_mem_do(
    proj: Path, flag: str, bad: object
) -> None:
    """``mem_do`` forwards params raw. A stringified boolean must be REFUSED,
    not coerced — ``"false"`` is truthy in Python, so a coercing implementation
    would turn a declined consent into a write to the git-tracked tier."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    out = await mem_do(
        action="context_pull",
        params={"kind": "agents", "name": "a", "scope": "project_shared", flag: bad},
    )
    assert out.startswith("error:")
    assert flag in out
    assert "literal boolean" in out
    assert not _store_exists(proj, "agents", "a")


async def test_apply_false_string_does_not_write(proj: Path) -> None:
    """The headline of the strict-bool guard: apply="false" must NOT execute."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    out = await mem_do(
        action="context_pull",
        params={
            "kind": "agents",
            "name": "a",
            "scope": "project_shared",
            "apply": "false",
            "confirm_project_shared": "false",
        },
    )
    assert out.startswith("error:")
    assert not _store_exists(proj, "agents", "a")


def test_pull_reason_scrubs_residual_absolute_paths() -> None:
    """Paths under neither the project root nor the frozen ``$HOME`` (a
    symlinked runtime dir, an odd mount) are scrubbed — the neutral twin of the
    web ``_redact_pull_reason`` backstop."""
    from memtomem.server.tools.context import _redact_pull_reason

    out = _redact_pull_reason(
        "could not read '/Volumes/My Drive/secrets/skill.md': EACCES",
        Path("/some/project"),
    )
    assert "/Volumes" not in out
    assert "My Drive" not in out
    assert "<path>" in out


async def test_force_unsafe_string_does_not_bypass_via_mem_do(proj: Path) -> None:
    """``mem_do`` forwards params raw, so a string ``"true"`` must NOT open the
    Gate A valve. Strict-bool refuses it outright (stricter than the web
    ``_only_literal_true``, which silently coerces to False) — either way the
    secret never lands."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")}, scope="user")
    out = await mem_do(
        action="context_pull",
        params={
            "kind": "agents",
            "name": "a",
            "scope": "user",
            "apply": True,
            "allow_host_writes": True,
            "force_unsafe_import": "true",  # string, not literal True
        },
    )
    assert out.startswith("error:")
    assert "force_unsafe_import" in out
    assert not _store_exists(proj, "agents", "a", scope="user")


# ── PullApplyStatus parity: every status has a rendering branch ────────────────


def test_pull_status_buckets_are_exhaustive() -> None:
    """Every ``PullApplyStatus`` member must be classified into exactly one
    rendering bucket. Adding an engine status without classifying it fails HERE
    (and renders fail-closed as ``error: unhandled Pull status`` at runtime),
    rather than silently inheriting a benign ``refused:`` prefix."""
    from typing import get_args

    from memtomem.context.pull_apply import PullApplyStatus
    from memtomem.server.tools.context import (
        _PULL_ERROR_STATUSES,
        _PULL_REFUSAL_STATUSES,
    )

    statuses = set(get_args(PullApplyStatus))
    explicit = {"applied", "gate_blocked"}  # handled by their own branches
    covered = explicit | set(_PULL_ERROR_STATUSES) | set(_PULL_REFUSAL_STATUSES)
    assert covered == statuses, (
        f"unclassified={sorted(statuses - covered)}, stale={sorted(covered - statuses)}"
    )
    # Buckets must not overlap — a status renders with exactly one prefix.
    assert not (_PULL_ERROR_STATUSES & _PULL_REFUSAL_STATUSES)
    # The four the web route maps to non-200 are exactly the MCP "error:" set.
    assert set(_PULL_ERROR_STATUSES) == {
        "lock_timeout",
        "plan_stale",
        "snapshot_failed",
        "write_failed",
    }


def test_unknown_status_fails_closed() -> None:
    """An unclassified status renders as ``error:``, never ``refused:``."""
    from memtomem.context.pull_apply import PullApplyResult
    from memtomem.server.tools.context import _format_pull_result

    bogus = PullApplyResult(
        status="some_future_status",  # type: ignore[arg-type]
        kind="agents",
        name="a",
        scope="user",
        reason="whatever",
    )
    out = _format_pull_result(bogus, Path("/nonexistent"))
    assert out.startswith("error:")
    assert "unhandled Pull status" in out
