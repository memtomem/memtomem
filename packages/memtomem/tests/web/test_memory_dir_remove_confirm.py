"""Browser tests for the memory-dir remove confirm (#2534).

Both remove entry points, the Sources tree's ``handleRemove`` and the Memory
Dirs panel's ``mdRemove``, build their dialog with
``memoryDirRemoveConfirmOptions``. The dialog offers *delete chunks* with the
status ``delete_chunk_count``, a preview of the number the server's sweep
deletes (#2537). A dir that another root still contains deletes nothing, so
the dialog drops the checkbox and says the chunks stay. A status without the field (an older
server) keeps offering the full ``chunk_count``.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser

_COVERED_KEY = "confirm.memory_dir_chunks_still_covered"


def _goto_after_i18n_ready(page, mm_web_url: str) -> None:
    page.goto(mm_web_url)
    page.wait_for_function(
        f"() => typeof t === 'function' && t('{_COVERED_KEY}') !== '{_COVERED_KEY}'"
    )


@pytest.mark.parametrize(
    ("status", "checkbox_count", "warns"),
    [
        pytest.param({"chunk_count": 5, "delete_chunk_count": 5}, 5, False, id="owned"),
        pytest.param({"chunk_count": 5, "delete_chunk_count": 0}, None, True, id="covered"),
        pytest.param({"chunk_count": 5}, 5, False, id="older-server"),
        pytest.param({"chunk_count": 0, "delete_chunk_count": 0}, None, False, id="empty"),
        pytest.param(None, None, False, id="no-status-row"),
    ],
)
def test_confirm_options_follow_delete_chunk_count(
    page, mm_web_url: str, status, checkbox_count, warns
) -> None:
    install_default_stubs(page)
    _goto_after_i18n_ready(page, mm_web_url)

    opts = page.evaluate(
        "(st) => memoryDirRemoveConfirmOptions('/tmp/notes', st ?? undefined)", status
    )

    if checkbox_count is None:
        assert opts["extraOption"] is None
    else:
        assert opts["extraOption"]["id"] == "deleteChunks"
        assert opts["extraOption"]["defaultChecked"] is False
        expected = page.evaluate(
            "(n) => t('confirm.memory_dir_delete_chunks_label', { count: n })", checkbox_count
        )
        assert opts["extraOption"]["label"] == expected
    expected_warning = page.evaluate(f"() => t('{_COVERED_KEY}')") if warns else ""
    assert opts["warningText"] == expected_warning


def test_md_remove_of_a_covered_dir_warns_and_deletes_nothing(page, mm_web_url: str) -> None:
    """End to end through the shared dialog: no checkbox, the warning line is
    shown, and the request asks for no chunk deletion."""
    install_default_stubs(page)
    posted: list[dict] = []

    def _remove(route):
        posted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "memory_dirs": ["/tmp/work"], "deleted_chunks": 0}),
        )

    page.route("**/api/memory-dirs/remove", _remove)
    _goto_after_i18n_ready(page, mm_web_url)

    page.evaluate(
        """() => {
          STATE.memoryStatusByPath = {
            '/tmp/work/notes': { chunk_count: 7, delete_chunk_count: 0 },
          };
          window.__mdRemoveDone = mdRemove('/tmp/work/notes');
        }"""
    )
    page.locator("#confirm-modal").wait_for(state="visible")
    assert page.locator("#confirm-extra-row").is_hidden()
    warning = page.locator("#confirm-warning")
    assert warning.is_visible()
    assert warning.text_content() == page.evaluate(f"() => t('{_COVERED_KEY}')")

    page.locator("#confirm-ok-btn").click()
    page.evaluate("() => window.__mdRemoveDone")

    assert posted == [{"path": "/tmp/work/notes", "delete_chunks": False}]
