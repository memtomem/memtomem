#!/usr/bin/env python3
"""Fail CI when a vendored frontend library has a known OSV advisory (#1252).

The Web UI ships a handful of browser libraries as SHA-pinned files (DOMPurify,
marked, Prism, Swagger UI) that live in no ``package.json``, so ``npm audit``
never sees them. This gate closes that hole: it reads the single source of truth
(``web/static/vendor/THIRD_PARTY_LICENSES.md`` via ``memtomem.web.vendor_manifest``)
and queries OSV.dev for every ``npm package`` / ``Version`` pair.

Exit status:
* ``0`` — no un-ignored advisory, and every ignore entry is in-date and used.
* ``1`` — an un-ignored advisory, an EXPIRED ignore, or a STALE ignore (matches
  no current advisory). A transient OSV outage also exits non-zero, with a
  distinct ``INFRA`` message so a rerun is the obvious fix (fail closed, never
  silently pass a security gate).

The ignore file (``tools/vendored_advisory_ignore.toml``) is the reviewed,
time-boxed exception valve recommended by the design review: a future advisory
with no fixed release or a disputed one is suppressed with an owner + expiry
instead of disabling the whole job. Empty by default.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem.web import vendor_manifest as vm

OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"
IGNORE_FILE = Path(__file__).resolve().parent / "vendored_advisory_ignore.toml"
_REQUIRED_IGNORE_KEYS = ("id", "package", "version", "reason", "owner", "expires")

# (npm, version, advisory_id)
Advisory = tuple[str, str, str]
Opener = Callable[..., Any]


class OsvUnreachable(RuntimeError):
    """OSV could not be queried (network / malformed response) — fail closed."""


@dataclass(frozen=True)
class Ignore:
    id: str
    package: str
    version: str
    reason: str
    owner: str
    expires: dt.date

    def matches(self, advisory_id: str, package: str, version: str) -> bool:
        return self.id == advisory_id and self.package == package and self.version == version


@dataclass(frozen=True)
class Outcome:
    advisories: list[Advisory]
    unsuppressed: list[Advisory]
    expired: list[Ignore]
    stale: list[Ignore]

    @property
    def ok(self) -> bool:
        return not (self.unsuppressed or self.expired or self.stale)


def load_ignores(path: Path = IGNORE_FILE) -> list[Ignore]:
    if not path.is_file():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    out: list[Ignore] = []
    for raw in data.get("ignore", []):
        missing = [k for k in _REQUIRED_IGNORE_KEYS if k not in raw]
        if missing:
            raise ValueError(f"ignore entry missing keys {missing}: {raw!r}")
        expires = raw["expires"]
        if isinstance(expires, dt.datetime):
            expires = expires.date()
        elif isinstance(expires, str):
            expires = dt.date.fromisoformat(expires)
        elif not isinstance(expires, dt.date):
            raise ValueError(f"ignore 'expires' must be a YYYY-MM-DD date: {raw!r}")
        out.append(
            Ignore(
                id=str(raw["id"]),
                package=str(raw["package"]),
                version=str(raw["version"]),
                reason=str(raw["reason"]),
                owner=str(raw["owner"]),
                expires=expires,
            )
        )
    return out


def query_osv(
    pairs: list[tuple[str, str]],
    *,
    opener: Opener = urllib.request.urlopen,
    retries: int = 3,
) -> list[Advisory]:
    """Batch-query OSV for npm (name, version) pairs. Raise OsvUnreachable on failure."""
    if not pairs:
        return []
    queries = [{"package": {"name": n, "ecosystem": "npm"}, "version": v} for n, v in pairs]
    body = json.dumps({"queries": queries}).encode()
    req = urllib.request.Request(
        OSV_BATCH_URL, data=body, headers={"Content-Type": "application/json"}
    )
    last_err: Exception | None = None
    data: Any = None
    for _ in range(retries):
        try:
            with opener(req, timeout=30) as resp:
                data = json.load(resp)
            break
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_err = exc
    else:
        raise OsvUnreachable(f"OSV unreachable after {retries} attempts: {last_err!r}")

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or len(results) != len(pairs):
        raise OsvUnreachable(f"malformed OSV response (results != {len(pairs)} queries): {data!r}")

    advisories: list[Advisory] = []
    for (name, version), result in zip(pairs, results):
        # A clean query is an empty dict ``{}``; anything else non-dict (None, a
        # list, a string) is malformed and must fail closed — not be read as
        # "no vulns" via a falsy default.
        if not isinstance(result, dict):
            raise OsvUnreachable(f"malformed OSV result for {name}@{version}: {result!r}")
        vulns = result.get("vulns", [])
        if not isinstance(vulns, list):
            raise OsvUnreachable(f"malformed 'vulns' for {name}@{version}: {vulns!r}")
        for vuln in vulns:
            # A present vuln with no usable id means an advisory we can't name —
            # treating it as clean would be the false pass this gate prevents.
            if not isinstance(vuln, dict) or not vuln.get("id"):
                raise OsvUnreachable(f"malformed advisory entry for {name}@{version}: {vuln!r}")
            advisories.append((name, version, str(vuln["id"])))
    return advisories


def classify_advisories(
    advisories: Iterable[Advisory], ignores: Iterable[Ignore], today: dt.date
) -> Outcome:
    advisories = list(advisories)
    ignores = list(ignores)
    expired = [ig for ig in ignores if ig.expires < today]
    active = [ig for ig in ignores if ig.expires >= today]

    used: set[int] = set()
    unsuppressed: list[Advisory] = []
    for name, version, advisory_id in advisories:
        match = next((ig for ig in active if ig.matches(advisory_id, name, version)), None)
        if match is None:
            unsuppressed.append((name, version, advisory_id))
        else:
            used.add(id(match))
    stale = [ig for ig in active if id(ig) not in used]
    return Outcome(advisories, unsuppressed, expired, stale)


def run(
    *,
    today: dt.date | None = None,
    opener: Opener = urllib.request.urlopen,
    out: Any = sys.stdout,
) -> int:
    today = today or dt.date.today()
    assets = vm.parse_vendor_table()
    pairs = vm.npm_version_pairs(assets)
    ignores = load_ignores()

    def emit(msg: str = "") -> None:
        print(msg, file=out)

    emit(f"Checking {len(pairs)} vendored npm package/version pair(s) against OSV:")
    for name, version in pairs:
        emit(f"  - {name}@{version}")

    try:
        advisories = query_osv(pairs, opener=opener)
    except OsvUnreachable as exc:
        emit(f"\nINFRA: {exc}")
        emit("Could not reach OSV — failing closed. Re-run the job once OSV is reachable.")
        return 1

    outcome = classify_advisories(advisories, ignores, today)

    if outcome.unsuppressed:
        emit("\nADVISORY: vendored libraries have un-ignored OSV advisories:")
        for name, version, advisory_id in outcome.unsuppressed:
            emit(
                f"  - {name}@{version}: {advisory_id}  (https://osv.dev/vulnerability/{advisory_id})"
            )
        emit(
            "\nBump the vendored asset to a fixed version (see web/static/vendor/README.md), or "
            "add a reviewed, expiring entry to tools/vendored_advisory_ignore.toml."
        )
    if outcome.expired:
        emit("\nIGNORE EXPIRED — these suppressions lapsed and must be re-reviewed or removed:")
        for ig in outcome.expired:
            emit(f"  - {ig.id} ({ig.package}@{ig.version}) expired {ig.expires.isoformat()}")
    if outcome.stale:
        emit("\nIGNORE STALE — these suppressions match no current advisory; remove them:")
        for ig in outcome.stale:
            emit(f"  - {ig.id} ({ig.package}@{ig.version})")

    if outcome.ok:
        emit("\nOK: no known advisories in the vendored frontend libraries.")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
