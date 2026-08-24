"""Every ``upsert_chunks`` caller states whether it owes entity extraction.

#2145 wired entity extraction into the indexing engine's chunk-write path, which
was correct and incomplete: two writers reach ``storage.upsert_chunks`` without
going through the engine, so the chunks they store never had entities written.

The reason this is a guard and not a fixed list: #2145's own scope was written
from a hand-made survey of bypass writers, and that survey was wrong: most of
the sites it listed turned out to be tag-only rewrites with nothing to do, and
it described import as corrupting existing entities when the conflict branch it
blamed matches on ``content_hash`` and changes no content at all. A list checked
against itself certifies nothing, so the source is the
authority here. A new writer that calls ``upsert_chunks`` and appears in neither
registry fails this test, forcing an explicit decision instead of a silent hole
in ``chunk_entities``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import memtomem

_SRC = Path(memtomem.__file__).parent

# Writers that store new or changed chunk *content*. Each must reach
# ``sync_entities_for_chunks`` in the same function — directly, or (for the
# engine) through the ``_extract_entities_for`` delegate that carries the config
# knob.
_CONTENT_WRITERS: dict[tuple[str, str], str] = {
    ("indexing/engine.py", "IndexEngine._index_file"): (
        "The indexer. Extracts for diff_result.to_upsert inside the chunk-write "
        "transaction, via _extract_entities_for."
    ),
    ("tools/export_import.py", "import_chunks"): (
        "Imported bundle chunks never pass through the engine. Syncs the rows it "
        "genuinely adds; hash-matched rows keep the entities they already have."
    ),
    ("tools/consolidation_engine.py", "apply_consolidation"): (
        "A synthesised summary chunk, virtual, never indexed from disk."
    ),
}

# Writers that rewrite metadata on chunks whose ``content`` and ``content_hash``
# pass through untouched. Their stored entities still describe the stored
# content, so extraction here would be wasted work — not an oversight.
_METADATA_ONLY: dict[tuple[str, str], str] = {
    ("tools/auto_tag.py", "auto_tag_storage"): "Replaces metadata.tags only.",
    (
        "search/dedup.py",
        "DedupScanner.merge",
    ): "Unions tags onto the kept chunk; losers are deleted.",
    ("services/tag_management.py", "replace_chunk_tags"): "Tag rename.",
    ("server/tools/importers.py", "mem_import_notion"): (
        "Tag pass after index_file already indexed the written files."
    ),
    ("server/tools/importers.py", "mem_import_obsidian"): (
        "Tag pass after index_file already indexed the written files."
    ),
    ("server/tools/url_index.py", "mem_fetch"): "Tag pass after index_file.",
    ("cli/memory.py", "_add"): "Tag pass after index_file.",
    ("cli/ingest_cmd.py", "_apply_tags"): "Tag merge helper; content untouched.",
    ("web/routes/system.py", "add_memory"): "Tag pass after index_file.",
}

_SYNC_CALLS = frozenset({"sync_entities_for_chunks", "_extract_entities_for"})


def _qualified_functions(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    """``(qualname, node)`` for every function, class-qualified.

    Qualified because two classes in one module can both define ``merge`` — a
    bare function name would let one class's exemption silently cover the
    other's writer.
    """
    out: list[tuple[str, ast.AST]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f"{prefix}{child.name}", child))
                walk(child, f"{prefix}{child.name}.")

    walk(tree, "")
    return out


def _own_body(fn: ast.AST):
    """Nodes belonging to ``fn`` itself, stopping at nested function boundaries.

    A call parked inside a nested ``def`` that nobody invokes would otherwise be
    attributed to the enclosing function. ``ast.walk`` cannot express this — it
    keeps descending past the nested ``def`` it was asked to skip — so this is an
    explicit non-descending traversal.
    """
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        # Stop *at* the boundary: a nested function is neither yielded nor
        # descended into. Filtering children instead of the node itself is the
        # subtle version of this bug — the nested ``def`` gets skipped while its
        # body is still walked.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _awaited_sync_calls(fn: ast.AST) -> set[str]:
    """Names of entity-sync calls this function actually awaits.

    Requires ``await``: these are coroutines, so an un-awaited call writes
    nothing and would otherwise read as compliance.
    """
    nested: set[int] = set()
    for stmt in fn.body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not fn:
                for inner in ast.walk(node):
                    nested.add(id(inner))

    found: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Await) or id(node) in nested:
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        # Accept only the two shapes that actually write entities: the module
        # function called by name, or the engine's own delegate on ``self``.
        # Matching any receiver would let ``await other.sync_entities_for_chunks()``
        # — a same-named method on an unrelated object — read as compliance.
        if isinstance(call.func, ast.Name):
            if call.func.id in _SYNC_CALLS:
                found.add(call.func.id)
        elif isinstance(call.func, ast.Attribute):
            if call.func.attr in _SYNC_CALLS and isinstance(call.func.value, ast.Name):
                if call.func.value.id == "self":
                    found.add(call.func.attr)
    return found


def _upsert_call_sites() -> dict[tuple[str, str], ast.AST]:
    """Every ``(relative_path, qualified_function)`` that calls ``upsert_chunks``.

    Built by walking the shipped source, so the guard's scope is whatever the
    tree currently contains rather than whatever a previous author remembered.
    """
    sites: dict[tuple[str, str], ast.AST] = {}
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        # The storage backend implements upsert_chunks; it is not a caller.
        if rel.startswith("storage/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for qualname, fn in _qualified_functions(tree):
            for node in _own_body(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "upsert_chunks"
                ):
                    sites[(rel, qualname)] = fn
                    break
    return sites


class TestEveryChunkWriterClassifiesItsEntityObligation:
    def test_no_unclassified_upsert_chunks_caller(self):
        """A new bypass writer must declare which kind it is."""
        sites = _upsert_call_sites()
        classified = set(_CONTENT_WRITERS) | set(_METADATA_ONLY)
        unclassified = sorted(set(sites) - classified)
        assert not unclassified, (
            "These functions call storage.upsert_chunks but are in neither "
            "_CONTENT_WRITERS nor _METADATA_ONLY. Decide which they are: does "
            "this write new or changed chunk content (then call "
            "sync_entities_for_chunks) or only rewrite metadata (then exempt it "
            f"with a reason)? {unclassified}"
        )

    def test_registries_have_no_stale_entries(self):
        """An entry whose call site is gone is a claim about code that no longer
        exists — it would keep the guard green while covering nothing."""
        sites = set(_upsert_call_sites())
        stale = sorted((set(_CONTENT_WRITERS) | set(_METADATA_ONLY)) - sites)
        assert not stale, f"registry entries no longer call upsert_chunks: {stale}"

    def test_every_content_writer_syncs_entities(self):
        """The obligation itself, not just the classification."""
        sites = _upsert_call_sites()
        missing = []
        for key in _CONTENT_WRITERS:
            fn = sites.get(key)
            if fn is None:
                continue  # covered by the staleness test
            if not _awaited_sync_calls(fn):
                missing.append(key)
        assert not missing, (
            f"declared content writers that never reach sync_entities_for_chunks: {missing}"
        )

    def test_metadata_only_writers_do_not_extract(self):
        """The exemption is a claim that extraction is unnecessary. If one of
        these starts extracting, the claim — and the reason recorded with it —
        needs revisiting rather than quietly becoming false."""
        sites = _upsert_call_sites()
        extracting = [
            key
            for key in _METADATA_ONLY
            if sites.get(key) is not None and _awaited_sync_calls(sites[key])
        ]
        assert not extracting, f"exempt-as-metadata-only writers now extract entities: {extracting}"


class TestGuardHelpersRejectFakeCompliance:
    """The guard's own unit tests.

    A guard is only worth its green tick if the shapes that *look* compliant
    without writing anything are actually rejected. Each case here is a way a
    future edit could satisfy a laxer version of this check — verified against
    the helper directly, so the cases stay readable and do not need a matching
    edit in production code.
    """

    @staticmethod
    def _fn(src: str) -> ast.AST:
        return ast.parse(src).body[0]

    def test_awaited_module_call_is_accepted(self):
        fn = self._fn(
            "async def w(storage, chunks):\n"
            "    await storage.upsert_chunks(chunks)\n"
            "    await sync_entities_for_chunks(storage, chunks)\n"
        )
        assert _awaited_sync_calls(fn) == {"sync_entities_for_chunks"}

    def test_self_delegate_is_accepted(self):
        fn = self._fn("async def w(self, chunks):\n    await self._extract_entities_for(chunks)\n")
        assert _awaited_sync_calls(fn) == {"_extract_entities_for"}

    def test_unawaited_call_is_rejected(self):
        """A coroutine that is never awaited writes nothing."""
        fn = self._fn(
            "async def w(storage, chunks):\n    sync_entities_for_chunks(storage, chunks)\n"
        )
        assert _awaited_sync_calls(fn) == set()

    def test_call_on_an_unrelated_receiver_is_rejected(self):
        """A same-named method on some other object is not this contract."""
        fn = self._fn(
            "async def w(other, chunks):\n    await other.sync_entities_for_chunks(chunks)\n"
        )
        assert _awaited_sync_calls(fn) == set()

    def test_call_parked_in_a_nested_def_is_rejected(self):
        fn = self._fn(
            "async def w(storage, chunks):\n"
            "    async def never():\n"
            "        await sync_entities_for_chunks(storage, chunks)\n"
        )
        assert _awaited_sync_calls(fn) == set()

    def test_upsert_inside_a_nested_def_is_not_attributed_to_the_parent(self):
        """``_own_body`` must stop at the nested boundary — a helper defined
        inside a function is its own call site, not its parent's."""
        fn = self._fn(
            "async def w(storage, chunks):\n"
            "    async def inner():\n"
            "        await storage.upsert_chunks(chunks)\n"
            "    return inner\n"
        )
        calls = [
            n
            for n in _own_body(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "upsert_chunks"
        ]
        assert calls == []
