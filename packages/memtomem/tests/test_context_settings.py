"""Tests for context/settings.py — canonical → runtime settings.json fan-out (Phase D).

Uses record-format hooks (Claude Code ≥ 2.1.104):
    {"hooks": {"EventName": [{"matcher": "...", "hooks": [...]}]}}
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    ClaudeSettingsGenerator,
    resolve_scope_path,
    diff_settings,
    generate_all_settings,
    host_write_targets,
)
from memtomem.web.app import create_app
from .helpers import set_home


# ── Helpers ────────────────────────────────────────────────────────


def _rule(matcher: str = "", command: str = "echo ok", timeout: int = 5000) -> dict:
    """Build a single hook rule in record format."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    }


def _marked(rule: dict, event: str) -> dict:
    """The rule as memtomem writes it for Claude: the ADR-0019 ownership marker
    (``statusMessage`` = ``"memtomem · <event>"``) stamped onto each command
    handler. Used to assert the exact on-disk shape of a memtomem-written rule."""
    out = json.loads(json.dumps(rule))  # deep copy
    for handler in out.get("hooks", []):
        if isinstance(handler, dict) and handler.get("type") == "command":
            handler["statusMessage"] = f"memtomem · {event}"
    return out


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Redirect HOME so writes target a temp dir.  Creates ``~/.claude/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


@pytest.fixture
def claude_home_missing(tmp_path, monkeypatch):
    """Redirect HOME **without** creating ``~/.claude/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


@pytest.fixture
def kimi_home(tmp_path, monkeypatch):
    """Redirect HOME so writes target a temp Kimi config dir."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".kimi").mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


def _make_canonical_settings(project_root, content: dict | str | None = None):
    """Write ``.memtomem/settings.json`` with the given content."""
    if content is None:
        content = {"hooks": {}}
    path = project_root / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return path


def _read_target(claude_home) -> dict:
    """Read the merged settings.json from the fake HOME."""
    path = claude_home / ".claude" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── Merge tests ─────────────────────────────────────────────────────


class TestClaudeSettingsMergeEmpty:
    """No existing settings.json — merge creates a new file."""

    def test_creates_file_from_canonical(self, claude_home, tmp_path):
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo ok")]}},
        )
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert "PostToolUse" in written["hooks"]
        assert len(written["hooks"]["PostToolUse"]) == 1
        assert written["hooks"]["PostToolUse"][0]["matcher"] == "Write"


class TestClaudeSettingsMergeSemantic:
    """Existing keys not owned by memtomem are preserved semantically."""

    def test_preserves_unrelated_keys(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        existing = {
            "permissions": {"allow": ["Read", "Edit"]},
            "env": {"FOO": "bar"},
            "mcpServers": {"example": {"command": "echo"}},
        }
        target.write_text(json.dumps(existing, indent=4) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert written["permissions"] == existing["permissions"]
        assert written["env"] == existing["env"]
        assert written["mcpServers"] == existing["mcpServers"]
        # No cosmetic "hooks": {} key for an empty canonical (#1229).
        assert "hooks" not in written


class TestClaudeSettingsMergeAdditive:
    """Existing user rules are preserved; memtomem rules are appended."""

    def test_appends_without_touching_user_rules(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("", "say done")
        target.write_text(json.dumps({"hooks": {"Stop": [user_rule]}}) + "\n", encoding="utf-8")

        mm_rule = _rule("Write", "mm index")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [mm_rule]}})

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert written["hooks"]["Stop"] == [user_rule]  # user rule untouched (no marker)
        assert written["hooks"]["PostToolUse"] == [_marked(mm_rule, "PostToolUse")]

    def test_appends_rule_to_same_event(self, claude_home, tmp_path):
        """Multiple rules under the same event with different matchers."""
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("Bash", "echo user")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule]}}) + "\n", encoding="utf-8"
        )

        mm_rule = _rule("Write", "mm index")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [mm_rule]}})

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert len(written["hooks"]["PostToolUse"]) == 2
        assert written["hooks"]["PostToolUse"][0] == user_rule  # user rule untouched
        assert written["hooks"]["PostToolUse"][1] == _marked(mm_rule, "PostToolUse")


class TestClaudeSettingsMergeConflict:
    """Same (event, matcher) → skip + emit warning.  User's rule wins."""

    def test_user_rule_wins_on_matcher_collision(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("Write", "echo custom")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule]}}) + "\n", encoding="utf-8"
        )

        mm_rule = _rule("Write", "mm index")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [mm_rule]}})

        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "ok"
        assert len(r.warnings) == 1

        written = _read_target(claude_home)
        assert len(written["hooks"]["PostToolUse"]) == 1
        assert written["hooks"]["PostToolUse"][0] == user_rule  # user wins

    def test_identical_rule_is_silently_skipped(self, claude_home, tmp_path):
        """If the user's rule is byte-identical, no warning is emitted."""
        rule = _rule("Write", "mm index")
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {"PostToolUse": [rule]}}) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [rule]}})
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].warnings == []

    def test_dedup_when_user_has_multiple_same_matcher_rules(self, claude_home, tmp_path):
        """Pin: existing rules with the same matcher should not collapse during indexing.

        Claude Code allows two rules under the same event to share a matcher
        (or omit it). Earlier indexing keyed ``existing_by_matcher`` as
        ``dict[str, dict]``, so the second rule silently shadowed the first.
        A byte-identical contribution that matched the *first* rule then
        emitted a spurious warning by comparing against the second.
        """
        target = claude_home / ".claude" / "settings.json"
        existing_a = _rule("", "echo first")
        existing_b = _rule("", "echo second")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [existing_a, existing_b]}}) + "\n",
            encoding="utf-8",
        )

        # Contribution exactly matches the first user rule.
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [existing_a]}})

        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "ok"
        assert r.warnings == []  # was 1 before the fix

        written = _read_target(claude_home)
        # Both user rules preserved verbatim, no contribution appended.
        assert written["hooks"]["PostToolUse"] == [existing_a, existing_b]


class TestClaudeSettingsMergeWarningContent:
    """Warning messages must contain the rule label, reason, and remediation."""

    def test_warning_includes_required_parts(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [_rule("Write", "old")]}}) + "\n", encoding="utf-8"
        )

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "new")]}},
        )
        results = generate_all_settings(tmp_path, scope="user")
        w = results["claude_settings"].warnings[0]

        # (a) rule label
        assert "PostToolUse:Write" in w
        # (b) reason
        assert "user-owned rule with the same event+matcher" in w
        # (c) concrete remediation step
        assert "Change one matcher" in w
        assert "remove" in w
        assert "mm context sync --include=settings" in w


class TestClaudeOwnershipResync:
    """ADR-0019 / issue #1110: re-sync updates memtomem's own emitted rules."""

    def test_resync_updates_own_marked_rule_no_warning(self, claude_home, tmp_path):
        """A memtomem-marked target rule is replaced when canonical changes."""
        target = claude_home / ".claude" / "settings.json"
        old = _marked(_rule("Write", "mm index --v1"), "PostToolUse")
        target.write_text(json.dumps({"hooks": {"PostToolUse": [old]}}) + "\n", encoding="utf-8")

        _make_canonical_settings(
            tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index --v2")]}}
        )
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "ok"
        assert r.warnings == []  # memtomem updating its own rule is not a conflict

        written = _read_target(claude_home)
        assert written["hooks"]["PostToolUse"] == [
            _marked(_rule("Write", "mm index --v2"), "PostToolUse")
        ]

    def test_resync_preserves_user_rule_at_other_matcher(self, claude_home, tmp_path):
        """Updating memtomem's rule leaves a user rule (other matcher) untouched."""
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("Bash", "echo user")
        old = _marked(_rule("Write", "mm index --v1"), "PostToolUse")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule, old]}}) + "\n", encoding="utf-8"
        )

        _make_canonical_settings(
            tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index --v2")]}}
        )
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []

        written = _read_target(claude_home)
        # User Bash rule kept in place; memtomem Write rule updated in place.
        assert written["hooks"]["PostToolUse"][0] == user_rule
        assert written["hooks"]["PostToolUse"][1] == _marked(
            _rule("Write", "mm index --v2"), "PostToolUse"
        )

    def test_resync_marked_and_user_same_matcher_both_kept(self, claude_home, tmp_path):
        """Marked + user rule under one matcher: marked updated in place, user kept."""
        target = claude_home / ".claude" / "settings.json"
        marked_old = _marked(_rule("Write", "mm index --v1"), "PostToolUse")
        user_rule = _rule("Write", "echo user")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [marked_old, user_rule]}}) + "\n", encoding="utf-8"
        )

        _make_canonical_settings(
            tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index --v2")]}}
        )
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []

        written = _read_target(claude_home)["hooks"]["PostToolUse"]
        assert written[0] == _marked(
            _rule("Write", "mm index --v2"), "PostToolUse"
        )  # updated in place
        assert written[1] == user_rule  # user rule preserved

    def test_resync_is_idempotent(self, claude_home, tmp_path):
        """Re-syncing an unchanged canonical produces no warning and no rewrite."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index")]}})

        generate_all_settings(tmp_path, scope="user")
        first = _read_target(claude_home)
        # First sync stamps the marker.
        assert first["hooks"]["PostToolUse"] == [_marked(_rule("Write", "mm index"), "PostToolUse")]

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []
        assert _read_target(claude_home) == first  # byte-stable

    def test_legacy_unmarked_same_command_warns_not_replaced(self, claude_home, tmp_path):
        """A pre-marker memtomem rule (unmarked) gets a sharper warning, not a clobber."""
        target = claude_home / ".claude" / "settings.json"
        legacy = _rule("Write", "mm index", timeout=5000)  # no marker (old release)
        target.write_text(json.dumps({"hooks": {"PostToolUse": [legacy]}}) + "\n", encoding="utf-8")

        # Canonical: same command, changed timeout — would be an update if marked.
        _make_canonical_settings(
            tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index", timeout=9000)]}}
        )
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert len(r.warnings) == 1
        assert "previous version" in r.warnings[0]  # migration-guidance wording

        written = _read_target(claude_home)
        assert written["hooks"]["PostToolUse"] == [legacy]  # never silently overwritten

    def test_preserves_author_statusMessage_text(self, claude_home, tmp_path):
        """A canonical statusMessage is preserved after the reserved prefix."""
        rule = {
            "matcher": "Write",
            "hooks": [
                {
                    "type": "command",
                    "command": "mm index",
                    "timeout": 5000,
                    "statusMessage": "Indexing memory",
                }
            ],
        }
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [rule]}})
        generate_all_settings(tmp_path, scope="user")

        written = _read_target(claude_home)
        handler = written["hooks"]["PostToolUse"][0]["hooks"][0]
        assert handler["statusMessage"] == "memtomem · Indexing memory"

    def test_two_same_matcher_canonical_rules_not_dropped(self, claude_home, tmp_path):
        """Two canonical rules under one matcher both survive (mutation-safety)."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "mm index"), _rule("Write", "mm graph")]}},
        )

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []
        written = _read_target(claude_home)["hooks"]["PostToolUse"]
        commands = {h["command"] for rule in written for h in rule["hooks"]}
        assert commands == {"mm index", "mm graph"}

        # Re-sync: both are now marked; both update in place, neither dropped.
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []
        written2 = _read_target(claude_home)["hooks"]["PostToolUse"]
        assert {h["command"] for rule in written2 for h in rule["hooks"]} == {
            "mm index",
            "mm graph",
        }

    def test_stale_event_owned_rule_pruned_user_kept(self, claude_home, tmp_path):
        """A memtomem rule under an event canonical no longer emits is pruned;
        a user rule under that same event is preserved."""
        target = claude_home / ".claude" / "settings.json"
        marked_stale = _marked(_rule("", "mm session-log"), "SessionStart")
        user_rule = _rule("", "echo my-session-hook")
        target.write_text(
            json.dumps({"hooks": {"SessionStart": [marked_stale, user_rule]}}) + "\n",
            encoding="utf-8",
        )
        # Canonical no longer emits any SessionStart rule.
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index")]}})

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)["hooks"]
        assert written["SessionStart"] == [user_rule]  # stale memtomem rule pruned, user kept
        assert written["PostToolUse"] == [_marked(_rule("Write", "mm index"), "PostToolUse")]

    def test_stale_event_fully_pruned_drops_empty_event(self, claude_home, tmp_path):
        """An event holding only a stale memtomem rule is removed entirely."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"SessionStart": [_marked(_rule("", "mm log"), "SessionStart")]}})
            + "\n",
            encoding="utf-8",
        )
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index")]}})

        generate_all_settings(tmp_path, scope="user")
        assert "SessionStart" not in _read_target(claude_home)["hooks"]

    def test_raw_user_rule_matching_pre_stamp_is_in_sync(self, claude_home, tmp_path):
        """A user rule byte-identical to the canonical (no marker) is in-sync,
        regardless of statusMessage — comparison ignores the marker-carrier
        field symmetrically (no spurious conflict warning)."""
        target = claude_home / ".claude" / "settings.json"
        # User authored the exact rule, including a hand-written statusMessage.
        raw = {
            "matcher": "Write",
            "hooks": [
                {
                    "type": "command",
                    "command": "mm index",
                    "timeout": 5000,
                    "statusMessage": "Indexing",
                }
            ],
        }
        target.write_text(json.dumps({"hooks": {"PostToolUse": [raw]}}) + "\n", encoding="utf-8")
        # Canonical is the same command/matcher/timeout (would stamp its own marker).
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "mm index", timeout=5000)]}},
        )

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []  # functionally identical → in sync
        assert _read_target(claude_home)["hooks"]["PostToolUse"] == [raw]  # left untouched

    def test_resync_drops_surplus_owned_duplicate(self, claude_home, tmp_path):
        """Two memtomem-owned rules at one matcher but one canonical rule → the
        surplus owned rule is pruned (kept count matches canonical)."""
        target = claude_home / ".claude" / "settings.json"
        o1 = _marked(_rule("Write", "mm index"), "PostToolUse")
        o2 = _marked(_rule("Write", "mm index --dup"), "PostToolUse")  # surplus owned
        target.write_text(json.dumps({"hooks": {"PostToolUse": [o1, o2]}}) + "\n", encoding="utf-8")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write", "mm index")]}})

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].warnings == []
        written = _read_target(claude_home)["hooks"]["PostToolUse"]
        assert written == [_marked(_rule("Write", "mm index"), "PostToolUse")]  # surplus dropped

    def test_empty_canonical_prunes_owned_keeps_user(self, claude_home, tmp_path):
        """Canonical emitting no hooks prunes every memtomem-owned target rule
        while leaving user rules in place."""
        target = claude_home / ".claude" / "settings.json"
        owned = _marked(_rule("Write", "mm index"), "PostToolUse")
        user_rule = _rule("Bash", "echo user")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [owned, user_rule]}}) + "\n", encoding="utf-8"
        )
        _make_canonical_settings(tmp_path, {"hooks": {}})

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"
        assert _read_target(claude_home)["hooks"]["PostToolUse"] == [user_rule]  # owned pruned


class TestClaudeSettingsMergeMalformed:
    """Existing settings.json is not valid JSON → skip, don't crash."""

    def test_malformed_target_returns_error(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text('{"hooks":{', encoding="utf-8")  # truncated

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not valid JSON" in r.reason

        # File should NOT have been modified
        assert target.read_text(encoding="utf-8") == '{"hooks":{'

    def test_malformed_canonical_returns_error(self, claude_home, tmp_path):
        _make_canonical_settings(tmp_path, "{bad json")
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not valid JSON" in r.reason

    def test_non_dict_json_canonical_returns_error(self, claude_home, tmp_path):
        """Valid JSON whose root is not an object used to AttributeError deep
        inside the merge layer, aborting every runtime (#1229)."""
        _make_canonical_settings(tmp_path, "[]\n")
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not a JSON object" in r.reason

    def test_non_dict_json_target_returns_error_and_preserves_file(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text('"just a string"\n', encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert target.read_text(encoding="utf-8") == '"just a string"\n'

    def test_list_shaped_hooks_target_returns_error_and_preserves_file(self, claude_home, tmp_path):
        """Array-format hooks used to be silently destroyed: dict() over the
        rule list coerced [{"matcher": ..., "hooks": [...]}] into the garbage
        {"matcher": "hooks"} and wrote it back with status="ok" (#1229)."""
        original = json.dumps(
            {"hooks": [{"matcher": "Edit|Write", "hooks": [{"type": "command"}]}]}
        )
        target = claude_home / ".claude" / "settings.json"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PreToolUse": [_rule("Bash")]}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "record keyed by event name" in r.reason
        # The user's hook configuration must survive untouched.
        assert target.read_text(encoding="utf-8") == original

    def test_list_shaped_hooks_with_three_key_rules_returns_error(self, claude_home, tmp_path):
        """Rules with != 2 keys made the same dict() coercion raise ValueError,
        crashing the whole fan-out instead of erroring one target (#1229)."""
        original = json.dumps(
            {"hooks": [{"matcher": "Bash", "hooks": [], "comment": "three keys"}]}
        )
        target = claude_home / ".claude" / "settings.json"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert target.read_text(encoding="utf-8") == original

    def test_dict_shaped_event_value_returns_error_and_preserves_file(self, claude_home, tmp_path):
        """A record-format hooks whose EVENT value is a dict (not a list)
        used to be coerced by list() into its key strings and written back as
        "rules" with status="ok" (Codex review on #1229)."""
        original = json.dumps(
            {"hooks": {"PreToolUse": {"matcher": "Bash", "hooks": [{"type": "command"}]}}}
        )
        target = claude_home / ".claude" / "settings.json"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PreToolUse": [_rule("Bash")]}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "'hooks.PreToolUse' must be a list" in r.reason
        assert target.read_text(encoding="utf-8") == original

    def test_scalar_event_value_returns_error_not_crash(self, claude_home, tmp_path):
        """A scalar event value made list() raise TypeError past the
        MalformedSettingsError catch, crashing the whole fan-out (Codex
        review on #1229)."""
        original = json.dumps({"hooks": {"PreToolUse": 5}})
        target = claude_home / ".claude" / "settings.json"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PreToolUse": [_rule("Bash")]}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert target.read_text(encoding="utf-8") == original

    def test_diff_settings_dict_shaped_event_value_returns_error(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": {"matcher": "", "hooks": []}}}),
            encoding="utf-8",
        )

        _make_canonical_settings(tmp_path, {"hooks": {"PreToolUse": [_rule("Bash")]}})
        results = diff_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "'hooks.PreToolUse' must be a list" in r.reason

    def test_diff_settings_non_dict_canonical_returns_error(self, claude_home, tmp_path):
        _make_canonical_settings(tmp_path, "42\n")
        results = diff_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not a JSON object" in r.reason

    def test_diff_settings_list_shaped_hooks_target_returns_error(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": [{"matcher": "", "hooks": []}]}), encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = diff_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "record keyed by event name" in r.reason


class TestKimiSettingsMerge:
    def test_writes_managed_toml_block_and_preserves_existing_config(self, kimi_home, tmp_path):
        target = kimi_home / ".kimi" / "config.toml"
        target.write_text('theme = "dark"\n', encoding="utf-8")
        _make_canonical_settings(
            tmp_path,
            {
                "hooks": {
                    "PreToolUse": [_rule("Bash", "mm search context")],
                    "Notification": [_rule("", "echo ignored")],
                }
            },
        )

        results = generate_all_settings(tmp_path, scope="user")
        r = results["kimi_settings"]

        assert r.status == "ok"
        assert any("Notification" in w for w in r.warnings)
        text = target.read_text(encoding="utf-8")
        assert 'theme = "dark"' in text
        assert "# BEGIN memtomem managed hooks" in text
        assert 'event = "PreToolUse"' in text
        assert 'matcher = "Shell"' in text
        assert 'command = "mm search context"' in text
        parsed = tomllib.loads(text)
        assert parsed["theme"] == "dark"
        assert parsed["hooks"][0]["matcher"] == "Shell"

    def test_replaces_existing_managed_block(self, kimi_home, tmp_path):
        target = kimi_home / ".kimi" / "config.toml"
        target.write_text(
            'theme = "dark"\n\n'
            "# BEGIN memtomem managed hooks\n"
            "[[hooks]]\n"
            'event = "Old"\n'
            "# END memtomem managed hooks\n",
            encoding="utf-8",
        )
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "mm index ~/memories")]}},
        )

        r = generate_all_settings(tmp_path, scope="user")["kimi_settings"]

        assert r.status == "ok"
        text = target.read_text(encoding="utf-8")
        assert 'event = "Old"' not in text
        assert 'event = "PostToolUse"' in text
        assert 'matcher = "WriteFile"' in text
        assert tomllib.loads(text)["hooks"][0]["command"] == "mm index ~/memories"

    def test_resync_preserves_backslashes_and_newlines_in_command(self, kimi_home, tmp_path):
        """Regression: ``_replace_kimi_managed_block`` used the rendered block
        as a plain-string ``re.sub`` replacement, so template processing halved
        the doubled-backslash escapes ``_toml_string`` emits (regex ``\\b``,
        Windows paths) and turned a literal backslash-n into a raw newline
        inside the TOML string. The FIRST sync takes the no-block concat branch
        and was always correct; the SECOND sync (block present →
        ``pattern.sub``) corrupted ``config.toml`` into unparseable TOML.
        Sync twice and require an exact command round-trip both times.
        """
        target = kimi_home / ".kimi" / "config.toml"
        target.write_text('theme = "dark"\n', encoding="utf-8")
        nasty = 'grep "\\bfoo" C:\\tools\\hook.exe && printf "a\\nb"'
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PreToolUse": [_rule("Bash", nasty)]}},
        )

        first = generate_all_settings(tmp_path, scope="user")["kimi_settings"]
        assert first.status == "ok"
        parsed_first = tomllib.loads(target.read_text(encoding="utf-8"))
        assert parsed_first["hooks"][0]["command"] == nasty

        # Second sync: the managed block now exists, so this run exercises
        # the ``pattern.sub`` replacement path that did the corrupting.
        second = generate_all_settings(tmp_path, scope="user")["kimi_settings"]
        assert second.status == "ok"
        text = target.read_text(encoding="utf-8")
        parsed_second = tomllib.loads(text)  # corrupted output raises TOMLDecodeError
        assert parsed_second["hooks"][0]["command"] == nasty
        assert parsed_second["theme"] == "dark"
        # Negative pin on the corruption signature: the doubled escape in the
        # TOML source (backslash backslash before ``bfoo``) must survive the
        # re-sync — template processing halved it to a single backslash.
        assert "\\\\bfoo" in text

    def test_bool_timeout_omitted_from_rendered_toml(self, kimi_home, tmp_path):
        """``timeout: true`` in a canonical handler must not render as
        ``timeout = True`` — Python's ``str(True)`` is not valid TOML, and the
        invalid write bricked the target into a permanent per-target error
        status on every later sync and diff (#1229)."""
        target = kimi_home / ".kimi" / "config.toml"
        target.write_text('theme = "dark"\n', encoding="utf-8")
        _make_canonical_settings(
            tmp_path,
            {
                "hooks": {
                    "PreToolUse": [
                        _rule("Bash", "echo bool", timeout=True),
                        _rule("Bash", "echo int", timeout=5000),
                    ]
                }
            },
        )

        first = generate_all_settings(tmp_path, scope="user")["kimi_settings"]
        assert first.status == "ok"
        text = target.read_text(encoding="utf-8")
        assert "timeout = True" not in text  # the invalid-TOML signature
        assert "timeout = 5000" in text  # numeric timeouts still rendered
        parsed = tomllib.loads(text)  # whole file must stay valid TOML
        assert [h["command"] for h in parsed["hooks"]] == ["echo bool", "echo int"]

        # Pre-fix, the corrupting first sync itself reported ok and every
        # LATER pass was bricked: re-sync and diff must stay healthy.
        second = generate_all_settings(tmp_path, scope="user")["kimi_settings"]
        assert second.status == "ok"
        assert diff_settings(tmp_path, scope="user")["kimi_settings"].status == "in sync"


class TestNoCosmeticHooksKey:
    """Empty/absent canonical hooks must not inject a cosmetic ``"hooks": {}``
    key into targets that never had one (#1229) — diff reported a false
    "out of sync" and sync rewrote the user's settings file just to add it."""

    @pytest.mark.parametrize("canonical", [{}, {"hooks": {}}])
    def test_sync_does_not_inject_hooks_key(self, claude_home, tmp_path, canonical):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
        _make_canonical_settings(tmp_path, canonical)

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"
        written = _read_target(claude_home)
        assert written["model"] == "opus"
        assert "hooks" not in written

    @pytest.mark.parametrize("canonical", [{}, {"hooks": {}}])
    def test_diff_reports_in_sync(self, claude_home, tmp_path, canonical):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
        _make_canonical_settings(tmp_path, canonical)

        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "in sync"

    def test_preexisting_empty_hooks_key_is_kept(self, claude_home, tmp_path):
        """A user-authored ``"hooks": {}`` key is never removed."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
        _make_canonical_settings(tmp_path, {"hooks": {}})

        assert diff_settings(tmp_path, scope="user")["claude_settings"].status == "in sync"
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"
        assert _read_target(claude_home)["hooks"] == {}

    def test_empty_contrib_event_not_copied_into_hookless_target(self, claude_home, tmp_path):
        """An event mapped to an empty list contributes nothing — it must not
        materialize a hooks record in a target that never had one."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"model": "opus"}) + "\n", encoding="utf-8")
        _make_canonical_settings(tmp_path, {"hooks": {"PreToolUse": []}})

        assert diff_settings(tmp_path, scope="user")["claude_settings"].status == "in sync"
        generate_all_settings(tmp_path, scope="user")
        assert "hooks" not in _read_target(claude_home)

    def test_empty_contrib_event_still_prunes_owned_rules(self, claude_home, tmp_path):
        """Guard: skipping empty contribution events applies only to events the
        target never had — an existing event must still go through Pass 1 so
        stale memtomem-owned rules are pruned."""
        target = claude_home / ".claude" / "settings.json"
        owned = _marked(_rule("Write", "mm index"), "PostToolUse")
        user_rule = _rule("Bash", "echo user")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [owned, user_rule]}}) + "\n", encoding="utf-8"
        )
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": []}})

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"
        assert _read_target(claude_home)["hooks"]["PostToolUse"] == [user_rule]


class TestClaudeSettingsMergeConcurrent:
    """Mtime changed between read and write → abort."""

    def test_aborts_on_mtime_change(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo")]}},
        )

        import memtomem.context.settings as settings_mod

        orig_read_with_mtime = settings_mod._read_with_mtime

        def patched_read_with_mtime(path):
            result = orig_read_with_mtime(path)
            if path == target:
                target.write_text(
                    json.dumps({"hooks": {}, "_bumped": True}) + "\n", encoding="utf-8"
                )
                # Two writes inside the same system-clock tick can report the
                # same ``st_mtime_ns`` on Windows (NTFS native resolution is
                # 100ns but ``WriteFile``-induced metadata updates use the
                # system clock, which often advances at ~15.6ms). Bump
                # explicitly so the simulated concurrent-writer is
                # distinguishable regardless of OS timer granularity — a
                # real-world second writer is naturally well above this
                # threshold, so this only covers the test-induced race.
                import os as _os

                st = target.stat()
                _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        import unittest.mock

        with unittest.mock.patch.object(settings_mod, "_read_with_mtime", patched_read_with_mtime):
            results = generate_all_settings(tmp_path, scope="user")

        r = results["claude_settings"]
        assert r.status == "aborted"
        assert "modified by another process" in r.reason


class TestClaudeSettingsCrossProcessLock:
    """B3-3 (#1123): the read-merge-recheck-write critical section runs under a
    per-target portalocker sidecar ``_file_lock`` so a separate-process writer
    cannot land between the mtime recheck and the atomic rename. The mtime
    check is retained as a second layer against direct disk edits."""

    def test_write_runs_inside_target_file_lock(self, claude_home, tmp_path, monkeypatch):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo")]}},
        )

        import memtomem.context.settings as settings_mod
        from memtomem.context._atomic import _lock_path_for

        events: list[str] = []
        orig_file_lock = settings_mod._file_lock
        orig_write_json = settings_mod._write_json

        @contextlib.contextmanager
        def spy_file_lock(lock_path, *, timeout=None):
            events.append(f"enter:{lock_path.name}")
            with orig_file_lock(lock_path, timeout=timeout):
                yield
            events.append(f"exit:{lock_path.name}")

        def spy_write_json(path, data):
            events.append(f"write:{path.name}")
            return orig_write_json(path, data)

        monkeypatch.setattr(settings_mod, "_file_lock", spy_file_lock)
        monkeypatch.setattr(settings_mod, "_write_json", spy_write_json)

        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"

        # The write happened strictly between lock-enter and lock-exit on the
        # target's sidecar — proving the critical section is lock-guarded.
        lock_name = _lock_path_for(target).name
        assert f"enter:{lock_name}" in events
        assert f"write:{target.name}" in events
        assert f"exit:{lock_name}" in events
        assert (
            events.index(f"enter:{lock_name}")
            < events.index(f"write:{target.name}")
            < events.index(f"exit:{lock_name}")
        )

    def test_concurrent_writers_do_not_deadlock_or_corrupt_under_contention(
        self, claude_home, tmp_path
    ):
        """Contention smoke for the new blocking ``_file_lock``: real thread
        contention neither deadlocks nor corrupts the target file.

        This is deliberately NOT the lock-efficacy pin — the genuine "the
        critical section is lock-guarded" assertion is
        :meth:`test_write_runs_inside_target_file_lock` above. A lost-update
        race can't be observed here: every writer reads the same canonical
        source and merges the identical payload, and ``atomic_write_text``
        already yields complete JSON on its own, so this test would pass even
        without the lock. What it DOES guard is that introducing a blocking
        ``portalocker`` ``LOCK_EX`` under thread contention (4 workers across 8
        submitted runs) does not hang (deadlock / lock-ordering cycle) and never
        leaves a torn file."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo")]}},
        )

        def _run():
            return generate_all_settings(tmp_path, scope="user")["claude_settings"].status

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = [f.result() for f in [pool.submit(_run) for _ in range(8)]]

        # Whatever the interleaving, every run resolves to a known terminal
        # status (no hang) and at least one writer succeeds.
        assert set(statuses) <= {"ok", "aborted"}
        assert "ok" in statuses
        # The final on-disk file is always complete, valid JSON with a hooks
        # record — never empty or half-written.
        final = json.loads(target.read_text(encoding="utf-8"))
        assert isinstance(final.get("hooks"), dict)

    def test_held_lock_aborts_within_bound_not_hangs(self, claude_home, tmp_path, monkeypatch):
        """When the target's sidecar lock is held past the budget, the sync
        ABORTS cleanly rather than blocking forever (#1145 review). This is what
        lets the web handler offload to a worker thread without orphaning it:
        the bounded acquisition self-terminates instead of writing after the
        request's own timeout already returned."""
        import memtomem.context.settings as settings_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo")]}},
        )

        # Tiny budget so the test is fast; hold the sidecar from "another holder"
        # (a separate fd — portalocker contends per open-file-description even
        # in-process).
        monkeypatch.setattr(settings_mod, "_SETTINGS_LOCK_BUDGET_S", 0.2)
        with _file_lock(_lock_path_for(target)):
            results = generate_all_settings(tmp_path, scope="user")

        assert results["claude_settings"].status == "aborted"
        assert "held the lock" in results["claude_settings"].reason
        # The held lock blocked the write, so the target keeps its original
        # (empty-hooks) content — no torn or partial write.
        assert json.loads(target.read_text(encoding="utf-8")) == {"hooks": {}}

    def test_lock_budget_bounds_whole_call_not_per_target(self, claude_home, tmp_path, monkeypatch):
        """The lock budget bounds the WHOLE call, not each target (#1145
        re-review). With several runtimes available and multiple sidecar locks
        held, the TOTAL wait stays within ~one budget — a per-target bound would
        instead accumulate ``N_held × budget`` and could overrun the web
        handler's 60s deadline, re-opening the orphaned-worker window."""
        import memtomem.context.settings as settings_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        # Make codex + gemini available too. Their dirs live under the fake HOME,
        # which is itself under project_root, so the host-write gate stays shut.
        (claude_home / ".codex").mkdir()
        (claude_home / ".gemini").mkdir()
        claude_t = claude_home / ".claude" / "settings.json"
        codex_t = claude_home / ".codex" / "hooks.json"
        gemini_t = claude_home / ".gemini" / "settings.json"

        def _reset_targets() -> None:
            for t in (claude_t, codex_t, gemini_t):
                t.parent.mkdir(parents=True, exist_ok=True)
                t.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

        _reset_targets()
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo")]}},
        )

        budget = 0.5

        # An absolute wall-clock bound was flaky: it folds the lock wait together
        # with fixed I/O + portalocker overhead, which on a slow/loaded runner
        # (the Windows CI runner) can dwarf the budget. Isolate the lock wait by
        # subtracting a *symmetric* baseline — the SAME two locks held, but with a
        # 0s budget so the contended targets abort immediately. That baseline runs
        # the identical work profile (two aborts + one Gemini write) minus only
        # the budgeted wait, so the subtraction removes overhead without the
        # asymmetry of a no-lock 3-write fan-out (which would over-subtract and
        # could mask a per-target regression).
        monkeypatch.setattr(settings_mod, "_SETTINGS_LOCK_BUDGET_S", 0.0)
        start = time.monotonic()
        with _file_lock(_lock_path_for(claude_t)), _file_lock(_lock_path_for(codex_t)):
            base_results = generate_all_settings(tmp_path, scope="user")
        baseline = time.monotonic() - start
        _reset_targets()
        # The 0s baseline already shows the abort/ok split — both held targets
        # abort and the free one writes, identical to the measured run's profile;
        # its only difference is the budgeted wait.
        assert base_results["claude_settings"].status == "aborted"
        assert base_results["codex_settings"].status == "aborted"
        assert base_results["gemini_settings"].status == "ok"

        # Now the real budget. Hold the same TWO locks: per-target bounding waits
        # ~2×budget; the shared deadline caps the *total* lock wait at ~one budget
        # (once it expires the remaining held target gets a 0s non-blocking
        # attempt).
        monkeypatch.setattr(settings_mod, "_SETTINGS_LOCK_BUDGET_S", budget)
        start = time.monotonic()
        with _file_lock(_lock_path_for(claude_t)), _file_lock(_lock_path_for(codex_t)):
            results = generate_all_settings(tmp_path, scope="user")
        held_elapsed = time.monotonic() - start

        # The wait added over the symmetric baseline reflects ONE shared budget
        # (~1×budget), strictly less than the ~2×budget a per-target bound would
        # accumulate — discriminated without a fragile absolute threshold.
        lock_wait = held_elapsed - baseline
        assert lock_wait < budget * 1.8, (held_elapsed, baseline, lock_wait)
        # Held targets aborted; the free one (gemini) still wrote ok.
        assert results["claude_settings"].status == "aborted"
        assert results["codex_settings"].status == "aborted"
        assert results["gemini_settings"].status == "ok"


class TestClaudeSettingsAtomicWrite:
    """_write_json is atomic — a crash between open() and replace() leaves the
    pre-existing settings.json untouched instead of producing a truncated file
    that reloads as 'no hooks configured' (issue #275)."""

    def test_crash_mid_replace_preserves_old_settings(self, claude_home, tmp_path, monkeypatch):
        target = claude_home / ".claude" / "settings.json"
        original = {"hooks": {"PostToolUse": [_rule("Write", "echo original")]}}
        target.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Edit", "echo new")]}},
        )

        def _boom(*_args, **_kwargs):
            raise OSError("simulated crash mid-replace")

        monkeypatch.setattr("memtomem.context._atomic.os.replace", _boom)

        with pytest.raises(OSError, match="simulated crash"):
            generate_all_settings(tmp_path, scope="user")

        # Old file survives, no .tmp sibling leaked. The persistent
        # ``.settings.json.lock`` sidecar (B3-3 cross-process lock, issue #1123)
        # is expected to remain — ``_file_lock`` never unlinks its sidecar by
        # design — so the leak check targets the mkstemp ``.tmp`` artifact only.
        assert json.loads(target.read_text(encoding="utf-8")) == original
        tmp_siblings = [
            p
            for p in target.parent.iterdir()
            if p.name.startswith(".settings.json.") and p.name.endswith(".tmp")
        ]
        assert tmp_siblings == []

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file mode (stat.S_IMODE) — Windows ignores POSIX permission bits",
    )
    def test_mode_is_0o600(self, claude_home, tmp_path):
        import stat as _stat

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Edit", "echo")]}},
        )
        generate_all_settings(tmp_path, scope="user")

        target = claude_home / ".claude" / "settings.json"
        assert _stat.S_IMODE(target.stat().st_mode) == 0o600


class TestClaudeSettingsNoClaudeCodeInstalled:
    """``~/.claude/`` does not exist → skip, never create it."""

    def test_skips_when_claude_not_installed(self, claude_home_missing, tmp_path):
        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "skipped"
        assert "not installed" in r.reason

        # Must NOT have created ~/.claude/
        assert not (claude_home_missing / ".claude").exists()


# ── Diff tests ──────────────────────────────────────────────────────


class TestClaudeSettingsDryRun:
    """diff_settings reports merge plan without writing."""

    def test_reports_missing_target(self, claude_home, tmp_path):
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write")]}},
        )
        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "missing target"

    def test_reports_in_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        content = {"hooks": {"PostToolUse": [_rule("Write")]}}
        target.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, content)
        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "in sync"

    def test_reports_out_of_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write")]}})
        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "out of sync"

    def test_does_not_write(self, claude_home, tmp_path):
        """diff must never modify the target file."""
        target = claude_home / ".claude" / "settings.json"
        original = json.dumps({"hooks": {}}) + "\n"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write")]}})
        diff_settings(tmp_path, scope="user")
        assert target.read_text(encoding="utf-8") == original


# ── CLI integration ─────────────────────────────────────────────────


class TestClaudeSettingsCliInclude:
    """``mm context generate --include=settings`` end-to-end via CliRunner."""

    def test_generate_includes_settings(self, claude_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a minimal .git so _find_project_root works
        (tmp_path / ".git").mkdir()

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo test")]}},
        )

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings"])
        assert result.exit_code == 0
        assert "Settings" in result.output or "settings" in result.output

        # Verify the file was actually written
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "PostToolUse" in written.get("hooks", {})

    def test_include_settings_validation(self):
        """Unknown include values are rejected."""
        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=bogus"])
        assert result.exit_code != 0
        assert "Unknown" in result.output or "bogus" in result.output


class TestClaudeSettingsHostWritePrompt:
    """``mm context {generate,sync} --include=settings`` confirms before
    mutating files outside the project root (e.g. ``~/.claude/settings.json``).
    Without confirmation users couldn't tell that running the command from a
    worktree silently edited their real home directory.

    The default ``claude_home`` fixture places fake $HOME *inside* tmp_path,
    which would defeat the ``is_relative_to(root)`` check; these tests use a
    sibling project dir so the project and the fake home don't overlap.
    """

    def _setup(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        _make_canonical_settings(
            project,
            {"hooks": {"PostToolUse": [_rule("Write", "echo test")]}},
        )
        return project

    def test_sync_prompts_before_host_write(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        # Decline the prompt → no file written.
        result = runner.invoke(context, ["sync", "--include=settings"], input="n\n")
        assert result.exit_code == 0
        assert "modify the following files outside this project" in result.output
        assert str(claude_home / ".claude" / "settings.json") in result.output
        assert "Skipped settings sync (declined)" in result.output
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_sync_proceeds_when_user_confirms(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["sync", "--include=settings"], input="y\n")
        assert result.exit_code == 0
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "PostToolUse" in written["hooks"]

    def test_sync_yes_flag_bypasses_prompt(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        # No stdin input — would block on prompt without --yes.
        result = runner.invoke(context, ["sync", "--include=settings", "--yes"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
        assert (claude_home / ".claude" / "settings.json").is_file()

    def test_generate_prompts_before_host_write(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings"], input="n\n")
        assert result.exit_code == 0
        assert "modify the following files outside this project" in result.output
        assert "Skipped settings sync (declined)" in result.output
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_generate_yes_flag_bypasses_prompt(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings", "-y"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
        assert (claude_home / ".claude" / "settings.json").is_file()

    def test_no_prompt_when_canonical_missing(self, claude_home, tmp_path, monkeypatch):
        """No canonical settings → nothing to write → no prompt, no error."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        monkeypatch.chdir(project)

        from memtomem.context.parser import CONTEXT_FILENAME

        (project / ".memtomem").mkdir(exist_ok=True)
        (project / CONTEXT_FILENAME).write_text("# Project\n", encoding="utf-8")

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        # No input — would deadlock if a prompt fired.
        result = runner.invoke(context, ["sync", "--include=settings"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_no_prompt_when_runtime_unavailable(self, claude_home_missing, tmp_path, monkeypatch):
        """Runtime not installed → generator skips → no prompt."""
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["sync", "--include=settings"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output


class TestGenerateAllSettingsHostWriteGate:
    """``generate_all_settings(allow_host_writes=False)`` — the library-level
    gate every front-end (CLI, MCP, Web) routes through. Without this gate
    living inside the I/O boundary the CLI confirm could be bypassed by any
    caller that imports the function directly (audit P0 review item 1)."""

    def _setup(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        canonical = project / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(
            json.dumps({"hooks": {"PostToolUse": [_rule("Write", "echo test")]}}, indent=2) + "\n",
            encoding="utf-8",
        )
        return project

    def test_default_refuses_host_write(self, claude_home, tmp_path):
        """Default ``allow_host_writes=False`` → status=needs_confirmation,
        no file written."""
        project = self._setup(tmp_path)
        results = generate_all_settings(project, scope="user")
        r = results["claude_settings"]
        assert r.status == "needs_confirmation"
        target = claude_home / ".claude" / "settings.json"
        assert str(target) in r.reason
        assert r.target == target
        assert not target.exists()

    def test_allow_host_writes_true_proceeds(self, claude_home, tmp_path):
        """``allow_host_writes=True`` → previous behavior, file written."""
        project = self._setup(tmp_path)
        results = generate_all_settings(project, scope="user", allow_host_writes=True)
        r = results["claude_settings"]
        assert r.status == "ok"
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "PostToolUse" in written["hooks"]

    def test_diff_unaffected_by_gate(self, claude_home, tmp_path):
        """``diff_settings`` is read-only — host-write gate must not make it
        report ``needs_confirmation``."""
        project = self._setup(tmp_path)
        results = diff_settings(project, scope="user")
        assert results["claude_settings"].status in {"in sync", "out of sync", "missing target"}

    def test_no_runtime_installed_skips_not_needs_confirmation(self, claude_home_missing, tmp_path):
        project = self._setup(tmp_path)
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "skipped"

    def test_no_canonical_skips_not_needs_confirmation(self, claude_home, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "skipped"

    def test_host_write_targets_lists_pending_paths(self, claude_home, tmp_path):
        """``host_write_targets()`` mirrors what ``generate_all_settings``
        would refuse — used by every front-end to surface the pending
        paths to the user."""
        project = self._setup(tmp_path)
        pending = host_write_targets(project, scope="user")
        target = claude_home / ".claude" / "settings.json"
        assert pending == [target]

    def test_host_write_targets_empty_when_no_canonical(self, claude_home, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        assert host_write_targets(project, scope="user") == []

    def test_host_write_targets_empty_when_runtime_missing(self, claude_home_missing, tmp_path):
        project = self._setup(tmp_path)
        assert host_write_targets(project, scope="user") == []

    @pytest.mark.requires_symlinks
    def test_symlink_target_is_treated_as_host_write(self, claude_home, tmp_path):
        """A symlink whose project-relative path *appears* under the project
        root must be classified by the resolved location (review item 4):
        ``Path.resolve()`` follows the symlink, so the gate still trips."""
        project = self._setup(tmp_path)
        # Symlink <project>/.claude → claude_home/.claude
        (claude_home / ".claude").mkdir(exist_ok=True)
        (project / ".claude").symlink_to(claude_home / ".claude")

        # The generator's target_file still computes Path.home() / .claude /
        # settings.json (the host path), and that resolves outside ``project``.
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "needs_confirmation"


# ── ADR-0010 §3 hooks.target_scope plumbing (issue #870) ────────────────────


class TestTargetScopeResolution:
    """6 path resolutions: 3 scope values × 2 entry points.

    Pins ADR-0010 §3 path math at both the resolver function and the
    end-to-end ``generate_all_settings`` call, so a regression in either
    layer surfaces. Mutation-validation per
    ``feedback_pin_test_mutation_validation``: swap the resolver's
    ``"user"`` branch return and at least one of these tests must fail.
    """

    @pytest.mark.parametrize(
        "scope,expected_relpath",
        [
            ("user", None),  # special: lives under HOME, computed at call time
            ("project_shared", ".claude/settings.json"),
            ("project_local", ".claude/settings.local.json"),
        ],
    )
    def test_resolver_function(self, claude_home, tmp_path, scope, expected_relpath):
        path = resolve_scope_path(tmp_path, scope)
        if scope == "user":
            assert path == claude_home / ".claude" / "settings.json"
        else:
            assert path == tmp_path / expected_relpath

    @pytest.mark.parametrize(
        "scope,expected_relpath",
        [
            ("user", None),
            ("project_shared", ".claude/settings.json"),
            ("project_local", ".claude/settings.local.json"),
        ],
    )
    def test_generator_target_file(self, claude_home, tmp_path, scope, expected_relpath):
        gen = ClaudeSettingsGenerator()
        path = gen.target_file(tmp_path, scope)
        if scope == "user":
            assert path == claude_home / ".claude" / "settings.json"
        else:
            assert path == tmp_path / expected_relpath

    @pytest.mark.parametrize(
        "scope,relpath",
        [
            ("project_shared", ".claude/settings.json"),
            ("project_local", ".claude/settings.local.json"),
        ],
    )
    def test_generate_writes_to_resolved_project_tier(self, claude_home, tmp_path, scope, relpath):
        """End-to-end: project-tier scopes write under the project root and
        do *not* touch ``~/.claude/settings.json``."""
        (tmp_path / ".claude").mkdir(exist_ok=True)
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo project")]}},
        )

        results = generate_all_settings(tmp_path, scope=scope, allow_host_writes=False)
        assert results["claude_settings"].status == "ok"
        assert (tmp_path / relpath).is_file()
        # The user-tier path must remain untouched.
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_generate_writes_to_user_tier_with_user_scope(self, claude_home, tmp_path):
        """End-to-end: ``scope="user"`` lands the merge at
        ``~/.claude/settings.json`` (the existing-install behavior)."""
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo user")]}},
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["claude_settings"].status == "ok"
        assert (claude_home / ".claude" / "settings.json").is_file()
        # No project-tier file written.
        assert not (tmp_path / ".claude" / "settings.json").exists()
        assert not (tmp_path / ".claude" / "settings.local.json").exists()


class TestIsAvailableLoosened:
    """ADR-0010 §3: loosen ``is_available`` so the tile shows up if Claude
    Code has *any* settings home for this project (user-tier or
    project-tier). Pins the case the loosening is for: the user has only
    project-local settings."""

    def test_user_only(self, claude_home, tmp_path):
        gen = ClaudeSettingsGenerator()
        assert gen.is_available(tmp_path) is True

    def test_project_only(self, claude_home_missing, tmp_path):
        gen = ClaudeSettingsGenerator()
        # Only project tier exists (with the local-tier settings file).
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        assert gen.is_available(tmp_path) is True

    def test_neither_returns_false(self, claude_home_missing, tmp_path):
        gen = ClaudeSettingsGenerator()
        assert gen.is_available(tmp_path) is False


class TestDefaultScopePreservation:
    """Pin: an install with no ``hooks.target_scope`` config — and no env
    var override — defaults to ``user`` and writes to
    ``~/.claude/settings.json``. This is the contract for "zero behavior
    change for existing installs" in ADR-0010 §2."""

    def test_default_scope_writes_to_user_home(self, claude_home, tmp_path, monkeypatch):
        from memtomem.config import Mem2MemConfig

        # Deterministic env: drop any host-shell scope override.
        monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)
        # ``feedback_path_home_cross_platform``: also pin USERPROFILE on
        # Windows so the test is deterministic across platforms.
        monkeypatch.setenv("USERPROFILE", str(claude_home))

        cfg = Mem2MemConfig()
        assert cfg.hooks.target_scope == "user"

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write")]}},
        )
        results = generate_all_settings(
            tmp_path, scope=cfg.hooks.target_scope, allow_host_writes=True
        )
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].target == claude_home / ".claude" / "settings.json"


class TestCliScopeFlag:
    """``mm context sync --scope=project_local`` writes to
    ``<project>/.claude/settings.local.json`` and leaves
    ``~/.claude/settings.json`` untouched. The codex-review regression
    pin: per-invocation override flows end-to-end through the click
    pipeline (``_SCOPE_OPTION`` → ``_resolve_cli_scope`` → both
    ``_confirm_settings_host_writes`` and ``_print_settings_generate``).
    """

    def test_sync_with_scope_project_local(self, claude_home, tmp_path, monkeypatch):
        # ``mm context sync`` walks up from cwd; chdir into the project so
        # ``_find_project_root`` lands here.
        (tmp_path / ".git").mkdir()
        (tmp_path / ".claude").mkdir()
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo flag")]}},
        )
        monkeypatch.chdir(tmp_path)

        from memtomem.cli.context_cmd import sync_cmd

        result = CliRunner().invoke(
            sync_cmd, ["--include=settings", "--scope=project_local", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".claude" / "settings.local.json").is_file()
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_generate_with_scope_project_local(self, claude_home, tmp_path, monkeypatch):
        """Symmetric pin for ``mm context generate --scope=…``. Both commands
        share ``_SCOPE_OPTION`` but a future drift in only one of them would
        slip past a sync-only test (codex review test-gap)."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".claude").mkdir()
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo gen-flag")]}},
        )
        monkeypatch.chdir(tmp_path)

        from memtomem.cli.context_cmd import generate_cmd

        result = CliRunner().invoke(
            generate_cmd, ["--include=settings", "--scope=project_local", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".claude" / "settings.local.json").is_file()
        assert not (claude_home / ".claude" / "settings.json").exists()


class TestTargetScopeValidation:
    """Pin: an unknown ``hooks.target_scope`` is rejected at construction
    time by Pydantic's ``Literal`` validator. This is the safety net
    behind ``_resolve_scope_path`` raising ``ValueError`` only as a
    defense-in-depth — production code never reaches that branch
    because Pydantic catches the typo first (codex review #5)."""

    def test_garbage_scope_raises_validation_error(self):
        from pydantic import ValidationError

        from memtomem.config import HooksConfig

        with pytest.raises(ValidationError):
            HooksConfig(target_scope="garbage")

    def test_resolver_rejects_unknown_scope(self, tmp_path):
        from memtomem.context.settings import resolve_scope_path

        with pytest.raises(ValueError, match="Unknown target_scope"):
            resolve_scope_path(tmp_path, "garbage")


class TestResolveScopeMigrationFree:
    """Pin: ``_resolve_cli_scope`` and ``_resolve_mcp_scope`` MUST NOT
    trigger the auto-discover migration (per
    ``feedback_doctor_no_migration_loader``). Scope reads are read-only
    diagnostic surfaces; touching disk as a side effect is the same
    footgun PR #838 hit."""

    def test_cli_scope_resolver_does_not_invoke_migration(self, monkeypatch):
        from memtomem.cli import context_cmd

        called: list[bool] = []

        def fake_migrate(_cfg):
            called.append(True)

        monkeypatch.setattr("memtomem.config._migrate_auto_discover_once", fake_migrate)
        # Drop env override so config-load path runs fully.
        monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)

        scope = context_cmd._resolve_cli_scope(None)
        assert scope in ("user", "project_shared", "project_local")
        assert called == []

    def test_mcp_scope_resolver_does_not_invoke_migration(self, monkeypatch):
        from memtomem.server.tools import context as mcp_ctx

        called: list[bool] = []

        def fake_migrate(_cfg):
            called.append(True)

        monkeypatch.setattr("memtomem.config._migrate_auto_discover_once", fake_migrate)
        monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)

        scope = mcp_ctx._resolve_mcp_scope()
        assert scope in ("user", "project_shared", "project_local")
        assert called == []


# ── HTTP-route contract tests (RFC #761 PR-2 — ADR-0001 §5 c2/c4) ────────────


class TestSettingsHttpLayer:
    """HTTP-route contract for ``/api/context/settings/*``.

    Pins the FastAPI surface that wires up ``settings_sync`` per
    ADR-0001 §5 c2 (round-trip, unidirectional shape) and §5 c4
    (conflict path covered — soft-abort response). Helper-level merge
    and mtime behavior is already covered by the merge / concurrent
    classes above; these two tests pin the route layer that production
    UI depends on.

    PR-3 of RFC #761 will move ``settings_sync`` from
    ``_DEV_ONLY_ROUTERS`` to ``_PROD_ROUTERS`` — these tests use
    ``mode="dev"`` to stay correct against current ``main`` and remain
    correct after that move (the router is mounted in either mode).
    """

    @pytest.fixture
    def app(self, claude_home, tmp_path):
        from memtomem.config import Mem2MemConfig

        application = create_app(lifespan=None, mode="dev")
        application.state.project_root = tmp_path
        application.state.storage = AsyncMock()
        # Real config so ``get_hooks_target_scope`` Depends can read
        # ``cfg.hooks.target_scope`` (default "user").
        application.state.config = Mem2MemConfig()
        application.state.search_pipeline = None
        application.state.index_engine = None
        application.state.embedder = None
        application.state.dedup_scanner = None
        application.state.last_reload_error = None
        return application

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def test_diff_no_source_still_reports_target_hooks(self, client, claude_home):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"Stop": [_rule("", "echo target")]}}),
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        assert diff.status_code == 200
        diff_body = diff.json()
        assert diff_body["status"] == "no_source"
        assert diff_body["target_hooks"]["configured"][0]["event"] == "Stop"
        assert diff_body["target_hooks"]["target_only"][0]["event"] == "Stop"

    async def test_diff_empty_canonical_reports_no_hooks(self, client, claude_home, tmp_path):
        """Empty canonical hooks are not a successful sync with hidden content."""
        _make_canonical_settings(tmp_path, {"hooks": {}})
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [_rule("Bash", "echo user")]}}),
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        assert diff.status_code == 200
        diff_body = diff.json()
        assert diff_body["status"] == "no_hooks"
        assert diff_body["hooks"] == {"synced": [], "conflicts": [], "pending": []}
        assert diff_body["target_hooks"]["configured"][0]["event"] == "PreToolUse"
        assert diff_body["target_hooks"]["target_only"][0]["matcher"] == "Bash"
        assert diff_body["target_hooks"]["configured"][0]["rule_index"] == 0
        assert diff_body["target_hooks"]["configured"][0]["rule_hash"]
        assert diff_body["target_mtime_ns"] == str(target.stat().st_mtime_ns)
        assert diff_body["canonical_mtime_ns"] == str(
            (tmp_path / CANONICAL_SETTINGS_FILE).stat().st_mtime_ns
        )

    async def test_promote_target_rule_creates_canonical_after_private_confirm(
        self, client, claude_home, tmp_path
    ):
        """Target → canonical promotion is rule-scoped and gated for user targets."""
        target = claude_home / ".claude" / "settings.json"
        target_rule = _rule("Bash", "echo target")
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [target_rule]}}, indent=2) + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        assert diff.status_code == 200
        body = diff.json()
        row = body["target_hooks"]["configured"][0]
        assert body["canonical_mtime_ns"] is None

        request = {
            "event": row["event"],
            "matcher": row["matcher"],
            "rule_index": row["rule_index"],
            "rule_hash": row["rule_hash"],
            "target_mtime_ns": body["target_mtime_ns"],
            "canonical_mtime_ns": body["canonical_mtime_ns"],
        }
        gated = await client.post(
            "/api/context/settings/rules/promote?target_scope=user",
            json=request,
        )
        assert gated.status_code == 200
        assert gated.json()["status"] == "needs_confirmation"
        assert not (tmp_path / CANONICAL_SETTINGS_FILE).exists()

        promoted = await client.post(
            "/api/context/settings/rules/promote?target_scope=user",
            json={**request, "confirm_private_to_shared": True},
        )
        assert promoted.status_code == 200, promoted.text
        assert promoted.json()["status"] == "ok"

        canonical = json.loads((tmp_path / CANONICAL_SETTINGS_FILE).read_text(encoding="utf-8"))
        assert canonical["hooks"]["PreToolUse"] == [target_rule]

    async def test_promote_target_rule_is_idempotent_when_canonical_has_same_hash(
        self, client, tmp_path
    ):
        """A repeated promote of the same rule is a no-op success."""
        shared_rule = _rule("Write", "echo shared")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [shared_rule]}})
        (tmp_path / ".claude").mkdir()
        target = tmp_path / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [shared_rule]}}, indent=2) + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=project_shared")
        row = diff.json()["target_hooks"]["configured"][0]
        resp = await client.post(
            "/api/context/settings/rules/promote?target_scope=project_shared",
            json={
                "event": row["event"],
                "matcher": row["matcher"],
                "rule_index": row["rule_index"],
                "rule_hash": row["rule_hash"],
                "target_mtime_ns": diff.json()["target_mtime_ns"],
                "canonical_mtime_ns": diff.json()["canonical_mtime_ns"],
            },
        )
        assert resp.status_code == 200
        out = resp.json()
        assert out["status"] == "ok"
        assert out["idempotent"] is True

        canonical = json.loads((tmp_path / CANONICAL_SETTINGS_FILE).read_text(encoding="utf-8"))
        assert canonical["hooks"]["PostToolUse"] == [shared_rule]

    async def test_promote_target_rule_conflicts_on_same_matcher_different_rule(
        self, client, tmp_path
    ):
        """Same event/matcher with different payload is reported, not merged."""
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo canonical")]}},
        )
        (tmp_path / ".claude").mkdir()
        target = tmp_path / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [_rule("Write", "echo target")]}}),
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=project_shared")
        row = diff.json()["target_hooks"]["configured"][0]
        resp = await client.post(
            "/api/context/settings/rules/promote?target_scope=project_shared",
            json={
                "event": row["event"],
                "matcher": row["matcher"],
                "rule_index": row["rule_index"],
                "rule_hash": row["rule_hash"],
                "target_mtime_ns": diff.json()["target_mtime_ns"],
                "canonical_mtime_ns": diff.json()["canonical_mtime_ns"],
            },
        )
        assert resp.status_code == 200
        out = resp.json()
        assert out["status"] == "conflict"
        assert "existing" in out

        canonical = json.loads((tmp_path / CANONICAL_SETTINGS_FILE).read_text(encoding="utf-8"))
        assert canonical["hooks"]["PostToolUse"] == [_rule("Write", "echo canonical")]

    async def test_delete_target_rule_uses_index_and_hash_for_duplicate_matchers(
        self, client, claude_home
    ):
        """Duplicate same-matcher rows are deleted by exact identity."""
        target = claude_home / ".claude" / "settings.json"
        first = _rule("", "echo first")
        second = _rule("", "echo second")
        target.write_text(
            json.dumps({"hooks": {"Stop": [first, second]}}, indent=2) + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        rows = diff.json()["target_hooks"]["configured"]
        second_row = rows[1]
        resp = await client.post(
            "/api/context/settings/rules/delete?target_scope=user",
            json={
                "event": second_row["event"],
                "matcher": second_row["matcher"],
                "rule_index": second_row["rule_index"],
                "rule_hash": second_row["rule_hash"],
                "target_mtime_ns": diff.json()["target_mtime_ns"],
                "canonical_mtime_ns": diff.json()["canonical_mtime_ns"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["hooks"]["Stop"] == [first]

    async def test_delete_target_rule_ignores_stale_canonical_mtime(
        self, client, claude_home, tmp_path
    ):
        """Delete mutates only target settings, so canonical freshness is not load-bearing."""
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo canonical")]}},
        )
        target = claude_home / ".claude" / "settings.json"
        target_rule = _rule("Bash", "echo target")
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [target_rule]}}, indent=2) + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        body = diff.json()
        row = body["target_hooks"]["configured"][0]

        canonical = tmp_path / CANONICAL_SETTINGS_FILE
        canonical.write_text(
            json.dumps({"hooks": {"Stop": [_rule("", "echo changed canonical")]}}),
            encoding="utf-8",
        )
        import os as _os

        st = canonical.stat()
        _os.utime(canonical, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        resp = await client.post(
            "/api/context/settings/rules/delete?target_scope=user",
            json={
                "event": row["event"],
                "matcher": row["matcher"],
                "rule_index": row["rule_index"],
                "rule_hash": row["rule_hash"],
                "target_mtime_ns": body["target_mtime_ns"],
                "canonical_mtime_ns": body["canonical_mtime_ns"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["hooks"] == {}

    async def test_rule_action_aborts_when_mtime_is_stale(self, client, claude_home):
        target = claude_home / ".claude" / "settings.json"
        original = _rule("Bash", "echo original")
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [original]}}, indent=2) + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        row = diff.json()["target_hooks"]["configured"][0]
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [_rule("Bash", "echo changed")]}}),
            encoding="utf-8",
        )
        import os as _os

        st = target.stat()
        _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        resp = await client.post(
            "/api/context/settings/rules/delete?target_scope=user",
            json={
                "event": row["event"],
                "matcher": row["matcher"],
                "rule_index": row["rule_index"],
                "rule_hash": row["rule_hash"],
                "target_mtime_ns": diff.json()["target_mtime_ns"],
                "canonical_mtime_ns": diff.json()["canonical_mtime_ns"],
            },
        )
        # Stale-write aborts are HTTP 409 with the status-keyed envelope,
        # matching the Skills/Commands/Agents contract (#1229).
        assert resp.status_code == 409
        assert resp.json()["status"] == "aborted"
        assert "echo changed" in target.read_text(encoding="utf-8")

    async def test_rule_action_aborts_when_index_hash_identity_is_stale(self, client, claude_home):
        """If the requested slot no longer has the same hash, do not scan elsewhere."""
        target = claude_home / ".claude" / "settings.json"
        first = _rule("Bash", "echo first")
        second = _rule("Bash", "echo second")
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [first, second]}}, indent=2) + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        body = diff.json()
        original_first_row = body["target_hooks"]["configured"][0]

        # Reorder the same two rules but preserve the original mtime token.
        # The target hash still exists at a different index; the endpoint
        # intentionally refuses to auto-search and returns stale.
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [second, first]}}, indent=2) + "\n",
            encoding="utf-8",
        )
        import os as _os

        old_mtime = int(body["target_mtime_ns"])
        st = target.stat()
        _os.utime(target, ns=(st.st_atime_ns, old_mtime))

        resp = await client.post(
            "/api/context/settings/rules/delete?target_scope=user",
            json={
                "event": original_first_row["event"],
                "matcher": original_first_row["matcher"],
                "rule_index": original_first_row["rule_index"],
                "rule_hash": original_first_row["rule_hash"],
                "target_mtime_ns": body["target_mtime_ns"],
                "canonical_mtime_ns": body["canonical_mtime_ns"],
            },
        )
        assert resp.status_code == 409
        assert resp.json()["status"] == "aborted"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["hooks"]["PreToolUse"] == [second, first]

    async def test_promote_aborts_with_409_when_mtime_is_stale(self, client, claude_home):
        """Promote shares the same stale-write 409 contract as delete (#1229)."""
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [_rule("Bash", "echo original")]}}, indent=2)
            + "\n",
            encoding="utf-8",
        )

        diff = await client.get("/api/context/settings?target_scope=user")
        body = diff.json()
        row = body["target_hooks"]["configured"][0]
        import os as _os

        st = target.stat()
        _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        resp = await client.post(
            "/api/context/settings/rules/promote?target_scope=user",
            json={
                "event": row["event"],
                "matcher": row["matcher"],
                "rule_index": row["rule_index"],
                "rule_hash": row["rule_hash"],
                "target_mtime_ns": body["target_mtime_ns"],
                "canonical_mtime_ns": body["canonical_mtime_ns"],
                "confirm_private_to_shared": True,
            },
        )
        assert resp.status_code == 409
        out = resp.json()
        assert out["status"] == "aborted"
        # The two-key freshness names are load-bearing for Promote All's
        # token refresh — pin they survive the 409 flip.
        assert "target_mtime_ns" in out
        assert "canonical_mtime_ns" in out

    async def test_sync_route_round_trip_unidirectional(self, client, claude_home, tmp_path):
        """ADR-0001 §5 c2 — unidirectional round-trip via the route layer.

        Settings has no reverse-import API by design (additive merge
        cannot distinguish canonical-authored from user-authored
        entries), so the round-trip is: write canonical → POST sync
        route → GET diff confirms ``in_sync`` AND target file on disk
        preserves user-authored hooks + non-hook keys.
        """
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write|Edit", "echo canonical")]}},
        )
        target = claude_home / ".claude" / "settings.json"
        user_authored = {
            "permissions": {"allow": ["Read"]},
            "env": {"FOO": "bar"},
            "hooks": {
                "PreToolUse": [_rule("Bash", "echo user")],
            },
        }
        target.write_text(json.dumps(user_authored, indent=2) + "\n", encoding="utf-8")

        resp = await client.post(
            "/api/context/settings/sync?target_scope=user",
            json={"allow_host_writes": True},
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        claude_result = next(r for r in results if r["name"] == "claude_settings")
        assert claude_result["status"] == "ok"

        diff = await client.get("/api/context/settings?target_scope=user")
        assert diff.status_code == 200
        diff_body = diff.json()
        assert diff_body["status"] == "in_sync"
        assert any(
            h["event"] == "PostToolUse" and h["matcher"] == "Write|Edit"
            for h in diff_body["hooks"]["synced"]
        )

        merged = json.loads(target.read_text(encoding="utf-8"))
        assert merged["permissions"] == {"allow": ["Read"]}
        assert merged["env"] == {"FOO": "bar"}
        assert any(r.get("matcher") == "Bash" for r in merged["hooks"]["PreToolUse"])
        assert any(r.get("matcher") == "Write|Edit" for r in merged["hooks"]["PostToolUse"])

    async def test_resolve_route_returns_soft_abort_on_stale_mtime(
        self, client, claude_home, tmp_path, monkeypatch
    ):
        """ADR-0001 §5 c4 — soft-abort response pinned at the route layer.

        Mirrors the helper-level pattern from
        ``TestClaudeSettingsMergeConcurrent.test_aborts_on_mtime_change``
        but exercises ``POST /api/context/settings/resolve``: the
        route captures ``target_path.stat().st_mtime_ns`` before the
        load and rechecks before write. We bump mtime as a side effect
        of the load so the recheck mismatches → HTTP 409 + ``{"status":
        "aborted", "reason": <... modified by another process ...>,
        "mtime_ns": <current st_mtime_ns>}`` (#1229 unified the HTTP
        status with the Skills/Commands/Agents stale-write envelope;
        the body shape is unchanged).

        Asserts the same triplet the Skills/Commands/Agents conflict
        tests pin (status / no-write content / current mtime_ns echo)
        — without all three a regression that wrote the proposed rule
        before returning aborted, or dropped the mtime_ns echo from
        the envelope, would still pass.
        """
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo canonical")]}},
        )
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps(
                {"hooks": {"PostToolUse": [_rule("Write", "echo user")]}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        from memtomem.web.routes import settings_sync as routes_sync_mod

        orig_load = routes_sync_mod._safe_load_json

        def bumping_load(path):
            result = orig_load(path)
            if path == target:
                # Same 1ms bump as the helper-level test — clears
                # Windows NTFS WriteFile mtime granularity (~15.6ms
                # tick) so the recheck reliably observes a mismatch.
                import os as _os

                st = path.stat()
                _os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        monkeypatch.setattr(routes_sync_mod, "_safe_load_json", bumping_load)

        resp = await client.post(
            "/api/context/settings/resolve?target_scope=user",
            json={
                "event": "PostToolUse",
                "matcher": "Write",
                "action": "use_proposed",
            },
        )
        assert resp.status_code == 409
        body = resp.json()
        assert body["status"] == "aborted"
        assert "modified by another process" in body["reason"]
        # mtime_ns echo — clients refresh local state without an extra
        # round-trip; matches Skills/Commands/Agents 409 envelopes.
        assert body["mtime_ns"] == str(target.stat().st_mtime_ns)
        # No-write content — pinned both ways: the original user rule
        # survives, and the canonical rule is *not* persisted to disk.
        # A regression that wrote before returning aborted would flip
        # the second assertion.
        on_disk = target.read_text(encoding="utf-8")
        assert "echo user" in on_disk
        assert "echo canonical" not in on_disk

    async def test_resolve_respects_target_scope(self, app, client, claude_home, tmp_path):
        """Codex blocker #2 regression pin: ``POST /settings-sync/resolve``
        must use the request ``target_scope`` query param, not the hardcoded
        user-tier path. With ``target_scope="project_local"``
        a resolve must mutate ``<project>/.claude/settings.local.json``
        and leave ``~/.claude/settings.json`` untouched.

        Without the fix, ``_claude_target()`` was a parameterless helper
        that always returned ``Path.home() / .claude / settings.json``
        and the resolve handler would have written there regardless of
        the configured scope.
        """
        # Configure the opposite scope on live app state as a regression
        # guard: Web settings routes must follow the request query param.
        app.state.config.hooks.target_scope = "user"

        # Seed canonical with a hook differing from the user-authored rule
        # so the route surfaces a conflict.
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo canonical")]}},
        )

        # Project-local target: <project>/.claude/settings.local.json
        (tmp_path / ".claude").mkdir()
        project_local_target = tmp_path / ".claude" / "settings.local.json"
        project_local_target.write_text(
            json.dumps(
                {"hooks": {"PostToolUse": [_rule("Write", "echo user")]}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        # User-tier target: ``~/.claude/settings.json`` — must remain
        # byte-identical after the resolve.
        user_target = claude_home / ".claude" / "settings.json"
        user_authored = (
            json.dumps(
                {"hooks": {"PreToolUse": [_rule("Bash", "echo untouched")]}},
                indent=2,
            )
            + "\n"
        )
        user_target.write_text(user_authored, encoding="utf-8")

        resp = await client.post(
            "/api/context/settings/resolve?target_scope=project_local",
            json={"event": "PostToolUse", "matcher": "Write", "action": "use_proposed"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok", body

        # The project-local file mutated to the canonical rule.
        merged = json.loads(project_local_target.read_text(encoding="utf-8"))
        assert any(
            "echo canonical" in r["hooks"][0]["command"] for r in merged["hooks"]["PostToolUse"]
        )
        # The user-tier file was NOT touched.
        assert user_target.read_text(encoding="utf-8") == user_authored
