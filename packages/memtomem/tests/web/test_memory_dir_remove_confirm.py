"""Browser tests for the memory-dir remove confirm (#2534).

Both remove entry points, the Sources tree's ``handleRemove`` and the Memory
Dirs panel's ``mdRemove``, build their dialog with
``memoryDirRemoveConfirmOptions``. The dialog offers *delete chunks* with the
status ``delete_chunk_count``, a preview of the number the server's sweep
deletes (#2537). A dir that another root still contains deletes nothing, so
the dialog drops the checkbox and says the chunks stay. A status without the field (an older
server) keeps offering the full ``chunk_count``.

Both entry points refetch the status when the dialog opens and build it from
the fresh entry, not the page's cached one. The count includes chunks of held
or pending sources, which the tree does not list, and the label says how many.
If the refetch fails, the dialog offers only the registration remove (#2561).
"""

from __future__ import annotations

import json

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_COVERED_KEY = "confirm.memory_dir_chunks_still_covered"


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
    goto_after_i18n_init(page, mm_web_url, probe_key=_COVERED_KEY)

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


def _stub_status(page, dirs: list[dict] | None) -> None:
    """Serve ``dirs`` from ``/api/memory-dirs/status``, or fail it when None."""

    def _status(route):
        if dirs is None:
            route.fulfill(status=500, content_type="application/json", body="{}")
        else:
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps({"dirs": dirs})
            )

    page.route("**/api/memory-dirs/status", _status)


def _stub_remove(page) -> list[dict]:
    posted: list[dict] = []

    def _remove(route):
        posted.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True, "memory_dirs": ["/tmp/work"], "deleted_chunks": 0}),
        )

    page.route("**/api/memory-dirs/remove", _remove)
    return posted


def test_confirm_label_names_the_held_part(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    goto_after_i18n_init(page, mm_web_url, probe_key=_COVERED_KEY)
    opts = page.evaluate(
        """() => memoryDirRemoveConfirmOptions('/tmp/notes', {
          chunk_count: 7, delete_chunk_count: 7, held_chunk_count: 5,
        })"""
    )
    assert opts["extraOption"]["label"] == page.evaluate(
        "() => t('confirm.memory_dir_delete_chunks_label_held', { count: 7, held: 5 })"
    )
    # A covered dir deletes nothing, so there is no held part to name.
    covered = page.evaluate(
        """() => memoryDirRemoveConfirmOptions('/tmp/notes', {
          chunk_count: 7, delete_chunk_count: 0, held_chunk_count: 5,
        })"""
    )
    assert covered["extraOption"] is None


def test_md_remove_of_a_covered_dir_warns_and_deletes_nothing(page, mm_web_url: str) -> None:
    """End to end through the shared dialog: no checkbox, the warning line is
    shown, and the request asks for no chunk deletion. The cached status
    still offers 7 chunks; the dialog follows the refetched one."""
    install_default_stubs(page)
    _stub_status(page, [{"path": "/tmp/work/notes", "chunk_count": 7, "delete_chunk_count": 0}])
    posted = _stub_remove(page)
    goto_after_i18n_init(page, mm_web_url, probe_key=_COVERED_KEY)

    page.evaluate(
        """() => {
          STATE.memoryStatusByPath = {
            '/tmp/work/notes': { chunk_count: 7, delete_chunk_count: 7 },
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


def test_md_remove_when_status_refresh_fails_offers_no_chunk_delete(page, mm_web_url: str) -> None:
    """The cached status offers 7 chunks, but the refetch fails: the dialog
    must not show a count it could not refresh."""
    install_default_stubs(page)
    _stub_status(page, None)
    posted = _stub_remove(page)
    goto_after_i18n_init(page, mm_web_url, probe_key="confirm.memory_dir_counts_unavailable")

    page.evaluate(
        """() => {
          STATE.memoryStatusByPath = {
            '/tmp/work/notes': { chunk_count: 7, delete_chunk_count: 7 },
          };
          window.__mdRemoveDone = mdRemove('/tmp/work/notes');
        }"""
    )
    page.locator("#confirm-modal").wait_for(state="visible")
    assert page.locator("#confirm-extra-row").is_hidden()
    assert page.locator("#confirm-warning").text_content() == page.evaluate(
        "() => t('confirm.memory_dir_counts_unavailable')"
    )

    page.locator("#confirm-ok-btn").click()
    page.evaluate("() => window.__mdRemoveDone")
    assert posted == [{"path": "/tmp/work/notes", "delete_chunks": False}]


def test_panel_remove_uses_the_refetched_status(page, mm_web_url: str) -> None:
    """The Memory Dirs panel's ``handleRemove`` opens the same dialog from the
    refetched status: its held part, not the panel's cached numbers."""
    install_default_stubs(page)
    fresh = [
        {
            "path": "/tmp/work/notes",
            "exists": True,
            "category": "user",
            "provider": "user",
            "chunk_count": 9,
            "delete_chunk_count": 9,
            "held_chunk_count": 4,
        },
        {
            "path": "/tmp/work/other",
            "exists": True,
            "category": "user",
            "provider": "user",
            "chunk_count": 1,
            "delete_chunk_count": 1,
        },
    ]
    served = {
        "dirs": [dict(fresh[0], chunk_count=2, delete_chunk_count=2, held_chunk_count=0), fresh[1]]
    }

    def _status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(served))

    page.route("**/api/memory-dirs/status", _status)
    posted = _stub_remove(page)
    goto_after_i18n_init(page, mm_web_url, probe_key=_COVERED_KEY)
    page.evaluate(
        """() => {
          const host = document.createElement('div');
          host.id = 'test-md-panel';
          document.body.appendChild(host);
          host.appendChild(_buildMemoryDirsPanel(['/tmp/work/notes', '/tmp/work/other']));
        }"""
    )
    row = page.locator("#test-md-panel .memory-dirs-item").filter(
        has=page.locator('.memory-dirs-path[title="/tmp/work/notes"]')
    )
    # The remove buttons render before ``fetchStatus`` resolves. Wait until the
    # row shows the stale entry's count, so the panel has cached it.
    stale_counts = page.evaluate(
        "() => t('sources.memory_dirs.status_group', { files: 0, indexed: 0, chunks: 2 })"
    )
    page.wait_for_function(
        """([sel, text]) => {
          const meta = document.querySelector(sel);
          return !!meta && meta.textContent.includes(text);
        }""",
        arg=[
            '#test-md-panel .memory-dirs-item:has(.memory-dirs-path[title="/tmp/work/notes"]) '
            ".memory-dirs-item-meta",
            stale_counts,
        ],
    )
    served["dirs"] = fresh
    row.locator(".memory-dirs-remove-btn").click()
    page.locator("#confirm-modal").wait_for(state="visible")
    assert page.locator("#confirm-extra-row").is_visible()
    assert page.locator("#confirm-extra-row").text_content().strip() == page.evaluate(
        "() => t('confirm.memory_dir_delete_chunks_label_held', { count: 9, held: 4 })"
    )
    page.locator("#confirm-extra-row input[type=checkbox]").check()
    with page.expect_response("**/api/memory-dirs/remove"):
        page.locator("#confirm-ok-btn").click()
    assert posted == [{"path": "/tmp/work/notes", "delete_chunks": True}]
