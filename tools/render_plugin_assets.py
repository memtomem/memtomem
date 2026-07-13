#!/usr/bin/env python3
"""Render the shipped Claude and Codex skills from one reviewed contract."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "packages" / "memtomem-plugin-assets"
CONTRACT = ASSETS / "contract.toml"
CLAUDE_ROOT = ROOT / "packages" / "memtomem-claude-plugin"
CODEX_ROOT = ROOT / "plugins" / "memtomem"


def _q(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _yaml_q(value: str) -> str:
    return _q(value)


def _allowed_tools(tools: list[str]) -> str:
    names: list[str] = []
    for tool in tools:
        names.extend(
            [
                f"mcp__plugin_memtomem_memtomem__{tool}",
                f"mcp__memtomem__{tool}",
            ]
        )
    return ", ".join(names)


def _claude_skill(workflow: dict, body: str) -> str:
    frontmatter = [
        "---",
        f"name: {workflow['id']}",
        f"description: {workflow['description']}",
    ]
    if hint := workflow.get("argument_hint"):
        frontmatter.append(f"argument-hint: {hint}")
    frontmatter.append(f"allowed-tools: {_allowed_tools(workflow['tools'])}")
    if not workflow["implicit"]:
        frontmatter.append("disable-model-invocation: true")
    frontmatter.extend(["---", "", f"# {workflow['title']}", ""])
    if workflow["id"] != "status":
        frontmatter.extend(
            [
                f"Use `$ARGUMENTS` as the {workflow['input_kind']}.",
                f"If the request does not clearly specify the {workflow['input_kind']}, ask before calling a tool.",
                "",
            ]
        )
    return "\n".join(frontmatter) + body.strip() + "\n"


def _codex_skill(workflow: dict, body: str) -> str:
    lines = [
        "---",
        f"name: {workflow['codex_name']}",
        f"description: {workflow['description']}",
        "---",
        "",
        f"# {workflow['title']}",
        "",
    ]
    if workflow["id"] != "status":
        lines.extend(
            [
                f"Derive the {workflow['input_kind']} from the current user request.",
                f"If the request does not clearly specify the {workflow['input_kind']}, ask before calling a tool.",
                "",
            ]
        )
    return "\n".join(lines) + body.strip() + "\n"


def _openai_yaml(workflow: dict) -> str:
    implicit = "true" if workflow["implicit"] else "false"
    return (
        "interface:\n"
        f"  display_name: {_yaml_q(workflow['ui_name'])}\n"
        f"  short_description: {_yaml_q(workflow['ui_description'])}\n"
        f"  default_prompt: {_yaml_q(workflow['default_prompt'])}\n"
        "policy:\n"
        f"  allow_implicit_invocation: {implicit}\n"
    )


def _mcp_config(core: dict) -> str:
    payload = {
        "mcpServers": {
            "memtomem": {
                "command": "uvx",
                "args": ["--from", f"memtomem=={core['version']}", "memtomem-server"],
                "env": {"MEMTOMEM_TOOL_MODE": core["tool_mode"]},
            }
        }
    }
    return json.dumps(payload, indent=2) + "\n"


def expected_files() -> dict[Path, str]:
    contract = tomllib.loads(CONTRACT.read_text(encoding="utf-8"))
    files: dict[Path, str] = {
        CLAUDE_ROOT / ".mcp.json": _mcp_config(contract["core"]),
        CODEX_ROOT / ".mcp.json": _mcp_config(contract["core"]),
    }
    for workflow in contract["workflows"]:
        body = (ASSETS / "workflows" / f"{workflow['id']}.md").read_text(encoding="utf-8")
        files[CLAUDE_ROOT / "skills" / workflow["id"] / "SKILL.md"] = _claude_skill(workflow, body)
        codex_dir = CODEX_ROOT / "skills" / workflow["codex_name"]
        files[codex_dir / "SKILL.md"] = _codex_skill(workflow, body)
        files[codex_dir / "agents" / "openai.yaml"] = _openai_yaml(workflow)
    return files


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when generated files drift")
    args = parser.parse_args()

    drift: list[Path] = []
    for path, content in expected_files().items():
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                drift.append(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    if drift:
        for path in drift:
            print(f"drift: {path.relative_to(ROOT)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
