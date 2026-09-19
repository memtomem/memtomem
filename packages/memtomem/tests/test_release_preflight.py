"""Tests for the fail-closed release preflight helper."""

from __future__ import annotations

import importlib.util
import io
import json
import shlex
import tarfile
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


def _contract_argv(document: dict, job_name: str, step_id: str) -> list[str]:
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
    assert tuple(argv[: len(_CONTRACT_PREFIX)]) == _CONTRACT_PREFIX, (
        f"step {step_id!r} must invoke {' '.join(_CONTRACT_PREFIX)}, "
        f"got: {argv[: len(_CONTRACT_PREFIX)]}"
    )
    return argv


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
