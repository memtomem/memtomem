"""Every id read on a caller-facing surface opts into the redaction screen (#2781).

``get_chunk`` and ``get_chunks_batch`` withhold a redaction-held chunk only
when the caller passes ``exclude_withheld=True`` (#2777). The default stays
``False`` because storage-internal callers (re-index, rollback, cleanup) must
see every row. That makes the screen opt-in, so a new MCP tool, web route, CLI
command or integration that calls plain ``get_chunk`` would serve held content
with nothing failing. This test makes it fail.

Every reference to either method under the surface directories must be a call
that passes the literal ``exclude_withheld=True``, or be listed in
``_ALLOWED`` with the number of times it occurs and why it is safe. Any
reference counts, not only calls: binding ``storage.get_chunk`` to a local and
calling that would otherwise walk past the check. The allowlist is pinned in
both directions — an entry whose count no longer matches fails too, so a
second copy of an allowed shape cannot ride on the first one's reason.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import memtomem

_SRC = Path(memtomem.__file__).parent
_SURFACES = ("server/tools", "web/routes", "cli", "integrations")
_METHODS = frozenset({"get_chunk", "get_chunks_batch"})

#: ``(module relative to the package, enclosing function, method) -> count``,
#: with the reason each shape may read a withheld row.
_ALLOWED: dict[tuple[str, str, str], int] = {
    # Tells a deleted relation target from a live one; nothing from this read
    # is rendered. The second read, which is rendered, passes the screen.
    ("server/tools/cross_ref.py", "mem_related", "get_chunk"): 1,
    # Write gates only, nothing returned: the first reads the group's scopes and
    # sources before the summary is published; the second re-reads the sources
    # under the transaction that writes the edges and the decay. Each refuses
    # when a chunk's source is held, so held rows must be found, not screened.
    ("server/tools/consolidation.py", "mem_consolidate_apply", "get_chunks_batch"): 2,
    # Post-delete cleanup: checks that the index row is gone and deletes it if
    # not. A held row must be found here, or it would survive the delete.
    ("web/routes/chunks.py", "delete_chunk.ensure_index_row_absent", "get_chunk"): 2,
}


def _passes_screen(call: ast.Call) -> bool:
    return any(
        kw.arg == "exclude_withheld"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


def _unscreened_references(src: Path = _SRC) -> Counter[tuple[str, str, str]]:
    found: Counter[tuple[str, str, str]] = Counter()
    for surface in _SURFACES:
        for path in sorted((src / surface).rglob("*.py")):
            rel = path.relative_to(src).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)

            def check(node: ast.AST, scope: tuple[str, ...]) -> None:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    scope = (*scope, node.name)
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _METHODS
                    and _passes_screen(node)
                ):
                    # Screened: the method name itself is fine, but its
                    # receiver and every argument are checked like any node.
                    for part in (node.func.value, *node.args, *(k.value for k in node.keywords)):
                        check(part, scope)
                    return
                if isinstance(node, ast.Attribute) and node.attr in _METHODS:
                    found[(rel, ".".join(scope), node.attr)] += 1
                if isinstance(node, ast.Constant) and node.value in _METHODS:
                    # ``getattr(storage, "get_chunk")`` reaches the same method.
                    found[(rel, ".".join(scope), str(node.value))] += 1
                for child in ast.iter_child_nodes(node):
                    check(child, scope)

            check(tree, ())
    return found


def test_every_unscreened_id_read_is_allowlisted_with_its_count():
    found = _unscreened_references()
    unlisted = {key: n for key, n in found.items() if key not in _ALLOWED}
    assert not unlisted, (
        "id read without exclude_withheld=True on a caller-facing surface: "
        f"{unlisted}. Pass exclude_withheld=True, or add the shape to _ALLOWED "
        "with its count and the reason it may read a redaction-held row."
    )
    miscounted = {key: (found.get(key, 0), n) for key, n in _ALLOWED.items() if found[key] != n}
    assert not miscounted, (
        f"_ALLOWED out of date, (found, listed): {miscounted}. Remove entries "
        "that no longer occur; a new copy of an allowed shape needs its own review."
    )


def test_the_scanner_sees_every_shape_it_claims_to(tmp_path):
    """The guard is only as good as its walk: pin each shape it must report."""
    pkg = tmp_path / "pkg"
    (pkg / "web" / "routes").mkdir(parents=True)
    (pkg / "web" / "routes" / "probe.py").write_text(
        "async def plain(storage, cid):\n"
        "    return await storage.get_chunk(cid)\n"
        "async def false_flag(storage, cid):\n"
        "    return await storage.get_chunk(cid, exclude_withheld=False)\n"
        "async def computed(storage, cid, flag):\n"
        "    return await storage.get_chunk(cid, exclude_withheld=flag)\n"
        "async def aliased(storage, cid):\n"
        "    read = storage.get_chunks_batch\n"
        "    return await read([cid])\n"
        "async def by_name(storage, cid):\n"
        "    return await getattr(storage, 'get_chunk')(cid)\n"
        "async def screened(storage, cid):\n"
        "    return await storage.get_chunk(cid, exclude_withheld=True)\n"
        "async def nested_arg(storage, cid):\n"
        "    return await storage.get_chunk(await storage.get_chunk(cid), exclude_withheld=True)\n"
        "async def in_receiver(storage, cid, choose):\n"
        "    store = await choose(storage.get_chunk)\n"
        "    return await (await choose(storage.get_chunks_batch)).get_chunk(\n"
        "        cid, exclude_withheld=True\n"
        "    )\n"
        "async def arg_root(storage, cid):\n"
        "    return await storage.get_chunk(storage.get_chunks_batch, exclude_withheld=True)\n",
        encoding="utf-8",
    )
    found = _unscreened_references(pkg)
    rel = "web/routes/probe.py"
    assert found == Counter(
        {
            (rel, "plain", "get_chunk"): 1,
            (rel, "false_flag", "get_chunk"): 1,
            (rel, "computed", "get_chunk"): 1,
            (rel, "aliased", "get_chunks_batch"): 1,
            (rel, "by_name", "get_chunk"): 1,
            (rel, "nested_arg", "get_chunk"): 1,
            (rel, "in_receiver", "get_chunk"): 1,
            (rel, "in_receiver", "get_chunks_batch"): 1,
            (rel, "arg_root", "get_chunks_batch"): 1,
        }
    )
