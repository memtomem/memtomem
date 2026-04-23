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
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GUIDES = _REPO_ROOT / "docs" / "guides"
_INTEGRATIONS = _GUIDES / "integrations"

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
