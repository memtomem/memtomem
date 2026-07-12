"""Tests for the fail-closed release preflight helper."""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "release_preflight.py"
    spec = importlib.util.spec_from_file_location("release_preflight", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = _load_tool()


def _repo(tmp_path: Path, *, version: str = "0.3.6", lock_version: str | None = None) -> Path:
    package = tmp_path / "packages" / "memtomem"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        f'[project]\nname = "memtomem"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        "version = 1\n\n"
        "[[package]]\n"
        'name = "memtomem"\n'
        f'version = "{lock_version or version}"\n'
        'source = { editable = "packages/memtomem" }\n',
        encoding="utf-8",
    )
    (tmp_path / "CHANGELOG.md").write_text(f"## [{version}] — 2026-07-12\n", encoding="utf-8")
    return tmp_path


def _metadata(version: str = "0.3.6") -> bytes:
    requirements = [
        "cryptography>=48.0.1",
        "starlette>=1.3.1",
        "idna>=3.15",
        "pyjwt>=2.13.0",
        "python-multipart>=0.0.27",
        'urllib3>=2.7.0; extra == "all"',
        'urllib3>=2.7.0; extra == "onnx"',
        'urllib3>=2.7.0; extra == "langfuse"',
    ]
    lines = ["Metadata-Version: 2.4", "Name: memtomem", f"Version: {version}"]
    lines.extend(f"Requires-Dist: {requirement}" for requirement in requirements)
    return ("\n".join(lines) + "\n\n").encode()


def _dist(tmp_path: Path, *, version: str = "0.3.6", metadata: bytes | None = None) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    body = metadata or _metadata(version)
    wheel = dist / f"memtomem-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"memtomem-{version}.dist-info/METADATA", body)
    sdist = dist / f"memtomem-{version}.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        info = tarfile.TarInfo(f"memtomem-{version}/PKG-INFO")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    return dist


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("v0.3.6", "0.3.6"), ("test-v0.3.6a1", "0.3.6"), ("test-v1.2.3a99", "1.2.3")],
)
def test_version_from_tag(tag: str, expected: str) -> None:
    assert rp.version_from_tag(tag) == expected


@pytest.mark.parametrize("tag", ["0.3.6", "v0.3.6a1", "test-v0.3.6", "test-v0.3.6rc1"])
def test_version_from_tag_rejects_unsupported_shapes(tag: str) -> None:
    with pytest.raises(rp.ReleaseCheckError):
        rp.version_from_tag(tag)


def test_contract_accepts_matching_project_lock_and_changelog(tmp_path: Path) -> None:
    assert rp.validate_contract("v0.3.6", _repo(tmp_path)) == "0.3.6"


def test_contract_rejects_lock_drift(tmp_path: Path) -> None:
    with pytest.raises(rp.ReleaseCheckError, match="lock version"):
        rp.validate_contract("v0.3.6", _repo(tmp_path, lock_version="0.3.5"))


def test_contract_rejects_missing_changelog_heading(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CHANGELOG.md").write_text("## [Unreleased]\n", encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="CHANGELOG"):
        rp.validate_contract("v0.3.6", repo)


def test_artifacts_accept_wheel_and_sdist_contract(tmp_path: Path) -> None:
    wheel, sdist = rp.validate_artifacts(_dist(tmp_path), "0.3.6")
    assert wheel.suffix == ".whl"
    assert sdist.name.endswith(".tar.gz")


def test_artifacts_reject_missing_floor(tmp_path: Path) -> None:
    body = _metadata().replace(b"Requires-Dist: starlette>=1.3.1\n", b"")
    with pytest.raises(rp.ReleaseCheckError, match="direct floors"):
        rp.validate_artifacts(_dist(tmp_path, metadata=body), "0.3.6")


def test_artifacts_reject_missing_extra_marker(tmp_path: Path) -> None:
    body = _metadata().replace(b'Requires-Dist: urllib3>=2.7.0; extra == "onnx"\n', b"")
    with pytest.raises(rp.ReleaseCheckError, match="urllib3"):
        rp.validate_artifacts(_dist(tmp_path, metadata=body), "0.3.6")


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _run(*, status: str, conclusion: str | None, sha: str = "abc", branch: str = "main"):
    return {
        "id": 123,
        "html_url": "https://example.test/run/123",
        "head_sha": sha,
        "head_branch": branch,
        "event": "push",
        "status": status,
        "conclusion": conclusion,
    }


def test_wait_ci_ignores_wrong_sha_then_accepts_success() -> None:
    clock = _Clock()
    responses = iter(
        [
            [_run(status="completed", conclusion="success", sha="other")],
            [_run(status="completed", conclusion="success")],
        ]
    )
    result = rp.wait_for_exact_main_ci(
        repository="memtomem/memtomem",
        sha="abc",
        token="token",
        timeout_seconds=10,
        interval_seconds=1,
        fetch_runs=lambda *_: next(responses),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["id"] == 123


def test_wait_ci_fails_immediately_on_exact_failed_run() -> None:
    clock = _Clock()
    with pytest.raises(rp.ReleaseCheckError, match="failure"):
        rp.wait_for_exact_main_ci(
            repository="memtomem/memtomem",
            sha="abc",
            token="token",
            timeout_seconds=10,
            interval_seconds=1,
            fetch_runs=lambda *_: [_run(status="completed", conclusion="failure")],
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_wait_ci_fails_closed_after_three_api_errors() -> None:
    clock = _Clock()

    def fail(*_args):
        raise OSError("offline")

    with pytest.raises(rp.ReleaseCheckError, match="3 consecutive"):
        rp.wait_for_exact_main_ci(
            repository="memtomem/memtomem",
            sha="abc",
            token="token",
            timeout_seconds=10,
            interval_seconds=1,
            fetch_runs=fail,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_wait_ci_times_out_when_no_exact_run_appears() -> None:
    clock = _Clock()
    with pytest.raises(rp.ReleaseCheckError, match="timed out"):
        rp.wait_for_exact_main_ci(
            repository="memtomem/memtomem",
            sha="abc",
            token="token",
            timeout_seconds=2,
            interval_seconds=1,
            fetch_runs=lambda *_: [],
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
