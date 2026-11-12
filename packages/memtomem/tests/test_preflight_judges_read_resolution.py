"""A memory tool's preflight judges the bytes under the path they were read from (#3191).

A tool reads the file and resolves its path in one worker call
(``read_with_source_key``), then hands both to ``IndexEngine.preflight_mutation``
in another. The preflight used to resolve the path again to choose the scope,
the exemption and the masking manifest entry, so a path replaced by a symlink
in between had the read's bytes judged under the link target's scope: a
``project_shared`` file whose declared exemption is refused there passed as a
user-scope file.

The preflight now judges under the read's path and resolves nothing again, and
when the path no longer resolves to it, the write is refused: it would reach a
file nobody judged. Bytes the gate refuses are still refused and held under the
read's key first (#3183).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.storage import sqlite_helpers
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_helpers import fold_path_form, norm_path
from memtomem.tools import memory_writer
from memtomem.tools.memory_writer import read_pre_image_or_empty, read_with_source_key
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

# Indexes clean anywhere. With ``_LABEL_HIT`` appended it hits only the label rule a
# ``documents-patterns`` declaration covers, so the file is ``exempted`` in user
# scope and ``blocked_project_shared`` in a project.
_EXEMPT_HEAD = "---\nredaction: documents-patterns\n---\n\n## Patterns\n\nCharlieword notes\n"
_LABEL_HIT = "\npassword: hunter2\n"
_EXEMPT_DOC = _EXEMPT_HEAD + _LABEL_HIT
_CLEAN_A = "## A\n\nAlphaword entry about deploys\n"
_CLEAN_B = "## B\n\nBravoword entry about reviews\n"
_RELINKED = "now resolves to a different location than when it was read"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _link_or_skip(link: Path, target: Path) -> None:
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")


def _register(comp, root: Path) -> Path:
    pdir = root / ".memtomem" / "memories"
    pdir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [pdir]
    return pdir


def _holds(comp) -> dict[str, str]:
    rows = comp.storage._get_db().execute("SELECT source_file, reason FROM held_sources")
    return {row[0]: row[1] for row in rows.fetchall()}


def _read(path: Path) -> tuple[bytes, Path]:
    return read_with_source_key(read_pre_image_or_empty, path)


async def _indexed_doc_then_label(comp, pdir: Path) -> Path:
    """``patterns.md`` indexed clean, then given a label hit no tool scanned."""
    shared = pdir / "patterns.md"
    shared.write_text(_EXEMPT_HEAD, encoding="utf-8")
    stats = await comp.index_engine.index_file(shared, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    with shared.open("a", encoding="utf-8") as fh:
        fh.write(_LABEL_HIT)
    return shared


async def _hits(comp, query: str) -> list[str]:
    comp.search_pipeline.invalidate_cache()
    results, _ = await comp.search_pipeline.search(query, top_k=10)
    return [r.chunk.content for r in results]


def _shared_doc_and_user_target(comp, tmp_path: Path, mem_dir: Path) -> tuple[Path, Path]:
    pdir = _register(comp, tmp_path / "proj")
    shared = pdir / "patterns.md"
    shared.write_text(_EXEMPT_DOC, encoding="utf-8")
    target = mem_dir / "target.md"
    target.write_text(_CLEAN_B, encoding="utf-8")
    return shared, target


def _relink_away(leaf: Path, target: Path, tmp_path: Path) -> None:
    """Move ``leaf``'s file aside and put a link to ``target`` at its path."""
    leaf.rename(tmp_path / f"moved-{leaf.name}")
    _link_or_skip(leaf, target)


async def test_relinked_shared_doc_is_refused_and_held_under_the_read_key(
    bm25_only_components, tmp_path
):
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    shared = await _indexed_doc_then_label(comp, _register(comp, tmp_path / "proj"))
    target = mem_dir / "target.md"
    target.write_text(_CLEAN_B, encoding="utf-8")
    pre, source_path = _read(shared)
    read_key = norm_path(shared)
    _relink_away(shared, target, tmp_path)

    preflight = engine.preflight_mutation(shared, pre, source_path=source_path)

    # Before #3191 this was ``None``: the bytes were judged under target.md's
    # user scope, where the declared exemption is honoured.
    assert preflight.rejection is not None
    assert preflight.rejection.decision == "blocked_project_shared"
    assert preflight.rejection.source_key == read_key
    assert preflight.relinked is True
    rejection = await engine.refuse_mutation(shared, preflight)
    assert rejection.held is True
    assert _holds(comp) == {read_key: REDACTION_BLOCKED_HOLD}
    assert await _hits(comp, "Charlieword") == []


async def test_relinked_clean_file_is_refused_with_nothing_held(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    alpha = mem_dir / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo = mem_dir / "bravo.md"
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    pre, source_path = _read(alpha)
    _relink_away(alpha, bravo, tmp_path)

    preflight = comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert preflight.rejection is None
    assert preflight.relinked is True
    blocker = preflight.blocker("add", alpha.name, needs_valve=False)
    assert blocker is not None and _RELINKED in blocker, blocker
    assert _holds(comp) == {}


@pytest.mark.parametrize("shape", ["empty", "missing"])
async def test_relinked_empty_or_missing_file_is_refused(bm25_only_components, tmp_path, shape):
    """The empty-file fast return comes after the relink check."""
    comp, mem_dir = bm25_only_components
    leaf = mem_dir / "new.md"
    if shape == "empty":
        leaf.write_text("", encoding="utf-8")
    bravo = mem_dir / "bravo.md"
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    pre, source_path = _read(leaf)
    assert pre == b""
    _link_or_skip(leaf, bravo)

    preflight = comp.index_engine.preflight_mutation(leaf, pre, source_path=source_path)

    assert preflight.rejection is None
    assert preflight.relinked is True
    assert _RELINKED in (preflight.blocker("add", leaf.name, needs_valve=False) or "")


async def test_a_path_that_was_already_a_link_is_judged_through_it(bm25_only_components, tmp_path):
    """A symlinked memory file (dotfiles) is not a relink: it is judged as its target."""
    comp, mem_dir = bm25_only_components
    shared, _ = _shared_doc_and_user_target(comp, tmp_path, mem_dir)
    link = mem_dir / "link.md"
    _link_or_skip(link, shared)
    pre, source_path = _read(link)

    preflight = comp.index_engine.preflight_mutation(link, pre, source_path=source_path)

    assert preflight.relinked is False
    assert preflight.rejection is not None
    assert preflight.rejection.decision == "blocked_project_shared"
    assert preflight.rejection.source_key == norm_path(shared)


async def test_the_read_path_is_compared_unfolded(bm25_only_components, tmp_path, monkeypatch):
    """A root spelled NFD still matches the read path; the folded key would not.

    Folding is switched on for every OS, so this simulates the stored key's
    NFC folding on macOS without relying on the filesystem treating the two
    spellings as one.
    """
    monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
    comp, mem_dir = bm25_only_components
    root = tmp_path / unicodedata.normalize("NFD", "café")
    shared = await _indexed_doc_then_label(comp, _register(comp, root))
    target = mem_dir / "target.md"
    target.write_text(_CLEAN_B, encoding="utf-8")
    pre, source_path = _read(shared)
    read_key = norm_path(shared)
    assert read_key != str(source_path), "the key must be folded and the path not"
    _relink_away(shared, target, tmp_path)

    preflight = comp.index_engine.preflight_mutation(shared, pre, source_path=source_path)

    assert preflight.rejection is not None
    assert preflight.rejection.decision == "blocked_project_shared"
    await comp.index_engine.refuse_mutation(shared, preflight)
    assert _holds(comp) == {read_key: REDACTION_BLOCKED_HOLD}


async def test_the_manifest_entry_is_the_read_paths(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    masked = mem_dir / "masked.md"
    text = "## A\n\npassword: hunter2\n"
    masked.write_text(text, encoding="utf-8")
    other = mem_dir / "other.md"
    other.write_text(_CLEAN_B, encoding="utf-8")
    manifest = tmp_path / "mask.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    str(masked.resolve()): {
                        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "spans": [[3, 3]],
                    }
                },
            }
        )
    )
    comp.index_engine._config.index_masking_manifest_path = str(manifest)
    pre, source_path = _read(masked)
    _relink_away(masked, other, tmp_path)

    preflight = comp.index_engine.preflight_mutation(masked, pre, source_path=source_path)

    # Looked up through the link, the entry would be other.md's (none), the
    # label hit would go unmasked, and the bytes would read as blocked.
    assert preflight.rejection is None
    assert preflight.masking_locked is True
    assert preflight.relinked is True


def test_a_read_whose_resolution_fails_keeps_the_given_path(tmp_path, monkeypatch):
    path = tmp_path / "note.md"
    path.write_bytes(_CLEAN_A.encode())  # bytes compared below: no CRLF on Windows

    def fail(_p):
        raise OSError("unresolvable")

    monkeypatch.setattr(memory_writer, "resolve_path", fail)
    monkeypatch.setattr(sqlite_helpers, "resolve_path", fail)

    data, source_path = read_with_source_key(read_pre_image_or_empty, path)

    assert data == _CLEAN_A.encode()
    assert source_path == path
    assert fold_path_form(str(source_path)) == norm_path(path)


async def test_a_preflight_whose_resolution_fails_refuses(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha = mem_dir / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    pre, source_path = _read(alpha)

    def fail(_p):
        raise OSError("unresolvable")

    monkeypatch.setattr("memtomem.indexing.engine.resolve_path", fail)
    preflight = comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert preflight.rejection is None
    assert preflight.relinked is True
    assert _holds(comp) == {}


async def test_only_the_preflight_freezes_the_scope(bm25_only_components, monkeypatch):
    """The doctor's preview keeps resolving the way the index gate does."""
    import memtomem.indexing.engine as engine_mod

    comp, mem_dir = bm25_only_components
    alpha = mem_dir / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    seen: list[bool] = []
    real = engine_mod.classify_scope

    def spy(path, dirs=None, **kwargs):
        seen.append(kwargs.get("resolved", False))
        return real(path, dirs, **kwargs)

    monkeypatch.setattr(engine_mod, "classify_scope", spy)
    comp.index_engine.preview_redaction_decision(alpha, _CLEAN_A)
    pre, source_path = _read(alpha)
    comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert seen == [False, True]


def _relink_before_the_preflight(monkeypatch, engine, leaf: Path, target: Path, tmp_path: Path):
    real = engine.preflight_mutation
    calls: list[Path] = []

    def preflight(file_path, pre_image, **kwargs):
        if not calls:
            _relink_away(leaf, target, tmp_path)
        calls.append(file_path)
        return real(file_path, pre_image, **kwargs)

    monkeypatch.setattr(engine, "preflight_mutation", preflight)
    return calls


def _two_clean_files(mem_dir: Path) -> tuple[Path, Path]:
    alpha = mem_dir / "alpha.md"
    alpha.write_text(_CLEAN_A, encoding="utf-8")
    bravo = mem_dir / "bravo.md"
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    return alpha, bravo


async def test_mem_add_writes_nothing_through_a_relinked_path(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _two_clean_files(mem_dir)
    calls = _relink_before_the_preflight(monkeypatch, comp.index_engine, alpha, bravo, tmp_path)

    result = await memory_crud.mem_add(  # type: ignore[arg-type]
        content="gamma entry", file=str(alpha), ctx=StubCtx(AppContext.from_components(comp))
    )

    assert calls, "the preflight never ran, so nothing was relinked"
    assert _RELINKED in result, result
    assert bravo.read_text(encoding="utf-8") == _CLEAN_B
    assert (tmp_path / "moved-alpha.md").read_text(encoding="utf-8") == _CLEAN_A


async def test_web_add_writes_nothing_through_a_relinked_path(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _two_clean_files(mem_dir)
    calls = _relink_before_the_preflight(monkeypatch, comp.index_engine, alpha, bravo, tmp_path)
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.state.csrf_enforce = False
    app.dependency_overrides[require_configured] = lambda: None
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/add", json={"content": "gamma", "file": alpha.name})

    assert calls, "the preflight never ran, so nothing was relinked"
    assert resp.status_code == 409, resp.text
    body = resp.json()["detail"]
    assert body["detail"] == "index_blocked", body
    assert body["written"] is False, body
    assert _RELINKED in body["message"], body
    assert bravo.read_text(encoding="utf-8") == _CLEAN_B
    assert (tmp_path / "moved-alpha.md").read_text(encoding="utf-8") == _CLEAN_A
