"""A11Y modal behaviour pins — Playwright lane (issue #1053).

PR #2 adds Tab trap + focus restoration + background ``inert`` to the 8
``.modal-overlay`` elements (``TestA11yModalFocusTrap`` /
``TestA11yModalFocusRestore`` / ``TestA11yBackgroundInert`` /
``TestA11yStackedModalInertSurvives``).

PR #3 adds a modal manager + global-shortcut gate
(``test_ctrl_k_blocked_while_confirm_modal_active``).

Every test pin here is marked ``xfail(strict=True)`` so it self-removes
the moment the relevant fix lands: the test xpasses, pytest reports it as
a failure, and the dev drops the marker rather than leaving a stale
expected-failure that could silently re-RED later.
"""

from __future__ import annotations

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_A11Y_XFAIL_PR2 = pytest.mark.xfail(
    strict=True,
    reason="A11Y-1.2/1.5/3.2 — pending focus trap + restore + inert in issue #1053 PR #2",
)


# PR #2 installs Tab trapping on these six modals. ``confirm-modal`` and
# ``path-picker-modal`` already trap Tab via their own custom listeners
# (dynamic focusable computation) and are exercised only by the restore +
# inert pins below.
_TRAP_MODALS = (
    "expand-modal",
    "source-preview-modal",
    "settings-modal",
    "shortcuts-modal",
    "cmd-palette",
    "ctx-conflict-modal",
)

# Every modal must capture its trigger on open and restore focus on close,
# and must inert background body-level siblings while it's up.
_ALL_MODALS = (
    "expand-modal",
    "source-preview-modal",
    "settings-modal",
    "shortcuts-modal",
    "cmd-palette",
    "confirm-modal",
    "ctx-conflict-modal",
    "path-picker-modal",
)


def _open_modal_js(modal_id: str) -> str:
    """JS expression that opens ``modal_id``.

    Every expression is ``void``-prefixed: Playwright's ``page.evaluate``
    awaits returned Promises by default, and the modal openers that resolve
    only on close (``showConfirm``, ``_ctxResolveConflict``) would hang the
    test until the modal is dismissed. ``void`` discards the return value
    so the call returns immediately while the modal stays open.

    Most modals route through a ``window.openXModal()`` wrapper that PR #2
    introduces — pre-fix the wrapper is undefined and the evaluate throws,
    which the strict xfail expects. ``confirm-modal`` (``window.showConfirm``)
    and ``path-picker-modal`` (``window.PathPicker.open``) already have a
    public open path; for those the helper just adds restore + inert.
    """
    if modal_id == "confirm-modal":
        return (
            "void window.showConfirm({title: 'a11y probe', message: 'x', "
            "confirmText: 'OK', cancelText: 'Cancel'})"
        )
    if modal_id == "path-picker-modal":
        return "void window.PathPicker.open()"
    # Per-modal wrappers exposed on window by PR #2.
    wrapper = {
        "expand-modal": "openExpandModal",
        "source-preview-modal": "openSourcePreviewModal",
        "settings-modal": "openSettingsModal",
        "shortcuts-modal": "openShortcutsModal",
        "cmd-palette": "openCmdPalette",
        "ctx-conflict-modal": "openCtxConflictModal",
    }[modal_id]
    return f"void window.{wrapper}()"


def _goto_with_stubs(mm_web_url: str, page) -> None:
    install_default_stubs(page)
    page.goto(mm_web_url)
    # All 8 modal-overlay nodes are in the static HTML — wait on one as a
    # cheap "SPA boot finished" gate. ``confirm-modal`` is always present.
    page.wait_for_selector("#confirm-modal", state="attached")


class TestA11yModalFocusTrap:
    """A11Y-1.2 — Tab cycles inside the modal, never escapes to background."""

    @_A11Y_XFAIL_PR2
    @pytest.mark.parametrize("modal_id", _TRAP_MODALS)
    def test_tab_stays_inside_modal(self, mm_web_url, page, modal_id):
        _goto_with_stubs(mm_web_url, page)
        page.evaluate(_open_modal_js(modal_id))
        page.wait_for_selector(f"#{modal_id}:not([hidden])", timeout=2_000)
        # Press Tab enough times to wrap any reasonable focusable list (the
        # widest modal — settings — has ~6 controls). 12 Tabs comfortably
        # covers the cycle; an untrapped modal leaks focus to background
        # within the first few presses.
        for _ in range(12):
            page.keyboard.press("Tab")
            inside = page.evaluate(
                f"document.getElementById('{modal_id}').contains(document.activeElement)"
            )
            assert inside, (
                f"focus left #{modal_id} during Tab cycling — install a "
                f"focus trap (A11Y-1.2, issue #1053)"
            )


class TestA11yModalFocusRestore:
    """A11Y-1.5 — closing a modal returns focus to the element that opened it."""

    @_A11Y_XFAIL_PR2
    @pytest.mark.parametrize("modal_id", _ALL_MODALS)
    def test_focus_returns_to_trigger(self, mm_web_url, page, modal_id):
        _goto_with_stubs(mm_web_url, page)
        # Use ``settings-btn`` as a stable trigger surrogate for every modal.
        # The helper captures ``document.activeElement`` at open time, so
        # focus restoration is trigger-agnostic; the test only needs a known,
        # always-present focusable element.
        page.evaluate("document.getElementById('settings-btn').focus()")
        page.evaluate(_open_modal_js(modal_id))
        page.wait_for_selector(f"#{modal_id}:not([hidden])", timeout=2_000)
        page.keyboard.press("Escape")
        page.wait_for_selector(f"#{modal_id}", state="hidden", timeout=2_000)
        active_id = page.evaluate("document.activeElement && document.activeElement.id")
        assert active_id == "settings-btn", (
            f"closing #{modal_id} did not restore focus to its trigger "
            f"(got '{active_id}') — capture previouslyFocused on open and "
            f"focus() it on close (A11Y-1.5, issue #1053)"
        )


class TestA11yBackgroundInert:
    """A11Y-3.2 — body-level siblings get ``inert`` while a modal is open."""

    @_A11Y_XFAIL_PR2
    @pytest.mark.parametrize("modal_id", _ALL_MODALS)
    def test_background_inerts_while_open(self, mm_web_url, page, modal_id):
        _goto_with_stubs(mm_web_url, page)
        page.evaluate(_open_modal_js(modal_id))
        page.wait_for_selector(f"#{modal_id}:not([hidden])", timeout=2_000)
        # ``<header>`` is the first body-level element (index.html:13) and is
        # not a modal — it must be inerted while any modal is up so SR + Tab
        # cannot escape into background chrome.
        header_inert_open = page.evaluate(
            "document.querySelector('body > header').hasAttribute('inert')"
        )
        assert header_inert_open, (
            f"<header> not inerted while #{modal_id} was open — apply "
            f"inert to body-level siblings (A11Y-3.2, issue #1053)"
        )
        page.keyboard.press("Escape")
        page.wait_for_selector(f"#{modal_id}", state="hidden", timeout=2_000)
        header_inert_closed = page.evaluate(
            "document.querySelector('body > header').hasAttribute('inert')"
        )
        assert not header_inert_closed, (
            f"<header> remained inert after #{modal_id} closed — release "
            f"inert when the modal stack empties (A11Y-3.2, issue #1053)"
        )


class TestA11yStackedModalInertSurvives:
    """A11Y-3.2 / A11Y-3.3 stack regression — closing a nested modal must NOT
    un-inert the outer modal's background.

    Today's audit flagged settings → maintenance ``showConfirm`` as a real
    stacking case (`a11y-1-2-3-audit-pass-dreamy-mccarthy.md` A11Y-3.3). A
    naive per-open snapshot of "siblings to inert" un-inerts the background
    when the inner modal closes because the outer modal was in the inner
    modal's snapshot. The refcount-based ``_recomputeBackgroundInert()``
    fixes this — this single test is the only thing standing between us and
    a silent regression there.
    """

    @_A11Y_XFAIL_PR2
    def test_inner_close_keeps_outer_background_inert(self, mm_web_url, page):
        _goto_with_stubs(mm_web_url, page)
        page.evaluate("document.getElementById('settings-btn').focus()")
        # Open outer: settings-modal.
        page.evaluate(_open_modal_js("settings-modal"))
        page.wait_for_selector("#settings-modal:not([hidden])", timeout=2_000)
        # Open inner on top: confirm-modal via showConfirm.
        page.evaluate(_open_modal_js("confirm-modal"))
        page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
        # Close inner by clicking OK (Escape would also fire, but OK is the
        # least-ambiguous close path through cleanup()).
        page.click("#confirm-ok-btn")
        page.wait_for_selector("#confirm-modal", state="hidden", timeout=2_000)
        # Settings is still open; therefore <header> must STILL be inert
        # (the refcount design holds) and settings-modal itself must NOT be
        # inert (a snapshot-based design would have un-inerted it).
        header_still_inert = page.evaluate(
            "document.querySelector('body > header').hasAttribute('inert')"
        )
        assert header_still_inert, (
            "closing the inner confirm-modal un-inerted the outer "
            "settings-modal's background — _recomputeBackgroundInert() "
            "must derive inert state from the active-modal stack, not "
            "per-open snapshots (A11Y-3.2/3.3, issue #1053)"
        )
        settings_not_inert = page.evaluate(
            "!document.getElementById('settings-modal').hasAttribute('inert')"
        )
        assert settings_not_inert, (
            "settings-modal itself became inert after the nested confirm "
            "closed — the active modal must never be inert (A11Y-3.2, "
            "issue #1053)"
        )


# ---------------------------------------------------------------------------
# PR #3 pin — kept here as the original tenant of this file.
# ---------------------------------------------------------------------------


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
