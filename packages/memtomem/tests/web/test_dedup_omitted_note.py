"""Browser tests: the dedup panel says how many pairs a scan left out (#3418).

A scan leaves out pairs of read-only chunks from one file. Without a note the
panel's "No duplicates found" would read as a clean store, so the count is
shown under both the empty state and the candidate list, follows a language
switch, and is cleared by the next scan or a panel reset.
"""

from __future__ import annotations

import json

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_EN_ONE = (
    "Left out 1 pair of read-only chunks from the same file: they cannot be removed from here."
)
_EN_THREE = (
    "Left out 3 pairs of read-only chunks from the same file: they cannot be removed from here."
)
_KO_THREE = "같은 파일의 읽기 전용 청크 쌍 3개를 제외했습니다. 여기서는 제거할 수 없습니다."


def _chunk(cid: str) -> dict:
    return {
        "id": cid,
        "content": "note",
        "source_file": f"/notes/{cid}.md",
        "start_line": 1,
        "end_line": 2,
        "tags": [],
    }


def _route_scan(page, *, omitted: int, candidates: list | None = None) -> None:
    body = {
        "candidates": candidates or [],
        "total": len(candidates or []),
        "scanned_chunks": 4,
        "same_source_read_only_pairs": omitted,
    }
    page.unroute("**/api/dedup/candidates?*")
    page.route(
        "**/api/dedup/candidates?*",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(body)),
    )


def _scan(page) -> None:
    page.evaluate("() => runDedupScan()")
    page.wait_for_function("() => !STATE.dedupScanActive")


def _note(page) -> tuple[bool, str]:
    hidden, text = page.evaluate(
        "() => { const el = qs('dedup-omitted'); return [el.hidden, el.textContent]; }"
    )
    return hidden, text


def test_note_under_the_empty_state_follows_language_and_clears(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    goto_after_i18n_init(page, mm_web_url, probe_key="settings.dedup.omitted_other", lang="en")

    _route_scan(page, omitted=3)
    _scan(page)
    assert page.evaluate("() => qs('dedup-empty').hidden") is False
    assert _note(page) == (False, _EN_THREE)

    page.evaluate("() => I18N.setLang('ko')")
    assert _note(page) == (False, _KO_THREE)
    page.evaluate("() => I18N.setLang('en')")

    _route_scan(page, omitted=0)
    _scan(page)
    assert _note(page) == (True, "")


def test_note_under_the_candidate_list_and_singular(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    goto_after_i18n_init(page, mm_web_url, probe_key="settings.dedup.omitted_one", lang="en")

    pair = {"chunk_a": _chunk("a"), "chunk_b": _chunk("b"), "score": 1.0, "exact": True}
    _route_scan(page, omitted=1, candidates=[pair])
    _scan(page)
    assert page.evaluate("() => qs('dedup-list').hidden") is False
    assert _note(page) == (False, _EN_ONE)

    page.evaluate("() => resetDedupPanel()")
    assert _note(page) == (True, "")
