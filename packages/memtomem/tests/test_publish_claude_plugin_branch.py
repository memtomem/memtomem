"""Tests for the tool that publishes the Claude plugin folder to its directory branch."""

from __future__ import annotations

import base64
import importlib.util
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_PATH = "packages/memtomem-claude-plugin"
_BRANCH = "claude-plugin-directory"
_REF = f"refs/heads/{_BRANCH}"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "publisher",
    "GIT_AUTHOR_EMAIL": "publisher@example.com",
    "GIT_COMMITTER_NAME": "publisher",
    "GIT_COMMITTER_EMAIL": "publisher@example.com",
}


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "publish_claude_plugin_branch.py"
    spec = importlib.util.spec_from_file_location("publish_claude_plugin_branch", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pub = _load_tool()


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    # ``-c`` beats the developer's global config: no signing prompt, a fixed identity.
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        input=stdin,
    ).stdout.strip()


def _write_plugin(repo: Path, version: object, body: str) -> None:
    folder = repo / _PATH / ".claude-plugin"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"name": "memtomem"} if version is None else {"name": "memtomem", "version": version}
    (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (repo / _PATH / "README.md").write_text(body, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _release(repo: Path, version: object, body: str) -> str:
    _write_plugin(repo, version, body)
    return _commit(repo, f"plugin {version}")


def _objects(repo: Path) -> str:
    # Unreachable objects included: a refusal that left an orphan commit behind
    # would be invisible to anything that walks from the refs.
    return _git(repo, "cat-file", "--batch-all-objects", "--batch-check")


def _remote_ref(remote: Path, ref: str = _REF) -> str:
    result = subprocess.run(
        ["git", "-C", str(remote), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip()


def _tree_of(repo: Path, *entries: tuple[str, str, str]) -> str:
    listing = "".join(
        f"{'040000' if kind == 'tree' else '100644'} {kind} {object_id}\t{name}\n"
        for kind, object_id, name in entries
    )
    return _git(repo, "mktree", stdin=listing)


def _handmade_tip(
    repo: Path,
    source: str,
    *,
    message: str | None = None,
    plugin: str | None = None,
    at_root: tuple[str, str, str] | None = None,
    beside_plugin: tuple[str, str, str] | None = None,
    parent: str | None = None,
) -> str:
    """A branch commit built by hand, well-formed unless an argument says otherwise."""
    plugin = plugin or _git(repo, "rev-parse", f"{source}:{_PATH}")
    inner = [("tree", plugin, "memtomem-claude-plugin")]
    if beside_plugin:
        inner.append(beside_plugin)
    outer = [("tree", _tree_of(repo, *inner), "packages")]
    if at_root:
        outer.append(at_root)
    if message is None:
        message = f"publish by hand\n\nSource-Commit: {source}\n"
    parents = ["-p", parent] if parent else []
    return _git(repo, "commit-tree", _tree_of(repo, *outer), *parents, stdin=message)


@dataclass
class _Work:
    repo: Path
    remote: Path
    first: str

    def run(self, source: str = "HEAD", *extra: str, remote: str | None = None) -> int:
        return pub.main(
            [
                "--source",
                source,
                "--remote",
                remote or str(self.remote),
                "--branch",
                _BRANCH,
                "--repo-root",
                str(self.repo),
                *extra,
            ]
        )

    def place(self, commit: str, ref: str = _REF) -> None:
        _git(self.repo, "push", "-q", "--force", str(self.remote), f"{commit}:{ref}")


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Work:
    for name, value in _IDENTITY.items():
        monkeypatch.setenv(name, value)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "pyproject.toml").write_text("[tool.uv.sources]\n", encoding="utf-8")
    first = _release(repo, "0.5.11", "one")
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare")
    return _Work(repo, remote, first)


def _refused(work: _Work, capsys: pytest.CaptureFixture[str], *args: str, **kwargs: str) -> str:
    """Run a publish that must refuse; return its message after the common checks."""
    before, remote_before = _objects(work.repo), _remote_ref(work.remote)
    capsys.readouterr()
    assert work.run(*args, **kwargs) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("publish refused: ")
    assert _objects(work.repo) == before
    assert _remote_ref(work.remote) == remote_before
    return captured.err


def test_object_inventory_notices_an_unreachable_object(work: _Work) -> None:
    before = _objects(work.repo)
    _git(work.repo, "hash-object", "-w", "--stdin", stdin="orphan")
    assert _objects(work.repo) != before


def test_first_publish_creates_a_root_commit_holding_only_the_plugin_folder(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    assert work.run() == 0
    commit = capsys.readouterr().out.strip()
    assert commit == _remote_ref(work.remote)
    assert _git(work.repo, "rev-list", "--parents", "-n", "1", commit) == commit
    files = _git(work.repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert files == [f"{_PATH}/.claude-plugin/plugin.json", f"{_PATH}/README.md"]
    assert _git(work.repo, "rev-parse", f"{commit}:{_PATH}") == _git(
        work.repo, "rev-parse", f"{work.first}:{_PATH}"
    )
    message = _git(work.repo, "log", "-1", "--format=%B", commit)
    assert (
        message.splitlines()[0] == f"publish: memtomem-claude-plugin 0.5.11 from {work.first[:8]}"
    )
    assert message.splitlines()[-1] == f"Source-Commit: {work.first}"
    assert _git(work.repo, "log", "-1", "--format=%an <%ae>", commit) == (
        "publisher <publisher@example.com>"
    )


def test_an_unchanged_plugin_folder_publishes_nothing(
    work: _Work, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert work.run() == 0
    tip = capsys.readouterr().out.strip()
    (work.repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _commit(work.repo, "touch only the root")
    before = _objects(work.repo)

    lookups = []
    real_lookup = pub._remote_tip
    monkeypatch.setattr(pub, "_remote_tip", lambda *a: lookups.append(a) or real_lookup(*a))
    monkeypatch.setattr(pub, "_push", lambda *a: pytest.fail("a no-op must not push"))
    assert work.run() == 0
    assert capsys.readouterr().out.strip() == tip
    assert _remote_ref(work.remote) == tip
    assert _objects(work.repo) == before
    assert len(lookups) == 2  # The read-back runs after a no-op too.


def test_a_higher_version_adds_one_commit_on_top_of_the_tip(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    assert work.run() == 0
    tip = capsys.readouterr().out.strip()
    source = _release(work.repo, "0.5.12", "two")
    assert work.run() == 0
    commit = capsys.readouterr().out.strip()
    assert commit == _remote_ref(work.remote)
    assert _git(work.repo, "rev-list", "--parents", "-n", "1", commit) == f"{commit} {tip}"
    assert _git(work.repo, "log", "-1", "--format=%B", commit).splitlines()[-1] == (
        f"Source-Commit: {source}"
    )


def test_version_order_is_numeric(work: _Work, capsys: pytest.CaptureFixture[str]) -> None:
    _release(work.repo, "0.9.0", "nine")
    assert work.run() == 0
    _release(work.repo, "0.10.0", "ten")
    assert work.run() == 0
    assert _remote_ref(work.remote) == capsys.readouterr().out.split()[-1]


@pytest.mark.parametrize("version", ["0.5.11", "0.5.10", "0.4.99"])
def test_changed_content_without_a_higher_version_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str], version: str
) -> None:
    assert work.run() == 0
    _release(work.repo, version, "changed")
    message = _refused(work, capsys)
    assert f"version {version} is not higher than the published 0.5.11" in message


@pytest.mark.parametrize(
    "version,reason",
    [
        ("1.2.3rc1", "expected X.Y.Z"),
        ("1.2.3.4", "expected X.Y.Z"),
        ("1.2", "expected X.Y.Z"),
        (5, "expected X.Y.Z"),
        (None, "expected X.Y.Z"),
    ],
)
def test_a_source_version_outside_the_grammar_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str], version: object, reason: str
) -> None:
    source = _release(work.repo, version, "odd")
    message = _refused(work, capsys)
    assert reason in message and f"source {source}" in message


def test_a_tip_version_outside_the_grammar_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    odd = _release(work.repo, "1.2.3.4", "odd")
    work.place(_handmade_tip(work.repo, odd))
    _release(work.repo, "2.0.0", "fine")
    message = _refused(work, capsys)
    assert "expected X.Y.Z" in message and "branch tip" in message


def test_a_source_manifest_that_is_not_json_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    (work.repo / _PATH / ".claude-plugin" / "plugin.json").write_text("[", encoding="utf-8")
    _commit(work.repo, "break the manifest")
    assert "is not a JSON object" in _refused(work, capsys)


def test_a_source_without_the_plugin_folder_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(work.repo, "rm", "-q", "-r", "packages")
    _commit(work.repo, "remove the plugin")
    assert f"has no directory at {_PATH}" in _refused(work, capsys)


def test_a_source_that_is_not_a_commit_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "is not a commit here" in _refused(work, capsys, "no-such-rev")


@pytest.mark.parametrize("level", ["root", "packages"])
def test_a_tip_holding_anything_besides_the_plugin_folder_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str], level: str
) -> None:
    blob = _git(work.repo, "hash-object", "-w", "--stdin", stdin="[tool.uv.sources]\n")
    extra = ("blob", blob, "pyproject.toml")
    tip = _handmade_tip(
        work.repo,
        work.first,
        at_root=extra if level == "root" else None,
        beside_plugin=extra if level == "packages" else None,
    )
    work.place(tip)
    _release(work.repo, "0.5.12", "two")
    message = _refused(work, capsys)
    assert "pyproject.toml" in message
    assert ("at its root" if level == "root" else "at packages") in message


def test_a_tip_whose_folder_differs_from_the_commit_it_names_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    other = _release(work.repo, "0.5.12", "two")
    tip = _handmade_tip(
        work.repo, work.first, plugin=_git(work.repo, "rev-parse", f"{other}:{_PATH}")
    )
    work.place(tip)
    _release(work.repo, "0.5.13", "three")
    assert "does not hold the plugin folder of the commit it names" in _refused(work, capsys)


@pytest.mark.parametrize(
    "message,reason",
    [
        ("publish by hand\n", "carries 0 Source-Commit trailers"),
        (
            "publish by hand\n\nSource-Commit: {first}\nSource-Commit: {first}\n",
            "carries 2 Source-Commit trailers",
        ),
        ("publish by hand\n\nSource-Commit: " + "0" * 40 + "\n", "which is not a commit here"),
        ("publish by hand\n\nSource-Commit: main\n", "which is not a commit here"),
    ],
)
def test_a_tip_without_one_usable_source_trailer_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str], message: str, reason: str
) -> None:
    work.place(_handmade_tip(work.repo, work.first, message=message.format(first=work.first)))
    _release(work.repo, "0.5.12", "two")
    assert reason in _refused(work, capsys)


def test_a_source_that_does_not_descend_from_the_published_one_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    _release(work.repo, "0.5.12", "two")
    assert work.run() == 0
    # An older commit on the same line, then a commit on a line that never had it.
    assert "does not descend from" in _refused(work, capsys, work.first)
    _git(work.repo, "checkout", "-q", "-b", "side", work.first)
    side = _release(work.repo, "0.6.0", "side")
    assert "does not descend from" in _refused(work, capsys, side)


def test_a_well_formed_hand_made_tip_is_accepted(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    """The tip check is about shape, and this pins that it is not about authorship.

    Nothing in a commit says which program wrote it, so a correctly shaped and
    correctly trailered commit pushed by hand is built on like any other.
    """
    tip = _handmade_tip(work.repo, work.first)
    work.place(tip)
    _release(work.repo, "0.5.12", "two")
    assert work.run() == 0
    commit = capsys.readouterr().out.strip()
    assert _git(work.repo, "rev-list", "--parents", "-n", "1", commit) == f"{commit} {tip}"


@pytest.mark.parametrize("name", sorted(_IDENTITY))
def test_a_missing_identity_variable_is_refused_before_anything_is_written(
    work: _Work, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.delenv(name)
    assert f"commit identity is not set: {name}" in _refused(work, capsys)


def test_a_branch_that_only_shares_the_name_suffix_is_not_the_branch(
    work: _Work, capsys: pytest.CaptureFixture[str]
) -> None:
    lookalike = _handmade_tip(work.repo, work.first, message="not ours\n")
    # The second name is one ``git ls-remote <remote> refs/heads/<branch>`` does
    # return: its pattern matches a trailing part of the ref name.
    others = [f"refs/heads/x/{_BRANCH}", f"refs/heads/x/{_REF}"]
    for name in others:
        work.place(lookalike, name)
    listed = _git(work.repo, "ls-remote", str(work.remote), _REF)
    assert others[1] in listed and f"\t{_REF}" not in listed

    assert work.run() == 0
    commit = capsys.readouterr().out.strip()
    assert _git(work.repo, "rev-list", "--parents", "-n", "1", commit) == commit
    assert _remote_ref(work.remote) == commit
    assert [_remote_ref(work.remote, name) for name in others] == [lookalike, lookalike]


def test_a_remote_that_cannot_be_read_is_not_an_absent_branch(
    work: _Work, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    message = _refused(work, capsys, remote=str(tmp_path / "missing.git"))
    assert "git ls-remote" in message and "failed" in message


def test_a_branch_moved_since_the_lookup_fails_the_push_and_is_left_alone(
    work: _Work, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert work.run() == 0
    tip = capsys.readouterr().out.strip()
    _release(work.repo, "0.5.12", "two")
    rival = _handmade_tip(work.repo, work.first, parent=tip)
    real_build = pub._build

    def build_then_lose_the_race(*args: object) -> str:
        commit = real_build(*args)
        _git(work.repo, "push", "-q", str(work.remote), f"{rival}:{_REF}")
        return commit

    monkeypatch.setattr(pub, "_build", build_then_lose_the_race)
    assert work.run() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "git push" in captured.err and "failed" in captured.err
    assert _remote_ref(work.remote) == rival


@pytest.mark.parametrize("answer", [None, "0" * 40])
def test_a_read_back_that_is_not_the_built_commit_fails_the_run(
    work: _Work,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    answer: str | None,
) -> None:
    real_lookup = pub._remote_tip
    answers = iter([real_lookup, lambda *_: answer])
    monkeypatch.setattr(pub, "_remote_tip", lambda *a: next(answers)(*a))
    assert work.run() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"at {answer}, not at the expected" in captured.err


def test_dry_run_builds_without_pushing(
    work: _Work, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pub, "_push", lambda *a: pytest.fail("a dry run must not push"))
    assert work.run("HEAD", "--dry-run") == 0
    commit = capsys.readouterr().out.strip()
    assert _git(work.repo, "cat-file", "-t", commit) == "commit"
    assert _remote_ref(work.remote) == ""


def test_a_named_but_unset_token_variable_is_refused(
    work: _Work, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PUBLISH_TOKEN", raising=False)
    assert "PUBLISH_TOKEN is not set" in _refused(
        work, capsys, "HEAD", "--token-env", "PUBLISH_TOKEN"
    )


def test_the_token_reaches_only_network_commands_and_only_through_the_environment(
    work: _Work, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "not-a-real-token"
    monkeypatch.setenv("PUBLISH_TOKEN", token)
    header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
    calls: list[tuple[str, list[str], dict[str, str]]] = []
    real_run = subprocess.run

    def recording_run(argv: list[str], **kwargs: object) -> object:
        calls.append((argv[3], argv, dict(kwargs["env"])))  # type: ignore[call-overload]
        return real_run(argv, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(pub.subprocess, "run", recording_run)
    assert work.run("HEAD", "--token-env", "PUBLISH_TOKEN") == 0
    captured = capsys.readouterr()

    network = {"ls-remote", "fetch", "push"}
    seen = {command for command, _argv, _env in calls}
    assert {"ls-remote", "push", "commit-tree"} <= seen
    for command, argv, env in calls:
        assert not any(token in part or header in part for part in argv), command
        carries = env.get("GIT_CONFIG_VALUE_0") == header
        assert carries is (command in network), command
        if carries:
            assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
            assert env["GIT_CONFIG_COUNT"] == "1"
    assert token not in captured.out + captured.err
    assert header not in captured.out + captured.err
