"""Tests for the fail-closed release preflight helper."""

from __future__ import annotations

import importlib.util
import copy
import http.client
import io
import json
import shlex
import tarfile
import urllib.error
import zipfile
from pathlib import Path
from types import ModuleType

import pytest
import yaml


_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "release_preflight.py"
    spec = importlib.util.spec_from_file_location("release_preflight", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rp = _load_tool()


def _repo(
    tmp_path: Path,
    *,
    version: str = "0.3.6",
    lock_version: str | None = None,
    server_version: str | None = None,
    server_package_version: str | None = None,
    server_manifest: str | None = None,
    cryptography: str = ">=48.0.1",
    urllib3: str = ">=2.7.0",
) -> Path:
    package = tmp_path / "packages" / "memtomem"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        f'[project]\nname = "memtomem"\nversion = "{version}"\n'
        "dependencies = [\n"
        f'  "cryptography{cryptography}",\n'
        '  "starlette>=1.3.1",\n'
        '  "idna>=3.15",\n'
        '  "pyjwt>=2.13.0",\n'
        '  "python-multipart>=0.0.27",\n'
        "]\n\n"
        "[project.optional-dependencies]\n"
        f'onnx = ["fastembed>=0.8,<0.9", "urllib3{urllib3}"]\n'
        f'langfuse = ["langfuse>=4.0", "urllib3{urllib3}"]\n'
        'all = ["memtomem[onnx,langfuse]"]\n',
        encoding="utf-8",
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
    if server_manifest is None:
        server_manifest = json.dumps(
            {
                "name": "io.github.memtomem/memtomem",
                "description": "test",
                "version": server_version or version,
                "packages": [
                    {
                        "registryType": "pypi",
                        "identifier": "memtomem",
                        "version": server_package_version or version,
                        "transport": {"type": "stdio"},
                    }
                ],
            }
        )
    (tmp_path / "server.json").write_text(server_manifest, encoding="utf-8")
    return tmp_path


def _metadata(
    version: str = "0.3.6",
    *,
    cryptography: str = ">=48.0.1",
    urllib3: str = ">=2.7.0",
) -> bytes:
    requirements = [
        f"cryptography{cryptography}",
        "starlette>=1.3.1",
        "idna>=3.15",
        "pyjwt>=2.13.0",
        "python-multipart>=0.0.27",
        f'urllib3{urllib3}; extra == "all"',
        f'urllib3{urllib3}; extra == "onnx"',
        f'urllib3{urllib3}; extra == "langfuse"',
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


def test_contract_accepts_a_matching_registry_manifest(tmp_path: Path) -> None:
    assert (
        rp.validate_contract("v0.3.6", _repo(tmp_path), require_registry_manifest=True) == "0.3.6"
    )


def test_contract_rejects_a_non_object_manifest_root(tmp_path: Path) -> None:
    with pytest.raises(rp.ReleaseCheckError, match="must contain a JSON object"):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest="[]"), require_registry_manifest=True
        )


def test_contract_rejects_a_malformed_package_entry_beside_a_valid_one(tmp_path: Path) -> None:
    """A valid match must not launder a malformed sibling entry."""
    manifest = json.dumps(
        {
            "version": "0.3.6",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "memtomem",
                    "version": "0.3.6",
                    "transport": {"type": "stdio"},
                },
                None,
            ],
        }
    )
    with pytest.raises(rp.ReleaseCheckError, match=r"packages\[1\]"):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest=manifest), require_registry_manifest=True
        )


def test_contract_rejects_an_empty_object_beside_a_valid_package(tmp_path: Path) -> None:
    """``{}`` is a dict, which is exactly why the isinstance sweep missed it.

    It clears the non-object check, then fails the registryType/identifier
    selection and is never read again, leaving the count of real pypi entries
    at 1 — measured accepted as ``('0.6.3', '0.6.3')`` before this guard.
    """
    manifest = json.dumps(
        {
            "version": "0.3.6",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "memtomem",
                    "version": "0.3.6",
                    "transport": {"type": "stdio"},
                },
                {},
            ],
        }
    )
    with pytest.raises(rp.ReleaseCheckError, match=r"packages\[1\].*missing required key"):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest=manifest), require_registry_manifest=True
        )


@pytest.mark.parametrize(
    "omitted",
    ["registryType", "identifier", "transport"],
)
def test_contract_requires_each_schema_mandated_package_key(tmp_path: Path, omitted: str) -> None:
    """One case per key, because ``{}`` omits all three at once.

    A single empty-object case cannot tell which keys are enforced: dropping
    ``transport`` from the required tuple left every other test green.
    """
    package = {
        "registryType": "pypi",
        "identifier": "memtomem",
        "version": "0.3.6",
        "transport": {"type": "stdio"},
    }
    del package[omitted]
    manifest = json.dumps(
        {
            "version": "0.3.6",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "memtomem",
                    "version": "0.3.6",
                    "transport": {"type": "stdio"},
                },
                package,
            ],
        }
    )
    with pytest.raises(rp.ReleaseCheckError, match=rf"packages\[1\].*{omitted}"):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest=manifest), require_registry_manifest=True
        )


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ({"type": "subcommand", "value": "serve"}, "not one of"),
        ({"value": "serve"}, "not one of"),
        ({"type": "named", "value": "x"}, "has no name"),
        # The schema types ``name`` as a bare string with no ``minLength``, so
        # this validates against it; refusing it is a deliberate extra step,
        # and it gets its own message so the distinction stays visible.
        ({"type": "named", "name": "", "value": "x"}, "empty name"),
        ({"type": "positional"}, "neither value nor valueHint"),
        ("serve", "is not an object"),
        # Unhashable: the membership test used to raise an uncaught TypeError
        # here, crashing the release instead of refusing the manifest.
        ({"type": []}, "not one of"),
        # The schema types ``name`` as a string; a number is not one.
        ({"type": "named", "name": 42, "value": "x"}, "non-string name"),
    ],
    ids=[
        "unknown-type",
        "no-type",
        "named-without-name",
        "named-with-empty-name",
        "positional-without-either",
        "bare-string",
        "unhashable-type",
        "named-with-non-string-name",
    ],
)
@pytest.mark.parametrize("field", ["packageArguments", "runtimeArguments"])
def test_contract_rejects_an_argument_the_registry_would_reject(
    tmp_path: Path, field: str, argument: object, expected: str
) -> None:
    """An unknown ``type`` used to be ignored entirely rather than refused.

    Crossed with the field, because the two are separate branches and only
    one of them was covered: removing ``runtimeArguments`` from the validator
    left the whole suite green.
    """
    manifest = json.dumps(
        {
            "version": "0.3.6",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "memtomem",
                    "version": "0.3.6",
                    "transport": {"type": "stdio"},
                    field: [argument],
                }
            ],
        }
    )
    with pytest.raises(rp.ReleaseCheckError, match=expected):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest=manifest), require_registry_manifest=True
        )


@pytest.mark.parametrize("field", ["packageArguments", "runtimeArguments"])
# ``null`` is a *present* field carrying an invalid value. Reading it with
# ``.get()`` made it indistinguishable from an absent field, so it skipped
# every check below.
@pytest.mark.parametrize(
    "container", [{}, "serve", 3, None], ids=["object", "string", "number", "null"]
)
def test_contract_rejects_an_argument_list_that_is_not_a_list(
    tmp_path: Path, field: str, container: object
) -> None:
    """``runtimeArguments: {}`` is the shape a mirrored copy of this check let through."""
    manifest = json.dumps(
        {
            "version": "0.3.6",
            "packages": [
                {
                    "registryType": "pypi",
                    "identifier": "memtomem",
                    "version": "0.3.6",
                    "transport": {"type": "stdio"},
                    field: container,
                }
            ],
        }
    )
    with pytest.raises(rp.ReleaseCheckError, match="is not an array"):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest=manifest), require_registry_manifest=True
        )


# The canonical invocation, read off ``release.yml`` rather than imagined.
# Fixing it positionally is what closes the whole family of counterexamples
# that token membership kept letting through: ``echo uv run … contract
# --flag`` and ``python -c pass tools/release_preflight.py contract --flag``
# both satisfy any "is the flag a token somewhere" test while running nothing.
_CONTRACT_PREFIX = (
    "uv",
    "run",
    "--no-project",
    "--python",
    "3.12",
    "python",
    "tools/release_preflight.py",
    "contract",
)
# ``$`` is deliberately absent: the real command interpolates
# ``"$GITHUB_REF_NAME"`` and ``"$GITHUB_OUTPUT"``, so forbidding it would
# refuse the workflow it is meant to protect. ``#`` is present because the
# shell drops a commented flag while a tokeniser happily keeps it.
_FORBIDDEN_METACHARS = frozenset("&|;()<>`#")


def _workflow(workflow_name: str) -> dict:
    return yaml.safe_load(
        (_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
    )


def _single_command_argv(
    document: dict, job_name: str, step_id: str, prefix: tuple[str, ...]
) -> list[str]:
    """The argv of one named step, on the condition that it is a lone command.

    This replaces a shell-command splitter that three consecutive review
    rounds found holes in — substring, then whole-block tokenisation, then an
    operator-splitting lexer — each fix larger than the last and each still
    defeated (``);``, a heredoc body, an ``echo`` prefix). The lesson was that
    deciding *which command a flag belongs to* inside an arbitrary shell
    script is a shell parser's job, and a test helper is not going to become
    one.

    So the direction inverts: refuse what cannot be reasoned about instead of
    parsing it. If the step is one simple command with no control operators,
    no redirection, no substitution and no comment, then there is exactly one
    command by construction and no splitting is needed. A step that needs
    shell control flow fails this assertion and has to move into a script,
    where it is reviewable on its own terms.
    """
    steps = [s for s in document["jobs"][job_name]["steps"] if s.get("id") == step_id]
    assert len(steps) == 1, f"job {job_name!r} has {len(steps)} steps with id {step_id!r}"

    # A trailing newline is how YAML block scalars end; an interior one is a
    # second command. Backslash-newline is refused rather than normalised:
    # an earlier version joined the halves with a space, which is *not* what
    # the shell does — it removes the pair, so ``--repo-root ./\`` followed by
    # ``--require-registry-manifest`` reaches the process as the single
    # argument ``./--require-registry-manifest`` with no flag at all, while
    # the guard read a flag that was never passed. Measured against bash.
    # The folded scalar this step actually uses has no continuations.
    command = steps[0]["run"].strip()
    assert command, f"step {step_id!r} has an empty run block"
    assert "\\" not in command, (
        f"step {step_id!r} uses a line continuation; keep it one line: {command!r}"
    )
    assert "\n" not in command, f"step {step_id!r} must be a single command, got: {command!r}"
    found = sorted(set(command) & _FORBIDDEN_METACHARS)
    assert not found, f"step {step_id!r} uses shell metacharacters {found}: {command!r}"

    # No ``comments=True`` here. ``#`` is in the forbidden set above, so a
    # comment never reaches this line — and a second mechanism that no test
    # can distinguish from its absence is not defence in depth, it is an
    # unpinned line. Measured: deleting ``comments=True`` changed nothing.
    argv = shlex.split(command)
    assert tuple(argv[: len(prefix)]) == prefix, (
        f"step {step_id!r} must invoke {' '.join(prefix)}, got: {argv[: len(prefix)]}"
    )
    return argv


def _contract_argv(document: dict, job_name: str, step_id: str) -> list[str]:
    return _single_command_argv(document, job_name, step_id, _CONTRACT_PREFIX)


def _parsed_contract_args(document: dict, job_name: str, step_id: str) -> object:
    """Run the step's own arguments through the script's own parser.

    Token membership cannot tell a flag from a flag-shaped *value*:
    ``--tag --require-registry-manifest`` contains the token and enables
    nothing. Handing the tail to ``_build_parser`` settles it with the same
    semantics the release actually runs under — and argparse rejects that
    particular shape outright, which is the correct answer either way.
    """
    argv = _contract_argv(document, job_name, step_id)
    return rp._build_parser().parse_args(argv[_CONTRACT_PREFIX.index("contract") :])


def test_release_workflow_opts_into_the_registry_manifest_check() -> None:
    """The opt-in must not rot into a silent skip at the one call site that needs it."""
    assert (
        _parsed_contract_args(
            _workflow("release.yml"), "preflight", "contract"
        ).require_registry_manifest
        is True
    )


def test_the_flag_is_what_makes_the_contract_check_refuse(tmp_path: Path) -> None:
    """Pins the wiring, not the spelling: the flag has to reach the validator.

    The workflow test above proves the flag is on the command line. This
    proves that a command line carrying it rejects a manifest that the same
    command line without it accepts — so a ``main()`` that parsed the flag and
    dropped it on the floor would fail here.
    """
    repo = _repo(tmp_path, server_version="0.3.5")
    argv = ["contract", "--tag", "v0.3.6", "--repo-root", str(repo)]

    assert rp.main(argv) == 0
    assert rp.main([*argv, "--require-registry-manifest"]) == 1


def test_sbom_workflow_does_not_require_the_registry_manifest() -> None:
    """It validates historical tags, which legitimately have no manifest.

    Asymmetric with the positive check above, on purpose. ``release-sbom.yml``
    runs its contract call inside a genuinely compound script — ``set -euo
    pipefail``, a branch guard, a tag regex, ``exit 1`` — so the single-command
    constraint cannot apply there without rewriting a step that is right as it
    is. A negative assertion tolerates over-approximation where a positive one
    could not: scanning the whole run block for the flag can only produce a
    false alarm, never a miss.

    What this does *not* establish: that the flag cannot arrive from a
    variable or a generated command line. It is a regression check on the flag
    being written down, and nothing wider.
    """
    document = _workflow("release-sbom.yml")

    scanned = 0
    for job_name, job in document["jobs"].items():
        for step in job.get("steps", []):
            run = step.get("run")
            if not isinstance(run, str) or "release_preflight.py" not in run:
                continue
            scanned += 1
            assert "--require-registry-manifest" not in run, (job_name, step.get("name"))
    assert scanned, "release-sbom.yml no longer runs release_preflight.py at all"


# Every counterexample three review rounds and one design debate produced.
# They are the regression suite for deleting the shell splitter: each one
# defeated some earlier version of this guard, and the replacement has to
# refuse all of them. ``flag_removed`` says whether the decoy is paired with
# deleting the real flag from the invocation.
_COUNTEREXAMPLES = {
    "newline": ("\necho --require-registry-manifest", True),
    "and-and": (" && echo --require-registry-manifest", True),
    "semicolon": ("; echo --require-registry-manifest", True),
    "pipe": (" | echo --require-registry-manifest", True),
    "subshell": (") ; echo --require-registry-manifest", True),
    "redirect": (" > /dev/null --require-registry-manifest", True),
    "heredoc": (" <<EOF\n--require-registry-manifest\nEOF", True),
    "backtick": (" `echo --require-registry-manifest`", True),
    # Backslash-newline is refused, not normalised. Joining the halves with a
    # space reads a flag the shell never delivers: bash removes the pair, so
    # this reaches the process as ``--repo-root ./--require-registry-manifest``
    # — one argument, no flag — while a space-joining guard reported
    # ``repo_root='.'`` with the flag enabled. Measured against bash.
    "line-continuation": ("\\\n--require-registry-manifest", True),
    # The shell drops this; a plain tokeniser keeps it. Found by the design
    # debate, not by any review round.
    "comment": (" # --require-registry-manifest", True),
}


@pytest.mark.parametrize("name", sorted(_COUNTEREXAMPLES))
def test_the_guard_refuses_every_shape_it_cannot_reason_about(name: str) -> None:
    """The accumulated counterexamples, all of which must now be refused.

    Each of these defeated a previous version: substring (round 0), whole-block
    tokenisation (round 1), and the operator-splitting lexer (round 2). The
    replacement does not try to work out which command the flag belongs to —
    it refuses a step it cannot read as one simple command, so the question
    never arises.
    """
    decoy, remove_flag = _COUNTEREXAMPLES[name]
    document = _workflow("release.yml")
    step = next(s for s in document["jobs"]["preflight"]["steps"] if s.get("id") == "contract")
    original = step["run"]
    mutated = original.replace(" --require-registry-manifest", "", 1) if remove_flag else original
    assert mutated != original or not remove_flag, "the counterexample no longer edits anything"
    step["run"] = mutated.rstrip("\n") + decoy

    with pytest.raises(AssertionError):
        _contract_argv(document, "preflight", "contract")


@pytest.mark.parametrize(
    "fake",
    [
        "echo uv run --no-project --python 3.12 python tools/release_preflight.py contract "
        "--require-registry-manifest",
        "python -c pass tools/release_preflight.py contract --require-registry-manifest",
        "uv run --no-project --python 3.12 python other.py contract --require-registry-manifest",
    ],
    ids=["echo-prefix", "python-c-pass", "different-script"],
)
def test_the_guard_refuses_a_command_that_runs_something_else(fake: str) -> None:
    """A flag on a command that never runs the checker proves nothing.

    All three are free of shell metacharacters, so the constraint above lets
    them through; the positional prefix is what stops them. ``python -c pass``
    in particular exits 0 having executed nothing at all.
    """
    document = _workflow("release.yml")
    step = next(s for s in document["jobs"]["preflight"]["steps"] if s.get("id") == "contract")
    step["run"] = fake

    with pytest.raises(AssertionError, match="must invoke"):
        _contract_argv(document, "preflight", "contract")


def test_the_real_workflow_still_passes_every_one_of_those_gates() -> None:
    """The witness: the refusals above are not refusing everything."""
    argv = _contract_argv(_workflow("release.yml"), "preflight", "contract")

    assert tuple(argv[: len(_CONTRACT_PREFIX)]) == _CONTRACT_PREFIX
    assert "--require-registry-manifest" in argv


def test_an_abbreviated_spelling_cannot_enable_the_gate() -> None:
    """argparse accepts any unambiguous prefix unless told not to.

    ``--require`` alone used to set ``require_registry_manifest``, which also
    made the SBOM guard's scan for the full spelling unsound: a shortened
    flag would have enabled registry validation without the audited string
    appearing anywhere. The parser is narrowed rather than the audit taught
    to enumerate abbreviations.
    """
    for abbreviation in ("--require", "--require-reg", "--require-registry"):
        with pytest.raises(SystemExit):
            rp._build_parser().parse_args(
                ["contract", "--tag", "v0.3.6", "--repo-root", ".", abbreviation]
            )

    full = rp._build_parser().parse_args(
        ["contract", "--tag", "v0.3.6", "--repo-root", ".", "--require-registry-manifest"]
    )
    assert full.require_registry_manifest is True


def test_a_flag_shaped_value_does_not_count_as_the_flag() -> None:
    """``--tag --require-registry-manifest`` carries the token and enables nothing.

    Token membership cannot see the difference; the real parser can. argparse
    refuses this shape outright rather than silently taking the flag as the
    tag's value, which is the right answer either way.
    """
    document = _workflow("release.yml")
    step = next(s for s in document["jobs"]["preflight"]["steps"] if s.get("id") == "contract")
    step["run"] = (
        "uv run --no-project --python 3.12 python tools/release_preflight.py contract "
        "--tag --require-registry-manifest --repo-root ."
    )

    with pytest.raises(SystemExit):
        _parsed_contract_args(document, "preflight", "contract")


def test_publish_runs_only_after_the_gating_job() -> None:
    """The gate is worth nothing if publish does not wait for it.

    ``publish`` re-derives the version with its own contract call that omits
    the flag. That is not a bypass *because* of this dependency; without it,
    it would be one.
    """
    document = _workflow("release.yml")

    needs = document["jobs"]["publish"].get("needs")
    needs = [needs] if isinstance(needs, str) else (needs or [])
    assert "preflight" in needs, needs


def test_contract_rejects_lock_drift(tmp_path: Path) -> None:
    with pytest.raises(rp.ReleaseCheckError, match="lock version"):
        rp.validate_contract("v0.3.6", _repo(tmp_path, lock_version="0.3.5"))


def test_contract_rejects_server_manifest_drift(tmp_path: Path) -> None:
    """The registry record's own version is the fourth place a release can drift."""
    with pytest.raises(rp.ReleaseCheckError, match="server.json version"):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_version="0.3.5"), require_registry_manifest=True
        )


def test_contract_rejects_server_manifest_package_drift(tmp_path: Path) -> None:
    """The registry rejects a record whose packaged distribution disagrees."""
    with pytest.raises(rp.ReleaseCheckError, match="pypi package version"):
        rp.validate_contract(
            "v0.3.6",
            _repo(tmp_path, server_package_version="0.3.5"),
            require_registry_manifest=True,
        )


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ("{ not json", "valid JSON"),
        (json.dumps({"packages": []}), "missing version"),
        (json.dumps({"version": "0.3.6"}), "missing packages array"),
        (
            json.dumps(
                {
                    "version": "0.3.6",
                    "packages": [
                        {
                            "registryType": "npm",
                            "identifier": "memtomem",
                            "transport": {"type": "stdio"},
                        }
                    ],
                }
            ),
            "found 0",
        ),
        (
            json.dumps(
                {
                    "version": "0.3.6",
                    "packages": [
                        {
                            "registryType": "pypi",
                            "identifier": "memtomem",
                            "version": "0.3.6",
                            "transport": {"type": "stdio"},
                        },
                        {
                            "registryType": "pypi",
                            "identifier": "memtomem",
                            "version": "0.3.6",
                            "transport": {"type": "stdio"},
                        },
                    ],
                }
            ),
            "found 2",
        ),
        (
            json.dumps(
                {
                    "version": "0.3.6",
                    "packages": [
                        {
                            "registryType": "pypi",
                            "identifier": "memtomem",
                            "transport": {"type": "stdio"},
                        }
                    ],
                }
            ),
            "missing pypi package version",
        ),
    ],
)
def test_contract_fails_closed_on_unusable_server_manifest(
    tmp_path: Path, manifest: str, expected: str
) -> None:
    """An unreadable manifest fails the release rather than being skipped."""
    with pytest.raises(rp.ReleaseCheckError, match=expected):
        rp.validate_contract(
            "v0.3.6", _repo(tmp_path, server_manifest=manifest), require_registry_manifest=True
        )


def test_contract_reports_a_missing_server_manifest(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "server.json").unlink()
    with pytest.raises(rp.ReleaseCheckError, match="cannot read valid JSON"):
        rp.validate_contract("v0.3.6", repo, require_registry_manifest=True)


def test_contract_skips_the_manifest_for_historical_tags(tmp_path: Path) -> None:
    """``release-sbom.yml`` runs current tooling against an immutable old tag.

    Every release predating ``server.json`` has no manifest; requiring one
    unconditionally would break SBOM backfills across the whole history.
    """
    repo = _repo(tmp_path)
    (repo / "server.json").unlink()

    assert rp.validate_contract("v0.3.6", repo) == "0.3.6"


def test_contract_ignores_manifest_drift_when_not_required(tmp_path: Path) -> None:
    """The opt-out really is an opt-out — otherwise the flag means nothing."""
    assert rp.validate_contract("v0.3.6", _repo(tmp_path, server_version="0.3.5")) == "0.3.6"


def test_contract_rejects_missing_changelog_heading(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CHANGELOG.md").write_text("## [Unreleased]\n", encoding="utf-8")
    with pytest.raises(rp.ReleaseCheckError, match="CHANGELOG"):
        rp.validate_contract("v0.3.6", repo)


def test_artifacts_accept_wheel_and_sdist_contract(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    wheel, sdist = rp.validate_artifacts(_dist(tmp_path), "0.3.6", repo)
    assert wheel.suffix == ".whl"
    assert sdist.name.endswith(".tar.gz")


def test_artifacts_reject_missing_floor(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    body = _metadata().replace(b"Requires-Dist: starlette>=1.3.1\n", b"")
    with pytest.raises(rp.ReleaseCheckError, match="direct floors"):
        rp.validate_artifacts(_dist(tmp_path, metadata=body), "0.3.6", repo)


def test_artifacts_reject_missing_extra_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    body = _metadata().replace(b'Requires-Dist: urllib3>=2.7.0; extra == "onnx"\n', b"")
    with pytest.raises(rp.ReleaseCheckError, match="urllib3"):
        rp.validate_artifacts(_dist(tmp_path, metadata=body), "0.3.6", repo)


def test_artifacts_accept_raised_project_floors(tmp_path: Path) -> None:
    repo = _repo(tmp_path, cryptography=">=48.0.2", urllib3=">=2.8.0")
    body = _metadata(cryptography=">=48.0.2", urllib3=">=2.8.0")
    rp.validate_artifacts(_dist(tmp_path, metadata=body), "0.3.6", repo)


def test_artifacts_reject_unexpected_wheel_top_level_member(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    dist = _dist(tmp_path)
    wheel = next(dist.glob("*.whl"))
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("unexpected_package/payload.py", b"pass\n")

    with pytest.raises(rp.ReleaseCheckError, match="unexpected top-level"):
        rp.validate_artifacts(dist, "0.3.6", repo)


@pytest.mark.parametrize("member_kind", ["symlink", "unexpected"])
def test_artifacts_reject_unsafe_sdist_members(tmp_path: Path, member_kind: str) -> None:
    repo = _repo(tmp_path)
    dist = _dist(tmp_path)
    sdist = next(dist.glob("*.tar.gz"))
    body = _metadata()
    with tarfile.open(sdist, "w:gz") as archive:
        metadata = tarfile.TarInfo("memtomem-0.3.6/PKG-INFO")
        metadata.size = len(body)
        archive.addfile(metadata, io.BytesIO(body))
        if member_kind == "symlink":
            hostile = tarfile.TarInfo("memtomem-0.3.6/src/link")
            hostile.type = tarfile.SYMTYPE
            hostile.linkname = "/etc/passwd"
        else:
            hostile = tarfile.TarInfo("memtomem-0.3.6/secrets.txt")
            hostile.size = 1
        archive.addfile(hostile, io.BytesIO(b"x") if hostile.isfile() else None)

    with pytest.raises(rp.ReleaseCheckError, match="unsupported|unexpected"):
        rp.validate_artifacts(dist, "0.3.6", repo)


class _JSONResponse(io.BytesIO):
    status = 200


class _TruncatedResponse(_JSONResponse):
    def read(self, *args):
        raise http.client.IncompleteRead(b'{"info":', 4096)


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


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: OSError("offline"), id="network"),
        pytest.param(lambda: http.client.IncompleteRead(b"{", 99), id="truncated-body"),
        pytest.param(lambda: http.client.BadStatusLine("broken status"), id="bad-status"),
        pytest.param(lambda: http.client.LineTooLong("status line"), id="long-status"),
    ],
)
def test_wait_ci_fails_closed_after_three_api_errors(error_factory) -> None:
    clock = _Clock()
    calls = []

    def fail(*_args):
        error = error_factory()
        calls.append(error)
        raise error

    with pytest.raises(rp.ReleaseCheckError, match="3 consecutive") as caught:
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
    assert len(calls) == 3
    assert clock.now == 2
    assert caught.value.__cause__ is calls[-1]


@pytest.mark.parametrize(
    "response_factory",
    [
        pytest.param(_TruncatedResponse, id="incomplete-read"),
        pytest.param(lambda: _JSONResponse(b'{"workflow_runs": [{"a": "\xc3'), id="utf8"),
    ],
)
def test_wait_ci_recovers_from_truncated_response_and_resets_error_count(
    monkeypatch, response_factory
):
    clock = _Clock()
    responses = iter(
        [
            response_factory(),
            response_factory(),
            _JSONResponse(b'{"workflow_runs": []}'),
            response_factory(),
            response_factory(),
            _JSONResponse(
                json.dumps(
                    {
                        "workflow_runs": [_run(status="completed", conclusion="success")],
                    }
                ).encode()
            ),
        ]
    )
    monkeypatch.setattr(rp.urllib.request, "urlopen", lambda *a, **kw: next(responses))
    result = rp.wait_for_exact_main_ci(
        repository="memtomem/memtomem",
        sha="abc",
        token="token",
        timeout_seconds=10,
        interval_seconds=1,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    assert result["id"] == 123
    assert clock.now == 5
    with pytest.raises(StopIteration):
        next(responses)


@pytest.mark.parametrize(
    "response_factory, diagnostic",
    [
        pytest.param(_TruncatedResponse, "IncompleteRead", id="incomplete-read"),
        pytest.param(
            lambda: _JSONResponse(b'{"workflow_runs": [{"a": "\xc3'),
            "unexpected end of data",
            id="utf8",
        ),
    ],
)
def test_wait_ci_cli_reports_truncated_response_without_traceback(
    monkeypatch, capsys, response_factory, diagnostic
):
    calls = []

    def open_request(request, timeout):
        assert request.full_url.startswith("https://api.github.com/repos/memtomem/memtomem/")
        response = response_factory()
        calls.append(response)
        return response

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(rp.urllib.request, "urlopen", open_request)
    assert (
        rp.main(
            [
                "wait-ci",
                "--repository",
                "memtomem/memtomem",
                "--sha",
                "abc",
                "--timeout-seconds",
                "10",
                "--interval-seconds",
                "0",
            ]
        )
        == 1
    )
    assert len(calls) == 3
    assert all(response.closed for response in calls)
    output = capsys.readouterr()
    assert "GitHub Actions API failed 3 consecutive times" in output.err
    assert diagnostic in output.err
    assert "Traceback" not in output.err
    assert not output.out


def test_cli_reports_unconverted_protocol_error(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise http.client.BadStatusLine("broken status")

    monkeypatch.setattr(rp, "validate_opencode_pypi", fail)
    assert rp.main(["opencode-pypi", "--repo-root", "."]) == 1
    output = capsys.readouterr()
    assert "release preflight failed: broken status" in output.err
    assert "Traceback" not in output.err
    assert not output.out


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


# Release-readiness checks use fake HTTP and time; they must never publish or
# depend on live registry contents in CI.
_SERVER_NAME = "io.github.memtomem/memtomem"


def _pypi_metadata(version="0.3.6", description=None):
    return {
        "info": {
            "name": "memtomem",
            "version": version,
            "description": description
            if description is not None
            else f"<!-- mcp-name: {_SERVER_NAME} -->",
        }
    }


def _opencode_repo(tmp_path):
    repo = _repo(tmp_path)
    contract = repo / "packages/memtomem-plugin-assets/contract.toml"
    contract.parent.mkdir(parents=True)
    contract.write_text('[core]\nversion = "0.3.6"\nmcp_extras = ["onnx"]\n', encoding="utf-8")
    generated = repo / "packages/opencode-memtomem/src/generated.ts"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        'export const CORE_VERSION = "0.3.6";\n'
        'export const MCP_REQUIREMENT = "memtomem[onnx]==0.3.6";\n',
        encoding="utf-8",
    )
    return repo


@pytest.mark.parametrize("suffix", ["", " ", "\n", "<br>", "-->", "--!>", ","])
def test_registry_ownership_token_accepts_upstream_boundaries(suffix):
    assert rp._contains_mcp_name(f"<!-- mcp-name: {_SERVER_NAME}{suffix}", _SERVER_NAME)


@pytest.mark.parametrize("suffix", ["-pro", ".extra", "_extra", "/extra", "2", "Z"])
def test_registry_ownership_token_rejects_name_prefix_confusion(suffix):
    wrong = f"mcp-name: {_SERVER_NAME}{suffix}"
    assert not rp._contains_mcp_name(wrong, _SERVER_NAME)
    assert rp._contains_mcp_name(wrong + f"\nmcp-name: {_SERVER_NAME}", _SERVER_NAME)


def test_registry_waits_for_released_description_before_checking_unused_version(tmp_path):
    clock = _Clock()
    calls = []
    replies = iter(
        [
            (404, None),
            (503, None),
            (200, _pypi_metadata(description="no marker yet")),
            (200, _pypi_metadata()),
            (404, None),
        ]
    )

    def fetch(url, timeout):
        calls.append((url, timeout))
        return next(replies)

    assert (
        rp.validate_registry_publish(
            "v0.3.6",
            _repo(tmp_path),
            timeout_seconds=40,
            interval_seconds=10,
            fetch_json=fetch,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        == "0.3.6"
    )
    assert clock.now == 30
    assert calls[:4] == [("https://pypi.org/pypi/memtomem/0.3.6/json", 10)] * 4
    assert calls[4] == (
        "https://registry.modelcontextprotocol.io/v0.1/servers/"
        "io.github.memtomem%2Fmemtomem/versions/0.3.6?include_deleted=true",
        10,
    )


@pytest.mark.parametrize(
    "response,reason",
    [
        ((404, None), "absent from PyPI"),
        ((429, None), "HTTP 429"),
        ((503, None), "HTTP 503"),
        ((200, _pypi_metadata(description="missing")), "ownership token"),
    ],
)
def test_registry_propagation_timeout_reports_last_observed_failure(tmp_path, response, reason):
    clock = _Clock()
    calls = []

    def fetch(url, timeout):
        assert url.startswith("https://pypi.org/")  # Never reaches the registry.
        calls.append(timeout)
        return response

    with pytest.raises(rp.ReleaseCheckError, match=reason):
        rp.validate_registry_publish(
            "v0.3.6",
            _repo(tmp_path),
            timeout_seconds=12,
            interval_seconds=10,
            fetch_json=fetch,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.now == 12
    assert calls == [10, 2]  # Final request and sleep respect the remaining budget.


def test_registry_retries_transport_error_but_not_bad_metadata(tmp_path):
    clock = _Clock()
    replies = iter([rp._ProbePending("network unavailable"), (200, _pypi_metadata()), (404, None)])

    def fetch(*_):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    assert (
        rp.validate_registry_publish(
            "v0.3.6",
            _repo(tmp_path),
            fetch_json=fetch,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        == "0.3.6"
    )
    assert clock.now == 10


@pytest.mark.parametrize(
    "response",
    [
        (403, None),
        (200, []),
        (200, {}),
        (200, {"info": []}),
        (200, _pypi_metadata(version="9.9.9")),
        (200, {"info": {"name": "other", "version": "0.3.6"}}),
        (200, {"info": {"name": "memtomem", "version": "0.3.6", "description": None}}),
    ],
)
def test_registry_rejects_bad_metadata_without_polling(tmp_path, response):
    clock = _Clock()
    with pytest.raises(rp.ReleaseCheckError):
        rp.validate_registry_publish(
            "v0.3.6",
            _repo(tmp_path),
            fetch_json=lambda *_: response,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.now == 0


@pytest.mark.parametrize("state", ["active", "deprecated", "deleted"])
def test_registry_refuses_every_already_listed_state(tmp_path, state):
    replies = iter([(200, _pypi_metadata()), (200, {"status": state})])
    with pytest.raises(rp.ReleaseCheckError, match="already listed"):
        rp.validate_registry_publish("v0.3.6", _repo(tmp_path), fetch_json=lambda *_: next(replies))


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500, 503])
def test_registry_does_not_treat_unavailable_lookup_as_unused(tmp_path, status):
    replies = iter([(200, _pypi_metadata()), (status, None)])
    with pytest.raises(rp.ReleaseCheckError, match="cannot confirm registry"):
        rp.validate_registry_publish("v0.3.6", _repo(tmp_path), fetch_json=lambda *_: next(replies))


@pytest.mark.parametrize(
    "change", ["test-tag", "tag-drift", "name", "pypi-base", "package-version"]
)
def test_registry_local_refusal_precedes_any_network(tmp_path, change):
    repo = _repo(tmp_path)
    manifest_path = repo / "server.json"
    manifest = json.loads(manifest_path.read_text())
    tag = "v0.3.6"
    if change == "test-tag":
        tag = "test-v0.3.6a1"
    elif change == "tag-drift":
        tag = "v0.3.7"
    elif change == "name":
        manifest["name"] = "io.github.someone/other"
    elif change == "pypi-base":
        manifest["packages"][0]["registryBaseUrl"] = "https://test.pypi.org"
    else:
        manifest["packages"][0]["version"] = "0.3.5"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(rp.ReleaseCheckError):
        rp.validate_registry_publish(tag, repo, fetch_json=lambda *_: pytest.fail("network called"))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["timeout_seconds", "interval_seconds"])
def test_registry_rejects_unbounded_or_nonpositive_polling(tmp_path, value, field):
    with pytest.raises(rp.ReleaseCheckError, match="finite and greater"):
        rp.validate_registry_publish("v0.3.6", tmp_path, **{field: value})


def test_opencode_checks_its_actual_core_pin_once(tmp_path):
    calls = []

    def fetch(url, timeout):
        calls.append((url, timeout))
        return 200, _pypi_metadata(description="no ownership marker needed for npm")

    assert rp.validate_opencode_pypi(_opencode_repo(tmp_path), fetch_json=fetch) == "0.3.6"
    assert calls == [("https://pypi.org/pypi/memtomem/0.3.6/json", 10)]


@pytest.mark.parametrize("status,reason", [(404, "absent"), (503, "could not confirm")])
def test_opencode_missing_or_unavailable_pypi_fails_with_recovery(tmp_path, status, reason):
    calls = []

    def fetch(*args):
        calls.append(args)
        return status, None

    with pytest.raises(rp.ReleaseCheckError) as error:
        rp.validate_opencode_pypi(_opencode_repo(tmp_path), fetch_json=fetch)
    message = str(error.value)
    assert all(text in message for text in ["memtomem==0.3.6", reason, "approve", "rerun"])
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["version", "requirement", "duplicate", "missing", "contract"])
def test_opencode_refuses_pin_drift_before_network(tmp_path, change):
    repo = _opencode_repo(tmp_path)
    generated = repo / "packages/opencode-memtomem/src/generated.ts"
    text = generated.read_text()
    if change == "version":
        text = text.replace('CORE_VERSION = "0.3.6"', 'CORE_VERSION = "0.3.7"')
    elif change == "requirement":
        text = text.replace("memtomem[onnx]", "memtomem[all]")
    elif change == "duplicate":
        text += 'export const CORE_VERSION = "0.3.6";\n'
    elif change == "missing":
        text = ""
    else:
        (repo / "packages/memtomem-plugin-assets/contract.toml").write_text("[core]\n")
    generated.write_text(text)
    with pytest.raises(rp.ReleaseCheckError):
        rp.validate_opencode_pypi(repo, fetch_json=lambda *_: pytest.fail("network called"))


@pytest.mark.parametrize("status", [200, 404, 503])
def test_http_probe_preserves_status_and_sets_headers_and_timeout(monkeypatch, status):
    def open_request(request, timeout):
        assert request.full_url == "https://pypi.org/pypi/memtomem/0.3.6/json"
        assert request.get_header("Accept") == "application/json"
        assert request.get_header("User-agent") == "memtomem-release-preflight"
        assert timeout == 10
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(b"bad"))
        return _JSONResponse(json.dumps(_pypi_metadata()).encode())

    monkeypatch.setattr(rp.urllib.request, "urlopen", open_request)
    actual, body = rp._request_json("https://pypi.org/pypi/memtomem/0.3.6/json", 10)
    assert actual == status
    assert body == (_pypi_metadata() if status == 200 else None)


def test_http_probe_distinguishes_bad_json_from_transient_network(monkeypatch):
    monkeypatch.setattr(rp.urllib.request, "urlopen", lambda *a, **kw: _JSONResponse(b"not JSON"))
    with pytest.raises(rp.ReleaseCheckError, match="invalid JSON") as error:
        rp._request_json("https://pypi.org/example", 10)
    assert not isinstance(error.value, rp._ProbePending)

    def unavailable(*a, **kw):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(rp.urllib.request, "urlopen", unavailable)
    with pytest.raises(rp._ProbePending, match="could not confirm"):
        rp._request_json("https://pypi.org/example", 10)


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: http.client.BadStatusLine("broken status"), id="bad-status"),
        pytest.param(lambda: http.client.LineTooLong("status line"), id="long-status"),
    ],
)
def test_http_probe_classifies_protocol_errors_as_pending(monkeypatch, error_factory):
    error = error_factory()

    def broken_response(*args, **kwargs):
        raise error

    monkeypatch.setattr(rp.urllib.request, "urlopen", broken_response)
    with pytest.raises(rp._ProbePending, match="could not confirm") as caught:
        rp._request_json("https://pypi.org/example", 10)
    assert caught.value.__cause__ is error


def test_registry_recovers_from_truncated_pypi_body(tmp_path, monkeypatch):
    clock = _Clock()
    calls = []
    truncated = _TruncatedResponse()

    def open_request(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            return truncated
        if len(calls) == 2:
            return _JSONResponse(json.dumps(_pypi_metadata()).encode())
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

    monkeypatch.setattr(rp.urllib.request, "urlopen", open_request)
    assert (
        rp.validate_registry_publish(
            "v0.3.6",
            _repo(tmp_path),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        == "0.3.6"
    )
    assert truncated.closed
    assert clock.now == 10
    assert calls[:2] == ["https://pypi.org/pypi/memtomem/0.3.6/json"] * 2
    assert len(calls) == 3
    assert calls[2].endswith("/versions/0.3.6?include_deleted=true")


def test_registry_repeated_body_truncation_exhausts_polling_budget(tmp_path, monkeypatch):
    clock = _Clock()
    timeouts = []

    def open_request(request, timeout):
        assert request.full_url.startswith("https://pypi.org/")
        timeouts.append(timeout)
        return _TruncatedResponse()

    monkeypatch.setattr(rp.urllib.request, "urlopen", open_request)
    with pytest.raises(
        rp.ReleaseCheckError, match="timed out after 12s: could not confirm.*IncompleteRead"
    ):
        rp.validate_registry_publish(
            "v0.3.6",
            _repo(tmp_path),
            timeout_seconds=12,
            interval_seconds=10,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
    assert clock.now == 12
    assert timeouts == [10, 2]


@pytest.mark.parametrize("command", ["registry", "opencode-pypi"])
def test_readiness_cli_reports_http_failure_without_traceback(
    tmp_path, monkeypatch, capsys, command
):
    def open_request(request, timeout):
        if command == "registry" and request.full_url.startswith("https://pypi.org/"):
            return _JSONResponse(json.dumps(_pypi_metadata()).encode())
        return _TruncatedResponse()

    monkeypatch.setattr(rp.urllib.request, "urlopen", open_request)
    args = [command, "--repo-root", str(_opencode_repo(tmp_path))]
    if command == "registry":
        args += ["--tag", "v0.3.6"]
    assert rp.main(args) == 1
    output = capsys.readouterr()
    assert "release preflight failed" in output.err
    assert "could not confirm" in output.err
    assert "IncompleteRead" in output.err
    assert "Traceback" not in output.err
    assert not output.out


@pytest.mark.parametrize("command", ["registry", "opencode-pypi"])
@pytest.mark.parametrize("allowed", [True, False])
def test_readiness_cli_actually_runs_the_gate(tmp_path, monkeypatch, capsys, command, allowed):
    repo = _opencode_repo(tmp_path)
    replies = iter(
        [(200, _pypi_metadata()), (404 if allowed else 200, {})]
        if command == "registry"
        else [(200, _pypi_metadata()) if allowed else (404, None)]
    )
    monkeypatch.setattr(rp, "_request_json", lambda *_: next(replies))
    args = [command, "--repo-root", str(repo)]
    if command == "registry":
        args += ["--tag", "v0.3.6"]
    assert rp.main(args) == (0 if allowed else 1)
    output = capsys.readouterr()
    assert ("release preflight failed" in output.err) is not allowed
    with pytest.raises(StopIteration):
        next(replies)  # Proves the selected CLI command used every required probe.


_REGISTRY_PREFIX = (*_CONTRACT_PREFIX[:-1], "registry")
_OPENCODE_PREFIX = ("python", "tools/release_preflight.py", "opencode-pypi")


def _required_step(document, job_name, step_id):
    job = document["jobs"][job_name]
    assert "continue-on-error" not in job
    steps = [step for step in job["steps"] if step.get("id") == step_id]
    assert len(steps) == 1
    step = steps[0]
    for bypass in ("if", "continue-on-error", "shell"):
        assert bypass not in step
    return step


def _assert_readiness_workflows(release, opencode):
    registry = release["jobs"]["mcp-registry"]
    assert registry["needs"] == "publish"
    assert registry["if"] == "${{ startsWith(github.ref_name, 'v') }}"
    assert registry["permissions"] == {"contents": "read", "id-token": "write"}
    assert registry["concurrency"] == {
        "group": "mcp-registry-${{ github.ref }}",
        "cancel-in-progress": False,
    }
    assert registry["runs-on"] == "ubuntu-latest"
    checkout = registry["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {"persist-credentials": False}
    ids = [step.get("id") for step in registry["steps"]]
    ordered = ["registry-gate", "publisher-install", "registry-login", "registry-publish"]
    assert [item for item in ids if item in ordered] == ordered
    for step_id in ordered:
        _required_step(release, "mcp-registry", step_id)
    argv = _single_command_argv(release, "mcp-registry", "registry-gate", _REGISTRY_PREFIX)
    args = rp._build_parser().parse_args(argv[len(_REGISTRY_PREFIX) - 1 :])
    assert (args.tag, args.repo_root, args.timeout_seconds, args.interval_seconds) == (
        "$GITHUB_REF_NAME",
        Path("."),
        300,
        10,
    )
    for step_id, expected in (
        ("registry-login", ("./mcp-publisher", "login", "github-oidc")),
        ("registry-publish", ("./mcp-publisher", "publish")),
    ):
        assert _single_command_argv(release, "mcp-registry", step_id, expected) == list(expected)
    install = _required_step(release, "mcp-registry", "publisher-install")
    assert install["env"] == {
        "MCP_PUBLISHER_VERSION": "1.8.1",
        "MCP_PUBLISHER_SHA256": "a06c9096dcb9727c13555b6be26c7effa707b01f06a4c561ba7a3635443cf2cc",
    }
    assert (
        '"https://github.com/modelcontextprotocol/registry/releases/download/v${MCP_PUBLISHER_VERSION}/mcp-publisher_linux_amd64.tar.gz"'
        in install["run"]
    )
    assert (
        'echo "$MCP_PUBLISHER_SHA256  $RUNNER_TEMP/mcp-publisher.tar.gz" | sha256sum --check --strict'
        in install["run"]
    )
    assert install["run"].index("sha256sum") < install["run"].index("tar xzf")

    gate = _required_step(opencode, "preflight", "pypi-core")
    assert "if" not in opencode["jobs"]["preflight"]
    assert gate["working-directory"] == "${{ github.workspace }}"
    argv = _single_command_argv(opencode, "preflight", "pypi-core", _OPENCODE_PREFIX)
    args = rp._build_parser().parse_args(argv[len(_OPENCODE_PREFIX) - 1 :])
    assert args.repo_root == Path(".")
    publish = opencode["jobs"]["publish"]
    assert publish["needs"] == "preflight"
    assert "if" not in publish  # No always() bypass of a failed preflight.
    assert "continue-on-error" not in publish


def test_readiness_workflows_enforce_publication_order_and_gates():
    _assert_readiness_workflows(_workflow("release.yml"), _workflow("release-opencode-plugin.yml"))


@pytest.mark.parametrize(
    "target", ["registry-gate", "registry-login", "registry-publish", "pypi-core"]
)
@pytest.mark.parametrize(
    "bypass", ["delete", "echo", "if", "continue-on-error", "shell", "comment", "or-true"]
)
def test_readiness_workflow_guard_rejects_disabled_or_decoy_commands(target, bypass):
    release = copy.deepcopy(_workflow("release.yml"))
    opencode = copy.deepcopy(_workflow("release-opencode-plugin.yml"))
    document, job = (opencode, "preflight") if target == "pypi-core" else (release, "mcp-registry")
    step = _required_step(document, job, target)
    if bypass == "delete":
        document["jobs"][job]["steps"].remove(step)
    elif bypass == "echo":
        step["run"] = "echo " + step["run"]
    elif bypass == "comment":
        step["run"] = "# " + step["run"]
    elif bypass == "or-true":
        step["run"] = step["run"].strip() + " || true"
    else:
        step[bypass] = {"if": "${{ false }}", "continue-on-error": True, "shell": "echo {0}"}[
            bypass
        ]
    with pytest.raises(AssertionError):
        _assert_readiness_workflows(release, opencode)


@pytest.mark.parametrize(
    "bypass",
    [
        "registry-needs",
        "opencode-needs",
        "prod-filter",
        "permissions",
        "concurrency",
        "order",
        "job-continue",
        "always-publish",
        "wrong-root",
        "checkout-main",
        "checksum",
    ],
)
def test_readiness_workflow_guard_rejects_dependency_and_order_bypasses(bypass):
    release = _workflow("release.yml")
    opencode = _workflow("release-opencode-plugin.yml")
    registry = release["jobs"]["mcp-registry"]
    if bypass == "registry-needs":
        registry["needs"] = "preflight"
    elif bypass == "opencode-needs":
        opencode["jobs"]["publish"]["needs"] = []
    elif bypass == "prod-filter":
        registry["if"] = "${{ always() }}"
    elif bypass == "permissions":
        registry["permissions"] = {"contents": "read"}
    elif bypass == "concurrency":
        registry["concurrency"]["cancel-in-progress"] = True
    elif bypass == "order":
        registry["steps"][-1], registry["steps"][2] = registry["steps"][2], registry["steps"][-1]
    elif bypass == "job-continue":
        registry["continue-on-error"] = True
    elif bypass == "always-publish":
        opencode["jobs"]["publish"]["if"] = "${{ always() }}"
    elif bypass == "wrong-root":
        _required_step(opencode, "preflight", "pypi-core")["working-directory"] = "."
    elif bypass == "checkout-main":
        registry["steps"][0]["with"]["ref"] = "main"
    else:
        step = _required_step(release, "mcp-registry", "publisher-install")
        step["run"] = step["run"].replace("sha256sum --check --strict", "cat")
    with pytest.raises(AssertionError):
        _assert_readiness_workflows(release, opencode)
