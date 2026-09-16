"""Settings-derived text is sanitized the same way at every output surface.

Hook commands, event names, matchers, tier paths and engine warnings are read
verbatim out of a user's settings files, so they are untrusted on two axes:
they can carry a credential, and they can carry terminal control sequences.

Before #2477/#2478 each surface decided for itself. One
``mm context settings-doctor`` run printed the same hook command redacted under
its portability heading and verbatim under its duplicate heading; the MCP
settings warnings shipped raw ESC sequences to the caller's transcript beside a
doctor line that escaped them; and the ``event:matcher`` label every engine
warning builds redacted its event half but not its matcher half.

The rules these tests pin:

* redact the secret shape FIRST, escape control characters LAST — the
  secret-shape check has to read the original text;
* structured surfaces (``--json``, the dashboard payload) keep raw values,
  because escaping is not reversible and those surfaces are how a caller
  identifies the offending entry;
* a path scrub never runs on escaped text — ``\\xNN`` backslashes read as path
  separators, so the path regex would swallow the escape and the remediation
  after it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.context.settings import CANONICAL_SETTINGS_FILE, _hook_label
from memtomem.context.settings_doctor import (
    DuplicateTier,
    HookSignature,
    UnscannedSettingsFile,
    format_signature_label,
    format_unscanned_settings_warning,
    format_warning,
    redact_command_shape,
)
from memtomem.server.tools.context import _redact_pull_reason, _redact_reason

from .helpers import set_home

ESC_RAW = "\x1b[2J\x1b[H"
ESC_ESCAPED = "\\x1b[2J\\x1b[H"

#: For anything read back through ``CliRunner``: with colour off Click strips
#: CSI sequences like ``ESC_RAW`` from echoed text, so an unescaped leak of
#: those would vanish before an assertion could see it. BEL is not stripped.
BEL_RAW = "\x07"
BEL_ESCAPED = "\\x07"

#: Assembled at runtime on purpose: a literal AWS-shaped key in the source
#: trips GitHub push protection on this repository.
SECRET = "AKIA" + "1234567890ABCDEF"


def _secret_command() -> str:
    """A hook command whose credential shape must never reach a display."""
    return f"/home/alice/hook.sh --token={SECRET}"


def _forged_command() -> str:
    """A hook command carrying terminal control sequences but no credential.

    The two axes need separate hooks: a secret-shaped command collapses to the
    redaction marker, which would hide whether the control characters were
    escaped or merely swallowed.
    """
    return f"/home/alice/hook.sh # {ESC_RAW}{BEL_RAW}forged"


def _rule(matcher: str, command: str) -> dict:
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": 5000}],
    }


def _write_settings(path: Path, hooks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude").mkdir()
    return root


@pytest.fixture
def forged_duplicate(project_root, fake_home, monkeypatch):
    """Canonical and user tier hold the same hostile hook; active scope is
    project_local, so the user tier is a duplicate."""
    hooks = {
        "PostToolUse": [
            _rule("Edit|Write", _secret_command()),
            _rule("Read", _forged_command()),
        ]
    }
    _write_settings(project_root / CANONICAL_SETTINGS_FILE, hooks)
    _write_settings(fake_home / ".claude" / "settings.json", hooks)
    monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
    monkeypatch.chdir(project_root)
    return project_root


# ── #2477: the doctor's duplicate axis matches its portability axis ──


class TestDoctorDuplicateAxis:
    def test_text_surface_redacts_and_escapes(self, forged_duplicate):
        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 1, result.output
        assert "other tier(s)" in result.output
        assert SECRET not in result.output
        assert "<redacted: secret-shape>" in result.output
        assert BEL_RAW not in result.output
        assert BEL_ESCAPED in result.output

    def test_json_surface_keeps_raw_values(self, forged_duplicate):
        """The identification path, deliberately unescaped and unredacted."""
        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        previews = {e["command_preview"] for e in payload["duplicates"][0]["entries"]}
        assert previews == {_secret_command(), _forged_command()}


# ── #2477: the shared formatters sanitize their own fields ──────────


class TestSharedFormatters:
    def test_duplicate_warning_escapes_tier_path(self):
        warning = format_warning(
            DuplicateTier(
                tier="user",
                path=Path(f"/home/alice/.claude/settings.json{ESC_RAW}"),
                entries=(
                    HookSignature(
                        event="PostToolUse",
                        matcher="Edit|Write",
                        command_shape="mm session start",
                    ),
                ),
            ),
            active_scope="project_local",
        )
        assert ESC_RAW not in warning
        assert ESC_ESCAPED in warning

    def test_unscanned_warning_escapes_reason(self):
        """Defensive: ``_read_settings`` produces a fixed reason vocabulary
        today, so this pins the formatter rather than an end-to-end run."""
        warning = format_unscanned_settings_warning(
            UnscannedSettingsFile(
                source="tier",
                tier="user",
                path=Path("/home/alice/.claude/settings.json"),
                reason=f"invalid JSON{ESC_RAW}",
            )
        )
        assert ESC_RAW not in warning
        assert ESC_ESCAPED in warning

    def test_command_shape_handles_both_axes(self):
        assert redact_command_shape(_secret_command()) == "<redacted: secret-shape>"
        plain = redact_command_shape(f"mm session start{ESC_RAW}")
        assert ESC_RAW not in plain
        assert ESC_ESCAPED in plain

    def test_signature_label_handles_both_halves(self):
        sig = HookSignature(
            event=f"PostToolUse{ESC_RAW}",
            matcher="api" + "_key=" + "B" * 24,
            command_shape="x",
        )
        label = format_signature_label(sig)
        assert ESC_RAW not in label
        assert ESC_ESCAPED in label
        assert "B" * 24 not in label


# ── #2478: engine warnings and reasons on the MCP wire ──────────────


class TestMcpWireReasons:
    def test_control_characters_escaped(self):
        raw = f"Hook event 'PostToolUse{ESC_RAW}forged' has no Codex equivalent."
        out = _redact_reason(raw, Path("/tmp/project"))
        assert ESC_RAW not in out
        assert ESC_ESCAPED in out

    def test_pull_composition_keeps_the_relative_remainder(self):
        """The regression that forced the escape to be the outermost step.

        ``_redact_pull_reason`` runs a second path scrub on top. Escaping
        before that scrub turns ``\\x1b[2J\\x1b[H`` into something the path
        regex reads as a two-segment absolute path, and its greedy tail eats
        the remediation text that follows.
        """
        raw = f"bad event: {ESC_RAW} keep this remediation"
        out = _redact_pull_reason(raw, Path("/tmp/project"))
        assert ESC_RAW not in out
        assert ESC_ESCAPED in out
        assert "keep this remediation" in out

    def test_home_redaction_still_applies(self):
        """The escape must not displace the path redaction it composes with."""
        out = _redact_reason("skipped x: /home/alice/secret/tree/file.json", Path("/tmp/project"))
        assert "/home/alice/secret/tree" not in out


# ── #2478: both halves of an engine warning label are redacted ──────


class TestHookLabelRedaction:
    def test_secret_shaped_matcher_redacted(self):
        secret_matcher = "api" + "_key=" + "C" * 24
        label = _hook_label("PostToolUse", secret_matcher)
        assert secret_matcher not in label
        assert label == "PostToolUse:<redacted: secret-shape>"

    def test_ordinary_label_unchanged(self):
        assert _hook_label("PostToolUse", "Edit|Write") == "PostToolUse:Edit|Write"
        assert _hook_label("SessionStart", "") == "SessionStart"


# ── Sibling fields beside a redacted one (round-2 review) ───────────


SECRET_MATCHER = "api" + "_key=" + "D" * 24


class TestRendererDropWarnings:
    """Redacting the label is not enough when the same warning repeats the
    matcher's tokens one clause later.

    ``_map_kimi_matcher`` / ``_map_gemini_matcher`` split the matcher on ``|``
    and name every token they could not map, so a credential-shaped matcher
    came back in full right after ``<redacted: secret-shape>``.
    """

    def _contributions(self) -> dict:
        return {
            "hooks": {
                "PostToolUse": [_rule(SECRET_MATCHER, "mm session start")],
            }
        }

    def test_kimi_drop_warning_redacts_the_tokens(self):
        from memtomem.context.settings import _render_kimi_hooks

        _body, warnings = _render_kimi_hooks(self._contributions())
        assert warnings, "expected an unmapped-token warning"
        joined = " ".join(warnings)
        assert SECRET_MATCHER not in joined
        assert "<redacted: secret-shape>" in joined

    def test_gemini_drop_warning_redacts_the_tokens(self):
        from memtomem.context.settings import _remap_for_gemini

        _out, warnings = _remap_for_gemini(self._contributions())
        assert warnings, "expected an unmapped-token warning"
        joined = " ".join(warnings)
        assert SECRET_MATCHER not in joined
        assert "<redacted: secret-shape>" in joined


class TestConflictReasons:
    """A conflict reason quotes the entry it collides with — which lives in a
    settings file this project never wrote."""

    def test_migrate_conflict_reason_redacts_the_label(self):
        from memtomem.context.settings_migrate import _classify_target

        sig = HookSignature(
            event="PostToolUse",
            matcher=SECRET_MATCHER,
            command_shape="mm session start",
        )
        canonical_inner = {"type": "command", "command": "mm session start", "timeout": 5000}
        target_index = {
            (sig.event, sig.matcher): [
                {"matcher": sig.matcher, "hooks": [{"type": "command", "command": "other"}]}
            ]
        }
        state, reason = _classify_target(target_index, sig, canonical_inner)
        assert state == "conflict"
        assert SECRET_MATCHER not in reason
        assert "<redacted: secret-shape>" in reason

    def test_copy_conflict_reason_redacts_the_colliding_command(self):
        """The colliding command belongs to the DESTINATION project."""
        from memtomem.context.settings_copy import _classify_leg

        sig = HookSignature(
            event="PostToolUse",
            matcher="Edit|Write",
            command_shape="mm session start",
        )
        canonical_inner = {"type": "command", "command": "mm session start", "timeout": 5000}
        doc = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [{"type": "command", "command": _secret_command()}],
                    }
                ]
            }
        }
        state, reason = _classify_leg(doc, sig, canonical_inner, leg="canonical")
        assert state == "conflict"
        assert SECRET not in reason
        assert "<redacted: secret-shape>" in reason


class TestCopySelectorErrors:
    """The copy selector's failure messages quote the canonical file too."""

    def test_available_label_listing_is_redacted(self, tmp_path):
        """``plan_hook_copy`` lists the canonical's labels when the selector
        matches nothing, so asking about one hook printed another's matcher."""
        from memtomem.context.settings import CANONICAL_SETTINGS_FILE as CANON
        from memtomem.context.settings_copy import plan_hook_copy

        src = tmp_path / "src-proj"
        _write_settings(
            src / CANON,
            {"PostToolUse": [_rule(SECRET_MATCHER, "mm session start")]},
        )
        dst = tmp_path / "dst-proj"
        (dst / ".memtomem").mkdir(parents=True)

        with pytest.raises(ValueError) as excinfo:
            plan_hook_copy(
                src,
                event="SessionStart",
                matcher="",
                dst_project_root=dst,
                dst_scope="project_shared",
            )
        message = str(excinfo.value)
        assert "available:" in message, message
        assert SECRET_MATCHER not in message
        assert "<redacted: secret-shape>" in message

    @pytest.mark.parametrize(
        ("hook_command", "error_name"),
        [
            pytest.param("no-such-substring", "HookNotFoundError", id="hook-command-matches-none"),
            pytest.param(None, "AmbiguousHookSelectorError", id="ambiguous-selector"),
        ],
    )
    def test_candidate_listing_redacts_commands(self, tmp_path, hook_command, error_name):
        """Both selector failures list every candidate command in the canonical.

        The caller asked about one hook; the listing shows all of them, so an
        inline credential in an unrelated entry was printed for the asking.
        """
        from memtomem.context.settings import CANONICAL_SETTINGS_FILE as CANON
        from memtomem.context.settings_copy import plan_hook_copy

        src = tmp_path / "src-proj"
        _write_settings(
            src / CANON,
            {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {"type": "command", "command": _secret_command()},
                            {"type": "command", "command": "mm session start"},
                        ],
                    }
                ]
            },
        )
        dst = tmp_path / "dst-proj"
        (dst / ".memtomem").mkdir(parents=True)

        with pytest.raises(ValueError) as excinfo:
            plan_hook_copy(
                src,
                event="PostToolUse",
                matcher="Edit|Write",
                hook_command=hook_command,
                dst_project_root=dst,
                dst_scope="project_shared",
            )
        assert type(excinfo.value).__name__ == error_name
        message = str(excinfo.value)
        assert "candidates:" in message, message
        assert SECRET not in message
        assert "<redacted: secret-shape>" in message
        # the unrelated, non-secret candidate is still listed, so the error
        # keeps telling the caller what they could have selected
        assert "mm session start" in message

    def test_non_list_rules_conflict_redacts_the_event(self):
        from memtomem.context.settings_copy import _classify_leg

        sig = HookSignature(
            event="api" + "_key=" + "E" * 24,
            matcher="",
            command_shape="mm session start",
        )
        state, reason = _classify_leg(
            {"hooks": {sig.event: "not-a-list"}},
            sig,
            {"type": "command", "command": "mm session start"},
            leg="canonical",
        )
        assert state == "conflict"
        assert sig.event not in reason
        assert "<redacted: secret-shape>" in reason


# ── Producers redact, displays escape (fresh-eyes review) ───────────


class TestProducersDoNotEscape:
    """A reason built by a producer also reaches ``--json``.

    Escaping is irreversible, so a producer that escapes hands every structured
    consumer a string it cannot turn back into the original, and a display that
    escapes again sees ``\\\\x1b`` it cannot tell from real text. The producer
    removes the secret shape; the display escapes.

    One exception predates #2477 and is pinned in
    :class:`TestReprCommandPreviewException`: settings-copy's command previews
    are ``repr()``-quoted, which escapes as a side effect.
    """

    def test_migrate_conflict_reason_keeps_control_characters(self):
        from memtomem.context.settings_migrate import _classify_target

        sig = HookSignature(event="PostToolUse", matcher=f"Edit{ESC_RAW}", command_shape="x")
        state, reason = _classify_target(
            {(sig.event, sig.matcher): [{"matcher": sig.matcher, "hooks": [{"command": "y"}]}]},
            sig,
            {"type": "command", "command": "x"},
        )
        assert state == "conflict"
        assert ESC_RAW in reason
        assert ESC_ESCAPED not in reason

    def test_copy_conflict_reason_keeps_control_characters_in_the_label(self):
        from memtomem.context.settings_copy import _classify_leg

        sig = HookSignature(event="PostToolUse", matcher=f"Edit{ESC_RAW}", command_shape="x")
        doc = {"hooks": {"PostToolUse": [{"matcher": sig.matcher, "hooks": [{"command": "y"}]}]}}
        state, reason = _classify_leg(
            doc, sig, {"type": "command", "command": "x"}, leg="canonical"
        )
        assert state == "conflict"
        assert f"PostToolUse:Edit{ESC_RAW}" in reason


# ── The one producer-side escape: repr'd command previews ──────────
#
# Goldens were produced by running these exact inputs through settings_copy.py
# at d9ed6976 (before this PR) and at this PR's head: both gave identical
# strings for every secret-free input. They are written out as literals, not
# rebuilt with repr() here, so a change in formatting cannot pass by changing
# the expectation along with the code.

_BEL_COMMAND = "hook.sh \x07ring"
_LITERAL_ESCAPE_COMMAND = "hook.sh \\x07literal"
_QUOTED_COMMAND = "hook.sh 'a' \"b\" c\\d"

_GOLDEN_CONFLICT_REASON = (
    r"""destination canonical settings already has a rule under 'PostToolUse:Edit' whose inner hooks differ """
    r"""from the copied entry (existing: 'hook.sh \x07ring'; 'hook.sh \\x07literal'; """
    r"""'hook.sh \'a\' "b" c\\d'). Resolve manually, then re-run the copy — a """
    r"""same-matcher duplicate would fire twice."""
)
_GOLDEN_NO_MATCH = (
    r"""--hook-command 'no-such-substring' matches none of the entries under """
    r"""'PostToolUse:Edit' (candidates: 'hook.sh \x07ring', 'hook.sh \'a\' "b" c\\d')."""
)
_GOLDEN_AMBIGUOUS = (
    r"""2 entries match 'PostToolUse:Edit'; disambiguate with --hook-command <substring> """
    r"""(candidates: 'hook.sh \x07ring', 'hook.sh \'a\' "b" c\\d')."""
)
_GOLDEN_NO_MATCH_SECRET = (
    "--hook-command 'no-such-substring' matches none of the entries under "
    "'PostToolUse:Edit' (candidates: '<redacted: secret-shape>', 'mm session start')."
)
_GOLDEN_AMBIGUOUS_SECRET = (
    "2 entries match 'PostToolUse:Edit'; disambiguate with --hook-command <substring> "
    "(candidates: '<redacted: secret-shape>', 'mm session start')."
)


def _hook_rule(matcher: str, *commands: str) -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": c} for c in commands]}


@pytest.fixture
def copy_projects(tmp_path, fake_home, monkeypatch):
    """A source project and a registered-by-path destination for settings-copy."""
    from memtomem.cli import context_cmd

    src = tmp_path / "src-proj"
    (src / ".git").mkdir(parents=True)
    dst = tmp_path / "dst-proj"
    (dst / ".memtomem").mkdir(parents=True)
    monkeypatch.chdir(src)
    monkeypatch.setattr(context_cmd, "_projects_gateway_cfg", lambda: None)
    monkeypatch.setattr(context_cmd, "_projects_discover", lambda *a, **k: [])
    return src, dst


class TestReprCommandPreviewException:
    """Command previews in settings-copy stay ``repr()``-quoted, exactly as before.

    The PR's rule is that producers redact and displays escape. These previews
    predate it: ``repr`` quotes each command so several stay delimited, and it
    spells control characters as ``\\\\xNN`` text as a side effect. They are kept
    for continuity of the messages. The exception is pinned to exactly these
    three strings — the conflict reason and the two selector errors — by their
    delivery path: parsed ``--json`` for the reason, stderr and exit code for the
    errors, which never have a JSON form.
    """

    def test_conflict_reason_in_json_matches_the_pre_pr_bytes(self, copy_projects):
        from memtomem.cli.context_cmd import settings_copy_cmd

        src, dst = copy_projects
        _write_settings(
            src / CANONICAL_SETTINGS_FILE, {"PostToolUse": [_hook_rule("Edit", "mm session start")]}
        )
        _write_settings(
            dst / CANONICAL_SETTINGS_FILE,
            {
                "PostToolUse": [
                    _hook_rule("Edit", _BEL_COMMAND, _LITERAL_ESCAPE_COMMAND, _QUOTED_COMMAND)
                ]
            },
        )
        result = CliRunner().invoke(
            settings_copy_cmd,
            [
                "--event",
                "PostToolUse",
                "--matcher",
                "Edit",
                "--to-project",
                str(dst),
                "--to",
                "project_local",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["canonical"]["state"] == "conflict"
        assert payload["canonical"]["reason"] == _GOLDEN_CONFLICT_REASON
        # The structured identification fields are still the raw source values.
        assert payload["command_preview"] == "mm session start"

    @pytest.mark.parametrize(
        ("hook_command", "commands", "golden"),
        [
            pytest.param(
                "no-such-substring",
                (_BEL_COMMAND, _QUOTED_COMMAND),
                _GOLDEN_NO_MATCH,
                id="no-match",
            ),
            pytest.param(None, (_BEL_COMMAND, _QUOTED_COMMAND), _GOLDEN_AMBIGUOUS, id="ambiguous"),
            pytest.param(
                "no-such-substring",
                (_secret_command(), "mm session start"),
                _GOLDEN_NO_MATCH_SECRET,
                id="no-match-secret",
            ),
            pytest.param(
                None,
                (_secret_command(), "mm session start"),
                _GOLDEN_AMBIGUOUS_SECRET,
                id="ambiguous-secret",
            ),
        ],
    )
    def test_selector_error_on_stderr_matches_its_golden(
        self, copy_projects, hook_command, commands, golden
    ):
        from memtomem.cli.context_cmd import settings_copy_cmd

        src, dst = copy_projects
        _write_settings(
            src / CANONICAL_SETTINGS_FILE, {"PostToolUse": [_hook_rule("Edit", *commands)]}
        )
        args = ["--event", "PostToolUse", "--matcher", "Edit", "--to-project", str(dst)]
        if hook_command is not None:
            args += ["--hook-command", hook_command]
        result = CliRunner().invoke(settings_copy_cmd, args)
        assert result.exit_code == 1, result.output
        assert result.stdout == ""
        assert result.stderr == f"Error: {golden}\n"
        assert SECRET not in result.stderr
        assert not [ch for ch in result.stderr if not ch.isprintable() and ch != "\n"]

    def test_the_exception_covers_the_command_preview_only(self):
        """The label beside the preview in the same reason is NOT escaped."""
        from memtomem.context.settings_copy import _classify_leg

        sig = HookSignature(event="PostToolUse", matcher=f"Edit{BEL_RAW}", command_shape="x")
        doc = {"hooks": {"PostToolUse": [_hook_rule(sig.matcher, _BEL_COMMAND)]}}
        state, reason = _classify_leg(
            doc, sig, {"type": "command", "command": "x"}, leg="canonical"
        )
        assert state == "conflict"
        assert reason.count(BEL_RAW) == 1  # the label keeps its raw character
        assert f"'PostToolUse:Edit{BEL_RAW}'" in reason
        assert r"'hook.sh \x07ring'" in reason  # the repr'd preview is escaped
