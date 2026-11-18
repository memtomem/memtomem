"""The browser-suite probe and its CI requirement switch in ``conftest.py`` (#3453).

The CI ``test-browser`` job used to pass with every browser test skipped: the
probe's 2 s Chromium launch timed out on a cold runner, swallowed the exception,
and pytest reports a run whose every selected test skipped as a pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import (
    _PLAYWRIGHT_LAUNCH_TIMEOUT_MS,
    REQUIRE_BROWSER_ENV,
    _browser_requirement_error,
    _playwright_browser_unavailable,
)

_TESTS_DIR = Path(__file__).resolve().parent
# Inherited pytest options must not reach the child: a ``-k`` in
# ``PYTEST_ADDOPTS`` deselects every child test (same list as
# ``test_ambient_env_hermeticity``).
_INHERITED_PYTEST_ENV = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_CURRENT_TEST")
_REPO_ROOT = _TESTS_DIR.parents[2]


@pytest.mark.parametrize("required", [None, "", "0"])
def test_not_required_never_errors(required: str | None) -> None:
    assert _browser_requirement_error("Chromium did not launch: boom", required) is None


def test_required_and_available_is_fine() -> None:
    assert _browser_requirement_error(None, "1") is None


def test_required_and_unavailable_names_the_reason() -> None:
    error = _browser_requirement_error("Chromium did not launch: TimeoutError: boom", "1")
    assert error == (
        f"{REQUIRE_BROWSER_ENV}=1 but the browser tests cannot run: "
        "Chromium did not launch: TimeoutError: boom"
    )


@pytest.mark.parametrize("required", ["true", "yes", " 1", "2"])
def test_unrecognised_value_is_an_error_even_when_available(required: str) -> None:
    # A typo that read as "not required" would be the same false green.
    assert _browser_requirement_error(None, required) == (
        f"{REQUIRE_BROWSER_ENV} must be 1 or 0, got {required!r}"
    )


class _FakeChromium:
    def __init__(self, exc: Exception | None) -> None:
        self.exc = exc
        self.launch_kwargs: dict[str, object] = {}

    def launch(self, **kwargs: object) -> object:
        self.launch_kwargs = kwargs
        if self.exc is not None:
            raise self.exc

        class _Browser:
            def close(self) -> None:
                pass

        return _Browser()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium

    def __enter__(self) -> _FakePlaywright:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _patch_sync_playwright(monkeypatch: pytest.MonkeyPatch, chromium: _FakeChromium) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _FakePlaywright(chromium))


def _boxed(*lines: str) -> str:
    # The shape of Playwright's ``wrapInASCIIBox`` (driver coreBundle.js).
    width = max(len(line) for line in lines)
    return "\n".join(
        ["╔" + "═" * (width + 2) + "╗"]
        + ["║ " + line.ljust(width) + " ║" for line in lines]
        + ["╚" + "═" * (width + 2) + "╝"]
    )


@pytest.mark.parametrize(
    ("message", "summary"),
    [
        (
            "BrowserType.launch: Timeout 30000ms exceeded.",
            "BrowserType.launch: Timeout 30000ms exceeded.",
        ),
        # Missing system libraries: the cause is only inside the box, which
        # starts on the line after the API prefix.
        (
            "BrowserType.launch: \n"
            + _boxed(
                "Host system is missing dependencies to run browsers.",
                "Please install them with the following command:",
                "",
                "    sudo playwright install-deps",
            ),
            "BrowserType.launch: Host system is missing dependencies to run browsers. "
            "Please install them with the following command: sudo playwright install-deps",
        ),
        (
            "BrowserType.launch: Executable doesn't exist at /cache/chrome\n"
            + _boxed("Looks like Playwright was just installed or updated."),
            "BrowserType.launch: Executable doesn't exist at /cache/chrome "
            "Looks like Playwright was just installed or updated.",
        ),
    ],
)
def test_probe_reports_the_launch_error_on_one_line(
    monkeypatch: pytest.MonkeyPatch, message: str, summary: str
) -> None:
    _patch_sync_playwright(monkeypatch, _FakeChromium(RuntimeError(message)))
    assert _playwright_browser_unavailable() == (
        f"Chromium did not launch: RuntimeError: {summary}"
    )


def test_probe_caps_a_long_launch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sync_playwright(monkeypatch, _FakeChromium(RuntimeError("x" * 1000)))
    reason = _playwright_browser_unavailable()
    assert reason is not None
    detail = reason.removeprefix("Chromium did not launch: RuntimeError: ")
    assert detail == "x" * 399 + "…"


def test_probe_launches_with_the_cold_runner_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    chromium = _FakeChromium(None)
    _patch_sync_playwright(monkeypatch, chromium)
    assert _playwright_browser_unavailable() is None
    assert chromium.launch_kwargs == {"headless": True, "timeout": _PLAYWRIGHT_LAUNCH_TIMEOUT_MS}
    assert _PLAYWRIGHT_LAUNCH_TIMEOUT_MS >= 30_000


def _collect_browser_suite(tmp_path: Path, required: str | None) -> subprocess.CompletedProcess:
    pytest.importorskip("playwright.sync_api")
    env = {k: v for k, v in os.environ.items() if k not in _INHERITED_PYTEST_ENV}
    # An empty browser cache makes the real probe fail fast in the child,
    # with Playwright's "Executable doesn't exist" error.
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp_path)
    env.pop(REQUIRE_BROWSER_ENV, None)
    if required is not None:
        env[REQUIRE_BROWSER_ENV] = required
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "-q",
            "-m",
            "browser",
            str(_TESTS_DIR / "web"),
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def test_required_run_fails_when_chromium_cannot_launch(tmp_path: Path) -> None:
    result = _collect_browser_suite(tmp_path, "1")
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    errors = [line for line in output.splitlines() if line.startswith("ERROR: ")]
    assert len(errors) == 1, output
    assert errors[0].startswith(
        f"ERROR: {REQUIRE_BROWSER_ENV}=1 but the browser tests cannot run: "
        "Chromium did not launch: "
    ), output


@pytest.mark.parametrize("parent_addopts", [None, "-k __no_test_matches_this__"])
def test_unrequired_run_still_collects_when_chromium_cannot_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_addopts: str | None
) -> None:
    # The contributor-laptop path: no Chromium, no requirement, the suite skips.
    # The hostile ``PYTEST_ADDOPTS`` case would exit 5 (nothing collected) if
    # the parent's options reached the child.
    if parent_addopts is not None:
        monkeypatch.setenv("PYTEST_ADDOPTS", parent_addopts)
    result = _collect_browser_suite(tmp_path, None)
    assert result.returncode == pytest.ExitCode.OK, result.stdout + result.stderr
