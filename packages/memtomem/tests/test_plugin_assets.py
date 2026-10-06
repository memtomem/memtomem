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
import yaml


_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT = _ROOT / "packages/memtomem-plugin-assets/contract.toml"
_DISPATCHER = _ROOT / "packages/memtomem-claude-automation-plugin/bin/hook_dispatch.py"
_RENDERER = _ROOT / "tools/render_plugin_assets.py"
_HERMES_SKILLS = _ROOT / "packages/memtomem-hermes-plugin/skills"


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


def _user_scope_workflows(workflows: list[dict]) -> list[dict]:
    return [row for row in workflows if "user" in row.get("scopes", ["project", "user"])]


def _rendered_skills() -> dict[str, str]:
    rendered = _renderer().expected_files()  # type: ignore[attr-defined]
    return {
        path.relative_to(_ROOT).as_posix(): text
        for path, text in rendered.items()
        if path.name == "SKILL.md"
    }


def _frontmatter(skill: str) -> object:
    # The block between the opening fence and the first line that is exactly
    # "---". Splitting on the substring instead would cut a value that contains
    # it, which can turn invalid YAML into a shorter document that parses.
    lines = skill.split("\n")
    assert lines[0] == "---", "no opening frontmatter fence"
    assert "---" in lines[1:], "no closing frontmatter fence"
    return yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))


def _claude_skill_key(workflow: dict) -> str:
    return f"packages/memtomem-claude-plugin/skills/{workflow['id']}/SKILL.md"


_RENDERED_SKILLS = _rendered_skills()


def _flat_body(path: Path) -> str:
    # Past frontmatter, prose reflow collapsed so clause asserts survive rewrapping.
    return " ".join(path.read_text(encoding="utf-8").split("---", 2)[2].split())


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
    hermes = _skill_files(_HERMES_SKILLS)
    assert {path.parent.name for path in hermes} == {
        row["codex_name"] for row in _user_scope_workflows(workflows)
    }
    assert "memtomem-handoff" not in {path.parent.name for path in hermes}


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

    hermes_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _skill_files(_HERMES_SKILLS)
    )
    assert "TODO" not in hermes_text
    assert "mem_do" not in hermes_text
    assert "$ARGUMENTS" not in hermes_text
    assert "mcp__" not in hermes_text
    # Scope variants stay on their side: the markers never ship, project-tier guidance
    # never reaches the user-scope host, and user-scope rules never reach the others.
    project_rendered = combined + opencode_text + generated_ts
    for text in (project_rendered, hermes_text):
        assert re.search(r"<!--\s*/?\s*scope", text, re.IGNORECASE) is None
    for project_only in ("mm mem init --scope project_local", "confirm_project_shared=true"):
        assert project_only in project_rendered
        assert project_only not in hermes_text
    for user_only in ("unsupported_scope", "never pass a relative path"):
        assert user_only in hermes_text
        assert user_only not in project_rendered
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


@pytest.mark.parametrize("workflow", ["setup", "status"])
def test_claude_setup_skill_carries_the_registration_check(workflow: str) -> None:
    """The Claude setup skill must keep the duplicate-registration check.

    Manual `claude mcp add` entries that don't match the plugin's exact launch
    command coexist with the plugin's server (both run, tool list doubles under
    both namespaces — measured on Claude Code 2.1.218). The session itself is
    the only place the pair is reliably observable, so the setup skill carries
    the check and names the remediation inline; it must never remove a
    registration itself.
    """
    setup = (_ROOT / f"packages/memtomem-claude-plugin/skills/{workflow}/SKILL.md").read_text(
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
    assert "mm doctor --claude-mcp" in body
    assert "does not prove two live processes or a shared database" in body

    sidecar = _ROOT / f"packages/memtomem-plugin-assets/workflows/{workflow}.claude.md"
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


@pytest.mark.parametrize("skill", sorted(_RENDERED_SKILLS))
def test_rendered_skill_frontmatter_is_a_yaml_mapping(skill: str) -> None:
    """Every rendered SKILL.md must carry frontmatter a YAML parser accepts.

    The renderer writes frontmatter as plain text lines, so nothing stops a
    contract value from producing invalid YAML: the handoff skill shipped with
    ``argument-hint: [save|resume] [target runtime or handoff id]``, which
    PyYAML and the Claude plugin directory's validator both reject. Parsing
    happens inside each case so one bad file cannot hide the others.
    """
    assert isinstance(_frontmatter(_RENDERED_SKILLS[skill]), dict)


def test_frontmatter_helper_keeps_a_value_that_contains_the_fence_text() -> None:
    skill = '---\nargument-hint: "[foo---bar]"\n---\n\n# Title\n'
    assert _frontmatter(skill) == {"argument-hint": "[foo---bar]"}


def test_frontmatter_helper_does_not_shorten_invalid_yaml_into_valid() -> None:
    with pytest.raises(yaml.YAMLError):
        _frontmatter("---\ndescription: hello--- world: invalid\n---\n\n# Title\n")


def test_frontmatter_helper_requires_a_closing_fence() -> None:
    with pytest.raises(AssertionError, match="no closing frontmatter fence"):
        _frontmatter("---\nname: search\n")


def test_rendered_skill_cases_cover_every_claude_workflow() -> None:
    workflows = _contract()["workflows"]
    assert workflows
    claude = {key for key in _RENDERED_SKILLS if key.startswith("packages/memtomem-claude-plugin/")}
    assert claude == {_claude_skill_key(row) for row in workflows}


@pytest.mark.parametrize("workflow", _contract()["workflows"], ids=lambda row: row["id"])
def test_claude_skill_argument_hint_matches_the_contract(workflow: dict) -> None:
    """A contract ``argument_hint`` must reach the Claude skill as that string.

    Driven from the contract rather than from what was rendered: checking the
    key only where it appears would pass if the renderer stopped emitting it,
    and the drift check would agree. Unquoted, a one-group hint such as
    ``[path]`` parses as a list, so the comparison is against the exact string.
    """
    frontmatter = _frontmatter(_RENDERED_SKILLS[_claude_skill_key(workflow)])
    assert isinstance(frontmatter, dict)
    hint = workflow.get("argument_hint")
    if not hint:
        assert "argument-hint" not in frontmatter
        return
    assert "argument-hint" in frontmatter
    assert isinstance(frontmatter["argument-hint"], str), frontmatter["argument-hint"]
    assert frontmatter["argument-hint"] == hint


def test_some_workflow_defines_an_argument_hint() -> None:
    # Keeps the hint test from passing vacuously if the contract key is renamed.
    assert any(row.get("argument_hint") for row in _contract()["workflows"])


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
    # OpenCode skills, which carry only the implicit read workflows, and the Hermes
    # skills, which carry only the user-scope ones.
    non_status = [row for row in contract["workflows"] if row["id"] != "status"]
    opencode_skills = [row for row in non_status if row["implicit"] and row["effect"] == "read"]
    hermes_skills = _user_scope_workflows(non_status)
    assert len(surfaces) == len(non_status) * 4 + len(opencode_skills) + len(hermes_skills)

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


def test_hermes_skills_carry_the_user_scope_rules() -> None:
    """The user-scope host has no project: each rule that says so must survive.

    The server starts in the plugin directory, so a relative path points inside the
    plugin and project tiers do not exist. One assertion per clause: deleting any one
    of them from the shared source's user block must fail here.
    """
    index = _flat_body(_HERMES_SKILLS / "memtomem-index/SKILL.md")
    assert "Require an explicit absolute file or directory path" in index
    assert "never pass a relative path" in index
    assert "this server's working directory is the plugin directory" in index

    remember = _flat_body(_HERMES_SKILLS / "memtomem-remember/SKILL.md")
    assert 'Call `mem_add` with `scope="user"`' in remember
    assert "without a `file` argument" in remember
    assert "never pass `project_local` or `project_shared`" in remember
    assert "If the user explicitly asked for a project-only destination" in remember
    assert "do not write" in remember
    assert "save there only if they then agree" in remember
    assert "stop without writing and report `unsupported_scope`" in remember
    assert "name the project in the content or a tag" in remember
    assert "Choose the destination from the user's context" not in remember

    setup = _flat_body(_HERMES_SKILLS / "memtomem-setup/SKILL.md")
    no_bootstrap = setup.find("Do not suggest `mm init`")
    absolute = setup.find("The path must be absolute")
    index_call = setup.find("Call `mem_index`")
    assert no_bootstrap != -1 and absolute != -1 and index_call != -1
    assert no_bootstrap < index_call and absolute < index_call
    assert "bootstrap command from the plugin README" not in setup

    by_name = {row["codex_name"]: row for row in _contract()["workflows"]}
    search = (_HERMES_SKILLS / "memtomem-search/SKILL.md").read_text(encoding="utf-8")
    expected = by_name["memtomem-search"]["user_scope_description"]
    assert f"\ndescription: {expected}\n" in search
    assert expected != by_name["memtomem-search"]["description"]


_SHARED = "head\n"
_BLOCKS = (
    "<!-- scope:project -->\nproject line\n<!-- /scope -->\n"
    "<!-- scope:user -->\n  user line\n\nuser tail\n<!-- /scope -->\n"
)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("project", "head\nproject line\ntail\n"),
        ("user", "head\n  user line\n\nuser tail\ntail\n"),
    ],
)
def test_scope_selector_keeps_shared_text_and_the_chosen_block(scope: str, expected: str) -> None:
    module = _renderer()
    rendered = module._select_scope(_SHARED + _BLOCKS + "tail\n", scope)  # type: ignore[attr-defined]
    assert rendered == expected


def test_scope_selector_passes_unmarked_text_through_byte_for_byte() -> None:
    module = _renderer()
    text = "a\r\n\n  b\u0085c\nno trailing newline"
    assert module._select_scope(text, "project") == text  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A marker on the last line, with no newline after it, takes only its own
        # line: the kept line before it keeps its terminator.
        ("<!-- scope:project -->\nx\n<!-- /scope -->", "x\n"),
        ("<!-- scope:project -->\r\nx\r\n<!-- /scope -->", "x\r\n"),
        ("<!-- scope:project -->\r\nx\r\n<!-- /scope -->\r\ny\r\n", "x\r\ny\r\n"),
        ("<!-- scope:user -->\nx\n<!-- /scope -->", ""),
    ],
)
def test_scope_selector_drops_each_marker_with_only_its_own_line_ending(
    text: str, expected: str
) -> None:
    module = _renderer()
    assert module._select_scope(text, "project") == expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("<!-- scope:project -->\nx\n", "unterminated"),
        ("<!-- scope:project -->\n<!-- scope:user -->\n<!-- /scope -->\n", "nested"),
        ("<!-- scope:team -->\nx\n<!-- /scope -->\n", "unknown scope"),
        ("x\n<!-- /scope -->\n", "without an open block"),
        ("<!-- scope:project -->\nx <!-- scope:user -->\n<!-- /scope -->\n", "malformed"),
        # Excluded variants are parsed too: a typo there must not hide until it is selected.
        ("<!-- scope:user -->\n<!--scope:user-->\n<!-- /scope -->\n", "malformed"),
        # A marker wrapped over two lines is neither marker form; it must not ship.
        ("<!--\n scope:user -->\nuser-only\n<!--\n /scope -->\n", "malformed"),
    ],
)
def test_scope_selector_rejects_malformed_markers(text: str, message: str) -> None:
    module = _renderer()
    with pytest.raises(ValueError, match=message):
        module._select_scope(text, "project")  # type: ignore[attr-defined]


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
    # The contract pins plugins to a core that must be the one this checkout
    # releases; the renderer and preflight each check only their own side.
    with (_ROOT / "packages/memtomem/pyproject.toml").open("rb") as handle:
        assert tomllib.load(handle)["project"]["version"] == version
    guide = (_ROOT / "docs/guides/integrations/claude-code.md").read_text(encoding="utf-8")
    assert f"memtomem[onnx]=={version}" in guide
    dispatcher = _DISPATCHER.read_text(encoding="utf-8")
    match = re.search(r'^CORE_VERSION = "([^"]+)"$', dispatcher, re.MULTILINE)
    assert match and match.group(1) == version
    readme = (_ROOT / "packages/memtomem-claude-automation-plugin/README.md").read_text(
        encoding="utf-8"
    )
    assert f"memtomem=={version}" in readme


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
    print(os.environ.get("FAKE_MM_VERSION", "mm, version 0.6.7"))
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
    assert "requires mm 0.6.7" in output["hookSpecificOutput"]["additionalContext"]
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
