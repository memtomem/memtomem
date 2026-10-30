"""The CLA gate (``.github/cla-check.py``) and its workflow, against an in-memory GitHub.

The script runs from ``pull_request_target`` and ``issue_comment`` events, which execute the
default branch's copy, so a regression here only surfaces on the next contributor's pull
request. These tests pin the token it uses, the single status comment it keeps current, the
permissions the workflow grants for that comment to land (#2911), and the ``CLA / PR #<n>``
commit status that carries the result, so that signing turns it green without a re-run (#2912).
"""

from __future__ import annotations

import base64
import copy
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
BASE = "b" * 40
HEAD = "h" * 40
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


def _pr(number: int, author: str, base: str = BASE, head: str = HEAD) -> dict[str, Any]:
    return {
        "number": number,
        "user": {"login": author},
        "base": {"sha": base},
        "head": {"sha": head},
    }


class FakeGitHub:
    """Serves the endpoints the script calls and records every write."""

    def __init__(self) -> None:
        self.prs: dict[int, dict[str, Any]] = {PR: _pr(PR, "alice")}
        # Commit authors between two SHAs, as `compare/{base}...{head}` reports them.
        self.ranges: dict[tuple[str, str], list[str]] = {(BASE, HEAD): ["alice"]}
        self.signatures: dict[str, Any] = {cla.SIGNATURES_KEY: []}
        self.comments: dict[int, list[dict[str, Any]]] = {PR: []}
        self.statuses: dict[tuple[str, str], dict[str, Any]] = {}
        self.next_comment_id = 1000
        self.tokens: list[str | None] = []
        self.writes: list[tuple[str, str]] = []
        self.reads: list[str] = []
        self.fail: set[str] = set()
        self.signature_reads = 0

    @property
    def commit_authors(self) -> list[str]:
        return self.ranges[(BASE, HEAD)]

    def add_comment(self, body: str, *, login: str = BOT, pr: int = PR) -> dict[str, Any]:
        self.next_comment_id += 1
        comment = {
            "id": self.next_comment_id,
            "body": body,
            "user": {"login": login},
            "created_at": f"2026-10-30T00:00:{self.next_comment_id % 60:02d}Z",
        }
        self.comments.setdefault(pr, []).append(comment)
        return comment

    def status_bodies(self, pr: int = PR) -> list[str]:
        return [c["body"] for c in self.comments[pr] if c["user"]["login"] == BOT]

    def status(self, pr: int = PR, sha: str = HEAD) -> dict[str, Any]:
        return self.statuses[(sha, f"CLA / PR #{pr}")]

    def comment_writes(self) -> list[tuple[str, str]]:
        return [write for write in self.writes if write[1].startswith("issues/")]

    def _raise_if(self, key: str, command: str) -> None:
        if key in self.fail:
            raise cla.GitHubApiError(["gh", "api", command], "gh: Forbidden (HTTP 403)")

    def _compare(self, rest: str, query: str) -> dict[str, Any]:
        base, head = rest[len("compare/") :].split("...")
        authors = self.ranges[(base, head)]
        params = dict(part.split("=") for part in query.split("&"))
        per_page, page = int(params["per_page"]), int(params["page"])
        commits = [
            {"sha": f"{index:040x}", "author": {"login": login}}
            for index, login in enumerate(authors)
        ]
        return {
            "total_commits": len(commits),
            "commits": commits[(page - 1) * per_page : page * per_page],
        }

    def gh_api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        token: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        self.tokens.append(token)
        path, _, query = endpoint.partition("?")
        prefix = f"repos/{REPO}/"
        assert path.startswith(prefix), endpoint
        rest = path[len(prefix) :]

        if method == "GET":
            self.reads.append(rest)
        else:
            self.writes.append((method, rest))

        if rest.startswith("pulls/") and rest.endswith("/commits"):
            raise AssertionError("the PR commits endpoint follows the moving head; use compare")
        if rest.startswith("pulls/") and method == "GET":
            return copy.deepcopy(self.prs[int(rest.split("/")[1])])
        if rest.startswith("compare/"):
            self._raise_if("compare", endpoint)
            return self._compare(rest, query)
        if rest.startswith("statuses/") and method == "POST":
            self._raise_if("status", endpoint)
            assert payload is not None
            self.statuses[(rest.split("/")[1], payload["context"])] = payload
            return {}
        if rest.startswith("contents/") and method == "GET":
            self.signature_reads += 1
            self._raise_if("signature-read", endpoint)
            if self.signature_reads > 1:
                # The signing path re-reads before it writes; fail only that read.
                self._raise_if("signature-reread", endpoint)
            raw = json.dumps(self.signatures).encode()
            return {"content": base64.b64encode(raw).decode(), "sha": "sig-sha"}
        if rest.startswith("contents/") and method == "PUT":
            self._raise_if("signature-write", endpoint)
            assert payload is not None
            self.signatures = json.loads(base64.b64decode(payload["content"]))
            return {}
        if rest.startswith("issues/comments/"):
            comment_id = int(rest.rsplit("/", 1)[1])
            owner = next(c for c in self.comments.values() if any(x["id"] == comment_id for x in c))
            comment = next(c for c in owner if c["id"] == comment_id)
            if method == "PATCH":
                self._raise_if("PATCH", endpoint)
                assert payload is not None
                comment["body"] = payload["body"]
                return comment
            if method == "DELETE":
                self._raise_if("DELETE", endpoint)
                owner.remove(comment)
                return None
        if rest.startswith("issues/") and rest.endswith("/comments"):
            number = int(rest.split("/")[1])
            if method == "GET":
                return list(self.comments.setdefault(number, []))
            if method == "POST":
                self._raise_if("POST", endpoint)
                assert payload is not None
                return self.add_comment(payload["body"], pr=number)
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


def _pr_event(github: FakeGitHub, number: int = PR) -> dict[str, Any]:
    """A pull_request_target payload: the PR as it was when the event fired."""
    return {"pull_request": copy.deepcopy(github.prs[number])}


def _comment_event(
    body: str, *, login: str = "alice", on_pr: bool = True, number: int = PR
) -> dict[str, Any]:
    issue: dict[str, Any] = {"number": number}
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


def _sign(github: FakeGitHub, *logins: str) -> None:
    github.signatures[cla.SIGNATURES_KEY].extend({"name": login} for login in logins)


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


# --- the commit status -------------------------------------------------------------------


def test_missing_contributor_is_a_failure_status_and_the_run_passes(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert github.status() == {
        "state": "failure",
        "target_url": "https://github.com/memtomem/memtomem/blob/main/CLA.md",
        "description": "Missing: alice",
        "context": f"CLA / PR #{PR}",
    }


def test_signing_turns_the_status_green_on_the_pull_request_head(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert github.status()["state"] == "failure"

    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE)) == 0

    assert github.status()["state"] == "success"
    assert github.status()["description"] == "All contributors have signed the CLA"
    assert [sha for sha, _ in github.statuses] == [HEAD]


def test_long_missing_list_is_truncated_to_the_api_limit_in_code_points(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    names = [f"기여자{index:02d}" for index in range(40)]
    github.ranges[(BASE, HEAD)] = names

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    description = github.status()["description"]
    assert len(description) == cla.STATUS_DESCRIPTION_LIMIT == 140
    assert description.endswith("…")
    assert description.startswith("Missing: alice, 기여자00, 기여자01")


def test_description_at_the_limit_is_not_truncated() -> None:
    name = "x" * (140 - len("Missing: "))
    assert cla.status_description([name]) == f"Missing: {name}"


# --- contributors come from immutable SHAs -----------------------------------------------


def test_contributors_are_read_from_compare_across_every_page(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.ranges[(BASE, HEAD)] = ["alice"] * 230 + ["zed"]
    _sign(github, "alice")

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    compare_reads = [read for read in github.reads if read.startswith("compare/")]
    assert compare_reads == [f"compare/{BASE}...{HEAD}"] * 3
    assert github.status()["description"] == "Missing: zed"


def test_check_run_uses_the_event_snapshot_even_if_the_branch_moved(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    event = _pr_event(github)
    moved = "m" * 40
    github.prs[PR]["head"]["sha"] = moved
    github.ranges[(BASE, moved)] = ["alice", "mallory"]

    assert _run(monkeypatch, tmp_path, event) == 0

    assert f"pulls/{PR}" not in github.reads
    assert [sha for sha, _ in github.statuses] == [HEAD]
    assert github.status()["description"] == "Missing: alice"


def test_sign_run_reads_the_pull_request_once_for_both_shas(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = "c" * 40
    github.prs[PR]["head"]["sha"] = current
    github.ranges[(BASE, current)] = ["alice"]

    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE)) == 0

    assert github.reads.count(f"pulls/{PR}") == 1
    assert [read for read in github.reads if read.startswith("compare/")] == [
        f"compare/{BASE}...{current}"
    ]
    assert github.status(sha=current)["state"] == "success"


def test_retargeted_base_brings_an_unsigned_contributor_back(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # History O—U—H: U's commit is by unsigned "ulrich".
    old_base, u_commit = "o" * 40, "u" * 40
    github.prs[PR]["base"]["sha"] = u_commit
    github.ranges[(u_commit, HEAD)] = ["alice"]
    github.ranges[(old_base, HEAD)] = ["ulrich", "alice"]
    _sign(github, "alice")

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert github.status()["state"] == "success"

    github.prs[PR]["base"]["sha"] = old_base  # the `edited` event after a retarget
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert github.status() | {"target_url": None} == {
        "state": "failure",
        "target_url": None,
        "description": "Missing: ulrich",
        "context": f"CLA / PR #{PR}",
    }


def test_two_pull_requests_on_one_head_keep_separate_statuses(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    other = PR + 1
    github.prs[other] = _pr(other, "bob")
    github.ranges[(BASE, HEAD)] = []  # only the PR authors differ
    _sign(github, "alice")

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert _run(monkeypatch, tmp_path, _pr_event(github, other)) == 0

    # Both land on the same commit, so each PR's page lists both; neither overwrote the other.
    assert sorted(github.statuses) == [(HEAD, f"CLA / PR #{PR}"), (HEAD, f"CLA / PR #{other}")]
    assert github.status(PR)["state"] == "success"
    assert github.status(other)["state"] == "failure"


# --- the status comment ------------------------------------------------------------------


def test_first_missing_check_posts_one_status_comment(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert github.comment_writes() == [("POST", f"issues/{PR}/comments")]
    (body,) = github.status_bodies()
    assert cla.MISSING_MARKER in body
    assert cla.STATUS_MARKER in body
    assert "Missing signature(s): `alice`" in body
    assert f"> {SIGNATURE}" in body


def test_identical_status_comment_is_not_rewritten(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    github.writes.clear()

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert github.comment_writes() == []
    assert len(github.status_bodies()) == 1


def test_signing_rewrites_the_comment_to_the_same_signed_body_a_check_writes(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    (missing,) = github.comments[PR]
    github.writes.clear()

    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE)) == 0

    assert github.writes == [
        ("PUT", "contents/signatures/v1/cla.json"),
        ("POST", f"statuses/{HEAD}"),
        ("PATCH", f"issues/comments/{missing['id']}"),
    ]
    (signed_by_comment,) = github.status_bodies()
    assert signed_by_comment == f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}"

    github.writes.clear()
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert github.comment_writes() == []


def test_new_unsigned_contributor_turns_signed_back_into_missing(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sign(github, "alice")
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert github.status_bodies()[0].startswith(cla.SIGNED_MARKER)

    github.commit_authors.append("bob")
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    (body,) = github.status_bodies()
    assert "Missing signature(s): `bob`" in body
    assert cla.SIGNED_MARKER not in body
    assert github.status()["description"] == "Missing: bob"


def test_missing_list_shrinks_when_one_of_two_contributors_signs(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.commit_authors.append("bob")
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert "Missing signature(s): `alice`, `bob`" in github.status_bodies()[0]

    assert _run(monkeypatch, tmp_path, _comment_event(SIGNATURE, login="bob")) == 0

    (body,) = github.status_bodies()
    assert "Missing signature(s): `alice`\n" in body
    assert github.status()["description"] == "Missing: alice"


def test_duplicate_status_comments_collapse_to_the_oldest(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    oldest = github.add_comment(f"stale\n\n{cla.STATUS_MARKER}")
    newer = github.add_comment(f"also stale\n\n{cla.STATUS_MARKER}")

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert github.comment_writes() == [
        ("PATCH", f"issues/comments/{oldest['id']}"),
        ("DELETE", f"issues/comments/{newer['id']}"),
    ]
    assert [c["id"] for c in github.comments[PR]] == [oldest["id"]]


def test_legacy_comments_without_the_status_marker_are_adopted(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    legacy_signed = github.add_comment(
        "All contributors have signed the CLA. ✍️ ✅", login="cla-assistant[bot]"
    )
    legacy_missing = github.add_comment(
        f"Thank you! {cla.MISSING_MARKER} the CLA.", login="github-actions[bot]"
    )

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert github.comment_writes() == [
        ("PATCH", f"issues/comments/{legacy_signed['id']}"),
        ("DELETE", f"issues/comments/{legacy_missing['id']}"),
    ]
    assert cla.STATUS_MARKER in legacy_signed["body"]


def test_comments_by_people_are_never_touched(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The old PAT path posted as the maintainer account; a person quoting the bot looks alike.
    human = github.add_comment(f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}", login="memtomem")

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert human in github.comments[PR]
    assert human["body"] == f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}"
    assert github.comment_writes() == [("POST", f"issues/{PR}/comments")]


# --- failure boundaries ------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["POST", "PATCH", "DELETE"])
@pytest.mark.parametrize("signed", [False, True])
def test_comment_failure_warns_and_still_writes_the_status(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failing: str,
    signed: bool,
) -> None:
    if signed:
        _sign(github, "alice")
    if failing != "POST":
        github.add_comment(f"old\n\n{cla.STATUS_MARKER}")
        github.add_comment(f"older duplicate\n\n{cla.STATUS_MARKER}")
    github.fail.add(failing)

    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0

    assert failing in {method for method, _ in github.writes}
    assert github.status()["state"] == ("success" if signed else "failure")
    err = capsys.readouterr().err
    assert "Warning: unable to update the CLA status comment" in err
    assert "HTTP 403" in err


@pytest.mark.parametrize("mode", ["check", "sign"])
def test_rejected_status_write_fails_the_run_and_leaves_the_earlier_status(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    earlier = dict(github.status())
    github.fail.add("status")

    event = _pr_event(github) if mode == "check" else _comment_event(SIGNATURE)
    assert _run(monkeypatch, tmp_path, event) == 1

    assert github.status() == earlier
    assert "Warning: unable to write the CLA commit status" in capsys.readouterr().err
    # The comment still reflects the new evaluation.
    expected_marker = cla.SIGNED_MARKER if mode == "sign" else cla.MISSING_MARKER
    assert expected_marker in github.status_bodies()[0]


@pytest.mark.parametrize(
    ("mode", "failing"),
    [
        ("check", "compare"),
        ("check", "signature-read"),
        ("recheck", "compare"),
        ("recheck", "signature-read"),
        ("sign", "compare"),
        ("sign", "signature-read"),
        ("sign", "signature-reread"),
        ("sign", "signature-write"),
    ],
)
def test_evaluation_failure_replaces_an_earlier_success_with_error(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    failing: str,
) -> None:
    _sign(github, "alice")
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert github.status()["state"] == "success"
    # A second contributor signs on a PR whose earlier result was green, as after a retarget.
    github.commit_authors.append("bob")
    github.signature_reads = 0
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_RUN_ID", "4242")
    github.fail.add(failing)

    events = {
        "check": _pr_event(github),
        "recheck": _comment_event("recheck"),
        "sign": _comment_event(SIGNATURE, login="bob"),
    }
    with pytest.raises(cla.GitHubApiError):
        _run(monkeypatch, tmp_path, events[mode])

    assert github.status() == {
        "state": "error",
        "target_url": f"https://github.example/{REPO}/actions/runs/4242",
        "description": "The CLA check failed; see the workflow run",
        "context": f"CLA / PR #{PR}",
    }


@pytest.mark.parametrize("mode", ["check", "recheck"])
def test_malformed_signature_store_replaces_an_earlier_success_with_error(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    _sign(github, "alice")
    assert _run(monkeypatch, tmp_path, _pr_event(github)) == 0
    assert github.status()["state"] == "success"
    github.signatures = {cla.SIGNATURES_KEY: None}

    event = _pr_event(github) if mode == "check" else _comment_event("recheck")
    with pytest.raises(TypeError):
        _run(monkeypatch, tmp_path, event)

    assert github.status()["state"] == "error"


def test_evaluation_failure_still_raises_when_the_error_status_cannot_be_written(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    github.fail.update({"compare", "status"})

    with pytest.raises(cla.GitHubApiError) as raised:
        _run(monkeypatch, tmp_path, _pr_event(github))

    assert "compare/" in raised.value.command[-1]
    assert github.statuses == {}
    assert "Warning: unable to write the CLA commit status" in capsys.readouterr().err


def test_signature_write_failure_records_nothing_and_writes_no_success(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.fail.add("signature-write")

    with pytest.raises(cla.GitHubApiError):
        _run(monkeypatch, tmp_path, _comment_event(SIGNATURE))

    assert _signed_logins(github) == set()
    assert github.status()["state"] == "error"
    assert github.comments[PR] == []


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
    _sign(github, "alice")

    assert _run(monkeypatch, tmp_path, _comment_event("recheck")) == 0

    assert ("PUT", "contents/signatures/v1/cla.json") not in github.writes
    assert github.status()["state"] == "success"
    assert github.status_bodies() == [f"{cla.SIGNED_MARKER}\n\n{cla.STATUS_MARKER}"]


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
    # `statuses: write` lets both jobs write the `CLA / PR #<n>` result.
    assert jobs["CLAAssistant"]["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
        "statuses": "write",
    }
    assert jobs["CLASign"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "statuses": "write",
    }


def test_workflow_reruns_the_check_when_the_base_is_retargeted() -> None:
    # PyYAML reads the bare `on:` key as the boolean True.
    triggers = _workflow()[True]
    assert triggers["pull_request_target"]["types"] == [
        "opened",
        "reopened",
        "synchronize",
        "edited",
    ]
    assert triggers["issue_comment"]["types"] == ["created"]


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
