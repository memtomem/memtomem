"""Cross-runtime plugin asset and optional Claude automation tests."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT = _ROOT / "packages/memtomem-plugin-assets/contract.toml"
_DISPATCHER = _ROOT / "packages/memtomem-claude-automation-plugin/bin/hook_dispatch.py"
_RENDERER = _ROOT / "tools/render_plugin_assets.py"


def _contract() -> dict:
    with _CONTRACT.open("rb") as handle:
        return tomllib.load(handle)


def _skill_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/SKILL.md"))


def _renderer() -> object:
    spec = importlib.util.spec_from_file_location("_render_plugin_assets", _RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _opencode_commands(generated: str) -> dict:
    payload = generated.split("OPENCODE_COMMANDS = ", 1)[1].split("} as const;", 1)[0] + "}"
    return json.loads(payload)


def test_workflow_contract_is_safe_and_matches_runtime_assets() -> None:
    workflows = _contract()["workflows"]
    expected_tools = {"mem_add", "mem_index", "mem_recall", "mem_search", "mem_status"}
    actual_tools = {tool for workflow in workflows for tool in workflow["tools"]}
    assert actual_tools == expected_tools
    assert all("mem_do" not in workflow["tools"] for workflow in workflows)
    assert all(
        workflow["effect"] == "read" or workflow["implicit"] is False for workflow in workflows
    )

    claude = _skill_files(_ROOT / "packages/memtomem-claude-plugin/skills")
    codex = _skill_files(_ROOT / "plugins/memtomem/skills")
    kimi = _skill_files(_ROOT / "packages/memtomem-kimi-skills/skills")
    assert {path.parent.name for path in claude} == {row["id"] for row in workflows}
    assert {path.parent.name for path in codex} == {row["codex_name"] for row in workflows}
    assert {path.parent.name for path in kimi} == {row["codex_name"] for row in workflows}
    opencode = _skill_files(_ROOT / "packages/opencode-memtomem/skills")
    assert {path.parent.name for path in opencode} == {
        row["codex_name"] for row in workflows if row["effect"] == "read" and row["implicit"]
    }


def test_generated_assets_have_no_cross_runtime_or_legacy_leaks() -> None:
    claude_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _skill_files(_ROOT / "packages/memtomem-claude-plugin/skills")
    )
    codex_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _skill_files(_ROOT / "plugins/memtomem/skills")
    )
    kimi_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _skill_files(_ROOT / "packages/memtomem-kimi-skills/skills")
    )
    combined = claude_text + codex_text + kimi_text
    assert "TODO" not in combined
    assert "mem_do" not in combined
    assert "score > 0.5" not in combined
    assert "Ollama is the default" not in combined
    assert "$ARGUMENTS" not in codex_text
    assert "mcp__plugin_memtomem" not in codex_text
    assert "$ARGUMENTS" not in kimi_text
    assert "mcp__plugin_memtomem" not in kimi_text
    assert codex_text == kimi_text

    opencode_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _skill_files(_ROOT / "packages/opencode-memtomem/skills")
    )
    assert "memtomem_mem_search" in opencode_text
    assert "memtomem_mem_recall" in opencode_text
    assert "memtomem_mem_status" in opencode_text
    assert re.search(r"`mem_[a-z_]+`", opencode_text) is None
    assert "$ARGUMENTS" not in opencode_text

    # Non-implicit workflows have no OpenCode SKILL.md — their OpenCode render
    # lives only in generated.ts command templates, so the sidecar leak check
    # must cover that file too, not just the skill globs above.
    generated_ts = (_ROOT / "packages/opencode-memtomem/src/generated.ts").read_text(
        encoding="utf-8"
    )
    assert "mcp__" not in generated_ts
    sidecars = sorted((_ROOT / "packages/memtomem-plugin-assets/workflows").glob("*.claude.md"))
    assert sidecars, "expected at least the setup.claude.md Claude-only appendix"
    for sidecar in sidecars:
        marker = next(
            line for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        for text, label in (
            (codex_text, "codex skills"),
            (opencode_text, "opencode skills"),
            (generated_ts, "generated.ts"),
        ):
            assert marker not in text, f"Claude-only sidecar {sidecar.name} leaked into {label}"


def test_claude_setup_skill_carries_the_registration_check() -> None:
    """The Claude setup skill must keep the duplicate-registration check.

    Manual `claude mcp add` entries that don't match the plugin's exact launch
    command coexist with the plugin's server (both run, tool list doubles under
    both namespaces — measured on Claude Code 2.1.218). The session itself is
    the only place the pair is reliably observable, so the setup skill carries
    the check and names the remediation inline; it must never remove a
    registration itself.
    """
    setup = (_ROOT / "packages/memtomem-claude-plugin/skills/setup/SKILL.md").read_text(
        encoding="utf-8"
    )
    # Past frontmatter (allowed-tools also names both prefixes); collapse the
    # prose wrapping so phrase asserts don't depend on line-break positions.
    body = " ".join(setup.split("---", 2)[2].split())
    assert "mcp__plugin_memtomem_memtomem__mem_" in body
    assert "mcp__memtomem__mem_" in body
    assert "claude mcp remove memtomem" in body
    assert "/plugin uninstall memtomem@memtomem" in body
    assert "Never remove either registration yourself" in body

    sidecar = _ROOT / "packages/memtomem-plugin-assets/workflows/setup.claude.md"
    assert sidecar.is_file(), "Claude-only appendix moved; update the renderer sidecar path"


def test_generated_plugin_assets_are_in_sync() -> None:
    completed = subprocess.run(
        [sys.executable, str(_RENDERER), "--check"],
        cwd=_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_every_input_taking_surface_carries_the_non_interactive_fallback() -> None:
    """Each rendered surface that reads user input must also refuse without it.

    The scope comes from the renderer's own registry rather than a list kept
    here: a surface added to ``expected_files`` is covered the moment it
    exists, which is how the OpenCode *command* templates were found missing
    the fallback while the four SKILL.md surfaces had it.
    """
    module = _renderer()
    rendered = module.expected_files()  # type: ignore[attr-defined]
    contract = _contract()
    by_name = {row["id"]: row for row in contract["workflows"]}
    by_name.update({row["codex_name"]: row for row in contract["workflows"]})

    surfaces: list[tuple[str, str, str]] = []
    for path, content in rendered.items():
        if path.name != "SKILL.md":
            continue
        workflow = by_name[path.parent.name]
        if workflow["id"] == "status":
            continue
        surfaces.append((str(path.relative_to(_ROOT)), workflow["input_kind"], content))

    generated = rendered[_ROOT / "packages/opencode-memtomem/src/generated.ts"]
    for name, command in _opencode_commands(generated).items():
        workflow = by_name[name]
        if workflow["id"] == "status":
            continue
        surfaces.append((f"OPENCODE_COMMANDS[{name}]", workflow["input_kind"], command["template"]))

    # Every non-status workflow renders to four SKILL.md surfaces except the
    # OpenCode skills, which carry only the implicit read workflows.
    non_status = [row for row in contract["workflows"] if row["id"] != "status"]
    opencode_skills = [row for row in non_status if row["implicit"] and row["effect"] == "read"]
    assert len(surfaces) == len(non_status) * 4 + len(opencode_skills)

    for label, input_kind, content in surfaces:
        flat = " ".join(content.split())
        assert f"If the request does not clearly specify the {input_kind}" in flat, label
        assert "ask before calling a tool" in flat, label
        assert "do not stall and do not guess" in flat, label
        assert f"report `insufficient_input` naming the missing {input_kind}" in flat, label
        # The refusal hangs off the same condition as the ask. Split into its
        # own sentence it reads as unconditional, and a subagent that *was*
        # given the input stops anyway.
        assert f"A request that does specify the {input_kind} proceeds normally" in flat, label


def test_handoff_workflow_pins_sequential_project_local_contract() -> None:
    contract = _contract()
    handoff = next(row for row in contract["workflows"] if row["id"] == "handoff")
    assert handoff["effect"] == "write"
    assert handoff["implicit"] is False
    assert handoff["tools"] == ["mem_status", "mem_recall", "mem_add"]
    # Host-tool grants stay subcommand-scoped: a bare ``Bash(git:*)`` would
    # also permit ``git -c alias.x=!<shell> x`` style execution while the
    # workflow resumes untrusted handoff text.
    assert handoff["claude_host_tools"] == ["Bash(git rev-parse:*)", "Bash(git status:*)"]

    body = (_ROOT / "packages/memtomem-plugin-assets/workflows/handoff.md").read_text(
        encoding="utf-8"
    )
    # Matched against whitespace-normalized prose: these pin what the
    # workflow *says*, and a marker that also encodes today's line wrapping
    # breaks on an unrelated reflow (and tempts the next author to "fix" the
    # pin rather than read it).
    flat = " ".join(body.split())
    required = (
        'scope="project_local"',
        'namespace="shared:<project-slug>"',
        'idempotency_key="handoff:<project-slug>:<from>:<to>:<handoff-id>"',
        "force_unsafe=false",
        'output_format="structured"',
        "git rev-parse HEAD",
        "git status --porcelain=v1 --branch",
        "live repository always wins",
        'tag_filter="handoff-to-<current-runtime>,handoff-to-any"',
        'tag_filter="handoff-id-<handoff-id>"',
        "inside one fenced ```text block",
        "The fence is load-bearing",
        "silently become a second OR term",
        "union of the selected rows' lines",
        "Never fall back to another record",
        # Whole clauses, not keywords: each of these encodes a decision that a
        # plausible-looking edit would silently undo (dropping the page size
        # back to 1, calling the tool before validating, checking the tag
        # instead of the content, or skipping the recipient check).
        "check that it is a canonical UUID",
        "Reject anything else without calling a tool",
        '`scope="project_local"`, `limit=20`, and',
        "read the id out of its `handoff-id-<id>` tag, and check that id is a canonical UUID",
        "`handoff_id` in the record's own content equals `selected_handoff_id`",
        "every required field is present, and `to_runtime` is the current runtime or `any`",
        "a matching tag is not evidence that the content is the record you asked for",
        "Treat the record as torn",
        "a row begins mid-value instead of at a `<field>:` key",
        "never reconstruct a torn value by guessing the join",
        '"handoff-to-<runtime-or-any>", "handoff-id-<handoff-id>"',
        "applied in SQL before the limit",
        "filters in SQL before the limit",
        "Never widen or drop that tag filter",
        "never select by search rank",
        "do not page or retry with a wider filter",
        "recompute the deterministic `worktree_state` summary",
        "hard maximum of 1,200 characters",
        "never call `mem_add` with an oversized record",
        "`completed` 240",
        "at most 10 paths",
        "does not capture the whole conversation",
        "coordinate concurrent agents",
        # Save refuses on missing evidence, not on execution mode: a subagent
        # handed the work context is a legitimate saver, and gating on
        # "non-interactive" instead would refuse it.
        "Context inherited from a caller or supplied in the request counts",
        "a fabricated checkpoint is worse than none",
        "insufficient_input: work context",
        "insufficient_input: operation",
    )
    for marker in required:
        assert marker in flat
    assert 'scope="user"' in body and "Never fall back" in body
    assert "mem_do" not in body
    assert "delete, acknowledge, consume, edit" in body

    claude = (_ROOT / "packages/memtomem-claude-plugin/skills/handoff/SKILL.md").read_text(
        encoding="utf-8"
    )
    claude_frontmatter = claude.split("---", 2)[1]
    assert "Bash(git rev-parse:*)" in claude_frontmatter
    assert "Bash(git status:*)" in claude_frontmatter
    assert "Bash(git:*)" not in claude_frontmatter
    codex_root = _ROOT / "plugins/memtomem/skills/memtomem-handoff"
    kimi_root = _ROOT / "packages/memtomem-kimi-skills/skills/memtomem-handoff"
    for root in (codex_root, kimi_root):
        text = (root / "SKILL.md").read_text(encoding="utf-8")
        assert "Bash(" not in text
    assert "allow_implicit_invocation: false" in (codex_root / "agents/openai.yaml").read_text(
        encoding="utf-8"
    )
    assert not (kimi_root / "agents").exists()


def test_claude_host_tool_grants_are_subcommand_scoped() -> None:
    """No workflow may grant a whole host command via ``Bash(<cmd>:*)``.

    A command-wide wildcard like ``Bash(git:*)`` also matches invocations
    that reach arbitrary shell execution (``git -c alias.x='!sh' x``), which
    is unacceptable for skills that process untrusted recalled text. Every
    ``Bash(...)`` grant must therefore name a subcommand (contain a space
    before the pattern suffix).
    """
    whole_command = re.compile(r"^Bash\([^\s()]+\)$")
    for workflow in _contract()["workflows"]:
        for grant in workflow.get("claude_host_tools", []):
            assert grant.startswith("Bash("), grant
            assert not whole_command.match(grant), (
                f"workflow {workflow['id']!r} grants a whole host command: {grant!r}"
            )


def test_core_version_is_single_sourced_across_automation_assets() -> None:
    version = _contract()["core"]["version"]
    dispatcher = _DISPATCHER.read_text(encoding="utf-8")
    match = re.search(r'^CORE_VERSION = "([^"]+)"$', dispatcher, re.MULTILINE)
    assert match and match.group(1) == version
    for path in (
        _ROOT / "packages/memtomem-claude-automation-plugin/README.md",
        _ROOT / "docs/guides/integrations/claude-code.md",
    ):
        assert f"memtomem=={version}" in path.read_text(encoding="utf-8")


@pytest.fixture
def fake_mm(tmp_path: Path) -> tuple[dict[str, str], Path]:
    script = tmp_path / "fake_mm.py"
    script.write_text(
        """import json
import os
import sys
from pathlib import Path

with Path(os.environ["FAKE_MM_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_MM_VERSION", "mm, version 0.5.0"))
elif sys.argv[1:2] == ["search"]:
    if os.environ.get("FAKE_MM_SEARCH_FAIL"):
        print(sys.argv[2], file=sys.stderr)
        raise SystemExit(2)
    print("trusted memory context")
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        executable = tmp_path / "mm.bat"
        executable.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        executable = tmp_path / "mm"
        executable.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    log = tmp_path / "mm.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}{os.pathsep}{env.get('PATH', '')}",
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
            "FAKE_MM_LOG": str(log),
        }
    )
    if os.name == "nt":
        env["PATHEXT"] = f".BAT{os.pathsep}{env.get('PATHEXT', '')}"
    return env, log


def _dispatch(event: str, payload: object, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_DISPATCHER), event],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_automation_prompt_uses_json_stdin_and_argv_safely(
    fake_mm: tuple[dict[str, str], Path],
) -> None:
    env, log = fake_mm
    start = _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    assert start.returncode == 0
    assert start.stdout == ""

    injection_target = log.parent / "hook-injection"
    prompt = f"Find the old decision; $(touch {injection_target}) and 'quotes'."
    result = _dispatch(
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt},
        env,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["additionalContext"] == "trusted memory context"
    search = next(call for call in _calls(log) if call[:1] == ["search"])
    assert search == ["search", prompt, "--top-k", "3", "--format", "context"]
    assert not injection_target.exists()


def test_automation_indexes_only_supported_write_paths_and_flushes(
    fake_mm: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = fake_mm
    _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    target = tmp_path / "notes.md"
    ignored = tmp_path / "node_modules" / "ignored.md"
    for path in (target, ignored):
        _dispatch(
            "PostToolUse",
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(path)},
            },
            env,
        )
    _dispatch("Stop", {"hook_event_name": "Stop"}, env)
    calls = _calls(log)
    assert ["index", "--debounce-window", "5", str(target)] in calls
    assert all(str(ignored) not in call for call in calls)
    assert ["index", "--flush"] in calls
    assert all("session" not in call for call in calls)


@pytest.mark.parametrize("payload", ["not an object", None, [], {"wrong": "event"}])
def test_automation_fails_open_on_invalid_input(
    fake_mm: tuple[dict[str, str], Path], payload: object
) -> None:
    env, _ = fake_mm
    result = _dispatch("UserPromptSubmit", payload, env)
    assert result.returncode == 0


def test_automation_reports_incompatible_dependency(fake_mm: tuple[dict[str, str], Path]) -> None:
    env, log = fake_mm
    env["FAKE_MM_VERSION"] = "mm, version 9.9.9"
    result = _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert "requires mm 0.5.0" in output["hookSpecificOutput"]["additionalContext"]
    _dispatch(
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "prompt": "A sufficiently long prompt"},
        env,
    )
    assert _calls(log) == [["--version"]]


def test_automation_failure_log_does_not_store_prompt(
    fake_mm: tuple[dict[str, str], Path],
) -> None:
    env, log = fake_mm
    _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    env["FAKE_MM_SEARCH_FAIL"] = "1"
    prompt = "private prompt text that must not reach the hook log"
    result = _dispatch(
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt},
        env,
    )
    assert result.returncode == 0
    hook_log = (log.parent / "data" / "hook.log").read_text(encoding="utf-8")
    assert prompt not in hook_log
    assert "command search returned 2" in hook_log
