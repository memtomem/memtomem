#!/usr/bin/env python3
"""Fail-closed release tag, CI, and distribution contract checks."""

from __future__ import annotations

import argparse
import email
import json
import os
import re
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable


_PROD_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)$")
_TEST_TAG_RE = re.compile(r"^test-v(?P<version>\d+\.\d+\.\d+)a\d+$")
_REQUIRED_DIRECT = {
    "cryptography>=48.0.1",
    "starlette>=1.3.1",
    "idna>=3.15",
    "pyjwt>=2.13.0",
    "python-multipart>=0.0.27",
}
_REQUIRED_URLLIB3_EXTRAS = {"all", "langfuse", "onnx"}
_TERMINAL_FAILURES = {"action_required", "cancelled", "failure", "stale", "timed_out"}


class ReleaseCheckError(RuntimeError):
    """A release invariant was not met."""


def version_from_tag(tag: str) -> str:
    """Return the distribution version encoded by a supported release tag."""
    for pattern in (_PROD_TAG_RE, _TEST_TAG_RE):
        match = pattern.fullmatch(tag)
        if match:
            return match.group("version")
    raise ReleaseCheckError(f"unsupported release tag {tag!r}; expected vX.Y.Z or test-vX.Y.ZaN")


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseCheckError(f"cannot read valid TOML from {path}: {exc}") from exc


def project_version(repo_root: Path) -> str:
    pyproject = repo_root / "packages" / "memtomem" / "pyproject.toml"
    value = _load_toml(pyproject).get("project", {}).get("version")
    if not isinstance(value, str) or not value:
        raise ReleaseCheckError(f"missing project.version in {pyproject}")
    return value


def lock_version(repo_root: Path) -> str:
    lock_path = repo_root / "uv.lock"
    packages = _load_toml(lock_path).get("package", [])
    matches = [
        row
        for row in packages
        if isinstance(row, dict)
        and row.get("name") == "memtomem"
        and row.get("source") == {"editable": "packages/memtomem"}
    ]
    if len(matches) != 1:
        raise ReleaseCheckError(
            f"expected one editable memtomem package in {lock_path}, found {len(matches)}"
        )
    value = matches[0].get("version")
    if not isinstance(value, str) or not value:
        raise ReleaseCheckError(f"missing memtomem version in {lock_path}")
    return value


def validate_contract(tag: str, repo_root: Path) -> str:
    """Validate tag, member metadata, lock metadata, and changelog parity."""
    expected = version_from_tag(tag)
    actual_project = project_version(repo_root)
    actual_lock = lock_version(repo_root)
    if actual_project != expected:
        raise ReleaseCheckError(
            f"tag version {expected} does not match package version {actual_project}"
        )
    if actual_lock != expected:
        raise ReleaseCheckError(f"tag version {expected} does not match lock version {actual_lock}")
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(expected)}\](?:\s|$)", changelog, re.MULTILINE):
        raise ReleaseCheckError(f"CHANGELOG.md has no release heading for {expected}")
    return expected


def _metadata_from_wheel(path: Path) -> email.message.Message:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise ReleaseCheckError(f"expected one METADATA in {path}, found {len(names)}")
        return email.message_from_bytes(archive.read(names[0]))


def _metadata_from_sdist(path: Path) -> email.message.Message:
    with tarfile.open(path, mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")]
        if len(members) != 1:
            raise ReleaseCheckError(f"expected one PKG-INFO in {path}, found {len(members)}")
        extracted = archive.extractfile(members[0])
        if extracted is None:
            raise ReleaseCheckError(f"cannot read PKG-INFO from {path}")
        return email.message_from_bytes(extracted.read())


def _normalized_requirement(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _validate_metadata(message: email.message.Message, expected: str, label: str) -> None:
    if message.get("Name", "").lower() != "memtomem":
        raise ReleaseCheckError(f"{label} metadata has unexpected Name: {message.get('Name')!r}")
    if message.get("Version") != expected:
        raise ReleaseCheckError(
            f"{label} metadata version {message.get('Version')!r} does not match {expected}"
        )
    requirements = {
        _normalized_requirement(value) for value in message.get_all("Requires-Dist", [])
    }
    missing_direct = {
        requirement for requirement in _REQUIRED_DIRECT if requirement.lower() not in requirements
    }
    if missing_direct:
        raise ReleaseCheckError(f"{label} metadata misses direct floors: {sorted(missing_direct)}")
    urllib3_extras: set[str] = set()
    for requirement in requirements:
        if not requirement.startswith("urllib3>=2.7.0;"):
            continue
        match = re.search(r"extra==[\'\"]([^\'\"]+)[\'\"]", requirement)
        if match:
            urllib3_extras.add(match.group(1))
    missing_extras = _REQUIRED_URLLIB3_EXTRAS - urllib3_extras
    if missing_extras:
        raise ReleaseCheckError(
            f"{label} metadata misses urllib3>=2.7.0 extras: {sorted(missing_extras)}"
        )


def validate_artifacts(dist: Path, expected: str) -> tuple[Path, Path]:
    """Validate exactly one wheel and sdist plus their published metadata contract."""
    wheels = sorted(dist.glob("memtomem-*.whl"))
    sdists = sorted(dist.glob("memtomem-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ReleaseCheckError(
            f"expected one wheel and one sdist in {dist}; found {len(wheels)} and {len(sdists)}"
        )
    _validate_metadata(_metadata_from_wheel(wheels[0]), expected, "wheel")
    _validate_metadata(_metadata_from_sdist(sdists[0]), expected, "sdist")
    return wheels[0], sdists[0]


def _request_runs(repository: str, sha: str, token: str) -> list[dict[str, Any]]:
    encoded_sha = urllib.parse.quote(sha, safe="")
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/ci.yml/runs"
        f"?event=push&head_sha={encoded_sha}&per_page=20"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "memtomem-release-preflight",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        payload = json.load(response)
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise ReleaseCheckError("GitHub workflow-runs response has no workflow_runs list")
    return [row for row in runs if isinstance(row, dict)]


def wait_for_exact_main_ci(
    *,
    repository: str,
    sha: str,
    token: str,
    timeout_seconds: float,
    interval_seconds: float,
    fetch_runs: Callable[[str, str, str], list[dict[str, Any]]] = _request_runs,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for the exact main-push CI run, failing closed on errors or failures."""
    deadline = monotonic() + timeout_seconds
    consecutive_errors = 0
    while monotonic() < deadline:
        try:
            rows = fetch_runs(repository, sha, token)
            consecutive_errors = 0
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ReleaseCheckError) as exc:
            consecutive_errors += 1
            if consecutive_errors >= 3:
                raise ReleaseCheckError(
                    f"GitHub Actions API failed {consecutive_errors} consecutive times: {exc}"
                ) from exc
            sleep(interval_seconds)
            continue

        exact = [
            row
            for row in rows
            if row.get("head_sha") == sha
            and row.get("head_branch") == "main"
            and row.get("event") == "push"
        ]
        successes = [
            row
            for row in exact
            if row.get("status") == "completed" and row.get("conclusion") == "success"
        ]
        if successes:
            return successes[0]
        failures = [
            row
            for row in exact
            if row.get("status") == "completed" and row.get("conclusion") in _TERMINAL_FAILURES
        ]
        if failures:
            run = failures[0]
            raise ReleaseCheckError(
                f"exact main CI run {run.get('id')} completed as {run.get('conclusion')}"
            )
        sleep(interval_seconds)
    raise ReleaseCheckError(
        f"timed out after {timeout_seconds:g}s waiting for successful main CI at {sha}"
    )


def _write_github_output(path: str | None, key: str, value: str) -> None:
    if path:
        with Path(path).open("a", encoding="utf-8") as handle:
            handle.write(f"{key}={value}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    contract = subparsers.add_parser("contract")
    contract.add_argument("--tag", required=True)
    contract.add_argument("--repo-root", type=Path, default=Path.cwd())
    contract.add_argument("--github-output")

    artifacts = subparsers.add_parser("artifacts")
    artifacts.add_argument("--dist", type=Path, required=True)
    artifacts.add_argument("--version", required=True)

    wait_ci = subparsers.add_parser("wait-ci")
    wait_ci.add_argument("--repository", required=True)
    wait_ci.add_argument("--sha", required=True)
    wait_ci.add_argument("--timeout-seconds", type=float, default=2400)
    wait_ci.add_argument("--interval-seconds", type=float, default=15)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "contract":
            version = validate_contract(args.tag, args.repo_root.resolve())
            _write_github_output(args.github_output, "version", version)
            print(version)
        elif args.command == "artifacts":
            wheel, sdist = validate_artifacts(args.dist.resolve(), args.version)
            print(f"validated {wheel.name} and {sdist.name}")
        else:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise ReleaseCheckError("GITHUB_TOKEN is required for wait-ci")
            run = wait_for_exact_main_ci(
                repository=args.repository,
                sha=args.sha,
                token=token,
                timeout_seconds=args.timeout_seconds,
                interval_seconds=args.interval_seconds,
            )
            print(f"exact main CI succeeded: {run.get('html_url') or run.get('id')}")
    except (OSError, UnicodeError, ReleaseCheckError) as exc:
        print(f"release preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
