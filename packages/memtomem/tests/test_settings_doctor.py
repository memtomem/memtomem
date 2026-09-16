"""Tests for settings_doctor — duplicate-tier hook detection (ADR-0010 §4).

Covers four surfaces:

* Pure detector unit tests (``settings_doctor.detect_duplicate_tiers``)
* CLI ``mm context settings-doctor`` subcommand (clean/duplicates exit
  codes, ``--json`` schema)
* CLI sync warning wired through ``_print_settings_generate`` /
  ``_print_settings_diff``
* Web ``/api/settings-sync`` GET / POST response payloads
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_doctor import (
    HookSignature,
    UnportableHookCommand,
    UnscannedSettingsFile,
    _extract_unportable_literals,
    detect_duplicate_tiers,
    find_malformed_matchers,
    find_unportable_hook_commands,
    find_unscanned_settings_files,
    format_unportable_command_warning,
    format_unscanned_settings_warning,
    load_canonical_signatures,
    redact_unportable_command_fields,
)
from memtomem.web.app import create_app
from .helpers import set_home


# ── Helpers ────────────────────────────────────────────────────────


def _rule(matcher: str = "", command: str = "mm session start") -> dict:
    """Build a single hook rule in record format."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": 5000}],
    }


def _write_settings(path, hooks: dict) -> None:
    """Write a settings.json file with the given hooks record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


def _write_canonical(project_root, hooks: dict) -> None:
    """Write ``.memtomem/settings.json`` with the given hooks record."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    _write_settings(canonical_path, hooks)


def _bundled_hook() -> dict:
    """A canonical-shape memtomem-managed hook record."""
    return {"PostToolUse": [_rule("Edit|Write", "mm session start")]}


def _stamped_bundled_hook() -> dict:
    """A generated ADR-0019-stamped hook record."""
    return {
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [
                    {
                        "type": "command",
                        "command": "mm session start",
                        "timeout": 5000,
                        "statusMessage": "memtomem · PostToolUse",
                    }
                ],
            }
        ]
    }


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """HOME pointing into tmp_path so the user-tier settings.json is
    isolated. Mirrors ``test_context_settings.claude_home`` but does not
    create ``.claude/`` — individual tests opt into populating it.
    """
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project_root(tmp_path):
    """Project root with ``.git`` so ``_find_project_root`` lands here."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude").mkdir()
    return root


# ── Detector unit tests ────────────────────────────────────────────


class TestDetectorCanonical:
    """``load_canonical_signatures`` extracts signatures from
    ``.memtomem/settings.json``."""

    def test_returns_empty_when_canonical_missing(self, project_root):
        assert load_canonical_signatures(project_root) == set()

    def test_returns_empty_when_canonical_malformed(self, project_root):
        canonical = project_root / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("{not valid json", encoding="utf-8")
        assert load_canonical_signatures(project_root) == set()

    def test_extracts_signatures(self, project_root):
        _write_canonical(project_root, _bundled_hook())
        signatures = load_canonical_signatures(project_root)
        assert (
            HookSignature(
                event="PostToolUse",
                matcher="Edit|Write",
                command_shape="mm session start",
            )
            in signatures
        )


class TestDetectDuplicateTiers:
    """Core detector behavior."""

    def test_no_canonical_means_no_duplicates(self, project_root, fake_home):
        # Even though user tier has a hook, no canonical means nothing to
        # compare against → empty list (the doctor doesn't invent matches).
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        assert detect_duplicate_tiers(project_root, active_scope="project_local") == []

    def test_no_duplicates_when_active_tier_only(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        # Active scope = user; hook lives in user tier → not a duplicate.
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        assert detect_duplicate_tiers(project_root, active_scope="user") == []

    def test_duplicate_in_user_when_active_is_project_local(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1
        assert duplicates[0].tier == "user"
        assert duplicates[0].path == fake_home / ".claude" / "settings.json"
        assert len(duplicates[0].entries) == 1

    def test_stamped_duplicate_in_user_when_active_is_project_local(self, project_root, fake_home):
        """ADR-0019 statusMessage markers do not perturb doctor classification."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _stamped_bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1
        assert duplicates[0].entries[0] == HookSignature(
            event="PostToolUse",
            matcher="Edit|Write",
            command_shape="mm session start",
        )

    def test_duplicate_in_project_shared_when_active_is_user(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="user")
        assert len(duplicates) == 1
        assert duplicates[0].tier == "project_shared"

    def test_duplicates_in_two_other_tiers(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        tiers = {dup.tier for dup in duplicates}
        assert tiers == {"user", "project_shared"}

    def test_canonical_signature_whitespace_robustness(self, project_root, fake_home):
        """Internal-whitespace variants still match (ADR-0010 §4)."""
        _write_canonical(project_root, _bundled_hook())
        # Same command but with extra spaces — must still be detected.
        variant = {"PostToolUse": [_rule("Edit|Write", "mm   session   start  ")]}
        _write_settings(fake_home / ".claude" / "settings.json", variant)
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1
        assert duplicates[0].entries[0].command_shape == "mm session start"

    def test_canonical_signature_matcher_whitespace_strip(self, project_root, fake_home):
        """Leading/trailing whitespace in matcher unifies."""
        _write_canonical(project_root, _bundled_hook())
        variant = {"PostToolUse": [_rule("  Edit|Write  ", "mm session start")]}
        _write_settings(fake_home / ".claude" / "settings.json", variant)
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1

    def test_missing_matcher_unified_with_empty(self, project_root, fake_home):
        """Missing matcher key is equivalent to ``matcher=""``."""
        _write_canonical(
            project_root,
            {"SessionStart": [_rule("", "mm index")]},
        )
        # Other tier omits the matcher key entirely.
        rule_no_matcher = {"hooks": [{"type": "command", "command": "mm index", "timeout": 5000}]}
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"SessionStart": [rule_no_matcher]},
        )
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1

    def test_non_canonical_hook_in_other_tier_ignored(self, project_root, fake_home):
        """A hook in the user tier that doesn't match the canonical
        signature is NOT reported — only canonical-matched entries
        count as duplicates per ADR-0010 §4."""
        _write_canonical(project_root, _bundled_hook())
        # User has a totally unrelated hand-authored hook.
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"PreToolUse": [_rule("Bash", "echo something")]},
        )
        assert detect_duplicate_tiers(project_root, active_scope="project_local") == []

    def test_malformed_other_tier_skipped(self, project_root, fake_home):
        """Malformed JSON in a non-active tier doesn't crash — that
        tier is skipped, the rest still classify."""
        _write_canonical(project_root, _bundled_hook())
        bad = fake_home / ".claude" / "settings.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not valid", encoding="utf-8")
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        # User tier was malformed → skipped. Project_shared still reported.
        tiers = {dup.tier for dup in duplicates}
        assert tiers == {"project_shared"}

    def test_missing_other_tier_silently_skipped(self, project_root, fake_home):
        """A non-existent tier file is not a duplicate (nothing to flag)."""
        _write_canonical(project_root, _bundled_hook())
        # User tier file does not exist; project_shared has the duplicate.
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        tiers = {dup.tier for dup in duplicates}
        assert tiers == {"project_shared"}

    def test_active_scope_excluded_even_when_path_resolves_via_symlink(
        self, project_root, fake_home, tmp_path
    ):
        """Symlink dedup: when ``project_shared`` symlinks into
        ``~/.claude/`` the same real file MUST NOT be reported twice.
        """
        # Make project_root/.claude a symlink to fake_home/.claude.
        (project_root / ".claude").rmdir()
        (fake_home / ".claude").mkdir(exist_ok=True)
        (project_root / ".claude").symlink_to(fake_home / ".claude")

        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())

        # Active scope = project_local. Both 'user' and 'project_shared'
        # are non-active; both resolve through .claude/, but the
        # settings.json filenames differ (settings.json vs
        # settings.local.json), so only the user-tier file actually
        # exists. project_shared's resolved path matches user's.
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        # Only one duplicate emitted, not two — the symlink dedup kept
        # the second iteration from re-reporting the same real file.
        assert len(duplicates) == 1


@pytest.mark.parametrize(
    "malformed",
    [None, 7, ["Bash"], {"tool": "Bash"}],
    ids=["null", "int", "list", "dict"],
)
class TestMalformedMatcherHandling:
    def test_malformed_canonical_never_matches_tier_match_all(
        self, project_root, fake_home, malformed
    ):
        _write_canonical(
            project_root,
            {"SessionStart": [_rule(malformed, "mm index")]},
        )
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"SessionStart": [_rule("", "mm index")]},
        )

        assert detect_duplicate_tiers(project_root, active_scope="project_local") == []
        findings = find_malformed_matchers(project_root)
        assert len(findings) == 1
        finding = findings[0]
        assert finding.source == "canonical"
        assert finding.tier is None
        assert finding.event == "SessionStart"
        assert finding.rule_index == 0
        assert finding.matcher_type == type(malformed).__name__

    def test_malformed_tier_never_matches_canonical_match_all(
        self, project_root, fake_home, malformed
    ):
        _write_canonical(
            project_root,
            {"SessionStart": [_rule("", "mm index")]},
        )
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"SessionStart": [_rule(malformed, "mm index")]},
        )

        assert detect_duplicate_tiers(project_root, active_scope="project_local") == []
        [finding] = find_malformed_matchers(project_root)
        assert finding.source == "tier"
        assert finding.tier == "user"
        assert finding.event == "SessionStart"
        assert finding.matcher_type == type(malformed).__name__


def test_healthy_duplicate_beside_malformed_rule_is_still_reported(project_root, fake_home):
    _write_canonical(
        project_root,
        {
            "PostToolUse": [
                _rule("Edit|Write", "mm session start"),
                _rule(["Bash"], "broken"),
            ]
        },
    )
    _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())

    duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
    assert len(duplicates) == 1
    assert duplicates[0].entries[0].matcher == "Edit|Write"
    [finding] = find_malformed_matchers(project_root)
    assert finding.rule_index == 1


# ── Unportable hook commands unit tests ────────────────────────────


_ALICE = "/home/alice"

# The #2407 detection contract as an example table. The scanner is lexical: a home
# root counts wherever its text appears unless the character before it embeds it in
# a longer word or path. Reported literals are roots, not full paths.
_FLAGGED = [
    ("/home/alice/hook.sh", [_ALICE]),
    ("/Users/alice/bin/hook.sh", ["/Users/alice"]),
    ("/users/alice/x", ["/users/alice"]),
    ('"/home/alice/hook.sh"', [_ALICE]),
    ('"/home/alice"/hook.sh', [_ALICE]),
    ("bash /home/alice/hook.sh", [_ALICE]),
    ("cd /tmp && /home/alice/hook.sh", [_ALICE]),
    ("true&&/Users/alice/hook.sh", ["/Users/alice"]),
    ("true;/home/bob/hook.sh", ["/home/bob"]),
    ("< /home/bob/hook.sh", ["/home/bob"]),
    ("tool --dir=/home/alice/hooks", [_ALICE]),
    ('"$(/home/alice/bin)/hook.sh"', [_ALICE]),
    ("echo `/home/bob/hook.sh`", ["/home/bob"]),
    ("sh -c 'echo ready; /home/alice/hook.sh'", [_ALICE]),
    ("bash <<EOF\n'${HOOK:-/home/alice/hook.sh}'\nEOF", [_ALICE]),
    ("${HOOK-/home/alice/hook.sh}", [_ALICE]),
    ("${HOOK:=/home/alice/hook.sh}", [_ALICE]),
    ("${HOOK:+/opt}/home/alice/hook.sh", [_ALICE]),
    ("eval '/home/alice/hook.sh'", [_ALICE]),
    ("PATH=/home/alice/bin:/Users/bob/bin", [_ALICE, "/Users/bob"]),
    ("/home/alice/a&/Users/bob/b", [_ALICE, "/Users/bob"]),
    ("/home/alice/a /home/alice/b", [_ALICE]),
    ("ssh-host:/Users/bob/x", ["/Users/bob"]),
    ("/home/alice+dev/hook.sh", ["/home/alice+dev"]),
    ("/home/álîce/hook.sh", ["/home/álîce"]),
    ("file:///home/alice/hook.sh", [_ALICE]),
    ("/Users/Shared/tool", ["/Users/Shared"]),
    ("/home/linuxbrew/.linuxbrew/bin/brew", ["/home/linuxbrew"]),
    (r"C:\Users\alice\hook.cmd", [r"C:\Users\alice"]),
    (r"C:\Users\Public\tool.exe", [r"C:\Users\Public"]),
    ('"D:/Users/alice/a=b.cmd"', ["D:/Users/alice"]),
    (r'tool "--dir=C:\Users\alice\hook.cmd"', [r"C:\Users\alice"]),
    (r"C:\\Users\\alice\\hook.cmd", [r"C:\\Users\\alice"]),
]

# Documented false positives: the warning is advisory, and telling these apart
# would take the shell grammar the scanner deliberately does not implement.
_ACCEPTED_FALSE_POSITIVES = [
    ('"$HOME"/home/bob/hook.sh', ["/home/bob"]),
    ("${HOME}/home/bob/hook.sh", ["/home/bob"]),
    ("uv run mm session start # formerly /home/alice/hook.sh", [_ALICE]),
    ("cat <<'EOF'\n/home/alice/data\nEOF", [_ALICE]),
    ("echo '${HOOK:-/home/alice/hook.sh}'", [_ALICE]),
]

_NOT_FLAGGED = [
    "uv run mm session start",
    "hooks/foo.sh",
    "./script.sh",
    "./home/alice/x",
    "../home/alice/x",
    "~/home/bob/hook.sh",
    "$HOME/home/bob/hook.sh",
    "/opt/home/alice/hook.sh",
    "/opt//home/alice/hook.sh",
    "pkg-config/home/alice",
    "https://example.com/home/alice/repo",
    "https://example.com//home/alice/repo",
    "./fixtures/C:/Users/alice/hook.cmd",
    r"%USERPROFILE%\Users\alice\hook.bat",
    "/usr/bin/bash",
    "/opt/homebrew/bin/python3",
    "/home/$USER/hook.sh",
    "/home/${USER}/hook.sh",
    "/home/*/hook.sh",
    "/home/",
    "/home/../etc/passwd",
]

_HOST_HOME_CASES = [
    ("/srv/jenkins/bin/hook.sh", "/srv/jenkins", ["/srv/jenkins"]),
    ('"/srv/o\'connor/hook.sh"', "/srv/o'connor", ["/srv/o'connor"]),
    ("/root", "/root", ["/root"]),
    ("/srv/jenkins-old/hook.sh", "/srv/jenkins", []),
    ("/data/srv/jenkins/hook.sh", "/srv/jenkins", []),
    ("/SRV/JENKINS/x", "/srv/jenkins", []),
    ("/Users/alice/x", "/Users/alice", ["/Users/alice"]),
    (r"D:\Profiles\alice\hook.cmd", r"D:\Profiles\alice", [r"D:\Profiles\alice"]),
    ("D:/Profiles/alice/hook.cmd", r"D:\Profiles\alice", ["D:/Profiles/alice"]),
    (r"d:\profiles\alice\sub\hook.cmd", r"D:\Profiles\alice", [r"d:\profiles\alice"]),
    (r'"D:\Profiles\alice\hook.cmd"', r"D:\Profiles\alice", [r"D:\Profiles\alice"]),
    (r"D:\Profiles\alice2\x", r"D:\Profiles\alice", []),
    (r"\\server\share\alice\hook.cmd", r"\\server\share\alice", [r"\\server\share\alice"]),
    ("//server/share/alice/hook.cmd", r"\\server\share\alice", ["//server/share/alice"]),
]

# No guarantee either way: only termination is pinned, never silence.
_NON_GOALS = [
    "DIR=/home; $DIR/alice/hook.sh",
    "echo /home/\\\nalice/hook.sh",
    "//home/alice/x",
    "\\/home/alice/hook.sh",
]


class TestUnportableLiteralContract:
    """The lexical home-root contract of ``_extract_unportable_literals`` (#2407)."""

    @pytest.mark.parametrize(("command", "expected"), _FLAGGED)
    def test_flagged(self, command, expected):
        assert _extract_unportable_literals(command, ()) == expected

    @pytest.mark.parametrize(("command", "expected"), _ACCEPTED_FALSE_POSITIVES)
    def test_accepted_false_positives(self, command, expected):
        assert _extract_unportable_literals(command, ()) == expected

    @pytest.mark.parametrize("command", _NOT_FLAGGED)
    def test_not_flagged(self, command):
        assert _extract_unportable_literals(command, ()) == []

    @pytest.mark.parametrize(("command", "home", "expected"), _HOST_HOME_CASES)
    def test_host_homes(self, command, home, expected):
        assert _extract_unportable_literals(command, (home,)) == expected

    @pytest.mark.parametrize("command", _NON_GOALS)
    def test_non_goals_terminate(self, command):
        assert isinstance(_extract_unportable_literals(command, ("/srv/jenkins",)), list)

    def test_pathological_input_scans_linearly(self):
        command = "/home/" * 50_000 + "C:" * 50_000 + "a:/Users/" * 20_000
        started = time.perf_counter()
        _extract_unportable_literals(command, ("/srv/jenkins", r"D:\Profiles\alice"))
        assert time.perf_counter() - started < 5


class TestFindUnportableHookCommands:
    """find_unportable_hook_commands wiring: files, attribution, rows, host homes."""

    def test_one_finding_per_root_per_hook(self, project_root, fake_home):
        _write_canonical(
            project_root,
            {
                "SessionStart": [
                    _rule("", "/home/alice/a.sh && /home/alice/b.sh /Users/bob/c.sh"),
                    _rule("", "uv run mm session start"),
                ]
            },
        )
        findings = find_unportable_hook_commands(project_root, host_homes=())
        assert [(f.rule_index, f.hook_index, f.unportable_literal) for f in findings] == [
            (0, 0, "/home/alice"),
            (0, 0, "/Users/bob"),
        ]
        assert all(
            f.command == "/home/alice/a.sh && /home/alice/b.sh /Users/bob/c.sh" for f in findings
        )

    def test_current_host_home_flagged(self, project_root, fake_home):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit|Write", f"{fake_home}/bin/hook.sh")]},
        )
        findings = find_unportable_hook_commands(project_root, host_homes=(str(fake_home),))
        assert [f.unportable_literal for f in findings] == [str(fake_home)]

    def test_canonical_and_tier_attribution(self, project_root, fake_home):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", "/home/canonical/hook.sh")]},
        )
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"PostToolUse": [_rule("", "/home/user/hook.sh")]},
        )
        _write_settings(
            project_root / ".claude" / "settings.json",
            {"PostToolUse": [_rule("", "/home/shared/hook.sh")]},
        )
        _write_settings(
            project_root / ".claude" / "settings.local.json",
            {"PostToolUse": [_rule("", "/home/local/hook.sh")]},
        )

        findings = find_unportable_hook_commands(project_root, host_homes=())
        by_source = {(f.source, f.tier): f.unportable_literal for f in findings}
        assert by_source == {
            ("canonical", None): "/home/canonical",
            ("tier", "user"): "/home/user",
            ("tier", "project_shared"): "/home/shared",
            ("tier", "project_local"): "/home/local",
        }

    def test_missing_and_malformed_settings_handled_gracefully(self, project_root, fake_home):
        findings = find_unportable_hook_commands(project_root, host_homes=())
        assert findings == []

        can_path = project_root / CANONICAL_SETTINGS_FILE
        can_path.parent.mkdir(parents=True, exist_ok=True)
        can_path.write_text("{broken json", encoding="utf-8")
        findings = find_unportable_hook_commands(project_root, host_homes=())
        assert findings == []

    def test_unscanned_settings_files_reported_by_reason(self, project_root, fake_home):
        # Absent files are not reported: there is nothing to scan.
        assert find_unscanned_settings_files(project_root) == []

        can_path = project_root / CANONICAL_SETTINGS_FILE
        can_path.parent.mkdir(parents=True, exist_ok=True)
        can_path.write_text("{broken json", encoding="utf-8")
        user_path = fake_home / ".claude" / "settings.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("[]", encoding="utf-8")
        (project_root / ".claude" / "settings.json").mkdir()
        local_path = project_root / ".claude" / "settings.local.json"
        local_path.write_bytes(b"\xff\xfe{}")

        rows = [
            (u.source, u.tier, u.path, u.reason)
            for u in find_unscanned_settings_files(project_root)
        ]
        assert rows == [
            ("canonical", None, can_path, "invalid JSON"),
            ("tier", "user", user_path, "not a JSON object"),
            ("tier", "project_shared", project_root / ".claude" / "settings.json", "not a file"),
            ("tier", "project_local", local_path, "unreadable"),
        ]

    def test_unscanned_broken_symlink_is_not_absent(self, project_root, fake_home):
        can_path = project_root / CANONICAL_SETTINGS_FILE
        can_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            can_path.symlink_to(project_root / "nowhere.json")
        except OSError:
            pytest.skip("symlinks unavailable")
        rows = [(u.source, u.reason) for u in find_unscanned_settings_files(project_root)]
        assert rows == [("canonical", "broken symlink")]

    @pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX permission bits")
    def test_unscanned_permission_denied_parent_is_unreadable(self, project_root, fake_home):
        if os.geteuid() == 0:
            pytest.skip("root bypasses directory permissions")
        user_dir = fake_home / ".claude"
        user_dir.mkdir()
        (user_dir / "settings.json").write_text("{}", encoding="utf-8")
        user_dir.chmod(0)
        try:
            rows = [(u.tier, u.reason) for u in find_unscanned_settings_files(project_root)]
            findings = find_unportable_hook_commands(project_root, host_homes=())
        finally:
            user_dir.chmod(0o755)
        assert rows == [("user", "unreadable")]
        assert findings == []

    def test_format_unscanned_settings_warning(self, project_root):
        unscanned = UnscannedSettingsFile(
            source="tier",
            path=project_root / ".claude" / "settings.json\x1b[2J",
            reason="invalid JSON",
            tier="project_shared",
        )
        warn_str = format_unscanned_settings_warning(unscanned)
        assert warn_str.startswith("project_shared tier file (")
        assert "was not checked: invalid JSON." in warn_str
        assert "\x1b[2J" not in warn_str

    def test_format_unportable_command_warning(self, project_root):
        finding = UnportableHookCommand(
            source="canonical",
            path=project_root / CANONICAL_SETTINGS_FILE,
            event="PostToolUse",
            rule_index=0,
            hook_index=0,
            command="/home/alice/hook.sh --flag",
            unportable_literal="/home/alice/hook.sh",
            tier=None,
        )
        warn_str = format_unportable_command_warning(finding)
        assert "canonical settings" in warn_str
        assert "PostToolUse" in warn_str
        assert "rule #0 hook #0" in warn_str
        assert "'/home/alice/hook.sh'" in warn_str
        assert "contains non-portable absolute home path" in warn_str
        assert "rewrite with $HOME, ~, or a repo-relative path" in warn_str

    def test_format_unportable_command_warning_scrubs_control_chars(self, project_root):
        finding = UnportableHookCommand(
            source="canonical",
            path=project_root / CANONICAL_SETTINGS_FILE,
            event="PostToolUse\x1b[2J",
            rule_index=0,
            hook_index=0,
            command="/home/alice/hook.sh # \x1b[2J\x1b[Hforged",
            unportable_literal="/home/alice/hook.sh\x1b[K",
            tier=None,
        )
        warn_str = format_unportable_command_warning(finding)
        assert "\x1b[2J" not in warn_str
        assert "\\x1b[2J" in warn_str

    def test_format_unportable_command_warning_redacts_secret_assignment(self, project_root):
        finding = UnportableHookCommand(
            source="canonical",
            path=project_root / CANONICAL_SETTINGS_FILE,
            event="PostToolUse",
            rule_index=0,
            hook_index=0,
            command="API_KEY=/home/alice/private-credential ./hook.sh",
            unportable_literal="/home/alice/private-credential",
            tier=None,
        )
        warn_str = format_unportable_command_warning(finding)
        assert "/home/alice/private-credential" not in warn_str
        assert "<redacted: secret-shape>" in warn_str

    def test_redact_unportable_command_fields(self):
        cmd, lit = redact_unportable_command_fields(
            "API_KEY=/home/alice/private-credential ./hook.sh",
            "/home/alice/private-credential",
        )
        assert cmd == "<redacted: secret-shape>"
        assert lit == "<redacted: secret-shape>"

        cmd2, lit2 = redact_unportable_command_fields(
            "FOO=/home/alice/safe.sh ./hook.sh",
            "/home/alice/safe.sh",
        )
        assert cmd2 == "FOO=/home/alice/safe.sh ./hook.sh"
        assert lit2 == "/home/alice/safe.sh"

        cmd3, lit3 = redact_unportable_command_fields(
            "<redacted: secret-shape>",
            "/home/alice/private-credential",
        )
        assert cmd3 == "<redacted: secret-shape>"
        assert lit3 == "<redacted: secret-shape>"


# ── CLI doctor subcommand ──────────────────────────────────────────


class TestSettingsDoctorCli:
    """``mm context settings-doctor`` exit codes + JSON schema."""

    def test_clean_exit_zero(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        # Active scope = user; the user-tier match is the active scope,
        # not a duplicate → clean.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert "No memtomem-managed hooks duplicated" in result.output

    def test_duplicates_exit_one(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 1, result.output
        assert "user" in result.output
        assert "settings-migrate" in result.output

    def test_scope_flag_overrides_config(self, project_root, fake_home, monkeypatch):
        """Per-invocation ``--scope=`` wins over the user config field."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        # Config says active=user, but flag says active=project_local
        # so the user-tier match becomes a duplicate.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--scope=project_local"])
        assert result.exit_code == 1, result.output

    def test_malformed_human_output_names_location_and_fix(
        self, project_root, fake_home, monkeypatch
    ):
        """Non-JSON path: the malformed section is rendered, not only counted."""
        _write_canonical(
            project_root,
            {"SessionStart": [_rule(["Bash"], "mm index")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 1, result.output
        assert "1 hook rule(s) with malformed matchers" in result.output
        assert str(project_root / CANONICAL_SETTINGS_FILE) in result.output
        assert "[SessionStart rule #0] non-string matcher (list)" in result.output
        assert "Omit it for match-all" in result.output
        # The clean line must not also print when only the malformed axis fired.
        assert "No memtomem-managed hooks duplicated" not in result.output

    def test_json_clean_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == {
            "status": "clean",
            "active_scope": "user",
            "duplicates": [],
            "malformed_matchers": [],
            "unportable_commands": [],
            "unscanned_settings": [],
        }

    def test_json_duplicates_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "duplicates"
        assert payload["active_scope"] == "project_local"
        assert len(payload["duplicates"]) == 1
        dup = payload["duplicates"][0]
        assert dup["tier"] == "user"
        # ``str.endswith("/.claude/settings.json")`` would break on Windows
        # (backslash separator). Compare via ``Path.parts`` for
        # cross-platform stability per ``feedback_path_comparison_relative_to``.
        dup_path = Path(dup["path"])
        assert dup_path.name == "settings.json"
        assert dup_path.parent.name == ".claude"
        assert len(dup["entries"]) == 1
        entry = dup["entries"][0]
        assert entry == {
            "event": "PostToolUse",
            "matcher": "Edit|Write",
            "command_preview": "mm session start",
        }

    def test_json_malformed_only_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(
            project_root,
            {"SessionStart": [_rule(["Bash"], "mm index")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "malformed"
        assert payload["duplicates"] == []
        assert payload["unportable_commands"] == []
        assert payload["malformed_matchers"] == [
            {
                "source": "canonical",
                "tier": None,
                "path": str(project_root / CANONICAL_SETTINGS_FILE),
                "event": "SessionStart",
                "rule_index": 0,
                "matcher_type": "list",
            }
        ]

    def test_duplicates_keep_status_precedence_over_malformed(
        self, project_root, fake_home, monkeypatch
    ):
        _write_canonical(
            project_root,
            {
                "PostToolUse": [
                    _rule("Edit|Write", "mm session start"),
                    _rule(None, "broken"),
                ]
            },
        )
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "duplicates"
        assert len(payload["duplicates"]) == 1
        assert len(payload["malformed_matchers"]) == 1

    def test_settings_doctor_unportable_exits_zero(self, project_root, fake_home, monkeypatch):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", "/Users/alice/hook.sh")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert "Found 1 hook command(s) with unportable home paths" in result.output
        assert "/Users/alice/hook.sh" in result.output
        assert "No memtomem-managed hooks duplicated" not in result.output

    def test_settings_doctor_unportable_with_duplicates_exits_one(
        self, project_root, fake_home, monkeypatch
    ):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("Edit|Write", "/Users/alice/hook.sh")]},
        )
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"PostToolUse": [_rule("Edit|Write", "/Users/alice/hook.sh")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 1, result.output
        assert "Found memtomem-managed hooks in 1 other tier" in result.output
        assert "Found 2 hook command(s) with unportable home paths" in result.output

    def test_settings_doctor_single_hook_multiple_literals_counts_one_command(
        self, project_root, fake_home, monkeypatch
    ):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", "cp /Users/alice/a.sh /Users/bob/b.sh")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert "Found 1 hook command(s) with unportable home paths" in result.output
        assert "/Users/alice/a.sh" in result.output
        assert "/Users/bob/b.sh" in result.output

    def test_settings_doctor_json_unportable_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", "/Users/alice/hook.sh")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "clean"
        assert len(payload["unportable_commands"]) == 1
        item = payload["unportable_commands"][0]
        assert item["source"] == "canonical"
        assert item["tier"] is None
        assert item["event"] == "PostToolUse"
        assert item["rule_index"] == 0
        assert item["hook_index"] == 0
        assert item["command"] == "/Users/alice/hook.sh"
        assert item["unportable_literal"] == "/Users/alice"

    def test_settings_doctor_unportable_secret_redacted(self, project_root, fake_home, monkeypatch):
        secret = "AKIA1234567890ABCDEF"
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", f"/home/alice/hook.sh --token={secret}")]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        # Human-readable output redacts secret-bearing command and derived literal
        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert secret not in result.output
        assert "<redacted: secret-shape>" in result.output

        # JSON output retains raw values for programmatic use
        json_result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert json_result.exit_code == 0, json_result.output
        assert secret in json_result.output
        assert "/home/alice/hook.sh" in json_result.output

    def test_settings_doctor_unportable_secret_assignment_redacted(
        self, project_root, fake_home, monkeypatch
    ):
        secret_cmd = "API_KEY=/home/alice/private-credential ./hook.sh"
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", secret_cmd)]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        # Human-readable output redacts both secret command and the derived literal
        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert "/home/alice/private-credential" not in result.output
        assert "<redacted: secret-shape>" in result.output

        # JSON output retains raw values for programmatic use
        json_result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert json_result.exit_code == 0, json_result.output
        assert "/home/alice/private-credential" in json_result.output

    def test_settings_doctor_unscanned_file_is_incomplete_not_clean(
        self, project_root, fake_home, monkeypatch
    ):
        _write_canonical(project_root, _bundled_hook())
        user_path = fake_home / ".claude" / "settings.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("{broken json", encoding="utf-8")
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert "Could not check 1 settings file(s)" in result.output
        assert "invalid JSON" in result.output
        assert "No memtomem-managed hooks duplicated" not in result.output

        json_result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert json_result.exit_code == 0, json_result.output
        payload = json.loads(json_result.stdout)
        assert payload["status"] == "incomplete"
        assert payload["unscanned_settings"] == [
            {"source": "tier", "tier": "user", "path": str(user_path), "reason": "invalid JSON"}
        ]

    def test_settings_doctor_invalid_utf8_tier_reported_not_raised(
        self, project_root, fake_home, monkeypatch
    ):
        """An undecodable tier must reach the unscanned report, not abort the run.

        The duplicate and matcher checks read the same file first, so a decode
        error there would end the command before anything is printed.
        """
        _write_canonical(project_root, _bundled_hook())
        user_path = fake_home / ".claude" / "settings.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_bytes(b"\xff\xfe{}")
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "incomplete"
        assert payload["unscanned_settings"] == [
            {"source": "tier", "tier": "user", "path": str(user_path), "reason": "unreadable"}
        ]

    def test_settings_doctor_unportable_control_characters_scrubbed(
        self, project_root, fake_home, monkeypatch
    ):
        raw_cmd = "/home/alice/hook.sh # \x1b[2J\x1b[Hforged output"
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", raw_cmd)]},
        )
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        # Human-readable output scrubs control characters
        result = CliRunner().invoke(settings_doctor_cmd, [], color=True)
        assert result.exit_code == 0, result.output
        assert "\x1b[2J\x1b[H" not in result.output
        assert "\\x1b[2J\\x1b[H" in result.output

        # JSON output preserves raw characters byte-for-byte
        json_result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert json_result.exit_code == 0, json_result.output
        payload = json.loads(json_result.output)
        assert payload["unportable_commands"][0]["command"] == raw_cmd


# ── CLI sync warning ───────────────────────────────────────────────


class TestSyncWarning:
    """``mm context sync --include=settings`` emits the duplicate-tier
    warning before write and does NOT block the sync (ADR-0010 §4
    "informational, non-blocking")."""

    def test_sync_emits_warning_for_other_tier(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        # Pre-populate user tier with the duplicate.
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import sync_cmd

        # Active scope = project_local via flag; user-tier becomes a
        # duplicate.
        result = CliRunner().invoke(
            sync_cmd,
            ["--include=settings", "--scope=project_local", "--yes"],
        )
        assert result.exit_code == 0, result.output
        # Sync still runs (file was written).
        assert (project_root / ".claude" / "settings.local.json").is_file()
        # Warning appears in the (mixed) output.
        assert "memtomem-managed hook" in result.output
        assert "user" in result.output

    def test_diff_emits_warning_for_other_tier(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        # Bare diff command runs through ``_print_settings_diff`` which
        # also wires the warning. Default scope = user, so user-tier
        # entry is NOT a duplicate; flip the env to make project_local
        # active so user-tier becomes a duplicate for this invocation.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import diff_cmd

        result = CliRunner().invoke(diff_cmd, ["--include=settings"])
        assert result.exit_code == 0, result.output
        assert "memtomem-managed hook" in result.output

    def test_sync_emits_warning_for_malformed_matcher(self, project_root, fake_home, monkeypatch):
        """The malformed axis rides the same non-blocking sync surface (#1987).

        ``_iter_signatures`` skips non-string matchers, so a corrupted rule is
        invisible to duplicate detection; without this leg the sync workflow
        would say nothing about a rule Claude Code silently ignores.
        """
        _write_canonical(project_root, _bundled_hook())
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"SessionStart": [_rule(["Bash"], "broken")]},
        )
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import sync_cmd

        result = CliRunner().invoke(
            sync_cmd,
            ["--include=settings", "--scope=project_local", "--yes"],
        )
        assert result.exit_code == 0, result.output
        # Non-blocking: the sync still wrote the active tier.
        assert (project_root / ".claude" / "settings.local.json").is_file()
        assert "non-string matcher (list)" in result.output
        assert "user tier" in result.output
        # Conditional wording: copy/migrate only inspect the files they touch,
        # so a malformed rule elsewhere does not block every such operation.
        assert "may cause a related" in result.output
        assert "settings-migrate` to refuse to run" in result.output

    def test_sync_emits_warning_for_unportable_command(self, project_root, fake_home, monkeypatch):
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", "/Users/alice/hook.sh")]},
        )
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import sync_cmd

        result = CliRunner().invoke(
            sync_cmd,
            ["--include=settings", "--scope=user", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "contains non-portable absolute home path" in result.output
        assert "/Users/alice/hook.sh" in result.output

    def test_sync_emits_warning_for_unscanned_settings_file(
        self, project_root, fake_home, monkeypatch
    ):
        _write_canonical(project_root, _bundled_hook())
        user_path = fake_home / ".claude" / "settings.json"
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("{broken json", encoding="utf-8")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import sync_cmd

        result = CliRunner().invoke(
            sync_cmd,
            ["--include=settings", "--scope=project_local", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "user tier file (" in result.output
        assert "was not checked: invalid JSON." in result.output

    def test_sync_emits_warning_for_unportable_command_secret_redacted(
        self, project_root, fake_home, monkeypatch
    ):
        secret = "AKIA1234567890ABCDEF"
        _write_canonical(
            project_root,
            {"PostToolUse": [_rule("", f"/Users/alice/hook.sh --token={secret}")]},
        )
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import sync_cmd

        result = CliRunner().invoke(
            sync_cmd,
            ["--include=settings", "--scope=user", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "contains non-portable absolute home path" in result.output
        assert secret not in result.output
        assert "<redacted: secret-shape>" in result.output


# ── Web route response ─────────────────────────────────────────────


class TestWebDuplicateTierWarnings:
    """``GET /api/settings-sync`` and ``POST /api/settings-sync`` both
    expose ``duplicate_tier_warnings`` (ADR-0010 §4 data surface). The
    consuming frontend banner ships alongside (#1247 id 32,
    ``settings-hooks-watchdog.js`` + vitest pins); these tests pin the
    data layer."""

    @pytest.fixture
    def app(self, project_root, fake_home, monkeypatch):
        from memtomem.config import Mem2MemConfig

        # Web settings routes are request-scoped. Keep the env override in
        # place as a regression guard: the route must follow
        # ?target_scope=..., not config.hooks.target_scope.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        application = create_app(lifespan=None, mode="dev")
        application.state.project_root = project_root
        application.state.storage = AsyncMock()
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

    async def test_get_includes_empty_warnings_when_clean(self, client, project_root):
        _write_canonical(project_root, _bundled_hook())
        # No other tiers populated → empty list.
        response = await client.get("/api/settings-sync?target_scope=project_local")
        assert response.status_code == 200
        data = response.json()
        assert data["duplicate_tier_warnings"] == []

    async def test_get_includes_warnings_when_duplicates(self, client, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        response = await client.get("/api/settings-sync?target_scope=project_local")
        assert response.status_code == 200
        data = response.json()
        warnings = data["duplicate_tier_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["tier"] == "user"
        assert warnings[0]["entries"][0]["event"] == "PostToolUse"

    async def test_post_includes_warnings(self, client, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        response = await client.post(
            "/api/settings-sync?target_scope=project_local",
            json={"allow_host_writes": False},
        )
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert "duplicate_tier_warnings" in data
        assert len(data["duplicate_tier_warnings"]) == 1
