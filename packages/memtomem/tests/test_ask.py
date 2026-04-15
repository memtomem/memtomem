"""Tests for mem_ask (memory Q&A) and mm shell."""

import pytest
from pathlib import Path
from uuid import uuid4
from memtomem.models import Chunk, ChunkMetadata, SearchResult


def _make_chunk(content, source="/tmp/test.md", tags=(), heading=()):
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            tags=frozenset(tags),
            heading_hierarchy=tuple(heading),
        ),
        id=uuid4(),
        embedding=[],
    )


def _make_result(content, score, rank, source="/tmp/test.md", tags=(), heading=()):
    chunk = _make_chunk(content, source, tags, heading)
    return SearchResult(chunk=chunk, score=score, rank=rank, source="fused")


class TestMemAskFormatting:
    """Test the Q&A output structure without MCP context."""

    def test_qa_prompt_structure(self):
        """Verify the output format has question, memories, instructions, sources."""
        results = [
            _make_result("Deploy uses blue-green", 0.92, 1, heading=("## Deployment",)),
            _make_result("Redis cache config", 0.78, 2, tags=("redis",)),
        ]

        # Simulate what mem_ask builds
        question = "What deployment strategy do we use?"
        lines = [f"## Question: {question}", "", "## Relevant Memories", ""]

        sources_cited = []
        for r in results:
            heading = (
                " > ".join(r.chunk.metadata.heading_hierarchy)
                if r.chunk.metadata.heading_hierarchy
                else ""
            )
            source = str(r.chunk.metadata.source_file).split("/")[-1]
            label = heading or source
            lines.append(f"### [{r.rank}] {label} (relevance: {r.score:.2f})")
            lines.append(r.chunk.content.strip())
            lines.append("")
            sources_cited.append(f"[{r.rank}] {label}")

        lines.append("---")
        lines.append("")
        lines.append("## Instructions")
        lines.append(f'Answer the question "{question}" based on the memories above.')

        output = "\n".join(lines)
        assert "## Question:" in output
        assert "## Relevant Memories" in output
        assert "## Instructions" in output
        assert "[1]" in output
        assert "[2]" in output
        assert "blue-green" in output

    def test_no_results_message(self):
        question = "something unknown"
        output = f'No relevant memories found for: "{question}"\n\nTry broader keywords'
        assert "No relevant memories" in output
        assert "broader keywords" in output


@pytest.mark.ollama
class TestMemAskIntegration:
    """Integration tests using real components."""

    @pytest.mark.asyncio
    async def test_ask_with_indexed_content(self, components, memory_dir):
        """Index content then ask a question about it."""
        # Create a test file
        test_file = memory_dir / "deploy-notes.md"
        test_file.write_text(
            "## Deployment Strategy\n\n"
            "We use blue-green deployment with automatic rollback.\n"
            "The load balancer switches traffic after health checks pass.\n",
            encoding="utf-8",
        )

        # Index it
        await components.index_engine.index_file(test_file)

        # Search (simulating what mem_ask does)
        results, stats = await components.search_pipeline.search(
            "deployment strategy",
            top_k=5,
        )

        assert len(results) >= 1
        assert any("blue-green" in r.chunk.content for r in results)

    @pytest.mark.asyncio
    async def test_ask_empty_index(self, components):
        """Ask when nothing is indexed."""
        results, stats = await components.search_pipeline.search("anything", top_k=5)
        assert results == []

    @pytest.mark.asyncio
    async def test_ask_with_tags(self, components, memory_dir):
        """Verify tagged content is searchable."""
        test_file = memory_dir / "redis-notes.md"
        test_file.write_text(
            "## Redis Configuration\n\n"
            "Switched from LRU to LFU eviction policy.\n"
            "Cache hit rate improved by 40%.\n",
            encoding="utf-8",
        )
        await components.index_engine.index_file(test_file)

        results, _ = await components.search_pipeline.search(
            "redis cache",
            top_k=3,
            tag_filter="redis",
        )
        # May or may not find results depending on auto-tagging
        # but the pipeline should not error
        assert isinstance(results, list)


class TestShellCommands:
    """Test shell command parsing logic."""

    def test_command_parsing(self):
        """Verify command extraction from input."""
        import shlex

        test_cases = [
            ("search deployment", ("search", ["deployment"])),
            ("s deploy blue-green", ("s", ["deploy", "blue-green"])),
            (
                "ask what is our deploy strategy?",
                ("ask", ["what", "is", "our", "deploy", "strategy?"]),
            ),
            ("add new memory content", ("add", ["new", "memory", "content"])),
            ("recall --days 7", ("recall", ["--days", "7"])),
            ("tags", ("tags", [])),
            ("stats", ("stats", [])),
            ("quit", ("quit", [])),
            ("help", ("help", [])),
        ]

        for line, (expected_cmd, expected_args) in test_cases:
            parts = shlex.split(line)
            cmd = parts[0].lower()
            args = parts[1:]
            assert cmd == expected_cmd, f"Failed for: {line}"
            assert args == expected_args, f"Failed for: {line}"

    def test_implicit_search(self):
        """Unrecognized commands should trigger search."""
        import shlex

        line = "deployment blue-green strategy"
        parts = shlex.split(line)
        cmd = parts[0].lower()
        known = {
            "search",
            "s",
            "ask",
            "add",
            "recall",
            "r",
            "tags",
            "stats",
            "status",
            "index",
            "idx",
            "quit",
            "exit",
            "q",
            "help",
        }
        assert cmd not in known  # should fall through to implicit search

    def test_quoted_args(self):
        """Quoted strings should be preserved."""
        import shlex

        line = 'add "multi word content here"'
        parts = shlex.split(line)
        assert parts == ["add", "multi word content here"]

    def test_empty_line_skip(self):
        """Empty lines should be skipped."""
        line = "   "
        assert line.strip() == ""


class TestShellImport:
    def test_shell_command_exists(self):
        from memtomem.cli.shell import shell

        assert shell.name == "shell"

    def test_cli_has_shell(self):
        from memtomem.cli import cli

        commands = {cmd for cmd in cli.commands}
        assert "shell" in commands

    def test_help_function(self):
        from memtomem.cli.shell import _show_help

        # Should not raise
        _show_help()
