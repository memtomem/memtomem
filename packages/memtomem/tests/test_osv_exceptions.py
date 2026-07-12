"""Tests for the reviewed Python/JavaScript lockfile OSV exception schema."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "check_osv_exceptions.py"
    spec = importlib.util.spec_from_file_location("check_osv_exceptions", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


osv = _load_tool()
_TODAY = dt.date(2026, 7, 12)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "exceptions.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _entry(**overrides: str) -> str:
    values = {
        "id": '"GHSA-xxxx-yyyy-zzzz"',
        "owner": '"security-owner"',
        "reason": '"No fixed release"',
        "ignoreUntil": "2026-08-01",
        **overrides,
    }
    return "[[exceptions]]\n" + "\n".join(f"{key} = {value}" for key, value in values.items())


def test_committed_empty_default_is_valid_and_renders_empty_native_config() -> None:
    entries = osv.load_exceptions(_ROOT / "tools" / "osv-exceptions.toml", today=_TODAY)
    assert entries == []
    assert "[[IgnoredVulns]]" not in osv.render_native(entries)


def test_complete_entry_is_converted_without_native_owner_key(tmp_path: Path) -> None:
    entries = osv.load_exceptions(_write(tmp_path, _entry()), today=_TODAY)
    rendered = osv.render_native(entries)
    assert "[[IgnoredVulns]]" in rendered
    assert 'id = "GHSA-xxxx-yyyy-zzzz"' in rendered
    assert "# reviewed owner: security-owner" in rendered
    assert "owner =" not in rendered
    assert "ignoreUntil = 2026-08-01" in rendered


@pytest.mark.parametrize("missing", ["id", "owner", "reason", "ignoreUntil"])
def test_missing_key_is_rejected(tmp_path: Path, missing: str) -> None:
    lines = [line for line in _entry().splitlines() if not line.startswith(f"{missing} =")]
    with pytest.raises(osv.ExceptionConfigError, match="schema mismatch"):
        osv.load_exceptions(_write(tmp_path, "\n".join(lines)), today=_TODAY)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(osv.ExceptionConfigError, match="extra"):
        osv.load_exceptions(_write(tmp_path, _entry(extra='"no"')), today=_TODAY)


@pytest.mark.parametrize("key", ["id", "owner", "reason"])
def test_blank_string_is_rejected(tmp_path: Path, key: str) -> None:
    with pytest.raises(osv.ExceptionConfigError, match="non-empty"):
        osv.load_exceptions(_write(tmp_path, _entry(**{key: '"  "'})), today=_TODAY)


def test_duplicate_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(osv.ExceptionConfigError, match="duplicate"):
        osv.load_exceptions(_write(tmp_path, f"{_entry()}\n\n{_entry()}"), today=_TODAY)


@pytest.mark.parametrize("date", ["2026-07-11", "2026-07-12"])
def test_past_and_today_are_expired(tmp_path: Path, date: str) -> None:
    with pytest.raises(osv.ExceptionConfigError, match="expired"):
        osv.load_exceptions(_write(tmp_path, _entry(ignoreUntil=date)), today=_TODAY)


def test_quoted_date_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(osv.ExceptionConfigError, match="bare TOML"):
        osv.load_exceptions(_write(tmp_path, _entry(ignoreUntil='"2026-08-01"')), today=_TODAY)


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(osv.ExceptionConfigError, match="top-level"):
        osv.load_exceptions(_write(tmp_path, "enabled = true\n"), today=_TODAY)
