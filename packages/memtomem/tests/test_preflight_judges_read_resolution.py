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
from memtomem.indexing.engine import PrivacyRejection
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.storage import sqlite_helpers
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD, RELINKED_HOLD
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


async def _hits(comp, query: str, *, project: Path | None = None) -> list[str]:
    """Search hits for ``query``; ``project`` is the root whose project tiers are in view.

    A ``project_shared`` chunk is returned only to a search made from its
    project, so a test about such a chunk has to pass it: without it the search
    is empty whether or not anything is held.
    """
    comp.search_pipeline.invalidate_cache()
    results, _ = await comp.search_pipeline.search(query, top_k=10, project_context_root=project)
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
    proj = tmp_path / "proj"
    shared = await _indexed_doc_then_label(comp, _register(comp, proj))
    target = mem_dir / "target.md"
    target.write_text(_CLEAN_B, encoding="utf-8")
    pre, source_path = _read(shared)
    read_key = norm_path(shared)
    assert len(await _hits(comp, "Charlieword", project=proj)) == 1
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
    assert await _hits(comp, "Charlieword", project=proj) == []
    # The judge refused, so the relinked hold has nothing to add.
    assert await engine.hold_relinked_mutation(shared, preflight) is False
    assert _holds(comp) == {read_key: REDACTION_BLOCKED_HOLD}


async def test_a_relinked_registered_directory_holds_the_read_source(
    bm25_only_components, tmp_path
):
    """The judge reads the bytes as user scope, and the source is held all the same (#3291)."""
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    proj = tmp_path / "proj"
    pdir = _register(comp, proj)
    shared = await _indexed_doc_then_label(comp, pdir)
    pre, source_path = _read(shared)
    read_key = norm_path(shared)
    assert len(await _hits(comp, "Charlieword", project=proj)) == 1
    moved = tmp_path / "moved-memories"
    pdir.rename(moved)
    try:
        pdir.symlink_to(mem_dir, target_is_directory=True)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")

    preflight = engine.preflight_mutation(shared, pre, source_path=source_path)

    # The registered root resolves into the user directory now, so the read's
    # path is under no project root and the declared exemption is honoured.
    assert preflight.rejection is None
    assert preflight.relinked is True
    assert preflight.source_key == read_key
    assert _RELINKED in (preflight.blocker("add", shared.name, needs_valve=False) or "")
    assert _holds(comp) == {}
    assert await engine.hold_relinked_mutation(shared, preflight) is True
    assert _holds(comp) == {read_key: RELINKED_HOLD}
    assert await _hits(comp, "Charlieword", project=proj) == []

    # Put back, the next index of the file judges it in its own scope and the
    # hold becomes the redaction one.
    pdir.unlink()
    moved.rename(pdir)
    with pytest.raises(PrivacyRejection) as refused:
        await engine.index_file(shared)
    assert refused.value.decision == "blocked_project_shared"
    assert _holds(comp) == {read_key: REDACTION_BLOCKED_HOLD}
    assert await _hits(comp, "Charlieword", project=proj) == []


async def test_relinked_unindexed_file_is_refused_with_nothing_held(bm25_only_components, tmp_path):
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
    # No chunks are stored for the file, so there is nothing to hold.
    assert await comp.index_engine.hold_relinked_mutation(alpha, preflight) is False
    assert _holds(comp) == {}


async def test_relinked_indexed_clean_file_is_held_under_the_read_key(
    bm25_only_components, tmp_path
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _two_clean_files(mem_dir)
    for path in (alpha, bravo):
        stats = await comp.index_engine.index_file(path, ingress_guarded=True)
        assert stats.indexed_chunks == 1, stats
    pre, source_path = _read(alpha)
    read_key = norm_path(alpha)
    _relink_away(alpha, bravo, tmp_path)

    preflight = comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert preflight.rejection is None
    assert preflight.relinked is True
    assert await comp.index_engine.hold_relinked_mutation(alpha, preflight) is True
    # The read's key, as stored: resolved again it would be bravo.md's.
    assert _holds(comp) == {read_key: RELINKED_HOLD}
    assert await _hits(comp, "Alphaword") == []
    assert await _hits(comp, "Bravoword")


async def test_holding_needs_a_preflight_that_found_the_path_relinked(bm25_only_components):
    """Like ``refuse_mutation`` without a rejection: a caller that asks for it unasked is wrong."""
    comp, mem_dir = bm25_only_components
    alpha, _ = _two_clean_files(mem_dir)
    stats = await comp.index_engine.index_file(alpha, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    pre, source_path = _read(alpha)

    preflight = comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert preflight.relinked is False
    with pytest.raises(ValueError, match="relinked"):
        await comp.index_engine.hold_relinked_mutation(alpha, preflight)
    assert _holds(comp) == {}
    assert await _hits(comp, "Alphaword")


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


async def test_the_gate_and_both_twins_freeze_the_scope(bm25_only_components, monkeypatch):
    """The index gate, the doctor's preview and the preflight all judge a path as given (#3290).

    Each makes one scope lookup: the index pass hands its projection to the
    chunker, which then looks nothing up.
    """
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
    stats = await comp.index_engine.index_file(alpha)
    assert stats.indexed_chunks == 1, stats
    assert seen == [True]
    comp.index_engine.preview_redaction_decision(alpha, _CLEAN_A)
    pre, source_path = _read(alpha)
    comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert seen == [True, True, True]


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


async def test_a_retry_after_the_path_is_put_back_lifts_the_hold(
    bm25_only_components, tmp_path, monkeypatch
):
    """The relinked hold is not sticky: the retried write indexes the file clean and releases it."""
    comp, mem_dir = bm25_only_components
    alpha, bravo = _two_clean_files(mem_dir)
    stats = await comp.index_engine.index_file(alpha, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    read_key = norm_path(alpha)
    calls = _relink_before_the_preflight(monkeypatch, comp.index_engine, alpha, bravo, tmp_path)
    ctx = StubCtx(AppContext.from_components(comp))

    refused = await memory_crud.mem_add(content="gamma entry", file=str(alpha), ctx=ctx)  # type: ignore[arg-type]

    assert calls and _RELINKED in refused, refused
    assert _holds(comp) == {read_key: RELINKED_HOLD}
    assert await _hits(comp, "Alphaword") == []

    alpha.unlink()
    (tmp_path / "moved-alpha.md").rename(alpha)
    retried = await memory_crud.mem_add(content="gamma entry", file=str(alpha), ctx=ctx)  # type: ignore[arg-type]

    assert retried.startswith("Memory added"), retried
    assert _holds(comp) == {}
    assert await _hits(comp, "Alphaword")


async def test_a_hold_that_fails_does_not_replace_the_refusal(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    alpha, bravo = _two_clean_files(mem_dir)
    stats = await comp.index_engine.index_file(alpha, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    calls = _relink_before_the_preflight(monkeypatch, comp.index_engine, alpha, bravo, tmp_path)

    async def fail(*_args, **_kwargs):
        raise RuntimeError("the writer is gone")

    monkeypatch.setattr(comp.index_engine, "_hold_waiting", fail)

    result = await memory_crud.mem_add(  # type: ignore[arg-type]
        content="gamma entry", file=str(alpha), ctx=StubCtx(AppContext.from_components(comp))
    )

    assert calls and _RELINKED in result, result
    assert _holds(comp) == {}
    assert bravo.read_text(encoding="utf-8") == _CLEAN_B


async def test_rows_stored_under_a_key_from_before_folding_are_not_held(
    bm25_only_components, tmp_path, monkeypatch
):
    """The hold uses the folded key; a legacy unfolded key holds nothing and stays served (#2907).

    Folding is off while the file is indexed, so its rows are stored under the
    NFD spelling as they were before folding existed, and on for the preflight.
    """
    comp, mem_dir = bm25_only_components
    monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", False)
    leaf = mem_dir / unicodedata.normalize("NFD", "café.md")
    leaf.write_text(_CLEAN_A, encoding="utf-8")
    bravo = mem_dir / "bravo.md"
    bravo.write_text(_CLEAN_B, encoding="utf-8")
    stats = await comp.index_engine.index_file(leaf, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    stored_key = norm_path(leaf)
    monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
    assert norm_path(leaf) != stored_key, "the folded key must differ from the stored one"
    pre, source_path = _read(leaf)
    _relink_away(leaf, bravo, tmp_path)

    preflight = comp.index_engine.preflight_mutation(leaf, pre, source_path=source_path)

    assert preflight.relinked is True
    assert await comp.index_engine.hold_relinked_mutation(leaf, preflight) is False
    assert _holds(comp) == {}
    assert await _hits(comp, "Alphaword")


async def test_an_indexed_file_whose_resolution_fails_is_held(
    bm25_only_components, tmp_path, monkeypatch
):
    """A path that cannot be resolved cannot be vouched for either, so it is held like a relink."""
    comp, mem_dir = bm25_only_components
    alpha, _ = _two_clean_files(mem_dir)
    stats = await comp.index_engine.index_file(alpha, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    pre, source_path = _read(alpha)
    read_key = norm_path(alpha)

    def fail(_p):
        raise OSError("unresolvable")

    monkeypatch.setattr("memtomem.indexing.engine.resolve_path", fail)
    preflight = comp.index_engine.preflight_mutation(alpha, pre, source_path=source_path)

    assert preflight.relinked is True
    assert await comp.index_engine.hold_relinked_mutation(alpha, preflight) is True
    assert _holds(comp) == {read_key: RELINKED_HOLD}
