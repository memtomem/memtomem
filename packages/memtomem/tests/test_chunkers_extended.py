"""Tests for PythonChunker, JavaScriptChunker, StructuredChunker, ChunkerRegistry,
and indexing utilities (hasher, differ)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata, ChunkType
from memtomem.chunking.structured import StructuredChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.indexing.hasher import content_hash, file_hash
from memtomem.indexing.differ import compute_diff

from helpers import make_chunk

# ---------------------------------------------------------------------------
# Tree-sitter availability check
# ---------------------------------------------------------------------------

try:
    import tree_sitter  # noqa: F401
    import tree_sitter_python  # noqa: F401
    HAS_TS_PYTHON = True
except ImportError:
    HAS_TS_PYTHON = False

try:
    import tree_sitter_javascript  # noqa: F401
    HAS_TS_JS = True
except ImportError:
    HAS_TS_JS = False

needs_ts_python = pytest.mark.skipif(not HAS_TS_PYTHON, reason="tree-sitter-python not installed")
needs_ts_js = pytest.mark.skipif(not HAS_TS_JS, reason="tree-sitter-javascript not installed")


# ===================================================================
# PythonChunker
# ===================================================================

class TestPythonChunker:
    """Tests for chunking/python_code.py."""

    @pytest.fixture
    def chunker(self):
        from memtomem.chunking.python_code import PythonChunker
        return PythonChunker()

    def test_supported_extensions(self, chunker):
        assert ".py" in chunker.supported_extensions()

    def test_empty_file_returns_empty(self, chunker):
        assert chunker.chunk_file(Path("/test.py"), "") == []
        assert chunker.chunk_file(Path("/test.py"), "   \n  ") == []

    @needs_ts_python
    def test_module_with_function(self, chunker):
        code = '''\
def greet(name):
    """Say hello."""
    return f"Hello, {name}"
'''
        chunks = chunker.chunk_file(Path("/greet.py"), code)
        assert len(chunks) >= 1
        func_chunks = [c for c in chunks if c.metadata.chunk_type == ChunkType.PYTHON_FUNCTION]
        assert len(func_chunks) == 1
        assert "greet" in func_chunks[0].content

    @needs_ts_python
    def test_module_with_class(self, chunker):
        code = '''\
class Dog:
    def bark(self):
        return "woof"
'''
        chunks = chunker.chunk_file(Path("/animals.py"), code)
        class_chunks = [c for c in chunks if c.metadata.chunk_type == ChunkType.PYTHON_CLASS]
        assert len(class_chunks) == 1
        assert "Dog" in class_chunks[0].content

    @needs_ts_python
    def test_module_docstring_extracted(self, chunker):
        code = '''\
"""This is a module docstring."""

def foo():
    pass
'''
        chunks = chunker.chunk_file(Path("/docmod.py"), code)
        # Expect the docstring chunk and the function chunk
        assert len(chunks) >= 2
        doc_chunks = [c for c in chunks if "__module_doc__" in c.metadata.heading_hierarchy]
        assert len(doc_chunks) == 1

    @needs_ts_python
    def test_heading_hierarchy_includes_module_stem(self, chunker):
        code = "def my_func(): pass\n"
        chunks = chunker.chunk_file(Path("/utils.py"), code)
        assert any("utils" in c.metadata.heading_hierarchy for c in chunks)

    def test_syntax_error_falls_back_to_whole_file(self, chunker):
        """Invalid Python should fall back to a single whole-file chunk."""
        bad_code = "def broken(\n    x = {unclosed"
        chunks = chunker.chunk_file(Path("/broken.py"), bad_code)
        assert len(chunks) == 1
        assert chunks[0].content == bad_code
        assert chunks[0].metadata.chunk_type == ChunkType.RAW_TEXT


# ===================================================================
# JavaScriptChunker
# ===================================================================

class TestJavaScriptChunker:
    """Tests for chunking/javascript.py."""

    @pytest.fixture
    def chunker(self):
        from memtomem.chunking.javascript import JavaScriptChunker
        return JavaScriptChunker()

    def test_supported_extensions(self, chunker):
        exts = chunker.supported_extensions()
        assert ".js" in exts
        assert ".ts" in exts
        assert ".tsx" in exts

    def test_empty_file_returns_empty(self, chunker):
        assert chunker.chunk_file(Path("/empty.js"), "") == []

    @needs_ts_js
    def test_function_declaration(self, chunker):
        code = 'function greet(name) {\n  return "Hello " + name;\n}\n'
        chunks = chunker.chunk_file(Path("/greet.js"), code)
        assert len(chunks) >= 1
        assert any("greet" in c.content for c in chunks)

    @needs_ts_js
    def test_export_default_function(self, chunker):
        code = 'export default function main() {\n  console.log("ok");\n}\n'
        chunks = chunker.chunk_file(Path("/main.js"), code)
        assert len(chunks) >= 1

    @needs_ts_js
    def test_const_arrow_function(self, chunker):
        code = 'const add = (a, b) => a + b;\n'
        chunks = chunker.chunk_file(Path("/math.js"), code)
        assert len(chunks) >= 1

    def test_syntax_error_falls_back(self, chunker):
        bad_js = "function broken( { unclosed"
        chunks = chunker.chunk_file(Path("/bad.js"), bad_js)
        # Fallback: single whole-file chunk
        assert len(chunks) == 1
        assert chunks[0].metadata.chunk_type == ChunkType.RAW_TEXT

    @needs_ts_js
    def test_js_language_metadata(self, chunker):
        code = 'function f() {}\n'
        chunks = chunker.chunk_file(Path("/app.js"), code)
        assert all(c.metadata.language == "javascript" for c in chunks)

    def test_ts_language_in_fallback(self, chunker):
        """TypeScript files should have language='typescript' even in fallback."""
        chunks = chunker.chunk_file(Path("/app.ts"), "const x = 1;")
        assert chunks[0].metadata.language == "typescript"


# ===================================================================
# StructuredChunker
# ===================================================================

class TestStructuredChunker:
    """Tests for chunking/structured.py."""

    def test_json_top_level_keys(self):
        chunker = StructuredChunker()
        data = {"name": "Alice", "age": 30, "hobbies": ["reading", "hiking"]}
        content = json.dumps(data, indent=2)
        chunks = chunker.chunk_file(Path("/person.json"), content)
        assert len(chunks) >= 2  # at least name, age, hobbies
        all_text = " ".join(c.content for c in chunks)
        assert "Alice" in all_text

    def test_yaml_sections(self):
        chunker = StructuredChunker()
        yaml_content = "server:\n  host: localhost\n  port: 8080\ndb:\n  name: mydb\n"
        chunks = chunker.chunk_file(Path("/config.yaml"), yaml_content)
        assert len(chunks) >= 2
        keys_in_hierarchy = set()
        for c in chunks:
            keys_in_hierarchy.update(c.metadata.heading_hierarchy)
        assert "server" in keys_in_hierarchy or "db" in keys_in_hierarchy

    def test_toml_parsing(self):
        chunker = StructuredChunker()
        toml_content = '[project]\nname = "memtomem"\nversion = "0.1.0"\n'
        chunks = chunker.chunk_file(Path("/pyproject.toml"), toml_content)
        assert len(chunks) >= 1
        assert any("memtomem" in c.content for c in chunks)

    def test_invalid_json_falls_back(self):
        chunker = StructuredChunker()
        bad = "{not valid json!!"
        chunks = chunker.chunk_file(Path("/broken.json"), bad)
        assert len(chunks) == 1
        assert chunks[0].content == bad

    def test_empty_content_returns_empty(self):
        chunker = StructuredChunker()
        assert chunker.chunk_file(Path("/empty.json"), "") == []
        assert chunker.chunk_file(Path("/empty.json"), "   ") == []

    def test_non_dict_top_level_falls_back(self):
        """A JSON array at top level should fall back to whole-file chunk."""
        chunker = StructuredChunker()
        content = json.dumps([1, 2, 3])
        chunks = chunker.chunk_file(Path("/array.json"), content)
        assert len(chunks) == 1

    def test_recursive_mode_splits_nested_dicts(self):
        chunker = StructuredChunker(mode="recursive", max_chunk_tokens=10)
        data = {
            "a": {"x": "value_x " * 20, "y": "value_y " * 20},
            "b": "short",
        }
        content = json.dumps(data, indent=2)
        chunks = chunker.chunk_file(Path("/nested.json"), content)
        # "b" is small enough for one chunk; "a" should be split into x and y
        assert len(chunks) >= 3
        hierarchies = [c.metadata.heading_hierarchy for c in chunks]
        assert any("x" in h for h in hierarchies)
        assert any("y" in h for h in hierarchies)

    def test_large_value_splitting_original_mode(self):
        chunker = StructuredChunker(mode="original", max_chunk_tokens=10)
        # Create a large section that exceeds max tokens (10 tokens * 3 chars = 30 chars)
        data = {"big": "x" * 200}
        content = json.dumps(data, indent=2)
        chunks = chunker.chunk_file(Path("/big.json"), content)
        assert len(chunks) >= 1

    def test_supported_extensions(self):
        chunker = StructuredChunker()
        exts = chunker.supported_extensions()
        assert ".json" in exts
        assert ".yaml" in exts
        assert ".yml" in exts
        assert ".toml" in exts


# ===================================================================
# ChunkerRegistry
# ===================================================================

class TestChunkerRegistry:
    """Tests for chunking/registry.py."""

    @pytest.fixture
    def registry(self):
        from memtomem.chunking.python_code import PythonChunker
        from memtomem.chunking.javascript import JavaScriptChunker
        from memtomem.chunking.markdown import MarkdownChunker

        return ChunkerRegistry([
            PythonChunker(),
            JavaScriptChunker(),
            StructuredChunker(),
            MarkdownChunker(),
        ])

    def test_get_python(self, registry):
        from memtomem.chunking.python_code import PythonChunker
        assert isinstance(registry.get(".py"), PythonChunker)

    def test_get_unknown_returns_none(self, registry):
        assert registry.get(".xyz") is None
        assert registry.get(".rs") is None

    def test_supported_extensions_includes_all(self, registry):
        exts = registry.supported_extensions()
        assert ".py" in exts
        assert ".js" in exts
        assert ".json" in exts
        assert ".md" in exts

    def test_chunk_file_dispatches_to_correct_chunker(self, registry):
        md_content = "# Title\n\nSome text."
        chunks = registry.chunk_file(Path("/readme.md"), md_content)
        assert len(chunks) >= 1
        assert "Title" in chunks[0].content or "Some text" in chunks[0].content

    def test_chunk_file_unknown_extension_returns_empty(self, registry):
        assert registry.chunk_file(Path("/data.csv"), "a,b,c") == []


# ===================================================================
# Indexing: hasher
# ===================================================================

class TestContentHash:
    """Tests for indexing/hasher.py."""

    def test_consistent_hash(self):
        """Same input always produces the same hash."""
        assert content_hash("hello") == content_hash("hello")

    def test_different_inputs_differ(self):
        assert content_hash("hello") != content_hash("world")

    def test_hash_is_hex_sha256(self):
        h = content_hash("test")
        assert len(h) == 64  # SHA-256 hex digest is 64 chars
        assert all(c in "0123456789abcdef" for c in h)

    def test_file_hash(self, tmp_path):
        p = tmp_path / "sample.txt"
        p.write_text("hello world")
        h = file_hash(str(p))
        assert len(h) == 64


# ===================================================================
# Indexing: differ
# ===================================================================

class TestComputeDiff:
    """Tests for indexing/differ.py."""

    def _make_indexed_chunk(self, content: str, chunk_id: str | None = None) -> Chunk:
        """Create a chunk with a predictable content_hash."""
        c = make_chunk(content=content)
        if chunk_id is not None:
            from uuid import UUID
            c.id = UUID(chunk_id)
        return c

    def test_all_new_chunks(self):
        """No existing hashes means everything is new."""
        c1 = make_chunk("new content 1")
        c2 = make_chunk("new content 2")
        diff = compute_diff({}, [c1, c2])
        assert len(diff.to_upsert) == 2
        assert len(diff.to_delete) == 0
        assert len(diff.unchanged) == 0

    def test_all_unchanged(self):
        """When all hashes match, nothing to upsert or delete."""
        c1 = make_chunk("same content")
        existing = {str(c1.id): c1.content_hash}
        # Re-create chunk with same content_hash but new id
        c1_new = Chunk(
            content="same content",
            metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
            content_hash=c1.content_hash,
        )
        diff = compute_diff(existing, [c1_new])
        assert len(diff.unchanged) == 1
        assert len(diff.to_upsert) == 0
        # The unchanged chunk should have the existing ID reused
        assert diff.unchanged[0].id == c1.id

    def test_deleted_chunks_detected(self):
        """Existing chunks not in new set should be marked for deletion."""
        old_id = str(uuid4())
        existing = {old_id: "old_hash_abc"}
        diff = compute_diff(existing, [])
        assert len(diff.to_delete) == 1
        assert str(diff.to_delete[0]) == old_id

    def test_mixed_upsert_delete_unchanged(self):
        """A realistic scenario with some new, some deleted, some unchanged."""
        # Existing: chunk A (hash_a) and chunk B (hash_b)
        id_a = str(uuid4())
        id_b = str(uuid4())
        existing = {id_a: "hash_a", id_b: "hash_b"}

        # New chunks: chunk with hash_a (unchanged) and chunk C (new)
        chunk_same = Chunk(
            content="content_a",
            metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
            content_hash="hash_a",
        )
        chunk_new = make_chunk("brand new content")

        diff = compute_diff(existing, [chunk_same, chunk_new])
        assert len(diff.unchanged) == 1
        assert len(diff.to_upsert) == 1
        assert len(diff.to_delete) == 1  # hash_b is gone
        # Unchanged chunk gets the old ID
        from uuid import UUID
        assert diff.unchanged[0].id == UUID(id_a)
        # Deleted is chunk B
        assert str(diff.to_delete[0]) == id_b
