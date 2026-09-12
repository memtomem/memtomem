"""ADR-0030 PR-C — the ``mm context pull`` CLI surface.

Pins the CLI translation of the prepare/commit engine (engine semantics live
in ``test_context_pull_apply.py``): the dry-run preview + ``--diff`` / ``--json``,
the flag-combination guards, the §5 ``source_conflict`` rendering, the
scope-explicitness + Gate B confirmation, and the pre-confirmation early
refusals.
"""

from __future__ import annotations

import json
import logging
import re
from importlib import metadata
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.scope_resolver import canonical_artifact_dir

from .helpers import consent_lines, seed_multi_runtime

_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _invoke(args: list[str], **kw: object):
    return CliRunner().invoke(context, args, **kw)  # type: ignore[arg-type]


def _canonical_agent_text(proj: Path, name: str) -> str:
    d = canonical_artifact_dir("agents", "project_shared", proj) / name
    return (d / "agent.md").read_text(encoding="utf-8")


# ── dry-run preview ───────────────────────────────────────────────────────────


def test_preview_default_no_writes(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "c"), "gemini": _agent("a", "g")}
    )
    res = _invoke(["pull", "agents", "a"])
    assert res.exit_code == 0
    assert "ambiguous" in res.output
    assert "Run with --apply" in res.output
    # No canonical written.
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_preview_diff_shows_unified(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "hello world")})
    res = _invoke(["pull", "agents", "a", "--diff"])
    assert res.exit_code == 0
    assert "hello world" in res.output
    assert "+++" in res.output


def test_preview_from_narrows_to_source(proj: Path) -> None:
    """`--from gemini` narrows the preview + diff to gemini (not all candidates)."""
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "CLAUDE"), "gemini": _agent("a", "GEM")}
    )
    res = _invoke(["pull", "agents", "a", "--from", "gemini", "--diff"])
    assert res.exit_code == 0
    assert "gemini" in res.output
    assert "claude" not in res.output  # filtered out
    assert "source: gemini" in res.output


def test_preview_json_shape(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["kind"] == "agents"
    assert data["name"] == "a"
    assert "candidates" in data and data["candidates"][0]["runtime"] == "claude"


# ── flag guards (exit 2) ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ["pull", "agents", "a", "--yes"],
        ["pull", "agents", "a", "--diff", "--apply"],
        ["pull", "agents", "a", "--json", "--apply"],
        ["pull", "agents", "a", "--diff", "--json"],
    ],
)
def test_flag_combo_usage_errors(proj: Path, args: list[str]) -> None:
    res = _invoke(args)
    assert res.exit_code == 2


def test_project_local_refused_both_modes(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    for extra in ([], ["--apply"]):
        res = _invoke(["pull", "agents", "a", "--scope", "project_local", *extra])
        assert res.exit_code != 0
        assert "project_local" in res.output


# ── apply ─────────────────────────────────────────────────────────────────────


def test_apply_single_candidate(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "only")})
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--scope", "project_shared", "--confirm-project-shared"]
    )
    assert res.exit_code == 0, res.output
    assert "Pulled agents/a from claude" in res.output
    assert "only" in _canonical_agent_text(proj, "a")


def test_apply_source_conflict_refuses(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "c"), "gemini": _agent("a", "g")}
    )
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--scope", "project_shared", "--confirm-project-shared"]
    )
    assert res.exit_code != 0
    assert "Pass --from <runtime>" in res.output
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_apply_from_lands_chosen(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "CLAUDE"), "gemini": _agent("a", "GEM")}
    )
    res = _invoke(
        [
            "pull",
            "agents",
            "a",
            "--apply",
            "--from",
            "gemini",
            "--scope",
            "project_shared",
            "--confirm-project-shared",
        ]
    )
    assert res.exit_code == 0, res.output
    assert "GEM" in _canonical_agent_text(proj, "a")


def test_from_codex_agents_is_export_only(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--from", "codex"])
    assert res.exit_code != 0
    assert "codex" in res.output.lower()


def test_from_unknown_runtime(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--from", "bogus"])
    # click.Choice on --from? No: --from is a free RUNTIME; engine rejects it.
    assert res.exit_code != 0


# ── overwrite / identical ─────────────────────────────────────────────────────


def test_apply_overwrite_refused_without_flag(proj: Path) -> None:
    # Seed a Store agent, then a divergent runtime copy.
    d = canonical_artifact_dir("agents", "project_shared", proj) / "a"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(_agent("a", "STORE"), encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "RUNTIME")})
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--scope", "project_shared", "--confirm-project-shared"]
    )
    assert res.exit_code != 0
    assert "--overwrite" in res.output


def test_apply_identical_noop(proj: Path) -> None:
    body = _agent("a", "same")
    d = canonical_artifact_dir("agents", "project_shared", proj) / "a"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(body, encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": body})
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--scope", "project_shared", "--confirm-project-shared"]
    )
    assert res.exit_code == 0, res.output
    assert "already identical" in res.output


def test_skills_overwrite_succeeds(proj: Path) -> None:
    """ADR-0030 §10 / PR-G4b: a skills overwrite-Pull now snapshots the pre-image
    into ``versions/v1/`` and swaps the runtime copy in (was a hard refusal)."""
    d = canonical_artifact_dir("skills", "project_shared", proj) / "s"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s\n---\nold\n", encoding="utf-8")
    seed_multi_runtime(proj, "skills", "s", {"claude": "---\nname: s\n---\nnew\n"})
    res = _invoke(
        [
            "pull",
            "skills",
            "s",
            "--apply",
            "--overwrite",
            "--scope",
            "project_shared",
            "--confirm-project-shared",
        ]
    )
    assert res.exit_code == 0, res.output
    assert "new" in (d / "SKILL.md").read_text(encoding="utf-8")
    assert (d / "versions" / "v1").is_dir()


# ── scope explicitness + Gate B (R1 Blocker 2) ────────────────────────────────


def test_apply_inferred_scope_refused(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--apply", "--yes"])
    assert res.exit_code != 0
    assert "explicit --scope project_shared" in res.output


def test_project_shared_prompt_decline_writes_nothing(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="n\n")
    assert res.exit_code != 0  # aborted
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_project_shared_prompt_accept_writes(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="y\n")
    assert res.exit_code == 0, res.output
    assert "c" in _canonical_agent_text(proj, "a")


# ── ADR-0011 §5 consent audit (#2306, #2318) ──────────────────────────────────
#
# #2318 gave this command the ``--confirm-project-shared`` every other CLI
# surface takes, so the AST guard in
# ``test_project_shared_confirmation_audit_guard.py`` can finally see it. What
# that guard buys is narrow — it only checks that the emit outlives the
# conditional. Whether ``--yes`` is still accepted, which flag the line names,
# and how many lines are emitted are all invisible to it, so the tests below
# remain the only thing holding those.


def test_prompt_accepted_records_the_consent(proj: Path, caplog) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="y\n")
    assert res.exit_code == 0, res.output
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "project_shared.confirmed_via=cli_context_pull" in lines[0]
    assert "mechanism=prompt" in lines[0]
    assert "action=pull" in lines[0]


def test_yes_flag_records_the_consent_and_names_the_flag(proj: Path, caplog) -> None:
    """``--yes`` still carries Gate B during the #2318 window, so it is
    recorded — and the line names which flag carried it, since ``--yes`` is
    not the ``--confirm-project-shared`` the other surfaces require."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code == 0, res.output
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "mechanism=flag" in lines[0]
    assert "flag='--yes'" in lines[0]


# ── ADR-0011 §5 parity: --confirm-project-shared and the --yes window (#2318) ──
#
# ``mm context pull`` was the one CLI surface where a generic ``--yes``
# satisfied Gate B, diverging from its own ``mem_context_pull`` tool and web
# route. It now takes the standard flag; ``--yes`` keeps working through 0.6.x
# behind a notice and stops satisfying Gate B in 0.7.0. Each test below pins
# exactly one cell of that contract, because the flip in 0.7.0 has to be able
# to change one row without silently rewriting the others.

_FLIP_NOTICE = "--yes alone will stop satisfying Gate B"


def _seed_one(proj: Path, scope: str = "project_shared") -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")}, scope=scope)


def test_confirm_flag_carries_gate_b_without_a_prompt(proj: Path, caplog) -> None:
    """The parity case: the standard flag alone lands the pull."""
    _seed_one(proj)
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(
            ["pull", "agents", "a", "--apply", "--scope", "project_shared"],
            input="",  # no prompt may be consumed
        )
    assert res.exit_code != 0  # sanity: without either flag it *does* prompt

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(
            [
                "pull",
                "agents",
                "a",
                "--apply",
                "--scope",
                "project_shared",
                "--confirm-project-shared",
            ]
        )
    assert res.exit_code == 0, res.output
    assert "c" in _canonical_agent_text(proj, "a")
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "mechanism=flag" in lines[0]
    assert "flag='--yes'" not in lines[0]


def test_confirm_flag_emits_no_deprecation_notice(proj: Path) -> None:
    _seed_one(proj)
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--scope", "project_shared", "--confirm-project-shared"]
    )
    assert res.exit_code == 0, res.output
    assert _FLIP_NOTICE not in res.output


def test_yes_alone_warns_on_stderr_and_still_applies(proj: Path) -> None:
    """The window's whole point: an existing ``--yes`` script keeps working,
    and is told once, on stderr, that it will not in 0.7.0."""
    _seed_one(proj)
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code == 0, res.output
    assert "c" in _canonical_agent_text(proj, "a")
    assert res.stderr.count(_FLIP_NOTICE) == 1
    assert "--confirm-project-shared" in res.stderr
    assert "0.7.0" in res.stderr
    assert _FLIP_NOTICE not in res.stdout


def test_both_flags_the_explicit_one_wins(proj: Path, caplog) -> None:
    """Precedence, and the reason it is computed once: with both flags the
    consent belongs to ``--confirm-project-shared``, so the line must not
    name ``--yes`` and the migration notice must not fire."""
    _seed_one(proj)
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(
            [
                "pull",
                "agents",
                "a",
                "--apply",
                "--scope",
                "project_shared",
                "--yes",
                "--confirm-project-shared",
            ]
        )
    assert res.exit_code == 0, res.output
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "mechanism=flag" in lines[0]
    assert "flag='--yes'" not in lines[0]
    assert _FLIP_NOTICE not in res.output


def test_prompt_path_emits_no_notice(proj: Path) -> None:
    _seed_one(proj)
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="y\n")
    assert res.exit_code == 0, res.output
    assert _FLIP_NOTICE not in res.output


def test_yes_without_apply_still_fails_before_the_notice(proj: Path) -> None:
    """The flag-combination guard runs first, so a preview never warns."""
    _seed_one(proj)
    res = _invoke(["pull", "agents", "a", "--yes"])
    assert res.exit_code == 2
    assert _FLIP_NOTICE not in res.output


def test_preview_with_the_confirm_flag_neither_warns_nor_consents(proj: Path, caplog) -> None:
    _seed_one(proj)
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(
            ["pull", "agents", "a", "--scope", "project_shared", "--confirm-project-shared"]
        )
    assert res.exit_code == 0, res.output
    assert consent_lines(caplog) == []
    assert _FLIP_NOTICE not in res.output
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_user_tier_yes_keeps_its_ordinary_meaning(proj: Path, caplog) -> None:
    """``--yes`` was never Gate B on the user tier, so nothing changes there:
    it still skips the prompt, warns about nothing, and records no consent."""
    _seed_one(proj, scope="user")
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(["pull", "agents", "a", "--apply", "--scope", "user", "--yes"])
    assert res.exit_code == 0, res.output
    assert consent_lines(caplog) == []
    assert _FLIP_NOTICE not in res.output


def test_user_tier_confirm_flag_does_not_skip_the_prompt(proj: Path, caplog) -> None:
    """The pin against widening the gate past ``project_shared``.
    ``--confirm-project-shared`` authorises the git-tracked tier and nothing
    else, so on the user tier the ordinary confirm still governs — and
    declining it must write nothing and record no consent.

    The nonzero exit alone would not show that: an implementation that
    rejected the flag outright with a usage error would also exit nonzero
    without ever prompting. So this asserts the prompt's own words."""
    _seed_one(proj, scope="user")
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(
            ["pull", "agents", "a", "--apply", "--scope", "user", "--confirm-project-shared"],
            input="n\n",
        )
    assert res.exit_code != 0
    assert "Pull agents/a from claude into user?" in res.output
    assert consent_lines(caplog) == []
    assert not (canonical_artifact_dir("agents", "user", None) / "a").exists()


def test_gate_a_block_warns_but_records_no_consent(proj: Path, caplog) -> None:
    """Notice is not consent. ``prepare_pull`` refuses before Gate B is ever
    reached, so the migration signal still fires — an automation owner needs
    it on a refused run too — while the audit line, which records an
    authorised write, must not."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", _SECRET)})
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code != 0
    assert "Gate A" in res.output
    assert res.stderr.count(_FLIP_NOTICE) == 1
    assert consent_lines(caplog) == []
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_the_yes_deprecation_window_expires_at_0_7_0() -> None:
    """The window is a promise with a date on it; this is the alarm clock.

    Nothing else in the suite notices when that date passes. The AST guard is
    green whether or not ``--yes`` is accepted — it only checks that a gate
    which mentions the flag also emits — and every test above is *written for*
    the window, so they all keep passing too. A 0.7.0 release with the
    compatibility block still in place would therefore ship green, and the
    deprecation would quietly become permanent, which is the outcome ADR-0011
    §5 objects to in the first place.

    So the version bump itself has to be what goes red. When it does, the flip
    is: turn the ``deprecated_yes`` block in ``pull_cmd`` into the siblings'
    refusal, rewrite (do not delete) the three window tests above —
    ``test_yes_flag_records_the_consent_and_names_the_flag``,
    ``test_yes_alone_warns_on_stderr_and_still_applies`` and
    ``test_gate_a_block_warns_but_records_no_consent``, the last of which
    should then pin that the Gate B refusal precedes Gate A — and delete this
    test.

    The threshold was 0.6.0 until 2026-09-10. Stage 1 landed after 0.5.0 had
    shipped and no 0.5.x release followed it, so the "through 0.5.x" window it
    promised was never installable: released 0.5.0 has no
    ``--confirm-project-shared`` on ``pull`` at all. Stage 1 therefore ships in
    0.6.0 — the release the window actually spends — and this alarm moved with
    it. A window is counted in published releases, not in merges to ``main``.

    That argument is spent once 0.6.0 ships: from then on users have had a
    release that warned them, and this threshold is a deadline rather than a
    dial. Moving it again is a maintainer decision that needs its own reason
    recorded here — not a routine edit to keep the suite green.
    """
    raw = metadata.version("memtomem")
    match = re.match(r"^(\d+)\.(\d+)", raw)
    assert match is not None, f"unparseable version {raw!r}"
    assert (int(match.group(1)), int(match.group(2))) < (0, 7), (
        f"memtomem {raw} still accepts a bare --yes as Gate B on "
        "`mm context pull`. The #2318 deprecation window ended at 0.7.0 — see "
        "this test's docstring for the flip."
    )


def test_declined_prompt_records_no_consent(proj: Path, caplog) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="n\n")
    assert res.exit_code != 0
    assert consent_lines(caplog) == []


def test_preview_records_no_consent(proj: Path, caplog) -> None:
    """A preview writes nothing, so it consents to nothing.

    This is the only tier negative available on this command: ``--scope
    user`` needs a user-level runtime the project fixture does not seed,
    and ``project_local`` is rejected outright (ADR-0011 §3).
    """
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        res = _invoke(["pull", "agents", "a", "--scope", "project_shared"])
    assert res.exit_code == 0, res.output
    assert consent_lines(caplog) == []


# ── Gate A ────────────────────────────────────────────────────────────────────


def test_project_shared_secret_hard_refused(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")})
    res = _invoke(
        [
            "pull",
            "agents",
            "a",
            "--apply",
            "--scope",
            "project_shared",
            "--confirm-project-shared",
            "--force-unsafe-import",
        ]
    )
    assert res.exit_code != 0
    assert "Gate A" in res.output
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_gate_blocked_refused_before_prompt(proj: Path) -> None:
    """A project_shared secret is refused BEFORE the confirmation prompt runs
    (input 'y' would otherwise proceed) — Codex Minor 2."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="y\n")
    assert res.exit_code != 0
    assert "Continue?" not in res.output  # never reached the prompt


# ── per-surface remediation hints (#1869) ────────────────────────────────────


def test_canonical_exists_refusal_names_the_cli_flag(proj: Path) -> None:
    """The engine states the condition; the CLI adds ``--overwrite``.

    The reason itself must stay flag-free — that is what lets MCP and the web
    render ``overwrite=True`` / the Overwrite checkbox for the same refusal.
    """
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")}, scope="user")
    assert _invoke(["pull", "agents", "a", "--apply", "--scope", "user", "--yes"]).exit_code == 0
    # Diverge the runtime copy — an identical second pull is a no-op, not a
    # ``canonical_exists`` refusal.
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c2")}, scope="user")

    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "user", "--yes"])
    assert res.exit_code != 0
    assert "a plain pull will not replace it" in res.output  # neutral condition
    assert "Pass --overwrite to replace it." in res.output  # CLI remediation
    assert "overwrite=True" not in res.output  # never another surface's spelling


def test_user_tier_gate_block_names_the_cli_force_flag(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")}, scope="user")
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "user", "--yes"])
    assert res.exit_code != 0
    assert "Pass --force-unsafe-import to bypass after review." in res.output


def test_project_shared_gate_block_offers_no_force_valve(proj: Path) -> None:
    """``privacy_blocked`` on the git-tracked tier carries the same reason code
    as the bypassable tiers, but the valve does not exist there (ADR-0011 §5).
    Keying the hint on the code alone would send the user to a flag that
    hard-refuses — the hint is gated on ``force_bypassable`` instead."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")})
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--scope", "project_shared", "--confirm-project-shared"]
    )
    assert res.exit_code != 0
    assert "--force-unsafe-import" not in res.output
