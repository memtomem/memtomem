"""Browser tests for the Context Gateway top-level tab (#962).

The Gateway was promoted from a Settings sub-section to a top-level tab.
Pin three pieces of behavior so a partial revert can't silently re-nest
the Gateway under Settings without breaking these tests:

* Clicking ``#tabbtn-context-gateway`` lands the user on ``#tab-context-gateway``
  with ``ctx-projects`` as the default active section.
* ``switchSettingsSection('ctx-skills')`` (legacy caller pattern from
  e.g. ``settings-namespaces.js`` quick links) auto-redirects into the
  new Gateway tab — no per-call-site fix needed.
* The Settings tab no longer surfaces the moved sections in its sidebar
  (test_web_mode.py covers this on the markup side; this is the
  runtime symmetry pin).
"""

from __future__ import annotations

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


def test_main_tab_button_activates_gateway_panel(page, mm_web_url: str) -> None:
    """Click on the new top-level Gateway button → ``#tab-context-gateway``
    becomes active and the default section ``ctx-projects`` populates.
    """
    install_default_stubs(page)

    page.goto(mm_web_url)
    page.locator("#tabbtn-context-gateway").click()
    page.wait_for_function(
        "() => {"
        "  const tab = document.getElementById('tab-context-gateway');"
        "  return tab && tab.classList.contains('active') && !tab.hidden;"
        "}",
        timeout=4_000,
    )

    # Default landing section is ctx-projects.
    active_section = page.evaluate(
        "() => {"
        "  const sections = document.querySelectorAll("
        "    '#tab-context-gateway .settings-section');"
        "  for (const s of sections) {"
        "    if (s.classList.contains('active')) return s.id;"
        "  }"
        "  return null;"
        "}"
    )
    assert active_section == "settings-ctx-projects", (
        f"Default Gateway sub-section must be ctx-projects, got {active_section!r}"
    )


def test_switch_settings_section_to_ctx_skills_auto_redirects(page, mm_web_url: str) -> None:
    """Legacy callers like ``settings-namespaces.js`` invoke
    ``switchSettingsSection('ctx-skills')`` to jump to a Gateway section.
    Post-#962 that call must auto-redirect into the new Gateway tab so
    no per-call-site update is needed.
    """
    install_default_stubs(page)

    page.goto(mm_web_url)
    page.evaluate("() => switchSettingsSection('ctx-skills')")

    page.wait_for_function(
        "() => {"
        "  const tab = document.getElementById('tab-context-gateway');"
        "  if (!tab || !tab.classList.contains('active')) return false;"
        "  const section = document.getElementById('settings-ctx-skills');"
        "  return section && section.classList.contains('active');"
        "}",
        timeout=4_000,
    )


def test_settings_tab_no_longer_lists_gateway_sections_in_sidebar(page, mm_web_url: str) -> None:
    """Symmetric runtime pin for the test_web_mode.py markup check —
    when Settings is the active tab, none of the moved sections should
    be reachable from the Settings sidebar (i.e. their nav buttons live
    elsewhere now).
    """
    install_default_stubs(page)

    page.goto(mm_web_url)
    page.evaluate("() => activateTab('settings')")
    page.wait_for_function(
        "() => {"
        "  const tab = document.getElementById('tab-settings');"
        "  return tab && tab.classList.contains('active');"
        "}",
        timeout=4_000,
    )

    moved_sections = (
        "ctx-overview",
        "ctx-skills",
        "ctx-commands",
        "ctx-agents",
        "hooks-sync",
    )
    for section in moved_sections:
        count = page.evaluate(
            f"() => document.querySelectorAll("
            f"'#tab-settings .settings-nav-btn[data-section=\"{section}\"]'"
            f").length"
        )
        assert count == 0, (
            f"Settings sidebar must not retain a nav button for {section!r} "
            f"after the Gateway promotion (#962); found {count} button(s)."
        )
