"""The langchain-memtomem release gates in ``tools/release_preflight.py`` (ADR-0042 §A3).

Each refusal has a test that builds the otherwise-valid input and changes the
one thing the gate is there to catch, so a gate that stopped checking fails
here rather than passing a bad tag.
"""

from __future__ import annotations

import copy
import email.message
import fnmatch
import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from .test_release_preflight import _single_command_argv

_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "release_preflight.py"
    spec = importlib.util.spec_from_file_location("release_preflight_langchain", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = _load_tool()

_DEPS = '"langgraph>=1.0,<2",\n    "memtomem>=0.7.4,<0.8",'


def _repo(
    tmp_path: Path,
    *,
    core: str = "0.7.4",
    version: str = "0.1.0",
    runtime: str = '__version__ = "0.1.0"\n',
    lock: str | None = "0.1.0",
    changelog: str = "## [Unreleased]\n\n## [0.1.0] — 2026-11-04\n\n- first\n",
    deps: str = _DEPS,
) -> Path:
    core_dir = tmp_path / "packages" / "memtomem"
    core_dir.mkdir(parents=True)
    (core_dir / "pyproject.toml").write_text(
        f'[project]\nname = "memtomem"\nversion = "{core}"\n', encoding="utf-8"
    )
    package = tmp_path / "packages" / "langchain-memtomem"
    (package / "src" / "langchain_memtomem").mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        f'[project]\nname = "langchain-memtomem"\nversion = "{version}"\n'
        f'requires-python = ">=3.12"\ndependencies = [\n    {deps}\n]\n',
        encoding="utf-8",
    )
    (package / "src" / "langchain_memtomem" / "__init__.py").write_text(
        f'"""doc"""\n\n{runtime}', encoding="utf-8"
    )
    (package / "CHANGELOG.md").write_text(f"# Changelog\n\n{changelog}", encoding="utf-8")
    rows = [
        '[[package]]\nname = "memtomem"\nversion = "' + core + '"\n'
        'source = { editable = "packages/memtomem" }\n'
    ]
    if lock is not None:
        rows.append(
            '[[package]]\nname = "langchain-memtomem"\nversion = "' + lock + '"\n'
            'source = { editable = "packages/langchain-memtomem" }\n'
        )
    (tmp_path / "uv.lock").write_text("version = 1\n\n" + "\n".join(rows), encoding="utf-8")
    return tmp_path


# --- tags --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("langchain-v0.1.0", ("0.1.0", False)), ("test-langchain-v0.1.0a3", ("0.1.0", True))],
)
def test_langchain_tags_name_a_version(tag: str, expected: tuple[str, bool]) -> None:
    assert rp.langchain_version_from_tag(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "v0.1.0",
        "test-v0.1.0a1",
        "langchain-v0.1",
        "langchain-v0.1.0a1",
        "test-langchain-v0.1.0",
        "langchain-memtomem-v0.1.0",
        " langchain-v0.1.0",
    ],
)
def test_other_tags_are_refused(tag: str) -> None:
    with pytest.raises(rp.ReleaseCheckError, match="unsupported tag"):
        rp.langchain_version_from_tag(tag)


# --- contract ----------------------------------------------------------------


def test_contract_accepts_a_consistent_package(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert rp.validate_langchain_contract("langchain-v0.1.0", repo) == ("0.1.0", "0.7.4")
    assert rp.validate_langchain_contract("test-langchain-v0.1.0a1", repo) == ("0.1.0", "0.7.4")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"version": "0.1.1"}, "does not match"),
        ({"runtime": '__version__ = "0.0.9"\n'}, "__version__"),
        ({"runtime": '__version__ = "0.1.0"\n__version__ = "0.1.0"\n'}, "__version__"),
        ({"runtime": "VERSION = '0.1.0'\n"}, "__version__"),
        ({"lock": "0.0.9"}, "uv.lock"),
        ({"lock": None}, "uv.lock"),
        ({"changelog": "## [0.1.0]\n"}, "YYYY-MM-DD"),
        ({"changelog": "## [Unreleased]\n"}, "YYYY-MM-DD"),
        ({"changelog": "## [0.1.0] — 2026-11-04\n\n## [0.1.0] — 2026-11-05\n"}, "YYYY-MM-DD"),
        ({"changelog": "## [0.1.0] - 2026-11-04\n"}, "YYYY-MM-DD"),
        # A floor above the checkout's core was never installed by CI.
        ({"core": "0.7.3"}, "does not admit"),
        # An upper bound the checkout's core already reaches.
        ({"core": "0.8.0"}, "does not admit"),
    ],
)
def test_contract_refuses_each_disagreement(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(rp.ReleaseCheckError, match=message):
        rp.validate_langchain_contract("langchain-v0.1.0", _repo(tmp_path, **overrides))


@pytest.mark.parametrize(
    "requirement",
    [
        '"memtomem>=0.7.4"',
        '"memtomem<0.8"',
        '"memtomem>=0.7.4,<0.8,!=0.7.5"',
        '"memtomem>=0.7,<0.8"',
        '"memtomem==0.7.4"',
        '"memtomem>=0.7.4,<=0.8"',
        '"memtomem[onnx]>=0.7.4,<0.8"',
        "\"memtomem>=0.7.4,<0.8; python_version>='3.12'\"",
    ],
)
def test_core_bounds_must_be_a_floor_and_an_upper_bound(tmp_path: Path, requirement: str) -> None:
    repo = _repo(tmp_path, deps=f'"langgraph>=1.0,<2",\n    {requirement},')
    with pytest.raises(rp.ReleaseCheckError):
        rp.langchain_core_bounds(repo)


def test_core_bounds_ignore_whitespace_and_order(tmp_path: Path) -> None:
    repo = _repo(tmp_path, deps='"memtomem < 0.8 , >= 0.7.4",')
    assert rp.langchain_core_bounds(repo) == ("0.7.4", "0.8")


def test_missing_core_dependency_is_refused(tmp_path: Path) -> None:
    with pytest.raises(rp.ReleaseCheckError, match="does not depend on memtomem"):
        rp.langchain_core_bounds(_repo(tmp_path, deps='"langgraph>=1.0,<2",'))


def test_the_checkout_keeps_a_floor_its_own_core_satisfies() -> None:
    """The invariant the core version bump must keep.

    CI installs the package beside the checkout's core wheel, so the floor
    may rise only once the workspace core has reached it.
    """
    floor, upper = rp.langchain_core_bounds(_ROOT)
    core = rp.project_version(_ROOT)
    assert rp._version_tuple(floor) <= rp._version_tuple(core) < rp._version_tuple(upper)


# --- PyPI --------------------------------------------------------------------


class _Index:
    """A fake ``fetch_json`` keyed by URL; any other URL fails the test."""

    def __init__(self, replies: dict[str, int]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    def __call__(self, url: str, timeout: float) -> tuple[int, Any]:
        self.calls.append(url)
        if url not in self.replies:
            pytest.fail(f"unexpected URL {url}")
        status = self.replies[url]
        if status == 200 and "/memtomem/" in url:
            version = url.split("/")[-2]
            return 200, {"info": {"name": "memtomem", "version": version}}
        return status, {} if status == 200 else None


_FLOOR_URL = "https://pypi.org/pypi/memtomem/0.7.4/json"
_PROD_URL = "https://pypi.org/pypi/langchain-memtomem/0.1.0/json"
_TEST_URL = "https://test.pypi.org/pypi/langchain-memtomem/0.1.0/json"


def test_pypi_gate_passes_with_the_floor_published_and_the_version_free(tmp_path: Path) -> None:
    index = _Index({_FLOOR_URL: 200, _PROD_URL: 404})
    assert rp.validate_langchain_pypi("langchain-v0.1.0", _repo(tmp_path), fetch_json=index) == (
        "0.7.4"
    )
    assert index.calls == [_FLOOR_URL, _PROD_URL]


def test_a_test_tag_checks_testpypi_for_the_version(tmp_path: Path) -> None:
    index = _Index({_FLOOR_URL: 200, _TEST_URL: 404})
    rp.validate_langchain_pypi("test-langchain-v0.1.0a1", _repo(tmp_path), fetch_json=index)
    assert index.calls == [_FLOOR_URL, _TEST_URL]


@pytest.mark.parametrize("status", [404, 503])
def test_pypi_gate_refuses_an_unverified_floor(tmp_path: Path, status: int) -> None:
    index = _Index({_FLOOR_URL: status})
    with pytest.raises(rp.ReleaseCheckError, match="Publish memtomem 0.7.4 first"):
        rp.validate_langchain_pypi("langchain-v0.1.0", _repo(tmp_path), fetch_json=index)


def test_pypi_gate_refuses_a_version_already_published(tmp_path: Path) -> None:
    index = _Index({_FLOOR_URL: 200, _PROD_URL: 200})
    with pytest.raises(rp.ReleaseCheckError, match="would upload nothing"):
        rp.validate_langchain_pypi("langchain-v0.1.0", _repo(tmp_path), fetch_json=index)


def test_pypi_gate_refuses_when_the_version_cannot_be_confirmed(tmp_path: Path) -> None:
    index = _Index({_FLOOR_URL: 200, _TEST_URL: 500})
    with pytest.raises(rp.ReleaseCheckError, match="cannot confirm"):
        rp.validate_langchain_pypi("test-langchain-v0.1.0a1", _repo(tmp_path), fetch_json=index)


# --- artifacts ---------------------------------------------------------------


def _metadata(
    *,
    name: str = "langchain-memtomem",
    version: str = "0.1.0",
    requires: tuple[str, ...] = ("langgraph<2,>=1.0", "memtomem<0.8,>=0.7.4"),
    python: str = ">=3.12",
) -> bytes:
    message = email.message.Message()
    message["Metadata-Version"] = "2.5"
    message["Name"] = name
    message["Version"] = version
    message["Requires-Python"] = python
    for requirement in requires:
        message["Requires-Dist"] = requirement
    return message.as_bytes()


def _wheel(
    dist: Path,
    *,
    metadata: bytes | None = None,
    extra: tuple[str, ...] = (),
    dist_info: str = "langchain_memtomem-0.1.0.dist-info",
) -> Path:
    path = dist / "langchain_memtomem-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("langchain_memtomem/__init__.py", "")
        archive.writestr(f"{dist_info}/METADATA", metadata or _metadata())
        for name in extra:
            archive.writestr(name, "")
    return path


_SDIST_FILES = (
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "docs/protocol.md",
    "pyproject.toml",
    "src/langchain_memtomem/__init__.py",
)


def _sdist(
    dist: Path,
    *,
    metadata: bytes | None = None,
    files: tuple[str, ...] = _SDIST_FILES,
) -> Path:
    path = dist / "langchain_memtomem-0.1.0.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        entries = {name: b"" for name in files}
        entries["PKG-INFO"] = metadata or _metadata()
        for name, data in entries.items():
            info = tarfile.TarInfo(f"langchain_memtomem-0.1.0/{name}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def _dist(tmp_path: Path, **wheel: Any) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    _wheel(dist, **wheel)
    _sdist(dist)
    return dist


def test_artifacts_accept_reordered_requirements_and_uv_gitignore(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    dist = _dist(tmp_path)
    (dist / ".gitignore").write_text("*", encoding="utf-8")
    wheel, sdist = rp.validate_langchain_artifacts(dist, "0.1.0", repo)
    assert (wheel.name, sdist.name) == (
        "langchain_memtomem-0.1.0-py3-none-any.whl",
        "langchain_memtomem-0.1.0.tar.gz",
    )


def test_artifacts_refuse_a_stray_distribution_in_the_upload_dir(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "repo")
    dist = _dist(tmp_path)
    (dist / "memtomem-0.7.4-py3-none-any.whl").write_bytes(b"")
    with pytest.raises(rp.ReleaseCheckError, match="must hold exactly"):
        rp.validate_langchain_artifacts(dist, "0.1.0", repo)


def test_artifacts_refuse_another_version(tmp_path: Path) -> None:
    with pytest.raises(rp.ReleaseCheckError, match="must hold exactly"):
        rp.validate_langchain_artifacts(_dist(tmp_path), "0.1.1", _repo(tmp_path / "repo"))


@pytest.mark.parametrize(
    ("wheel", "message"),
    [
        ({"extra": ("tests/test_x.py",)}, "unexpected top-level"),
        ({"extra": ("memtomem/__init__.py",)}, "unexpected top-level"),
        ({"dist_info": "langchain_memtomem-0.0.9.dist-info"}, "unexpected top-level"),
        ({"metadata": _metadata(name="memtomem")}, "metadata is"),
        ({"metadata": _metadata(version="0.1.1")}, "metadata is"),
        ({"metadata": _metadata(python=">=3.11")}, "Requires-Python"),
        ({"metadata": _metadata(requires=("langgraph<2,>=1.0",))}, "Requires-Dist"),
        (
            {"metadata": _metadata(requires=("langgraph<2,>=1.0", "memtomem<0.8,>=0.7.3"))},
            "Requires-Dist",
        ),
        (
            {
                "metadata": _metadata(
                    requires=("langgraph<2,>=1.0", "memtomem<0.8,>=0.7.4", "requests")
                )
            },
            "Requires-Dist",
        ),
    ],
)
def test_artifacts_refuse_a_wheel_that_is_not_the_package(
    tmp_path: Path, wheel: dict[str, Any], message: str
) -> None:
    with pytest.raises(rp.ReleaseCheckError, match=message):
        rp.validate_langchain_artifacts(_dist(tmp_path, **wheel), "0.1.0", _repo(tmp_path / "r"))


@pytest.mark.parametrize("missing", ["docs/protocol.md", "LICENSE", "README.md"])
def test_artifacts_refuse_an_sdist_without_a_required_file(tmp_path: Path, missing: str) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _wheel(dist)
    _sdist(dist, files=tuple(name for name in _SDIST_FILES if name != missing))
    with pytest.raises(rp.ReleaseCheckError, match="lacks"):
        rp.validate_langchain_artifacts(dist, "0.1.0", _repo(tmp_path / "repo"))


def test_artifacts_refuse_an_sdist_with_an_unexpected_member(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    _wheel(dist)
    _sdist(dist, files=(*_SDIST_FILES, "tests/test_x.py"))
    with pytest.raises(rp.ReleaseCheckError, match="unexpected member"):
        rp.validate_langchain_artifacts(dist, "0.1.0", _repo(tmp_path / "repo"))


def test_core_wheel_check_still_refuses_another_package(tmp_path: Path) -> None:
    """Parameterising the wheel reader kept the core default: ``memtomem`` only."""
    path = tmp_path / "memtomem-0.7.4-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("langchain_memtomem/__init__.py", "")
        archive.writestr("memtomem-0.7.4.dist-info/METADATA", _metadata(name="memtomem"))
    with pytest.raises(rp.ReleaseCheckError, match="unexpected top-level"):
        rp._metadata_from_wheel(path)


# --- the workflow --------------------------------------------------------------


def _workflow(name: str) -> dict:
    return yaml.safe_load((_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _tag_patterns(document: dict) -> list[str]:
    # PyYAML reads the bare key ``on`` as the boolean True.
    trigger = document.get("on", document.get(True))
    return trigger["push"]["tags"]


_PUBLISHERS = (
    "release.yml",
    "release-langchain.yml",
    "release-opencode-plugin.yml",
    "release-claude-plugin.yml",
)


@pytest.mark.parametrize(
    ("tag", "publisher"),
    [
        ("v0.7.4", "release.yml"),
        ("test-v0.7.4a1", "release.yml"),
        ("langchain-v0.1.0", "release-langchain.yml"),
        ("test-langchain-v0.1.0a1", "release-langchain.yml"),
        ("opencode-v0.3.16", "release-opencode-plugin.yml"),
        ("claude-plugin-v0.5.13", "release-claude-plugin.yml"),
    ],
)
def test_each_release_tag_starts_exactly_one_publisher(tag: str, publisher: str) -> None:
    started = [
        name
        for name in _PUBLISHERS
        if any(fnmatch.fnmatchcase(tag, pattern) for pattern in _tag_patterns(_workflow(name)))
    ]
    assert started == [publisher]


def _load_smoke() -> ModuleType:
    path = _ROOT / "tools" / "langchain_smoke.py"
    spec = importlib.util.spec_from_file_location("langchain_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_smoke()

_PREFLIGHT_PREFIX = (
    "uv",
    "run",
    "--no-project",
    "--python",
    "3.12",
    "python",
    "tools/release_preflight.py",
)
_SMOKE_PREFIX = (*_PREFLIGHT_PREFIX[:-1], "tools/langchain_smoke.py")
_VERSION = "${{ steps.contract.outputs.version }}"
_FLOOR = "${{ steps.contract.outputs.floor }}"
# Gates whose job is to stop the release; none may be conditional or allowed to fail.
_PREFLIGHT_GATES = (
    "contract",
    "on-main",
    "wait-ci",
    "lock-check",
    "pypi",
    "build",
    "artifacts",
    "smoke-wheel",
    "smoke-sdist",
)
_PUBLISH_GATES = ("build", "contract", "artifacts")


def _preflight_args(document: dict, job: str, step_id: str) -> Any:
    argv = _single_command_argv(document, job, step_id, _PREFLIGHT_PREFIX)
    return rp._build_parser().parse_args(argv[len(_PREFLIGHT_PREFIX) :])


def _smoke_args(document: dict, step_id: str) -> Any:
    argv = _single_command_argv(document, "preflight", step_id, _SMOKE_PREFIX)
    return smoke.build_parser().parse_args(argv[len(_SMOKE_PREFIX) :])


_UV_INPUTS = {"version": "${{ env.UV_VERSION }}"}
_UPLOADS = [
    (
        "${{ !startsWith(github.ref_name, 'test-') }}",
        {"packages-dir": "dist", "skip-existing": True},
    ),
    (
        "${{ startsWith(github.ref_name, 'test-') }}",
        {
            "repository-url": "https://test.pypi.org/legacy/",
            "packages-dir": "dist",
            "skip-existing": True,
        },
    ),
]


def _shape(step: dict) -> str | None:
    """An action by name (its pin is test_supply_chain_contracts.py's business), a run by id."""
    return step["uses"].split("@")[0] if "uses" in step else step.get("id")


def _assert_closed_job(job: dict, keys: set[str], gates: tuple[str, ...], checkout: dict) -> None:
    """The job is exactly this: these keys, these steps in this order, these inputs.

    Closed-world on purpose. A guard that checked only the steps it knew
    about let an added step through (a rebuild between validation and upload,
    a checkout of another ref); listing what is allowed refuses every
    addition at once, and a deliberate change edits this test with it.
    """
    assert set(job) == keys
    steps = job["steps"]
    shape = [_shape(step) for step in steps]
    assert shape[:2] == ["actions/checkout", "astral-sh/setup-uv"]
    assert shape[2 : 2 + len(gates)] == list(gates)
    assert set(steps[0]) == {"uses", "with"} and steps[0]["with"] == checkout
    assert set(steps[1]) == {"uses", "with"} and steps[1]["with"] == _UV_INPUTS
    for step in steps[2 : 2 + len(gates)]:
        allowed = {"name", "id", "run"} | ({"env"} if step["id"] == "wait-ci" else set())
        assert set(step) <= allowed, (step["id"], sorted(set(step) - allowed))


def _assert_langchain_workflow(document: dict) -> None:
    assert set(document) == {"name", True, "env", "jobs"}  # PyYAML reads ``on`` as True
    assert set(document["env"]) == {"UV_VERSION"}
    jobs = document["jobs"]
    assert set(jobs) == {"preflight", "publish"}
    preflight, publish = jobs["preflight"], jobs["publish"]
    assert preflight.get("permissions") == {"actions": "read", "contents": "read"}
    assert publish.get("permissions") == {"id-token": "write"}
    assert publish.get("needs") == "preflight"
    assert publish.get("environment", {}).get("name") == "pypi-langchain"

    common = {"name", "runs-on", "timeout-minutes", "permissions", "steps"}
    _assert_closed_job(
        preflight,
        common,
        _PREFLIGHT_GATES,
        {"fetch-depth": 0, "persist-credentials": False},
    )
    assert len(preflight["steps"]) == 2 + len(_PREFLIGHT_GATES)
    _assert_closed_job(
        publish,
        common | {"needs", "environment"},
        _PUBLISH_GATES,
        {"persist-credentials": False},
    )
    uploads = publish["steps"][2 + len(_PUBLISH_GATES) :]
    assert [_shape(step) for step in uploads] == ["pypa/gh-action-pypi-publish"] * 2
    assert [set(step) for step in uploads] == [{"name", "if", "uses", "with"}] * 2
    assert [(step["if"], step["with"]) for step in uploads] == _UPLOADS
    wait_env = next(s for s in preflight["steps"] if s.get("id") == "wait-ci").get("env")
    assert wait_env == {"GITHUB_TOKEN": "${{ github.token }}"}

    contract = _preflight_args(document, "preflight", "contract")
    assert (contract.command, contract.tag, contract.repo_root, contract.github_output) == (
        "langchain-contract",
        "$GITHUB_REF_NAME",
        Path("."),
        "$GITHUB_OUTPUT",
    )
    on_main = [line.strip() for line in _step(document, "preflight", "on-main")["run"].splitlines()]
    assert on_main == [
        "set -euo pipefail",
        "git fetch --no-tags origin main:refs/remotes/origin/main",
        'git merge-base --is-ancestor "$GITHUB_SHA" origin/main',
    ]
    wait = _preflight_args(document, "preflight", "wait-ci")
    assert (wait.command, wait.sha) == ("wait-ci", "$GITHUB_SHA")
    assert _single_command_argv(document, "preflight", "lock-check", ("uv",)) == [
        "uv",
        "lock",
        "--check",
    ]
    pypi = _preflight_args(document, "preflight", "pypi")
    assert (pypi.command, pypi.tag) == ("langchain-pypi", "$GITHUB_REF_NAME")
    assert _single_command_argv(document, "preflight", "build", ("uv",)) == [
        "uv",
        "build",
        "packages/langchain-memtomem",
        "--out-dir",
        "preflight-dist",
    ]
    artifacts = _preflight_args(document, "preflight", "artifacts")
    assert (artifacts.command, artifacts.dist, artifacts.version) == (
        "langchain-artifacts",
        Path("preflight-dist"),
        _VERSION,
    )
    for step_id, artifact in (
        ("smoke-wheel", f"preflight-dist/langchain_memtomem-{_VERSION}-py3-none-any.whl"),
        ("smoke-sdist", f"preflight-dist/langchain_memtomem-{_VERSION}.tar.gz"),
    ):
        args = _smoke_args(document, step_id)
        assert (args.command, args.artifact, args.core) == (
            "install",
            artifact,
            f"memtomem=={_FLOOR}",
        )
        assert (args.expect_version, args.expect_core) == (_VERSION, _FLOOR)

    assert _single_command_argv(document, "publish", "build", ("uv",)) == [
        "uv",
        "build",
        "packages/langchain-memtomem",
        "--out-dir",
        "dist",
    ]
    again = _preflight_args(document, "publish", "contract")
    assert (again.command, again.tag, again.github_output) == (
        "langchain-contract",
        "$GITHUB_REF_NAME",
        "$GITHUB_OUTPUT",
    )
    checked = _preflight_args(document, "publish", "artifacts")
    assert (checked.command, checked.dist, checked.version) == (
        "langchain-artifacts",
        Path("dist"),
        _VERSION,
    )


def test_the_langchain_workflow_runs_every_gate_before_the_upload() -> None:
    _assert_langchain_workflow(_workflow("release-langchain.yml"))


def _step(document: dict, job: str, step_id: str) -> dict:
    return next(s for s in document["jobs"][job]["steps"] if s.get("id") == step_id)


def _echo_contract(document: dict) -> None:
    step = _step(document, "preflight", "contract")
    step["run"] = "echo " + step["run"]


def _drop_github_output(document: dict) -> None:
    step = _step(document, "preflight", "contract")
    step["run"] = step["run"].replace(' --github-output "$GITHUB_OUTPUT"', "")


def _skip_pypi(document: dict) -> None:
    _step(document, "preflight", "pypi")["if"] = "false"


def _tolerate_smoke_failure(document: dict) -> None:
    _step(document, "preflight", "smoke-sdist")["continue-on-error"] = True


def _drop_publish_revalidation(document: dict) -> None:
    steps = document["jobs"]["publish"]["steps"]
    steps.remove(_step(document, "publish", "artifacts"))


def _upload_before_revalidation(document: dict) -> None:
    steps = document["jobs"]["publish"]["steps"]
    upload = next(s for s in steps if "gh-action-pypi-publish" in s.get("uses", ""))
    steps.remove(upload)
    steps.insert(2, upload)


def _smoke_newest_core(document: dict) -> None:
    step = _step(document, "preflight", "smoke-wheel")
    step["run"] = step["run"].replace(f'"memtomem=={_FLOOR}"', '"memtomem"')


def _publish_without_preflight(document: dict) -> None:
    del document["jobs"]["publish"]["needs"]


def _checkout_main_to_publish(document: dict) -> None:
    document["jobs"]["publish"]["steps"][0]["with"]["ref"] = "main"


def _rebuild_after_validation(document: dict) -> None:
    steps = document["jobs"]["publish"]["steps"]
    steps.insert(
        5, {"name": "Rebuild", "run": "uv build packages/langchain-memtomem --out-dir dist"}
    )


def _gate_in_another_directory(document: dict) -> None:
    _step(document, "preflight", "artifacts")["working-directory"] = "packages"


def _job_level_shell_default(document: dict) -> None:
    document["jobs"]["publish"]["defaults"] = {"run": {"working-directory": "packages"}}


def _chained_gate(document: dict) -> None:
    step = _step(document, "preflight", "wait-ci")
    step["run"] = step["run"] + " || true"


@pytest.mark.parametrize(
    "mutate",
    [
        _echo_contract,
        _drop_github_output,
        _skip_pypi,
        _tolerate_smoke_failure,
        _drop_publish_revalidation,
        _upload_before_revalidation,
        _smoke_newest_core,
        _publish_without_preflight,
        _chained_gate,
        _checkout_main_to_publish,
        _rebuild_after_validation,
        _gate_in_another_directory,
        _job_level_shell_default,
    ],
)
def test_the_workflow_guard_refuses_a_disabled_gate(mutate) -> None:
    document = copy.deepcopy(_workflow("release-langchain.yml"))
    mutate(document)
    with pytest.raises((AssertionError, SystemExit)):
        _assert_langchain_workflow(document)
