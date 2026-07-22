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
* a skills overwrite-Pull refuses cleanly (``refused: canonical_exists``) without
  ``overwrite`` and succeeds with it (ADR-0030 §10 / PR-G4b) — MCP inherits the
  engine transaction with no MCP-side change;
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
from memtomem.server.tools.context import _PULL_BOOL_FLAGS, mem_context_pull
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


def _pull_arg_model() -> type:
    """The pydantic arg model FastMCP validates a direct ``mem_context_pull``
    call against — built from the function so it is available in every tool
    mode (the tool itself is pruned from ``mcp`` outside ``full``)."""
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    return func_metadata(mem_context_pull).arg_model


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
        # Pin the InvalidNameError wording — a bare "name" needle appears in
        # nearly every message in this table, so the row could not fail for its
        # own reason.
        ({"kind": "agents", "name": "bad name!"}, "must match [A-Za-z0-9._-]+"),
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
    # The engine names the divergent sources; the REMEDIATION is MCP's own
    # parameter, never the CLI flag this reason used to hard-code (#1869).
    assert "distinct" in out
    assert "claude" in out and "gemini" in out
    assert 'Re-call with from_runtime="<runtime>" to choose a source.' in out
    assert "--from" not in out
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
    assert "Re-call with overwrite=True to replace it." in out
    assert "--overwrite" not in out
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


# ── skills overwrite (ADR-0030 §10 / PR-G4b) ───────────────────────────────────


async def test_skills_overwrite_without_flag_is_refused(proj: Path) -> None:
    """A skills Pull over an existing Store entry WITHOUT ``overwrite`` is a
    clean ``refused: canonical_exists`` — the same posture as agents/commands."""
    d = canonical_artifact_dir("skills", "project_shared", proj) / "s"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s\n---\nold\n", encoding="utf-8")
    seed_multi_runtime(proj, "skills", "s", {"claude": "---\nname: s\n---\nnew\n"})
    out = await mem_context_pull(
        kind="skills",
        name="s",
        scope="project_shared",
        apply=True,
        confirm_project_shared=True,
    )
    assert out.startswith("refused:")
    assert "canonical_exists" in out or "will not replace it" in out


async def test_skills_overwrite_succeeds_over_mcp(proj: Path) -> None:
    """With ``overwrite=True`` the skills Pull snapshots the pre-image and swaps
    the runtime copy in — MCP inherits the engine transaction with no MCP work."""
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
    assert not out.startswith(("error:", "refused:", "privacy block:"))
    assert "new" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert (d / "versions" / "v1").is_dir()


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
    # No valve exists on the git-tracked tier (ADR-0011 §5) — and the refusal
    # must not advertise one, even though it carries the same
    # ``privacy_blocked`` code the bypassable tiers use.
    assert "force_unsafe_import=True" not in out
    assert not _store_exists(proj, "agents", "a")


async def test_user_tier_secret_bypassable_with_literal_true(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")}, scope="user")
    # Without the valve: bypassable Gate A blocks and offers the valve.
    blocked = await mem_context_pull(
        kind="agents", name="a", scope="user", apply=True, allow_host_writes=True
    )
    assert blocked.startswith("privacy block:")
    # Exactly ONE valve hint: the engine reason used to carry
    # ``--force-unsafe-import`` while this tool appended its own
    # ``force_unsafe_import=True``, so an agent saw both spellings (#1869).
    assert blocked.count("force_unsafe_import=True") == 1
    assert "--force-unsafe-import" not in blocked
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


@pytest.mark.parametrize("flag", _PULL_BOOL_FLAGS)
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


@pytest.mark.parametrize("flag", _PULL_BOOL_FLAGS)
@pytest.mark.parametrize("bad", ["true", "false", 1, 0, "yes"])
def test_fastmcp_boundary_rejects_non_literal_booleans(flag: str, bad: object) -> None:
    """The OTHER dispatch path: a direct ``mem_context_pull`` tool call goes
    through FastMCP's pydantic arg model, which is LAX by default and would
    coerce ``"true"`` / ``1`` / ``"yes"`` → ``True`` before the function body's
    ``_strict_bool`` ever runs — opening the Gate A valve with a non-literal
    value, which the web ``_only_literal_true`` refuses. ``StrictBool``
    annotations close that; real JSON booleans still pass (below).

    Built via ``func_metadata`` rather than the tool manager so this runs in
    EVERY tool mode — the tool is pruned from ``mcp`` in the default ``core``
    mode (which CI uses), and a skip here would be a false green on the
    security-relevant path.

    Pins ``ValidationError`` specifically: this is the guard on the Gate A
    valve, so a bare ``Exception`` would let a typo'd field, a renamed param, or
    an import error inside ``_pull_arg_model`` pass as a "rejection"."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _pull_arg_model().model_validate({"kind": "agents", "name": "a", flag: bad})


def test_fastmcp_boundary_accepts_real_booleans() -> None:
    """Guard the other direction: StrictBool must not break legitimate calls."""
    model = _pull_arg_model()
    for val in (True, False):
        m = model.model_validate({"kind": "agents", "name": "a", "force_unsafe_import": val})
        assert m.force_unsafe_import is val
    # The wire schema stays a plain boolean — clients see no StrictBool leak.
    schema = model.model_json_schema()["properties"]["force_unsafe_import"]
    assert schema["type"] == "boolean"


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


def test_gate_blocked_reason_is_redacted() -> None:
    """``gate_blocked`` goes through the same redaction as every other branch.

    Pull's engine reason is runtime + scope only (no path) today, so this is a
    pure backstop — but the earlier code exempted it on a
    ``mem_context_sync``-style "keep the full path for remediation" rationale
    that does not hold here, which would have become a leak the first time the
    engine enriched that reason. The web twin redacts it for the same reason.
    """
    from memtomem.context.pull_apply import PullApplyResult
    from memtomem.server.tools.context import _format_pull_result

    blocked = PullApplyResult(
        status="gate_blocked",
        kind="agents",
        name="a",
        scope="user",
        reason="Gate A flagged the copy at /Volumes/secret/stuff/agent.md",
        force_bypassable=True,
    )
    out = _format_pull_result(blocked, Path("/some/project"))
    assert out.startswith("privacy block:")
    assert "/Volumes" not in out
    assert "<path>" in out
    assert "force_unsafe_import=True" in out  # the valve hint survives


def test_identical_noop_never_renders_empty() -> None:
    """A future ``identical`` result without a reason must not produce an empty
    MCP response (``_redact_reason`` coalesces ``None`` → ``""``)."""
    from memtomem.context.pull_apply import PullApplyResult
    from memtomem.server.tools.context import _format_pull_result

    out = _format_pull_result(
        PullApplyResult(
            status="applied",
            kind="agents",
            name="a",
            scope="user",
            reason="",
            write_outcome="identical",
        ),
        Path("/some/project"),
    )
    assert out.strip()
    assert "already identical" in out


@pytest.mark.parametrize(
    ("path", "leaked"),
    [
        ("/Volumes/My Drive/secrets/skill.md", "/Volumes"),
        ("/secretmount", "/secretmount"),
        (r"C:\secretmount", r"C:\secretmount"),
    ],
)
def test_pull_reason_scrubs_residual_absolute_paths(path: str, leaked: str) -> None:
    """Paths under neither the project root nor the frozen ``$HOME`` (a
    symlinked runtime dir, an odd mount) are scrubbed — the neutral twin of the
    web ``_redact_pull_reason`` backstop."""
    from memtomem.server.tools.context import _redact_pull_reason

    out = _redact_pull_reason(
        f"could not read '{path}': EACCES",
        Path("/some/project"),
    )
    assert leaked not in out
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
    # The five the web route maps to non-200 are exactly the MCP "error:" set.
    # ``swap_recovery_pending`` is a 409 there, so it belongs here rather than
    # with the refusals — that bucket is defined as "what the web route returns
    # as a typed 200", and it is also not an actionable refusal (the
    # remediation is an operator inspecting paths, not a parameter to change).
    assert set(_PULL_ERROR_STATUSES) == {
        "lock_timeout",
        "plan_stale",
        "snapshot_failed",
        "write_failed",
        "swap_recovery_pending",
    }


def test_swap_recovery_pending_renders_as_error_not_refused() -> None:
    """A wedged artifact must not inherit the benign ``refused:`` prefix.

    ``refused:`` reads as "your request was declined, adjust and retry"; this
    state needs an operator to look at two paths on disk. The reason is
    redacted like every other one.
    """
    from memtomem.context.pull_apply import PullApplyResult
    from memtomem.server.tools.context import _format_pull_result

    result = PullApplyResult(
        status="swap_recovery_pending",
        kind="skills",
        name="demo",
        scope="user",
        reason="an interrupted directory swap left two candidate trees",
        reason_code="swap_recovery_pending",
    )
    out = _format_pull_result(result, Path("/nonexistent"))
    assert out.startswith("error:")
    assert "unhandled Pull status" not in out
    assert "interrupted directory swap" in out


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
