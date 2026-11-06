"""Home lists the configured remote inference endpoints, and only those."""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser

NOT_COVERED = (
    "Not covered: Langfuse session tracing, webhooks, and content returned to the "
    "connected MCP client."
)


def _ok(route, payload) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _endpoint(component: str, provider: str, host: str, remote: bool) -> dict[str, object]:
    return {
        "component": component,
        "provider": provider,
        "host": host,
        "remote": remote,
        "sends": "(server phrase; the UI uses its own locale strings)",
    }


def _open_home(page, mm_web_url: str, endpoints: list[dict[str, object]]) -> None:
    install_default_stubs(page)
    page.route(
        "**/api/inference-endpoints",
        lambda r: _ok(r, {"endpoints": endpoints, "not_covered": NOT_COVERED}),
    )
    page.goto(f"{mm_web_url}/#home")


def test_remote_endpoints_are_listed(page, mm_web_url: str) -> None:
    _open_home(
        page,
        mm_web_url,
        [
            _endpoint("embedding", "openai", "api.openai.com", True),
            _endpoint("llm", "ollama", "localhost:11434", False),
            _endpoint("rerank", "cohere", "api.cohere.ai", True),
        ],
    )
    block = page.locator("#home-inference-info")
    block.wait_for(state="visible")
    assert block.locator(".home-section-title").text_content() == "Remote inference endpoints"
    assert block.locator("li").all_text_contents() == [
        "embedding (openai) → api.openai.com: sends note text and search queries",
        "rerank (cohere) → api.cohere.ai: sends search queries and the text of candidate results",
    ]
    assert block.locator("p").text_content() == NOT_COVERED


def test_block_stays_hidden_when_nothing_is_remote(page, mm_web_url: str) -> None:
    _open_home(page, mm_web_url, [_endpoint("embedding", "ollama", "localhost:11434", False)])
    # Rendered right after the payload is stored; wait for the payload, not
    # just the response, so "hidden" is the rendered state and not the initial one.
    page.wait_for_function("() => _homeInference !== null")
    block = page.locator("#home-inference-info")
    assert block.is_hidden()
    assert block.inner_html() == ""


def test_revisiting_home_picks_up_a_config_changed_elsewhere(page, mm_web_url: str) -> None:
    """``mm config set rerank.enabled true`` while the Web UI is open: the next
    visit to Home shows the new remote endpoint, and the reverse hides it."""
    payload: dict[str, object] = {"endpoints": [], "not_covered": NOT_COVERED}
    install_default_stubs(page)
    page.route("**/api/inference-endpoints", lambda r: _ok(r, payload))
    page.goto(f"{mm_web_url}/#home")
    page.wait_for_function("() => _homeInference !== null")
    block = page.locator("#home-inference-info")
    assert block.is_hidden()

    payload["endpoints"] = [_endpoint("rerank", "cohere", "api.cohere.ai", True)]
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => activateTab('home')")
    block.wait_for(state="visible")
    assert block.locator("li").all_text_contents() == [
        "rerank (cohere) → api.cohere.ai: sends search queries and the text of candidate results"
    ]

    payload["endpoints"] = []
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => activateTab('home')")
    block.wait_for(state="hidden")
