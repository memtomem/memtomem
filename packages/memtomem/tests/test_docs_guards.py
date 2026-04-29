"""Source-scan guards for public-doc cross-file invariants.

These guards protect invariants that code cannot enforce directly:

- Every editor integration's Verify Connection section must surface the
  `mm status` CLI — it's the terminal mirror of `mem_status` for users
  whose editor has not reconnected yet.
- Every editor integration's First Indexing example must use the same
  multiline `Indexing complete:` block, so users comparing editors see
  the same expected output shape.
- `mem_config` / `mem_embedding_reset` / `mem_reset` live in the Config
  tool group in both ``reference.md`` and ``mcp-clients.md``; both files
  must mark them with the ``\\*`` + ``MEMTOMEM_TOOL_MODE=full`` footnote,
  or users reading one file won't know they are gated.
- The ``hooks.json`` snippet rendered in ``claude-code.md`` Hooks
  Automation Setup must declare byte-identical ``command`` strings to
  the plugin's shipped ``hooks.json`` for every event the snippet
  covers. Drift between the two sites silently ships an outdated
  user-facing recipe.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUIDES = _REPO_ROOT / "docs" / "guides"
_INTEGRATIONS = _GUIDES / "integrations"
_PLUGIN_HOOKS_JSON = _REPO_ROOT / "packages" / "memtomem-claude-plugin" / "hooks" / "hooks.json"
_HOOKS_SNIPPET_ANCHOR = "Add the following to `~/.claude/settings.json`:"

_ASTERISK_TOOLS = ("mem_config", "mem_embedding_reset", "mem_reset")
_FOOTNOTE_PREFIX = r"\* Requires `MEMTOMEM_TOOL_MODE=full`"


def _read(path: Path) -> str:
    assert path.exists(), f"Doc file missing: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def claude_code() -> str:
    return _read(_INTEGRATIONS / "claude-code.md")


@pytest.fixture(scope="module")
def claude_desktop() -> str:
    return _read(_INTEGRATIONS / "claude-desktop.md")


@pytest.fixture(scope="module")
def cursor() -> str:
    return _read(_INTEGRATIONS / "cursor.md")


@pytest.fixture(scope="module")
def mcp_clients() -> str:
    return _read(_GUIDES / "mcp-clients.md")


@pytest.fixture(scope="module")
def reference() -> str:
    return _read(_GUIDES / "reference.md")


@pytest.fixture(scope="module")
def canonical_footnote(reference: str) -> str:
    """The tool-mode footnote line, extracted from reference.md.

    reference.md is the canonical source; other docs (mcp-clients.md)
    must carry this line verbatim. Extracting it here keeps parity
    failures scoped to "target file drifted" — if reference.md itself
    loses the footnote, this fixture fails and parity tests never run,
    so a reference-side regression can't be mistaken for a target-side one.
    """
    for line in reference.splitlines():
        if line.startswith(_FOOTNOTE_PREFIX):
            return line
    pytest.fail(
        f"reference.md lost its tool-mode footnote line (no line starts with {_FOOTNOTE_PREFIX!r})"
    )


class TestIntegrationsMmStatus:
    def test_claude_code_surfaces_mm_status(self, claude_code: str) -> None:
        assert "mm status" in claude_code

    def test_claude_desktop_surfaces_mm_status(self, claude_desktop: str) -> None:
        assert "mm status" in claude_desktop

    def test_cursor_surfaces_mm_status(self, cursor: str) -> None:
        assert "mm status" in cursor


class TestIntegrationsIndexingBlock:
    def test_claude_code_indexing_block(self, claude_code: str) -> None:
        assert "Indexing complete:" in claude_code

    def test_claude_desktop_indexing_block(self, claude_desktop: str) -> None:
        assert "Indexing complete:" in claude_desktop

    def test_cursor_indexing_block(self, cursor: str) -> None:
        assert "Indexing complete:" in cursor, (
            "cursor.md First Indexing example must use the multiline "
            "'Indexing complete:' block (Files scanned / Total chunks / "
            "Indexed / Skipped / Deleted) — parity with claude-code.md "
            "and claude-desktop.md."
        )


class TestToolModeFootnoteParity:
    def test_reference_marks_tools(self, reference: str) -> None:
        for name in _ASTERISK_TOOLS:
            assert f"`{name}`\\*" in reference, (
                f"reference.md Config table must tag `{name}` with `\\*`."
            )

    def test_mcp_clients_marks_tools(self, mcp_clients: str) -> None:
        for name in _ASTERISK_TOOLS:
            assert f"`{name}`\\*" in mcp_clients, (
                f"mcp-clients.md Config table must tag `{name}` with `\\*` "
                f"(parity with reference.md so users see the tool-mode gate)."
            )

    def test_mcp_clients_matches_reference_footnote(
        self, canonical_footnote: str, mcp_clients: str
    ) -> None:
        assert canonical_footnote in mcp_clients, (
            "mcp-clients.md must carry reference.md's tool-mode footnote "
            "line verbatim so the CLI / Web UI alternate-access hint stays "
            "in sync across the two Config-table entry points."
        )


def _extract_hooks_snippet(claude_code_md: str) -> dict:
    """Extract the ``Add the following to ~/.claude/settings.json`` JSON
    block from claude-code.md. Returns the parsed dict.

    The Hooks Automation Setup section embeds a fenced ``json`` block that
    users copy-paste into their Claude Code settings; this helper returns
    that block as the parsed dict so parity tests can compare commands
    against the plugin's shipped hooks.json.
    """
    anchor_idx = claude_code_md.find(_HOOKS_SNIPPET_ANCHOR)
    if anchor_idx == -1:
        pytest.fail(f"claude-code.md lost its hooks-snippet anchor: {_HOOKS_SNIPPET_ANCHOR!r}")
    fence_re = re.compile(r"```json\n(.*?)\n```", re.DOTALL)
    match = fence_re.search(claude_code_md, anchor_idx)
    if match is None:
        pytest.fail("claude-code.md has the hooks-snippet anchor but no ```json fence after it")
    return json.loads(match.group(1))


def _commands_by_event_matcher(hooks_doc: dict) -> dict[tuple[str, str], str]:
    """Flatten a hooks.json shape into ``{(event, matcher): command}``.

    Only entries with a single command are included; multi-command entries
    fail loudly because the parity test isn't designed for them yet.
    """
    out: dict[tuple[str, str], str] = {}
    for event, entries in hooks_doc.get("hooks", {}).items():
        for entry in entries:
            matcher = entry.get("matcher", "")
            commands = entry.get("hooks", [])
            assert len(commands) == 1, (
                f"hooks parity helper expected exactly one command per entry, "
                f"got {len(commands)} at {event}/{matcher!r}"
            )
            out[(event, matcher)] = commands[0]["command"]
    return out


class TestPluginHooksDocsParity:
    """The hooks.json snippet in claude-code.md must declare byte-identical
    ``command`` strings to the plugin's shipped hooks.json for every
    (event, matcher) pair the docs cover. The docs intentionally show a
    subset (the ``activity log`` PostToolUse entry is omitted to keep the
    copy-paste recipe tight), so we iterate over the docs entries and
    require each to match the plugin file — not the other way around.
    """

    @pytest.fixture(scope="class")
    def plugin_commands(self) -> dict[tuple[str, str], str]:
        plugin_hooks = json.loads(_PLUGIN_HOOKS_JSON.read_text(encoding="utf-8"))
        return _commands_by_event_matcher(plugin_hooks)

    @pytest.fixture(scope="class")
    def docs_commands(self, claude_code: str) -> dict[tuple[str, str], str]:
        snippet = _extract_hooks_snippet(claude_code)
        return _commands_by_event_matcher(snippet)

    def test_docs_snippet_is_subset_of_plugin(
        self,
        plugin_commands: dict[tuple[str, str], str],
        docs_commands: dict[tuple[str, str], str],
    ) -> None:
        missing = [k for k in docs_commands if k not in plugin_commands]
        assert not missing, (
            f"claude-code.md hooks snippet declares (event, matcher) entries "
            f"that the plugin hooks.json does not ship: {missing}. Either add "
            f"them to packages/memtomem-claude-plugin/hooks/hooks.json or "
            f"remove them from the docs."
        )

    def test_docs_snippet_commands_match_plugin(
        self,
        plugin_commands: dict[tuple[str, str], str],
        docs_commands: dict[tuple[str, str], str],
    ) -> None:
        diffs = [
            (event_matcher, plugin_commands[event_matcher], docs_cmd)
            for event_matcher, docs_cmd in docs_commands.items()
            if plugin_commands.get(event_matcher) != docs_cmd
        ]
        assert not diffs, (
            "claude-code.md hooks snippet drifted from the plugin's "
            "hooks.json. The two sites must declare byte-identical commands "
            "for every (event, matcher) the docs render. Diffs:\n"
            + "\n".join(f"  {em}:\n    plugin: {p}\n    docs:   {d}" for em, p, d in diffs)
        )
