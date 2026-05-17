"""A11Y-3.1 — global shortcut gate when a modal is active (issue #1053).

The bug today: ``settings-namespaces.js:747`` registers a window-level
keydown listener for ``Cmd/Ctrl+K`` that checks only
``STATE.cmdPaletteOpen``. When ``confirm-modal`` is up, Ctrl+K still opens
the command palette on top of it, stealing focus and dismissing the
confirm gate visually.

The fix introduces a modal manager (``modal-manager.js``) that tracks
which ``.modal-overlay`` elements are open, and the keydown listener
gates on ``STATE.activeModals.size > 0`` (and the ``?`` / ``Cmd+,``
listeners gain the same gate).

This test is RED until the manager + gate land. It boots the SPA, opens
a confirm modal the way the rest of the app does (``showConfirm()``),
presses Ctrl+K, and asserts the palette stays hidden.
"""

from __future__ import annotations

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# ``strict=True`` so the marker self-removes once the modal manager + gate
# land in issue #1053 PR #3: the test xpasses, pytest reports it as a
# failure, and the dev drops the marker rather than leaving a stale
# expected-failure that could silently re-RED later.
@pytest.mark.xfail(strict=True, reason="A11Y-3.1 — pending modal manager in issue #1053 PR #3")
def test_ctrl_k_blocked_while_confirm_modal_active(mm_web_url, page):
    install_default_stubs(page)
    page.goto(mm_web_url)
    page.wait_for_selector("#confirm-modal", state="attached")
    page.wait_for_selector("#cmd-palette", state="attached")

    # ``showConfirm`` returns a Promise that only resolves on OK/Cancel.
    # Playwright's ``page.evaluate`` awaits Promises by default, so calling
    # it directly would hang until the modal is dismissed — but the whole
    # point of this test is to keep the modal open while pressing Ctrl+K.
    # ``void`` discards the returned Promise so the evaluate call returns
    # immediately while ``showConfirm`` runs to first render in the
    # background.
    page.evaluate(
        "void window.showConfirm({"
        "title: 'A11Y gate probe', "
        "message: 'open modal to probe the Cmd+K gate', "
        "confirmText: 'OK', cancelText: 'Cancel'"
        "})"
    )
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)

    # If the gate is in place, Ctrl+K is swallowed (preventDefault) and
    # the palette stays hidden. Without the gate, the palette opens on
    # top of the confirm modal — the bug at A11Y-3.1.
    page.keyboard.press("Control+k")

    cmd_palette_hidden = page.evaluate("document.getElementById('cmd-palette').hidden")
    assert cmd_palette_hidden, (
        "cmd-palette opened while confirm-modal was active — Ctrl+K must "
        "be gated when any modal-overlay is on screen (A11Y-3.1, "
        "issue #1053). Check the modal manager wiring in "
        "static/modal-manager.js + the keydown listener at "
        "settings-namespaces.js:747"
    )
