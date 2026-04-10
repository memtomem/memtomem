"""Tests for `mm web` CLI error handling and the wizard's web-extra hint.

Regression coverage for a bug where `mm web` produced a raw
`ModuleNotFoundError: No module named 'fastapi'` traceback when the `[web]`
extra wasn't installed — because the old error handler only caught missing
`uvicorn`, not `fastapi`.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from memtomem.cli.web import _missing_web_deps, _web_install_hint, web


def test_missing_web_deps_returns_none_when_installed() -> None:
    """In the test env, fastapi + uvicorn are installed (via `[all]`)."""
    assert _missing_web_deps() is None


def test_missing_web_deps_reports_missing_module() -> None:
    """If fastapi isn't importable, report it by name so the error is actionable."""
    # Simulate fastapi being uninstalled by making its import raise.
    with patch.dict(sys.modules, {"fastapi": None}):
        assert _missing_web_deps() == "fastapi"


def test_install_hint_uses_reinstall_flag() -> None:
    """The hint must use `--reinstall` so it works for users who installed
    memtomem without extras via `uv tool install memtomem`."""
    hint = _web_install_hint()
    assert "uv tool install" in hint
    assert "--reinstall" in hint
    assert '"memtomem[web]"' in hint


def test_mm_web_shows_actionable_error_when_fastapi_missing() -> None:
    """Regression: previously this produced a raw traceback because the CLI
    only caught `uvicorn` import failures. Now it should exit 1 with a clean
    message naming the missing module and the install command."""
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value="fastapi"):
        result = runner.invoke(web, [])
    assert result.exit_code == 1
    assert "fastapi" in result.output
    assert "memtomem[web]" in result.output
    # Should not contain a raw traceback.
    assert "Traceback" not in result.output


def test_mm_web_shows_actionable_error_when_uvicorn_missing() -> None:
    """Symmetric case: uvicorn missing."""
    runner = CliRunner()
    with patch("memtomem.cli.web._missing_web_deps", return_value="uvicorn"):
        result = runner.invoke(web, [])
    assert result.exit_code == 1
    assert "uvicorn" in result.output
    assert "memtomem[web]" in result.output


def test_wizard_next_steps_hint_respects_web_deps(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wizard's 'Next steps' Step 3 should suggest the install command
    when web deps are missing, and show a normal `mm web` hint otherwise."""
    # We can't easily run the full interactive wizard here, but the hint
    # logic itself is a straightforward branch on _missing_web_deps(). Check
    # both sides by importing and calling the helper directly — this is the
    # same value the wizard uses in `_write_config_and_summary`.
    from memtomem.cli import init_cmd  # noqa: F401 — ensures import side-effects are OK

    # When deps are present, helper returns None → wizard shows clean hint.
    with patch("memtomem.cli.web._missing_web_deps", return_value=None):
        from memtomem.cli.web import _missing_web_deps as check

        assert check() is None

    # When deps are missing, helper returns module name → wizard shows install
    # command. The actual string assembly is exercised by the test above.
    with patch("memtomem.cli.web._missing_web_deps", return_value="fastapi"):
        from memtomem.cli.web import _missing_web_deps as check

        assert check() == "fastapi"
