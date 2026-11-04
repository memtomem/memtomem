"""langchain-memtomem's release SBOM: the validator and the workflow that attaches it (#3057).

The validator cases each take the otherwise-valid document and change the one
thing a check exists for. The workflow cases parse the YAML and the commands
the steps run, rather than looking for substrings in the file.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from .test_release_preflight import _single_command_argv

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW = "release-langchain-sbom.yml"


def _load(name: str) -> ModuleType:
    path = _ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_langchain_sbom", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load("release_sbom")
rp = _load("release_preflight")

_WORKSPACE = [{"name": "uv:workspace:path", "value": "packages/memtomem"}]


def _payload(*, version: str = "0.1.0", core: str = "0.7.4") -> dict[str, Any]:
    """The shape ``uv export --format cyclonedx1.5`` writes, cut down to the anchors."""
    root = f"langchain-memtomem-1@{version}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:one",
        "version": 1,
        "metadata": {
            "timestamp": "2026-11-04T00:00:00Z",
            "tools": [{"vendor": "Astral Software Inc.", "name": "uv", "version": "0.11.16"}],
            "component": {
                "type": "library",
                "bom-ref": root,
                "name": "langchain-memtomem",
                "version": version,
            },
        },
        "components": [
            {
                "type": "library",
                "bom-ref": "langchain-core-2@1.4.9",
                "name": "langchain-core",
                "version": "1.4.9",
                "purl": "pkg:pypi/langchain-core@1.4.9",
            },
            {
                "type": "library",
                "bom-ref": "langgraph-3@1.2.14",
                "name": "langgraph",
                "version": "1.2.14",
                "purl": "pkg:pypi/langgraph@1.2.14",
            },
            {
                "type": "library",
                "bom-ref": f"memtomem-4@{core}",
                "name": "memtomem",
                "version": core,
                "properties": copy.deepcopy(_WORKSPACE),
            },
        ],
        "dependencies": [
            {"ref": root, "dependsOn": ["langgraph-3@1.2.14", f"memtomem-4@{core}"]},
            {"ref": "langgraph-3@1.2.14", "dependsOn": ["langchain-core-2@1.4.9"]},
        ],
    }


def _component(payload: dict[str, Any], name: str) -> dict[str, Any]:
    return next(row for row in payload["components"] if row["name"] == name)


def test_a_workspace_graph_is_accepted() -> None:
    rs.validate_langchain(_payload(), version="0.1.0", core_version="0.7.4")


def _root_version(p: dict) -> None:
    p["metadata"]["component"]["version"] = "0.1.1"


def _root_name(p: dict) -> None:
    p["metadata"]["component"]["name"] = "memtomem"


def _root_type(p: dict) -> None:
    p["metadata"]["component"]["type"] = "application"


def _format(p: dict) -> None:
    p["specVersion"] = "1.6"


def _no_root_row(p: dict) -> None:
    p["dependencies"] = p["dependencies"][1:]


def _root_skips_memtomem(p: dict) -> None:
    p["dependencies"][0]["dependsOn"] = ["langgraph-3@1.2.14"]


def _root_skips_langgraph(p: dict) -> None:
    p["dependencies"][0]["dependsOn"] = ["memtomem-4@0.7.4"]


def _no_memtomem(p: dict) -> None:
    p["components"] = [row for row in p["components"] if row["name"] != "memtomem"]


def _memtomem_twice(p: dict) -> None:
    p["components"].append(copy.deepcopy(_component(p, "memtomem")))


def _memtomem_another_version(p: dict) -> None:
    _component(p, "memtomem")["version"] = "0.7.5"


def _memtomem_from_a_registry(p: dict) -> None:
    _component(p, "memtomem")["purl"] = "pkg:pypi/memtomem@0.7.4"


def _memtomem_without_workspace_path(p: dict) -> None:
    del _component(p, "memtomem")["properties"]


def _memtomem_at_another_path(p: dict) -> None:
    _component(p, "memtomem")["properties"] = [
        {"name": "uv:workspace:path", "value": "vendor/memtomem"}
    ]


def _no_langchain_core(p: dict) -> None:
    p["components"] = [row for row in p["components"] if row["name"] != "langchain-core"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_root_version, "root component"),
        (_root_name, "root component"),
        (_root_type, "root component"),
        (_format, "CycloneDX 1.5"),
        (_no_root_row, "no dependency row"),
        (_root_skips_memtomem, "must depend on memtomem and langgraph"),
        (_root_skips_langgraph, "must depend on memtomem and langgraph"),
        (_no_memtomem, "misses required components"),
        (_memtomem_twice, "once, found 2"),
        (_memtomem_another_version, "memtomem 0.7.5, the checkout 0.7.4"),
        (_memtomem_from_a_registry, "workspace member"),
        (_memtomem_without_workspace_path, "workspace member"),
        (_memtomem_at_another_path, "workspace member"),
        (_no_langchain_core, "misses required components"),
    ],
)
def test_each_contract_break_is_refused(mutate: Callable[[dict], None], message: str) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(rs.SbomError, match=message):
        rs.validate_langchain(payload, version="0.1.0", core_version="0.7.4")


def test_output_is_reproducible_across_runs(tmp_path: Path) -> None:
    first, second = _payload(), _payload()
    second["serialNumber"] = "urn:uuid:two"
    second["metadata"]["timestamp"] = "2026-11-05T09:30:00Z"
    outputs = []
    for index, payload in enumerate((first, second)):
        raw, out = tmp_path / f"raw-{index}.json", tmp_path / f"out-{index}.json"
        raw.write_text(json.dumps(payload), encoding="utf-8")
        rs.normalize_validate_langchain(raw=raw, out=out, version="0.1.0", core_version="0.7.4")
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1]
    written = json.loads(outputs[0])
    assert "serialNumber" not in written and "timestamp" not in written["metadata"]


def test_the_cli_reports_a_refusal_without_writing(tmp_path: Path, capsys) -> None:
    raw, out = tmp_path / "raw.json", tmp_path / "out.json"
    raw.write_text(json.dumps(_payload(core="0.7.5")), encoding="utf-8")
    argv = ["langchain", "--raw", str(raw), "--out", str(out), "--version", "0.1.0"]
    assert rs.main([*argv, "--core-version", "0.7.4"]) == 1
    assert "SBOM validation failed" in capsys.readouterr().err
    assert not out.exists()
    assert rs.main([*argv, "--core-version", "0.7.5"]) == 0
    assert out.exists()


def test_the_core_pair_invocation_is_unchanged() -> None:
    """The flat form release-sbom.yml runs still parses: ``langchain`` is a prefix, not a mode."""
    with pytest.raises(SystemExit) as exc:
        rs.main(["--core-raw", "a"])
    assert exc.value.code == 2  # argparse: the pair form's other options are required


# --- the workflow ----------------------------------------------------------------


def _workflow(name: str = _WORKFLOW) -> dict:
    return yaml.safe_load((_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _step(document: dict, step_id: str) -> dict:
    steps = [s for s in document["jobs"]["sbom"]["steps"] if s.get("id") == step_id]
    assert len(steps) == 1, step_id
    return steps[0]


def _assert_sbom_workflow(document: dict) -> None:
    """The whole parsed workflow, compared to the one below.

    Closed-world for the same reason as ``_assert_closed_job`` in
    test_release_preflight_langchain.py. Two review rounds found weakenings a
    field-by-field guard let through (an ``echo`` prefix, ``if false && cmp``,
    a workflow-level ``shell: bash -n {0}``, setup-uv at ``latest``), each in
    a key nobody had thought to check. Equality has no such key. A deliberate
    change edits this test with it.
    """
    assert document == _EXPECTED


_CONTRACT_SCRIPT = r"""set -euo pipefail
if [[ "$GITHUB_EVENT_NAME" == "workflow_dispatch" && "$GITHUB_REF" != "refs/heads/main" ]]; then
  echo "manual SBOM backfills must run from the protected main branch" >&2
  exit 1
fi
if [[ ! "$RELEASE_TAG" =~ ^langchain-v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "langchain SBOM requires an exact langchain-vX.Y.Z tag, got: $RELEASE_TAG" >&2
  exit 1
fi
uv run --no-project --python 3.12 python tooling/tools/release_preflight.py langchain-contract \
  --tag "$RELEASE_TAG" \
  --repo-root release \
  --github-output "$GITHUB_OUTPUT"
echo "release_sha=$(git -C release rev-parse HEAD)" >> "$GITHUB_OUTPUT"
"""

# --latest=false keeps the core release the repository's Latest; an existing
# asset is compared and never replaced.
_UPLOAD_SCRIPT = r"""set -euo pipefail
if ! gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
  gh release create "$RELEASE_TAG" --verify-tag --title "$RELEASE_TAG" \
    --latest=false --notes-file "$RUNNER_TEMP/langchain-release-notes.md"
fi
name="langchain-memtomem-${VERSION}.cdx.json"
generated="$RUNNER_TEMP/$name"
if gh release view "$RELEASE_TAG" --json assets \
  --jq '.assets[].name' | grep -Fxq "$name"
then
  existing_dir="$RUNNER_TEMP/existing"
  mkdir -p "$existing_dir"
  gh release download "$RELEASE_TAG" --pattern "$name" --dir "$existing_dir"
  if ! cmp -s "$generated" "$existing_dir/$name"; then
    echo "refusing to overwrite differing release asset: $name" >&2
    exit 1
  fi
  echo "$name already exists with identical bytes"
else
  gh release upload "$RELEASE_TAG" "$generated"
fi
sha256sum "$generated" | tee -a "$GITHUB_STEP_SUMMARY"
{
  echo "Release tag: $RELEASE_TAG"
  echo "Release commit: ${{ steps.contract.outputs.release_sha }}"
  echo "Tooling commit: $GITHUB_SHA"
  echo "Toolchain: $(uv --version)"
} >> "$GITHUB_STEP_SUMMARY"
"""

_CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
_TAG_INPUT = {"required": True, "type": "string"}
_PY = "uv run --no-project --python 3.12 python tooling/tools"
_VERSION = "${{ steps.contract.outputs.version }}"
_EXPECTED = {
    "name": "Release langchain-memtomem SBOM",
    True: {  # PyYAML reads ``on`` as True
        "workflow_call": {
            "inputs": {
                "release_tag": {
                    "description": "Immutable langchain-v* production tag to describe",
                    **_TAG_INPUT,
                }
            }
        },
        "workflow_dispatch": {
            "inputs": {
                "release_tag": {
                    "description": "Existing langchain-v* production tag to backfill",
                    **_TAG_INPUT,
                }
            }
        },
    },
    "permissions": {},
    "env": {"UV_VERSION": "0.11.16"},
    "jobs": {
        "sbom": {
            "name": "Build and attach the CycloneDX SBOM",
            "runs-on": "ubuntu-latest",
            "timeout-minutes": 15,
            "permissions": {"contents": "write"},
            "steps": [
                {
                    "name": "Check out trusted SBOM tooling",
                    "uses": _CHECKOUT,
                    "with": {
                        "ref": "${{ github.sha }}",
                        "path": "tooling",
                        "persist-credentials": False,
                    },
                },
                {
                    "name": "Check out immutable release tag",
                    "uses": _CHECKOUT,
                    # Fully qualified: a bare name resolves to a same-named branch first.
                    "with": {
                        "ref": "refs/tags/${{ inputs.release_tag }}",
                        "path": "release",
                        "persist-credentials": False,
                    },
                },
                {
                    "uses": "astral-sh/setup-uv@68559e99cc807c1eb7e9238fd43a15bba460eec2",
                    "with": {"version": "${{ env.UV_VERSION }}"},
                },
                {
                    "name": "Validate production tag contract",
                    "id": "contract",
                    "env": {"RELEASE_TAG": "${{ inputs.release_tag }}"},
                    "run": _CONTRACT_SCRIPT,
                },
                {
                    "name": "Export the locked graph",
                    "id": "export",
                    "working-directory": "release",
                    "run": "uv export --quiet --frozen --preview-features sbom-export"
                    " --format cyclonedx1.5 --package langchain-memtomem --no-dev"
                    ' -o "$RUNNER_TEMP/langchain-memtomem.raw.json"',
                },
                {
                    "name": "Normalize and validate the SBOM contract",
                    "id": "validate",
                    "run": f"{_PY}/release_sbom.py langchain"
                    ' --raw "$RUNNER_TEMP/langchain-memtomem.raw.json"'
                    f' --out "$RUNNER_TEMP/langchain-memtomem-{_VERSION}.cdx.json"'
                    f' --version "{_VERSION}"'
                    ' --core-version "${{ steps.contract.outputs.core }}"',
                },
                {
                    "name": "Write the release notes",
                    "id": "notes",
                    "env": {"RELEASE_TAG": "${{ inputs.release_tag }}"},
                    "run": f"{_PY}/release_preflight.py langchain-notes"
                    ' --tag "$RELEASE_TAG" --repo-root release'
                    ' --out "$RUNNER_TEMP/langchain-release-notes.md"',
                },
                {
                    "name": "Create release if needed and upload the missing asset",
                    "id": "upload",
                    "env": {
                        "GH_TOKEN": "${{ github.token }}",
                        "GH_REPO": "${{ github.repository }}",
                        "RELEASE_TAG": "${{ inputs.release_tag }}",
                        "VERSION": _VERSION,
                    },
                    "run": _UPLOAD_SCRIPT,
                },
            ],
        }
    },
}


def test_the_sbom_workflow_is_exactly_this() -> None:
    _assert_sbom_workflow(_workflow())


_UV_PY = ("uv", "run", "--no-project", "--python", "3.12", "python")


def test_the_pinned_commands_parse_with_the_tools_own_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equality pins the text; this checks the text means the arguments it looks like."""
    document = _workflow()
    notes = _single_command_argv(document, "sbom", "notes", _UV_PY)
    assert notes[len(_UV_PY)] == "tooling/tools/release_preflight.py"
    args = rp._build_parser().parse_args(notes[len(_UV_PY) + 1 :])
    assert (args.command, args.tag, args.repo_root, args.out) == (
        "langchain-notes",
        "$RELEASE_TAG",
        Path("release"),
        Path("$RUNNER_TEMP/langchain-release-notes.md"),
    )
    validate = _single_command_argv(document, "sbom", "validate", _UV_PY)
    assert validate[len(_UV_PY) : len(_UV_PY) + 2] == ["tooling/tools/release_sbom.py", "langchain"]
    # The validator's own parser: an unknown or misspelt option would exit 2 here.
    parsed: dict[str, Any] = {}
    monkeypatch.setattr(rs, "normalize_validate_langchain", lambda **kw: parsed.update(kw))
    assert rs.main(validate[len(_UV_PY) + 1 :]) == 0
    assert parsed == {
        "raw": Path("$RUNNER_TEMP/langchain-memtomem.raw.json"),
        "out": Path(
            "$RUNNER_TEMP/langchain-memtomem-${{ steps.contract.outputs.version }}.cdx.json"
        ),
        "version": "${{ steps.contract.outputs.version }}",
        "core_version": "${{ steps.contract.outputs.core }}",
    }


@pytest.mark.parametrize(
    ("tag", "admitted"),
    [
        ("langchain-v0.1.0", True),
        ("langchain-v10.20.30", True),
        ("test-langchain-v0.1.0a1", False),
        ("v0.7.4", False),
        ("langchain-v0.1", False),
        ("langchain-v0.1.0a1", False),
        ("langchain-memtomem-v0.1.0", False),
    ],
)
def test_the_pinned_tag_pattern_admits_only_production_tags(tag: str, admitted: bool) -> None:
    """What the pinned regex means, not just that it is spelled this way (bash ERE ≡ ``re`` here)."""
    match = re.search(r'"\$RELEASE_TAG" =~ (\S+) \]\]', _CONTRACT_SCRIPT)
    assert match
    assert (re.fullmatch(match.group(1), tag) is not None) is admitted


def _branch_first_checkout(d: dict) -> None:
    d["jobs"]["sbom"]["steps"][1]["with"]["ref"] = "${{ inputs.release_tag }}"


def _admit_test_tags(d: dict) -> None:
    step = _step(d, "contract")
    prod = r"^langchain-v[0-9]+\.[0-9]+\.[0-9]+$"
    widened = r"^(test-)?langchain-v[0-9]+\.[0-9]+\.[0-9]+(a[0-9]+)?$"
    assert re.fullmatch(widened, "test-langchain-v0.1.0a1")  # the mutant really admits one
    step["run"] = step["run"].replace(prod, widened)


def _dispatch_from_any_branch(d: dict) -> None:
    step = _step(d, "contract")
    step["run"] = step["run"].replace('"$GITHUB_REF" != "refs/heads/main"', "false")


def _dispatch_check_never_fires(d: dict) -> None:
    step = _step(d, "contract")
    step["run"] = step["run"].replace('== "workflow_dispatch"', '== "never"')


def _echo_the_contract(d: dict) -> None:
    step = _step(d, "contract")
    step["run"] = step["run"].replace("uv run --no-project", "echo uv run --no-project")


def _skip_the_byte_comparison(d: dict) -> None:
    step = _step(d, "upload")
    step["run"] = step["run"].replace("if ! cmp -s", "if false && cmp -s")


def _create_unconditionally(d: dict) -> None:
    step = _step(d, "upload")
    step["run"] = step["run"].replace(
        'if ! gh release view "$RELEASE_TAG" >/dev/null 2>&1; then', "if true; then"
    )


def _mark_latest(d: dict) -> None:
    step = _step(d, "upload")
    step["run"] = step["run"].replace("--latest=false", "--latest")


def _generated_notes(d: dict) -> None:
    step = _step(d, "upload")
    step["run"] = step["run"].replace(
        '--notes-file "$RUNNER_TEMP/langchain-release-notes.md"', "--generate-notes"
    )


def _overwrite_asset(d: dict) -> None:
    step = _step(d, "upload")
    step["run"] = step["run"].replace(
        'gh release upload "$RELEASE_TAG" "$generated"',
        'gh release upload --clobber "$RELEASE_TAG" "$generated"',
    )


def _skip_validation(d: dict) -> None:
    _step(d, "validate")["continue-on-error"] = True


def _export_with_extras(d: dict) -> None:
    step = _step(d, "export")
    step["run"] = step["run"].replace("--no-dev", "--no-dev --all-extras")


def _export_from_tooling(d: dict) -> None:
    _step(d, "export")["working-directory"] = "tooling"


def _validate_against_floor(d: dict) -> None:
    step = _step(d, "validate")
    step["run"] = step["run"].replace("outputs.core", "outputs.floor")


def _wider_token(d: dict) -> None:
    d["permissions"] = {"contents": "write"}


def _syntax_check_only(d: dict) -> None:
    d["defaults"] = {"run": {"shell": "bash -n {0}"}}


def _unpinned_uv(d: dict) -> None:
    d["jobs"]["sbom"]["steps"][2]["with"]["version"] = "latest"


@pytest.mark.parametrize(
    "mutate",
    [
        _branch_first_checkout,
        _admit_test_tags,
        _dispatch_from_any_branch,
        _dispatch_check_never_fires,
        _echo_the_contract,
        _skip_the_byte_comparison,
        _create_unconditionally,
        _mark_latest,
        _generated_notes,
        _overwrite_asset,
        _skip_validation,
        _export_with_extras,
        _export_from_tooling,
        _validate_against_floor,
        _wider_token,
        _syntax_check_only,
        _unpinned_uv,
    ],
)
def test_the_sbom_workflow_guard_refuses_each_weakening(mutate: Callable[[dict], None]) -> None:
    document = copy.deepcopy(_workflow())
    before = copy.deepcopy(document)
    mutate(document)
    assert document != before, "the mutation no longer applies to the workflow"
    with pytest.raises(AssertionError):
        _assert_sbom_workflow(document)


# --- the real export ---------------------------------------------------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH (CI installs the pin)")
def test_the_workflow_export_of_this_checkout_passes_the_validator(tmp_path: Path) -> None:
    """The command the workflow runs, on this checkout's lock, through the validator.

    CI's test jobs install the pinned uv, so a pin bump that changes how uv
    writes the workspace member fails on its own PR instead of after a publish.
    """
    argv = _single_command_argv(_workflow(), "sbom", "export", ("uv",))
    raw = tmp_path / "raw.json"
    argv[argv.index("-o") + 1] = str(raw)
    subprocess.run(argv, cwd=_ROOT, check=True, timeout=120)
    package = rp._langchain_project(_ROOT)["version"]
    rs.normalize_validate_langchain(
        raw=raw,
        out=tmp_path / "out.json",
        version=package,
        core_version=rp.project_version(_ROOT),
    )
