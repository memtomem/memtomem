"""Unit tests for tools/check_vendored_advisories.py (#1252).

The advisory gate is loaded by path (``tools/`` is a script dir, not a package).
OSV is never hit: ``query_osv`` takes an injectable opener, and the classify /
ignore logic is pure, so these run offline in the normal test job.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TOOL_PATH = _REPO_ROOT / "tools" / "check_vendored_advisories.py"


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("check_vendored_advisories", _TOOL_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: dataclasses + `from __future__ import annotations`
    # resolve a module's own names via sys.modules[cls.__module__].
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


cva = _load_tool()
TODAY = dt.date(2026, 6, 14)


class _FakeResp(io.BytesIO):
    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _opener_returning(payload: dict[str, Any]):
    def _open(req: Any, timeout: float | None = None) -> _FakeResp:
        return _FakeResp(json.dumps(payload).encode())

    return _open


def _opener_raising(exc: Exception):
    def _open(req: Any, timeout: float | None = None) -> _FakeResp:
        raise exc

    return _open


def _ig(advisory_id: str, package: str, version: str, expires: dt.date) -> Any:
    return cva.Ignore(
        id=advisory_id,
        package=package,
        version=version,
        reason="test",
        owner="tester",
        expires=expires,
    )


# --- query_osv ------------------------------------------------------------


def test_query_osv_maps_vulns_to_advisories() -> None:
    payload = {"results": [{"vulns": [{"id": "GHSA-aaa"}, {"id": "GHSA-bbb"}]}, {}]}
    got = cva.query_osv(
        [("dompurify", "3.1.6"), ("marked", "18.0.3")], opener=_opener_returning(payload)
    )
    assert got == [("dompurify", "3.1.6", "GHSA-aaa"), ("dompurify", "3.1.6", "GHSA-bbb")]


def test_query_osv_empty_pairs_short_circuits() -> None:
    # No opener call needed when there is nothing to query.
    assert cva.query_osv([], opener=_opener_raising(AssertionError("must not call"))) == []


def test_query_osv_length_mismatch_is_unreachable() -> None:
    with pytest.raises(cva.OsvUnreachable):
        cva.query_osv([("marked", "18.0.3")], opener=_opener_returning({"results": []}))


def test_query_osv_network_error_is_unreachable() -> None:
    with pytest.raises(cva.OsvUnreachable):
        cva.query_osv(
            [("marked", "18.0.3")], opener=_opener_raising(urllib.error.URLError("down")), retries=2
        )


def test_query_osv_empty_dict_result_is_clean() -> None:
    # {} is OSV's "no vulns" — must NOT be read as malformed.
    assert cva.query_osv([("marked", "18.0.3")], opener=_opener_returning({"results": [{}]})) == []


def test_query_osv_null_result_is_unreachable() -> None:
    # A falsy non-dict per-query result must fail closed, not pass as "no vulns".
    with pytest.raises(cva.OsvUnreachable):
        cva.query_osv([("marked", "18.0.3")], opener=_opener_returning({"results": [None]}))


def test_query_osv_non_list_vulns_is_unreachable() -> None:
    with pytest.raises(cva.OsvUnreachable):
        cva.query_osv(
            [("marked", "18.0.3")], opener=_opener_returning({"results": [{"vulns": "oops"}]})
        )


def test_query_osv_vuln_without_id_is_unreachable() -> None:
    with pytest.raises(cva.OsvUnreachable):
        cva.query_osv(
            [("marked", "18.0.3")], opener=_opener_returning({"results": [{"vulns": [{}]}]})
        )


# --- classify_advisories --------------------------------------------------


def test_classify_clean() -> None:
    outcome = cva.classify_advisories([], [], TODAY)
    assert outcome.ok


def test_classify_unsuppressed_advisory_fails() -> None:
    outcome = cva.classify_advisories([("marked", "1.0", "GHSA-x")], [], TODAY)
    assert not outcome.ok
    assert outcome.unsuppressed == [("marked", "1.0", "GHSA-x")]


def test_classify_active_ignore_suppresses() -> None:
    ig = _ig("GHSA-x", "marked", "1.0", TODAY + dt.timedelta(days=30))
    outcome = cva.classify_advisories([("marked", "1.0", "GHSA-x")], [ig], TODAY)
    assert outcome.ok
    assert not outcome.unsuppressed and not outcome.stale and not outcome.expired


def test_classify_expired_ignore_fails_and_advisory_resurfaces() -> None:
    ig = _ig("GHSA-x", "marked", "1.0", TODAY - dt.timedelta(days=1))
    outcome = cva.classify_advisories([("marked", "1.0", "GHSA-x")], [ig], TODAY)
    assert not outcome.ok
    assert outcome.expired == [ig]
    assert outcome.unsuppressed == [("marked", "1.0", "GHSA-x")]


def test_classify_stale_ignore_fails() -> None:
    ig = _ig("GHSA-x", "marked", "1.0", TODAY + dt.timedelta(days=30))
    outcome = cva.classify_advisories([], [ig], TODAY)
    assert not outcome.ok
    assert outcome.stale == [ig]


# --- load_ignores ---------------------------------------------------------


def test_load_ignores_missing_file_is_empty(tmp_path: Path) -> None:
    assert cva.load_ignores(tmp_path / "nope.toml") == []


def test_load_ignores_parses_bare_toml_date(tmp_path: Path) -> None:
    p = tmp_path / "ig.toml"
    p.write_text(
        "\n".join(
            [
                "[[ignore]]",
                'id = "GHSA-x"',
                'package = "marked"',
                'version = "18.0.3"',
                'reason = "disputed"',
                'owner = "tester"',
                "expires = 2026-09-01",
            ]
        ),
        encoding="utf-8",
    )
    (ig,) = cva.load_ignores(p)
    assert ig.expires == dt.date(2026, 9, 1) and ig.package == "marked"


def test_load_ignores_parses_string_date(tmp_path: Path) -> None:
    p = tmp_path / "ig.toml"
    p.write_text(
        "\n".join(
            [
                "[[ignore]]",
                'id = "GHSA-x"',
                'package = "marked"',
                'version = "18.0.3"',
                'reason = "disputed"',
                'owner = "tester"',
                'expires = "2026-09-01"',
            ]
        ),
        encoding="utf-8",
    )
    (ig,) = cva.load_ignores(p)
    assert ig.expires == dt.date(2026, 9, 1)


def test_load_ignores_missing_key_raises(tmp_path: Path) -> None:
    p = tmp_path / "ig.toml"
    p.write_text(
        "\n".join(
            [
                "[[ignore]]",
                'id = "GHSA-x"',
                'package = "marked"',
                'version = "18.0.3"',
                'reason = "disputed"',
                "expires = 2026-09-01",
            ]
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        cva.load_ignores(p)


# --- the shipped ignore file is valid and currently empty -----------------


def test_repo_ignore_file_is_valid_and_empty() -> None:
    assert cva.load_ignores() == []
