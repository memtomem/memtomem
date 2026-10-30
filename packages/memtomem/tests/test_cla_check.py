"""The CLA gate (``.github/cla-check.py``) and its workflow, against an in-memory GitHub.

The script runs from ``pull_request_target`` and ``issue_comment`` events, which execute the
default branch's copy, so a regression here only surfaces on the next contributor's pull
request. These tests pin the token it uses, the single status comment it keeps current, and
the permissions the workflow grants for that comment to land (#2911).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / ".github" / "cla-check.py"
_WORKFLOW = _ROOT / ".github" / "workflows" / "cla.yml"

REPO = "memtomem/memtomem"
PR = 77
SIGNATURE = "I have read the CLA Document and I hereby sign the CLA"
GITHUB_TOKEN = "github-token-sentinel"
PAT = "pat-sentinel-must-not-be-used"
BOT = "github-actions[bot]"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("cla_check", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["cla_check"] = module
    spec.loader.exec_module(module)
    return module


cla = _load()


class FakeGitHub:
    """Serves the endpoints the script calls and records every write."""

    def __init__(self, *, author: str = "alice", signed: tuple[str, ...] = ()) -> None:
        self.pr: dict[str, Any] = {"number": PR, "user": {"login": author}}
        self.commit_authors = [author]
        self.signatures: dict[str, Any] = {
            cla.SIGNATURES_KEY: [{"name": login} for login in signed]
        }
        self.comments: list[dict[str, Any]] = []
        self.next_comment_id = 1000
        self.tokens: list[str | None] = []
        self.writes: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    def add_comment(self, body: str, *, login: str = BOT) -> dict[str, Any]:
        self.next_comment_id += 1
        comment = {
            "id": self.next_comment_id,
            "body": body,
            "user": {"login": login},
            "created_at": f"2026-10-30T00:00:{self.next_comment_id % 60:02d}Z",
        }
        self.comments.append(comment)
        return comment

    def status_bodies(self) -> list[str]:
        return [c["body"] for c in self.comments if c["user"]["login"] == BOT]

    def _raise_if(self, key: str, command: str) -> None:
        if key in self.fail:
            raise cla.GitHubApiError(["gh", "api", command], "gh: Forbidden (HTTP 403)")

    def gh_api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.tokens.append(token)
        path = endpoint.split("?", 1)[0]
        prefix = f"repos/{REPO}/"
        assert path.startswith(prefix), endpoint
        rest = path[len(prefix) :]

        if method != "GET":
            self.writes.append((method, rest))

        if rest == f"pulls/{PR}" and method == "GET":
            return self.pr
        if rest == f"pulls/{PR}/commits":
            return [{"author": {"login": login}} for login in self.commit_authors]
        if rest.startswith("contents/") and method == "GET":
            raw = json.dumps(self.signatures).encode()
            return {"content": base64.b64encode(raw).decode(), "sha": "sig-sha"}
        if rest.startswith("contents/") and method == "PUT":
            self._raise_if("signature-write", endpoint)
            assert payload is not None
            self.signatures = json.loads(base64.b64decode(payload["content"]))
            return {}
        if rest == f"issues/{PR}/comments" and method == "GET":
            return list(self.comments)
        if rest == f"issues/{PR}/comments" and method == "POST":
            self._raise_if("POST", endpoint)
            assert payload is not None
            return self.add_comment(payload["body"])
        if rest.startswith("issues/comments/"):
            comment_id = int(rest.rsplit("/", 1)[1])
            comment = next(c for c in self.comments if c["id"] == comment_id)
            if method == "PATCH":
                self._raise_if("PATCH", endpoint)
                assert payload is not None
                comment["body"] = payload["body"]
                return comment
            if method == "DELETE":
                self._raise_if("DELETE", endpoint)
                self.comments.remove(comment)
                return None
        raise AssertionError(f"unexpected call: {method} {endpoint}")

    def gh_paginated(self, endpoint: str, *, token: str) -> list[Any]:
        return self.gh_api(endpoint, token=token)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(cla, "gh_api", fake.gh_api)
    monkeypatch.setattr(cla, "gh_paginated", fake.gh_paginated)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", GITHUB_TOKEN)
    monkeypatch.setenv("PERSONAL_ACCESS_TOKEN", PAT)
    monkeypatch.setenv("CLA_SIGNATURE_TEXT", SIGNATURE)
    monkeypatch.setenv("CLA_ALLOWLIST", "dependabot[bot],memtomem")
    return fake


def _run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, event: dict[str, Any]) -> int:
    event = {"repository": {"id": 1}, **event}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    return cla.main()


def _pr_event() -> dict[str, Any]:
    return {"pull_request": {"number": PR}}


def _comment_event(body: str, *, login: str = "alice", on_pr: bool = True) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": PR}
    if on_pr:
        issue["pull_request"] = {"url": "..."}
    return {
        "issue": issue,
        "comment": {
            "id": 555,
            "body": body,
            "user": {"login": login, "id": 9},
            "created_at": "2026-10-30T02:25:07Z",
        },
    }


def _signed_logins(github: FakeGitHub) -> set[str]:
    return {entry["name"] for entry in github.signatures[cla.SIGNATURES_KEY]}


# --- token -------------------------------------------------------------------------------


def test_every_call_uses_github_token_even_when_a_pat_is_set(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE)) == 0

    assert github.tokens, "the run made no API calls"
    assert set(github.tokens) == {GITHUB_TOKEN}
    assert _signed_logins(github) == {"alice"}


# --- the status comment ------------------------------------------------------------------


def test_first_missing_check_posts_one_status_comment(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event()) == 1

    assert github.writes == [("POST", f"issues/{PR}/comments")]
    (body,) = github.status_bodies()
    assert cla.MISSING_MARKER in body
    assert cla.STATUS_MARKER in body
    assert "Missing signature(s): `alice`" in body
    assert f"> {SIGNATURE}" in body


def test_identical_status_is_not_rewritten(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event()) == 1
    github.writes.clear()

    assert _run(monkeypatch, tmp_path, _pr_event()) == 1

    assert github.writes == []
    assert len(github.status_bodies()) == 1


def test_signing_rewrites_the_comment_and_says_a_maintainer_reruns_the_check(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event()) == 1
    (missing,) = github.comments
    github.writes.clear()

    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE)) == 0

    assert github.writes == [
        ("PUT", "contents/signatures/v1/cla.json"),
        ("PATCH", f"issues/comments/{missing['id']}"),
    ]
    (body,) = github.status_bodies()
    assert body.startswith(cla.SIGNED_MARKER)
    assert cla.MAINTAINER_RERUN_NOTE in body
    assert cla.MISSING_MARKER not in body


def test_pull_request_check_drops_the_rerun_note_once_signed(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE)) == 0
    assert cla.MAINTAINER_RERUN_NOTE in github.status_bodies()[0]

    assert _run(monkeypatch, tmp_path, _pr_event()) == 0

    (body,) = github.status_bodies()
    assert body.startswith(cla.SIGNED_MARKER)
    assert cla.MAINTAINER_RERUN_NOTE not in body


def test_new_unsigned_contributor_turns_signed_back_into_missing(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.signatures[cla.SIGNATURES_KEY].append({"name": "alice"})
    assert _run(monkeypatch, tmp_path, _pr_event()) == 0
    assert github.status_bodies()[0].startswith(cla.SIGNED_MARKER)

    github.commit_authors.append("bob")
    assert _run(monkeypatch, tmp_path, _pr_event()) == 1

    (body,) = github.status_bodies()
    assert "Missing signature(s): `bob`" in body
    assert cla.SIGNED_MARKER not in body


def test_missing_list_shrinks_when_one_of_two_contributors_signs(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.commit_authors.append("bob")
    assert _run(monkeypatch, tmp_path, _pr_event()) == 1
    assert "Missing signature(s): `alice`, `bob`" in github.status_bodies()[0]

    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE, login="bob")) == 1

    (body,) = github.status_bodies()
    assert "Missing signature(s): `alice`\n" in body


def test_duplicate_status_comments_collapse_to_the_oldest(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    oldest = github.add_comment(f"stale\n\n{cla.STATUS_MARKER}")
    newer = github.add_comment(f"also stale\n\n{cla.STATUS_MARKER}")

    assert _run(monkeypatch, tmp_path, _pr_event()) == 1

    assert github.writes == [
        ("PATCH", f"issues/comments/{oldest['id']}"),
        ("DELETE", f"issues/comments/{newer['id']}"),
    ]
    assert [c["id"] for c in github.comments] == [oldest["id"]]


def test_legacy_comments_without_the_status_marker_are_adopted(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy_signed = github.add_comment(
        "All contributors have signed the CLA. ✍️ ✅", login="cla-assistant[bot]"
    )
    legacy_missing = github.add_comment(
        f"Thank you! {cla.MISSING_MARKER} the CLA.", login="github-actions[bot]"
    )

    assert _run(monkeypatch, tmp_path, _pr_event()) == 1

    assert github.writes == [
        ("PATCH", f"issues/comments/{legacy_signed['id']}"),
        ("DELETE", f"issues/comments/{legacy_missing['id']}"),
    ]
    assert cla.STATUS_MARKER in legacy_signed["body"]


def test_comments_by_people_are_never_touched(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The old PAT path posted as the maintainer account; a person quoting the bot looks alike.
    human = github.add_comment(f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}", login="memtomem")

    assert _run(monkeypatch, tmp_path, _pr_event()) == 1

    assert human in github.comments
    assert human["body"] == f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}"
    assert github.writes == [("POST", f"issues/{PR}/comments")]


# --- failure boundaries ------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("signed", [False, True])
def test_comment_failure_warns_and_keeps_the_exit_code(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
    signed: bool,
) -> None:
    if signed:
        github.signatures[cla.SIGNATURES_KEY].append({"name": "alice"})
    if failing != "POST":
        github.add_comment(f"old\n\n{cla.STATUS_MARKER}")
        github.add_comment(f"older duplicate\n\n{cla.STATUS_MARKER}")
    github.fail.add(failing)

    assert _run(monkeypatch, tmp_path, _pr_event()) == (0 if signed else 1)

    assert failing in {method for method, _ in github.writes}
    err = capsys.readouterr().err
    assert "Warning: unable to update the CLA status comment" in err
    assert "HTTP 403" in err


def test_signature_write_failure_fails_the_run(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.fail.add("signature-write")

    with pytest.raises(cla.GitHubApiError):
        _run(monkeypatch, tmp_path, _comment_event(SIGNATURE))

    assert _signed_logins(github) == set()
    assert github.comments == []


# --- which comments count ----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Thanks, looks good!",
        f"> {SIGNATURE}",
        f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}",
    ],
    ids=["ordinary", "quoted-signature", "bot-status-comment"],
)
def test_other_pull_request_comments_are_ignored(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    assert _run(monkeypatch, tmp_path, _comment_event(body)) == 0

    assert github.tokens == []
    assert _signed_logins(github) == set()


def test_signature_on_a_plain_issue_is_ignored(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE, on_pr=False)) == 0

    assert github.tokens == []
    assert _signed_logins(github) == set()


@pytest.mark.parametrize(
    ("body", "command"),
    [
        (SIGNATURE, "sign"),
        (f"  {SIGNATURE}\n", "sign"),
        ("recheck", "recheck"),
        ("  ReCheck ", "recheck"),
        (SIGNATURE.lower(), "ignore"),
    ],
)
def test_comment_command_normalisation(body: str, command: str) -> None:
    event = {"comment": {"body": body}}
    assert cla.comment_command(event, SIGNATURE) == command


def test_recheck_re_evaluates_without_recording(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.signatures[cla.SIGNATURES_KEY].append({"name": "alice"})

    assert _run(monkeypatch, tmp_path, _comment_event("recheck")) == 0

    assert ("PUT", "contents/signatures/v1/cla.json") not in github.writes
    assert cla.MAINTAINER_RERUN_NOTE in github.status_bodies()[0]


# --- the workflow ------------------------------------------------------------------------


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_workflow_grants_each_job_exactly_its_permissions() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]

    assert workflow["permissions"] == {}
    assert set(jobs) == {"CLAAssistant", "CLASign"}
    # `pull-requests: write` is what lets GITHUB_TOKEN comment on a pull request;
    # `issues: write` with `pull-requests: read` is the configuration that returned 403.
    assert jobs["CLAAssistant"]["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }
    assert jobs["CLASign"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
    }


def test_workflow_has_no_concurrency_and_no_pat() -> None:
    workflow = _workflow()

    assert "concurrency" not in workflow
    for job in workflow["jobs"].values():
        assert "concurrency" not in job
    assert "PERSONAL_ACCESS_TOKEN" not in _WORKFLOW.read_text(encoding="utf-8")


def test_workflow_checks_out_only_the_default_branch_without_credentials() -> None:
    for name, job in _workflow()["jobs"].items():
        checkouts = [step for step in job["steps"] if "actions/checkout@" in step.get("uses", "")]
        assert len(checkouts) == 1, name
        assert checkouts[0]["with"] == {
            "ref": "${{ github.event.repository.default_branch }}",
            "persist-credentials": False,
        }, name


def test_workflow_job_conditions_split_the_events() -> None:
    jobs = _workflow()["jobs"]

    assert jobs["CLAAssistant"]["if"] == "${{ github.event_name == 'pull_request_target' }}"
    assert " ".join(jobs["CLASign"]["if"].split()) == (
        "${{ github.event_name == 'issue_comment' && github.event.issue.pull_request && "
        "( github.event.comment.body == 'recheck' || "
        f"github.event.comment.body == '{SIGNATURE}' ) }}}}"
    )
    for job in jobs.values():
        (run_step,) = [step for step in job["steps"] if "run" in step]
        assert run_step["env"]["CLA_SIGNATURE_TEXT"] == SIGNATURE
