"""Temporary CI probe for #2922: fails on macOS only, then removed."""

import sys


def test_ci_probe_fails_on_macos_only() -> None:
    assert sys.platform != "darwin"
