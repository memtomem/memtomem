"""Tests for multi-runtime hook fan-out — Codex + Gemini settings generators.

Companion to ``test_context_settings.py`` (Claude). Pins the ADR-0010
multi-runtime extension: canonical ``.memtomem/settings.json`` (Claude-shaped
hooks record) fans out to Codex ``.codex/hooks.json`` (near-identity) and
Gemini ``.gemini/settings.json`` (event + tool-name remap, drop-with-warning
for anything that can't convert faithfully).

Mappings were verified against official docs (learn.chatgpt.com/docs/hooks,
gemini-cli docs/hooks/writing-hooks.md).
"""

from __future__ import annotations

import json
import tomllib

import pytest

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    CodexSettingsGenerator,
    GeminiSettingsGenerator,
    diff_settings,
    generate_all_settings,
    host_write_targets,
)

from .helpers import set_home


def _rule(matcher: str = "", command: str = "echo ok", timeout: int = 5000) -> dict:
    """A single hook rule in canonical (Claude) record format."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    }


def _canonical(project_root, hooks: dict) -> None:
    """Write ``.memtomem/settings.json`` with the given ``hooks`` record."""
    path = project_root / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def all_home(tmp_path, monkeypatch):
    """Redirect HOME and create ``~/.claude``, ``~/.codex``, ``~/.gemini`` so
    all three runtimes register as installed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    for marker in (".claude", ".codex", ".gemini"):
        (fake_home / marker).mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


# ── Codex (near-identity) ───────────────────────────────────────────


class TestCodexGenerator:
    def test_target_file_scopes(self, tmp_path, all_home):
        gen = CodexSettingsGenerator()
        assert gen.target_file(tmp_path, "user") == all_home / ".codex" / "hooks.json"
        assert gen.target_file(tmp_path, "project_shared") == tmp_path / ".codex" / "hooks.json"
        # Codex has no project_local hooks target.
        assert gen.target_file(tmp_path, "project_local") is None

    def test_near_identity_fanout(self, tmp_path, all_home):
        """Supported events + Bash/Edit/Write matchers pass through verbatim."""
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "echo hi")]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["codex_settings"].status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        rule = written["hooks"]["PreToolUse"][0]
        assert rule["matcher"] == "Bash"  # NOT remapped — Codex accepts it natively
        assert rule["hooks"][0]["command"] == "echo hi"

    def test_unsupported_events_dropped_with_warning(self, tmp_path, all_home):
        """The one event Codex lacks (Notification) is dropped + warned."""
        _canonical(
            tmp_path,
            {
                "PreToolUse": [_rule("Bash", "ok")],
                "Notification": [_rule("", "n")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        assert "PreToolUse" in written["hooks"]
        assert "Notification" not in written["hooks"]
        assert any("Notification" in w for w in r.warnings)

    def test_session_end_reaches_codex(self, tmp_path, all_home):
        """Codex documents SessionEnd, so it must fan out rather than drop.

        Regression pin for #1976: ``SessionEnd`` was grouped with
        ``Notification`` as unsupported, so every canonical SessionEnd hook
        was silently withheld from Codex while the warning blamed Codex for
        not understanding an event it documents.
        """
        _canonical(
            tmp_path,
            {
                # Timeout inside Codex's 3s SessionEnd cap, so this case pins
                # the fan-out alone; the clamp has its own test below.
                "SessionEnd": [_rule("", "bye", timeout=2)],
                "Notification": [_rule("", "n")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        handler = written["hooks"]["SessionEnd"][0]["hooks"][0]
        assert handler["command"] == "bye"
        assert handler["timeout"] == 2  # untouched — already within the cap
        # The sibling event must keep being dropped — this pin is about
        # SessionEnd alone, not about loosening the filter.
        assert "Notification" not in written["hooks"]
        # Match the drop message's subject, not a bare substring: the
        # Notification warning enumerates every supported event, so it now
        # legitimately contains the word "SessionEnd".
        assert not any(w.startswith("Hook event 'SessionEnd'") for w in r.warnings), r.warnings

    def test_session_end_timeout_clamped_to_codex_cap(self, tmp_path, all_home):
        """Codex caps SessionEnd at 3s; other runtimes keep the canonical value.

        Claude lets a per-hook ``timeout`` raise its SessionEnd budget to 60s,
        so a canonical 30s hook is legal there and out of contract on Codex.
        Clamp rather than drop — a shorter hook still runs.
        """
        _canonical(tmp_path, {"SessionEnd": [_rule("", "bye", timeout=30)]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        handler = written["hooks"]["SessionEnd"][0]["hooks"][0]
        assert handler["timeout"] == 3
        assert handler["command"] == "bye"  # the hook itself survives
        assert any("clamped" in w for w in r.warnings), r.warnings

        # The canonical record and the Claude fan-out keep 30 — the clamp is
        # a Codex-local translation, not a rewrite of the user's intent.
        claude = json.loads((all_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert claude["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"] == 30

    @pytest.mark.parametrize(
        "matcher",
        ["", "other", "^other$", ".*", "*", "other|clear"],
        ids=["empty", "literal", "anchored", "wildcard-regex", "star", "alternation"],
    )
    def test_session_end_regex_matchers_survive_for_codex(self, matcher, tmp_path, all_home):
        """Codex's matcher is a regex string, so regexes must pass through.

        An allow-list of literals would drop every one of these even though
        each fires on Codex — dropping a working hook under a "could never
        fire" warning is worse than passing it through.
        """
        _canonical(tmp_path, {"SessionEnd": [_rule(matcher, "kept", timeout=2)]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        commands = [h["command"] for rule in written["hooks"]["SessionEnd"] for h in rule["hooks"]]
        assert commands == ["kept"], written
        assert not any("could never fire" in w for w in r.warnings), r.warnings

    @pytest.mark.parametrize(
        "matcher",
        ["clear", "resume", "logout", "prompt_input_exit", "bypass_permissions_disabled"],
    )
    def test_session_end_claude_only_reason_dropped_for_codex(self, matcher, tmp_path, all_home):
        """The five Claude-only `reason` literals cannot match Codex's `other`.

        Writing such a rule out would look like a registration while never
        firing; an explicit warning beats a silent no-op.
        """
        _canonical(
            tmp_path,
            {
                "SessionEnd": [
                    _rule(matcher, "dead", timeout=2),
                    _rule("", "always", timeout=2),
                ]
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        commands = [h["command"] for rule in written["hooks"]["SessionEnd"] for h in rule["hooks"]]
        assert commands == ["always"]
        assert any("could never fire" in w for w in r.warnings), r.warnings

        # Claude keeps the reason-filtered rule — it is valid there.
        claude = json.loads((all_home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert matcher in [rule["matcher"] for rule in claude["hooks"]["SessionEnd"]]

    @pytest.mark.parametrize(
        "matcher", [None, 7, ["other"], {"reason": "other"}], ids=["null", "int", "list", "dict"]
    )
    @pytest.mark.parametrize("preexisting", [False, True], ids=["fresh", "merge"])
    def test_session_end_non_string_matcher_dropped(self, matcher, preexisting):
        """A non-string matcher is malformed for Codex's regex-string field.

        Passing it through wrote it verbatim on a fresh target, and with the
        event already present it reached ``_merge_hooks_record``'s
        matcher-keyed dict and raised ``TypeError: unhashable type``.

        Scoped to the Codex generator on purpose: routing a non-string
        matcher through ``generate_all_settings`` crashes earlier, inside
        ``_ensure_gemini_handler_names``' ``re.sub``, for *any* event. That is
        a separate pre-existing defect in the Gemini path, not this one.
        """
        rule = _rule("", "dropped", timeout=2)
        rule["matcher"] = matcher
        existing = (
            {"hooks": {"SessionEnd": [_rule("", "user-rule", timeout=1)]}} if preexisting else None
        )

        merged, warnings = CodexSettingsGenerator().merge(
            existing, {"hooks": {"SessionEnd": [rule]}}
        )

        commands = [
            h["command"]
            for rule in merged.get("hooks", {}).get("SessionEnd", [])
            for h in rule["hooks"]
        ]
        assert "dropped" not in commands, merged
        assert any("non-string matcher" in w for w in warnings), warnings
        if preexisting:
            assert "user-rule" in commands  # the user's own rule is untouched

    def test_session_end_absent_matcher_is_match_all(self, tmp_path, all_home):
        """Omitting `matcher` is documented as match-all — never malformed."""
        rule = _rule("", "always", timeout=2)
        del rule["matcher"]
        _canonical(tmp_path, {"SessionEnd": [rule]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        commands = [h["command"] for rule in written["hooks"]["SessionEnd"] for h in rule["hooks"]]
        assert commands == ["always"]
        assert not any("matcher" in w for w in r.warnings), r.warnings

    def test_session_end_event_omitted_when_every_rule_is_dropped(self, tmp_path, all_home):
        """An event left with no usable rule must not be written as empty."""
        _canonical(tmp_path, {"SessionEnd": [_rule("clear", "on-clear", timeout=2)]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["codex_settings"].status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        # A contribution with nothing left writes ``{}`` — no empty ``hooks``
        # key — which is the same shape a Notification-only record produces.
        assert "SessionEnd" not in written.get("hooks", {}), written

    def test_additive_merge_preserves_user_codex_rules(self, tmp_path, all_home):
        target = all_home / ".codex" / "hooks.json"
        user_rule = _rule("apply_patch", "user codex")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule]}}) + "\n", encoding="utf-8"
        )
        _canonical(tmp_path, {"PostToolUse": [_rule("Bash", "mm")]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["codex_settings"].status == "ok"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert user_rule in written["hooks"]["PostToolUse"]
        assert any(rr["matcher"] == "Bash" for rr in written["hooks"]["PostToolUse"])


# ── Gemini (event + tool-name remap) ─────────────────────────────────


class TestGeminiGenerator:
    def test_target_file_scopes(self, tmp_path, all_home):
        gen = GeminiSettingsGenerator()
        assert gen.target_file(tmp_path, "user") == all_home / ".gemini" / "settings.json"
        assert gen.target_file(tmp_path, "project_shared") == tmp_path / ".gemini" / "settings.json"
        assert gen.target_file(tmp_path, "project_local") is None

    def test_event_and_tool_name_mapping(self, tmp_path, all_home):
        _canonical(
            tmp_path,
            {
                "PreToolUse": [_rule("Bash", "b"), _rule("Edit|Write", "w")],
                "PostToolUse": [_rule("Read", "r")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        # Event names remapped.
        assert "BeforeTool" in hooks and "AfterTool" in hooks
        assert "PreToolUse" not in hooks and "PostToolUse" not in hooks
        # Tool-name matchers remapped: Bash→run_shell_command; Edit|Write→
        # replace|write_file (Edit = in-place → replace, Write = create → write_file).
        before_matchers = {rr["matcher"] for rr in hooks["BeforeTool"]}
        assert before_matchers == {"run_shell_command", "replace|write_file"}
        assert hooks["AfterTool"][0]["matcher"] == "read_file"
        # Handler name synthesized (Gemini handlers carry a name).
        assert hooks["BeforeTool"][0]["hooks"][0].get("name")

    def test_empty_matcher_maps_to_star(self, tmp_path, all_home):
        _canonical(tmp_path, {"PreToolUse": [_rule("", "all-tools")]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        assert hooks["BeforeTool"][0]["matcher"] == "*"

    def test_lifecycle_events_best_effort_mapped(self, tmp_path, all_home):
        """UserPromptSubmit→BeforeAgent and Stop→AfterAgent are best-effort
        lifecycle mappings (approximate timing) — they must be emitted, not
        dropped, so memtomem's context-injection / session-close hook paths
        still fire on Gemini."""
        _canonical(
            tmp_path,
            {
                "UserPromptSubmit": [_rule("", "inject")],
                "Stop": [_rule("", "close")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        assert "BeforeAgent" in hooks and "AfterAgent" in hooks
        assert "UserPromptSubmit" not in hooks and "Stop" not in hooks

    def test_unmapped_event_dropped_with_warning(self, tmp_path, all_home):
        # SubagentStop has no Gemini equivalent (UserPromptSubmit/Stop are now
        # best-effort-mapped to BeforeAgent/AfterAgent).
        _canonical(tmp_path, {"SubagentStop": [_rule("", "x")]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["gemini_settings"]
        assert r.status == "ok"
        assert any("SubagentStop" in w for w in r.warnings)
        written = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert written.get("hooks", {}) == {}

    def test_unmapped_matcher_token_dropped_with_warning(self, tmp_path, all_home):
        # WebFetch is a Claude tool with no Gemini equivalent — the rule would
        # never fire, so it is dropped (not silently emitted with a dead matcher).
        _canonical(tmp_path, {"PreToolUse": [_rule("WebFetch", "x")]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["gemini_settings"]
        assert any("WebFetch" in w for w in r.warnings)
        written = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert written.get("hooks", {}) == {}

    def test_preserves_other_settings_keys(self, tmp_path, all_home):
        target = all_home / ".gemini" / "settings.json"
        target.write_text(
            json.dumps({"theme": "dark", "mcpServers": {"x": {"command": "y"}}}) + "\n",
            encoding="utf-8",
        )
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b")]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["theme"] == "dark"
        assert written["mcpServers"] == {"x": {"command": "y"}}
        assert "BeforeTool" in written["hooks"]


# ── None fan-out (project_local) skip semantics ──────────────────────


class TestProjectLocalNoneSkip:
    def test_generate_skips_codex_gemini_at_project_local(self, tmp_path, all_home):
        (tmp_path / ".claude").mkdir()
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b")]})

        results = generate_all_settings(tmp_path, scope="project_local", allow_host_writes=False)
        # Claude has a project_local target (.claude/settings.local.json).
        assert results["claude_settings"].status == "ok"
        assert (tmp_path / ".claude" / "settings.local.json").is_file()
        # Codex/Gemini have no project_local target → skipped, dirs not created.
        assert results["codex_settings"].status == "skipped"
        assert "no fan-out target" in results["codex_settings"].reason
        assert results["gemini_settings"].status == "skipped"
        assert not (tmp_path / ".codex").exists()
        assert not (tmp_path / ".gemini").exists()

    def test_diff_skips_codex_gemini_at_project_local(self, tmp_path, all_home):
        (tmp_path / ".claude").mkdir()
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b")]})

        results = diff_settings(tmp_path, scope="project_local")
        assert results["codex_settings"].status == "skipped"
        assert results["gemini_settings"].status == "skipped"


# ── Ownership markers + re-sync (ADR-0019 / issue #1110) ─────────────


class TestCodexOwnership:
    def test_fresh_sync_stamps_status_message_marker(self, tmp_path, all_home):
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "mm index")]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        handler = written["hooks"]["PreToolUse"][0]["hooks"][0]
        assert handler["statusMessage"] == "memtomem · PreToolUse"

    def test_resync_updates_own_marked_codex_rule(self, tmp_path, all_home):
        target = all_home / ".codex" / "hooks.json"
        old = {
            "matcher": "Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": "mm index --v1",
                    "timeout": 5000,
                    "statusMessage": "memtomem · PreToolUse",
                }
            ],
        }
        target.write_text(json.dumps({"hooks": {"PreToolUse": [old]}}) + "\n", encoding="utf-8")
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "mm index --v2")]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["codex_settings"].warnings == []

        written = json.loads(target.read_text(encoding="utf-8"))
        rule = written["hooks"]["PreToolUse"][0]
        assert rule["hooks"][0]["command"] == "mm index --v2"  # updated
        assert rule["hooks"][0]["statusMessage"] == "memtomem · PreToolUse"


class TestGeminiOwnership:
    def test_canonical_name_is_overridden_with_memtomem_prefix(self, tmp_path, all_home):
        """A canonical handler ``name`` is overridden so memtomem owns the Gemini name."""
        rule = {
            "matcher": "Bash",
            "hooks": [{"type": "command", "command": "mm index", "name": "user-chosen-name"}],
        }
        _canonical(tmp_path, {"PreToolUse": [rule]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        name = hooks["BeforeTool"][0]["hooks"][0]["name"]
        assert name.startswith("memtomem-")
        assert name != "user-chosen-name"

    def test_no_status_message_leaks_into_gemini(self, tmp_path, all_home):
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "mm index")]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        for handler in hooks["BeforeTool"][0]["hooks"]:
            assert "statusMessage" not in handler  # marker is Claude/Codex-only

    def test_resync_updates_own_marked_gemini_rule(self, tmp_path, all_home):
        target = all_home / ".gemini" / "settings.json"
        # An old memtomem-emitted Gemini rule (remapped event/matcher, memtomem- name).
        old = {
            "matcher": "run_shell_command",
            "hooks": [
                {
                    "type": "command",
                    "command": "mm index --v1",
                    "timeout": 5000000,
                    "name": "memtomem-BeforeTool-run_shell_command-deadbeef",
                }
            ],
        }
        target.write_text(json.dumps({"hooks": {"BeforeTool": [old]}}) + "\n", encoding="utf-8")
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "mm index --v2")]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].warnings == []

        hooks = json.loads(target.read_text(encoding="utf-8"))["hooks"]
        rules = hooks["BeforeTool"]
        assert len(rules) == 1  # replaced in place, not appended alongside
        assert rules[0]["hooks"][0]["command"] == "mm index --v2"


# ── host-write gate across runtimes ──────────────────────────────────


class TestHostWriteMultiRuntime:
    def _setup(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        canonical = project / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True)
        canonical.write_text(
            json.dumps({"hooks": {"PreToolUse": [_rule("Bash", "b")]}}) + "\n", encoding="utf-8"
        )
        return project

    def test_all_three_markers_yields_three_host_paths(self, tmp_path, all_home):
        project = self._setup(tmp_path)
        pending = host_write_targets(project, scope="user")
        assert set(pending) == {
            all_home / ".claude" / "settings.json",
            all_home / ".codex" / "hooks.json",
            all_home / ".gemini" / "settings.json",
        }

    def test_only_present_markers_are_listed(self, tmp_path, monkeypatch):
        """``is_available`` skips runtimes with no home/project marker, so the
        host-write list reflects only installed runtimes."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".codex").mkdir()  # only Codex installed
        set_home(monkeypatch, fake_home)

        project = self._setup(tmp_path)
        pending = host_write_targets(project, scope="user")
        assert pending == [fake_home / ".codex" / "hooks.json"]


class TestIsAvailable:
    def test_codex_available_via_project_marker(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        set_home(monkeypatch, fake_home)
        (tmp_path / ".codex").mkdir()
        assert CodexSettingsGenerator().is_available(tmp_path) is True
        assert GeminiSettingsGenerator().is_available(tmp_path) is False

    def test_neither_marker_unavailable(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        set_home(monkeypatch, fake_home)
        assert CodexSettingsGenerator().is_available(tmp_path) is False
        assert GeminiSettingsGenerator().is_available(tmp_path) is False


# ── End-to-end through the CLI ───────────────────────────────────────


class TestCliMultiRuntimeSync:
    """``mm context sync --include=settings`` fans out to every installed
    runtime (end-to-end through the CLI wrapper, not just the engine)."""

    def test_sync_fans_out_to_three_runtimes(self, tmp_path, all_home, monkeypatch):
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        # Project is a sibling of the fake HOME so user-tier targets resolve
        # *outside* the project root (host writes) — ``--yes`` bypasses the
        # confirmation that would otherwise block the three home writes.
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        _canonical(project, {"PreToolUse": [_rule("Bash", "echo e2e")]})
        monkeypatch.chdir(project)

        result = CliRunner().invoke(context, ["sync", "--include=settings", "--yes"])
        assert result.exit_code == 0, result.output

        assert (all_home / ".claude" / "settings.json").is_file()
        assert (all_home / ".codex" / "hooks.json").is_file()
        assert (all_home / ".gemini" / "settings.json").is_file()
        # Gemini event remap (PreToolUse → BeforeTool) landed end-to-end.
        gemini = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert "BeforeTool" in gemini["hooks"]
        assert gemini["hooks"]["BeforeTool"][0]["matcher"] == "run_shell_command"


# ── _map_gemini_matcher edge cases (Codex review Major 2) ────────────


class TestGeminiMatcherEdgeCases:
    """A whitespace-only or separator-only matcher must map to ``"*"`` (all
    tools), not an empty string — an empty Gemini matcher is invalid."""

    def test_empty_blank_and_separator_only_map_to_star(self):
        from memtomem.context.settings import _map_gemini_matcher

        assert _map_gemini_matcher("") == ("*", [])
        assert _map_gemini_matcher("   ") == ("*", [])
        assert _map_gemini_matcher("|") == ("*", [])
        assert _map_gemini_matcher(" | ") == ("*", [])

    def test_real_tokens_still_map_and_dedupe(self):
        from memtomem.context.settings import _map_gemini_matcher

        assert _map_gemini_matcher("Bash") == ("run_shell_command", [])
        # Edit and Write map to DIFFERENT Gemini tools (replace vs write_file).
        assert _map_gemini_matcher("Edit|Write") == ("replace|write_file", [])
        # Edit and MultiEdit both → replace → deduped to a single token.
        assert _map_gemini_matcher("Edit|MultiEdit") == ("replace", [])

    def test_unmapped_token_returns_none(self):
        from memtomem.context.settings import _map_gemini_matcher

        mapped, unmapped = _map_gemini_matcher("WebFetch")
        assert mapped is None
        assert unmapped == ["WebFetch"]


# ── Gemini handler normalization (timeout units + name collisions) ───


class TestGeminiHandlerNormalization:
    """Gemini handler config needs two conversions the Claude/Codex paths
    don't: seconds→ms timeout rescale, and collision-proof synthesized names
    (distinct Claude matchers can collapse to one Gemini tool)."""

    def test_timeout_seconds_rescaled_to_milliseconds(self, tmp_path, all_home):
        """Claude/Codex timeouts are seconds; Gemini's are ms. A canonical
        ``timeout: 30`` (30s) must land as ``30000`` on Gemini — otherwise it
        would be read as 30ms and kill the hook before it runs."""
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b", timeout=30)]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        assert hooks["BeforeTool"][0]["hooks"][0]["timeout"] == 30000

    def test_codex_timeout_left_in_seconds(self, tmp_path, all_home):
        """Codex shares Claude's seconds unit — its timeout must NOT be rescaled."""
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b", timeout=30)]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        hooks = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        assert hooks["PreToolUse"][0]["hooks"][0]["timeout"] == 30

    def test_collapsed_matchers_get_distinct_handler_names(self, tmp_path, all_home):
        """``Edit`` and ``MultiEdit`` both map to Gemini's ``replace`` tool. Two
        handlers with DIFFERENT commands must NOT share one synthesized name —
        Gemini's ``/hooks disable <name>`` would otherwise be ambiguous."""
        _canonical(
            tmp_path,
            {
                "PreToolUse": [
                    _rule("Edit", "edit-cmd"),
                    _rule("MultiEdit", "multiedit-cmd"),
                ]
            },
        )
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        before = hooks["BeforeTool"]
        # Both rules collapsed onto the same ``replace`` matcher...
        assert {r["matcher"] for r in before} == {"replace"}
        # ...but the synthesized handler names are distinct.
        names = [r["hooks"][0]["name"] for r in before]
        assert len(names) == 2
        assert names[0] != names[1]

    def test_collapsed_matchers_same_command_still_distinct(self, tmp_path, all_home):
        """Even when ``Edit`` and ``MultiEdit`` collapse to ``replace`` AND run
        the SAME command, the names must differ — the synthesized name hashes
        the *original* (pre-remap) matcher, not just the command, so
        ``/hooks disable <name>`` stays unambiguous."""
        _canonical(
            tmp_path,
            {
                "PreToolUse": [
                    _rule("Edit", "same-cmd"),
                    _rule("MultiEdit", "same-cmd"),
                ]
            },
        )
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        names = [r["hooks"][0]["name"] for r in hooks["BeforeTool"]]
        assert len(names) == 2
        assert names[0] != names[1], f"same-command collapsed matchers must differ, got {names!r}"

    def test_synthesized_name_is_deterministic_across_syncs(self, tmp_path, all_home):
        """The command-hash name is stable, so a re-sync is idempotent (no dup
        rule, no spurious conflict warning)."""
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "stable")]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        first = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"
        assert not results["gemini_settings"].warnings
        second = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert first == second  # byte-identical → idempotent

    def test_handler_name_slug_sanitizes_unsafe_matcher_chars(self):
        """A matcher carrying spaces/punctuation must not leak unsafe chars into
        the synthesized ``memtomem-<event>-<slug>-<digest>`` name — that would
        break Gemini's ``/hooks disable <name>``. Uniqueness still comes from the
        digest, so the slug is sanitized to the safe charset (slug-regex fold)."""
        from memtomem.context.settings import _ensure_gemini_handler_names

        out = _ensure_gemini_handler_names(
            [{"type": "command", "command": "echo hi"}],
            "BeforeTool",
            "weird matcher!@#|*",
            "weird matcher!@#|*",
        )
        name = out[0]["name"]
        assert name.startswith("memtomem-BeforeTool-")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
        assert set(name) <= allowed, f"unsafe chars leaked into Gemini name: {name!r}"


# ── Canonical matcher validation (issue #1983) ───────────────────────


@pytest.fixture
def every_home(tmp_path, monkeypatch):
    """HOME with all four runtime markers, so one fan-out exercises every
    registered generator (Claude, Codex, Gemini, Kimi)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    for marker in (".claude", ".codex", ".gemini", ".kimi"):
        (fake_home / marker).mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


#: ``matcher`` shapes that are present but not a string. ``null`` is included:
#: an *absent* matcher means match-all, an explicit ``null`` does not.
_BAD_MATCHERS = [None, 7, ["Bash"], {"tool": "Bash"}]
_BAD_MATCHER_IDS = ["null", "int", "list", "dict"]

#: ``(canonical event, a valid matcher for it, Gemini event, Kimi event)`` —
#: one tool-matching event (matcher is remapped per runtime) and one lifecycle
#: event (matcher passes through), because the two take different code paths.
_EVENTS = [
    ("PreToolUse", "Bash", "BeforeTool", "PreToolUse"),
    ("SessionStart", "", "SessionStart", "SessionStart"),
]
_EVENT_IDS = ["tool-event", "lifecycle-event"]

#: ``event → (target matcher, Gemini target matcher)`` for a *user*-authored
#: rule already sitting under the event. Deliberately distinct from the
#: contribution's matcher: a same-matcher user rule wins by design (the merge
#: yields to it), which would mask whether the good rule was delivered.
_USER_MATCHERS = {
    "PreToolUse": ("Read", "read_file"),
    "SessionStart": ("startup", "startup"),
}


def _kimi_rows(home):
    """The ``[[hooks]]`` rows memtomem rendered into Kimi's config.toml."""
    text = (home / ".kimi" / "config.toml").read_text(encoding="utf-8")
    return tomllib.loads(text).get("hooks", []), text


def _written_commands(home, event, gemini_event, kimi_event):
    """Commands memtomem delivered to each runtime for *event*."""

    def _record(path, ev):
        if not path.is_file():
            return []
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        return [h.get("command") for rule in hooks.get(ev, []) for h in rule.get("hooks", [])]

    rows, _ = _kimi_rows(home)
    return {
        "claude_settings": _record(home / ".claude" / "settings.json", event),
        "codex_settings": _record(home / ".codex" / "hooks.json", event),
        "gemini_settings": _record(home / ".gemini" / "settings.json", gemini_event),
        "kimi_settings": [r.get("command") for r in rows if r.get("event") == kimi_event],
    }


class TestNonStringMatcherIsDropped:
    """A non-string ``matcher`` is malformed canonical input, not a crash.

    Regression pins for #1983: the canonical record is user-authored JSON, so
    ``"matcher": ["Bash"]`` is a plausible typo. It used to take down the whole
    fan-out — ``TypeError`` out of ``_ensure_gemini_handler_names``' ``re.sub``
    on a fresh target, ``TypeError: unhashable type`` out of the matcher-keyed
    additive merge when the event already existed — instead of the
    drop-with-warning ADR-0018 §5 promises. Kimi failed a third way, coercing
    it with ``str()`` into a matcher that could never fire.
    """

    @pytest.mark.parametrize("matcher", _BAD_MATCHERS, ids=_BAD_MATCHER_IDS)
    @pytest.mark.parametrize("event,good_matcher,gemini_event,kimi_event", _EVENTS, ids=_EVENT_IDS)
    def test_fresh_target_drops_only_the_malformed_rule(
        self, tmp_path, every_home, matcher, event, good_matcher, gemini_event, kimi_event
    ):
        """Every generator writes the healthy rule and warns about the bad one."""
        bad = _rule("", "bad", timeout=2)
        bad["matcher"] = matcher
        _canonical(tmp_path, {event: [bad, _rule(good_matcher, "good", timeout=2)]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        commands = _written_commands(every_home, event, gemini_event, kimi_event)
        for name in ("claude_settings", "codex_settings", "gemini_settings", "kimi_settings"):
            r = results[name]
            assert r.status == "ok", (name, r.reason)
            assert any("non-string matcher" in w for w in r.warnings), (name, r.warnings)
            assert "good" in commands[name], (name, commands[name])
            assert "bad" not in commands[name], (name, commands[name])

    @pytest.mark.parametrize("matcher", _BAD_MATCHERS, ids=_BAD_MATCHER_IDS)
    @pytest.mark.parametrize("event,good_matcher,gemini_event,kimi_event", _EVENTS, ids=_EVENT_IDS)
    def test_existing_event_in_target_still_merges(
        self, tmp_path, every_home, matcher, event, good_matcher, gemini_event, kimi_event
    ):
        """With the event already present the merge keys rules by matcher — an
        unhashable one must never reach it, and the user's rule survives."""
        user_matcher, gemini_user_matcher = _USER_MATCHERS[event]
        (every_home / ".claude" / "settings.json").write_text(
            json.dumps({"hooks": {event: [_rule(user_matcher, "user-claude")]}}) + "\n",
            encoding="utf-8",
        )
        (every_home / ".codex" / "hooks.json").write_text(
            json.dumps({"hooks": {event: [_rule(user_matcher, "user-codex")]}}) + "\n",
            encoding="utf-8",
        )
        (every_home / ".gemini" / "settings.json").write_text(
            json.dumps({"hooks": {gemini_event: [_rule(gemini_user_matcher, "user-gemini")]}})
            + "\n",
            encoding="utf-8",
        )
        (every_home / ".kimi" / "config.toml").write_text('model = "kimi-k2"\n', encoding="utf-8")

        bad = _rule("", "bad", timeout=2)
        bad["matcher"] = matcher
        _canonical(tmp_path, {event: [bad, _rule(good_matcher, "good", timeout=2)]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)

        commands = _written_commands(every_home, event, gemini_event, kimi_event)
        for name in ("claude_settings", "codex_settings", "gemini_settings", "kimi_settings"):
            r = results[name]
            assert r.status == "ok", (name, r.reason)
            assert any("non-string matcher" in w for w in r.warnings), (name, r.warnings)
            assert "bad" not in commands[name], (name, commands[name])
            assert "good" in commands[name], (name, commands[name])
        # User rules under the same event are untouched by the drop.
        assert "user-claude" in commands["claude_settings"]
        assert "user-codex" in commands["codex_settings"]
        assert "user-gemini" in commands["gemini_settings"]
        assert 'model = "kimi-k2"' in _kimi_rows(every_home)[1]

    @pytest.mark.parametrize("matcher", _BAD_MATCHERS, ids=_BAD_MATCHER_IDS)
    def test_kimi_never_renders_a_stringified_matcher(self, tmp_path, every_home, matcher):
        """Kimi's renderer used ``str(matcher)``, so a lifecycle event wrote
        ``matcher = "['Bash']"`` — silently unfireable rather than crashing."""
        bad = _rule("", "bad", timeout=2)
        bad["matcher"] = matcher
        _canonical(tmp_path, {"SessionStart": [bad]})

        assert (
            generate_all_settings(tmp_path, scope="user", allow_host_writes=True)[
                "kimi_settings"
            ].status
            == "ok"
        )
        rows, text = _kimi_rows(every_home)
        assert rows == []
        assert str(matcher) not in text

    def test_absent_matcher_is_still_match_all(self, tmp_path, every_home):
        """The validator must not confuse "omitted" with "malformed"."""
        rule = _rule("", "always", timeout=2)
        del rule["matcher"]
        _canonical(tmp_path, {"SessionStart": [rule]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        commands = _written_commands(every_home, "SessionStart", "SessionStart", "SessionStart")
        for name in ("claude_settings", "codex_settings", "gemini_settings", "kimi_settings"):
            assert results[name].status == "ok", (name, results[name].reason)
            assert not results[name].warnings, (name, results[name].warnings)
            assert commands[name] == ["always"], (name, commands[name])

    @pytest.mark.parametrize("matcher", _BAD_MATCHERS, ids=_BAD_MATCHER_IDS)
    def test_owned_target_rule_with_bad_matcher_is_kept_verbatim(
        self, tmp_path, every_home, matcher
    ):
        """The *target* file is hand-editable too, and the merge's in-place pass
        keys a dict by the matcher of every ownership-marked rule it finds
        there — an unhashable one raised ``TypeError`` before it could ever be
        compared. memtomem cannot have written such a rule, so it is kept
        verbatim rather than replaced or pruned as stale.
        """
        owned_bad = {
            "matcher": matcher,
            "hooks": [
                {
                    "type": "command",
                    "command": "target-owned",
                    "statusMessage": "memtomem · PreToolUse",
                }
            ],
        }
        target = every_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PreToolUse": [owned_bad]}}) + "\n", encoding="utf-8"
        )
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "good", timeout=2)]})

        r = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)["claude_settings"]
        assert r.status == "ok", r.reason

        rules = json.loads(target.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        assert owned_bad in rules, rules
        assert any(rule.get("matcher") == "Bash" for rule in rules), rules

    def test_diff_reports_the_same_drop(self, tmp_path, every_home):
        """``diff_settings`` shares the merge, so the dry run must not crash
        either — it is what the Web UI and ``mm context diff`` call."""
        bad = _rule("", "bad", timeout=2)
        bad["matcher"] = ["Bash"]
        _canonical(tmp_path, {"PreToolUse": [bad, _rule("Bash", "good", timeout=2)]})

        results = diff_settings(tmp_path, scope="user")
        for name in ("claude_settings", "codex_settings", "gemini_settings", "kimi_settings"):
            r = results[name]
            assert r.status != "error", (name, r.reason)
            assert any("non-string matcher" in w for w in r.warnings), (name, r.warnings)
