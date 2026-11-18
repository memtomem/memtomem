"""The linked pull request notice (``.github/linked-pr-notice.py``) and its workflow.

The script runs from ``issue_comment`` on the default branch's copy, so a regression only
shows on the next outside comment. These tests pin when an issue gets a notice (decided from
all of its comments, not the one that started the run), that it names only open pull
requests (the GraphQL filter alone also returns merged ones), that an issue gets one
notice, and the permissions and narrowing the workflow grants.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / ".github" / "linked-pr-notice.py"
_WORKFLOW = _ROOT / ".github" / "workflows" / "linked-pr-notice.yml"

REPO = "memtomem/memtomem"
ISSUE = 3359
TOKEN = "github-token-sentinel"
BOT = "github-actions[bot]"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("linked_pr_notice", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["linked_pr_notice"] = module
    spec.loader.exec_module(module)
    return module


notice = _load()


def _pr(
    number: int,
    author: str | None = "maintainer",
    state: str = "OPEN",
    *,
    draft: bool = False,
    repo: str = REPO,
) -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "isDraft": draft,
        "author": {"login": author} if author else None,
        "repository": {"nameWithOwner": repo},
    }


class FakeGitHub:
    """Serves the calls the script makes and records every comment it posts."""

    def __init__(self) -> None:
        self.closing: list[dict[str, Any] | None] = [_pr(3360)]
        self.comments: list[dict[str, Any]] = []
        self.posted: list[str] = []
        self.tokens: list[str] = []
        self.graphql_fields: list[str] = []

    def gh_api(
        self,
        endpoint: str,
        *,
        token: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        fields: list[str] | None = None,
    ) -> Any:
        self.tokens.append(token)
        if endpoint == "graphql":
            assert method == "POST"
            self.graphql_fields = list(fields or [])
            nodes = {"nodes": self.closing}
            return {"data": {"repository": {"issue": {"closedByPullRequestsReferences": nodes}}}}
        path, _, query = endpoint.partition("?")
        assert path == f"repos/{REPO}/issues/{ISSUE}/comments", endpoint
        if method == "GET":
            params = dict(part.split("=") for part in query.split("&"))
            per_page, page = int(params["per_page"]), int(params["page"])
            return self.comments[(page - 1) * per_page : page * per_page]
        assert method == "POST" and payload is not None
        self.posted.append(payload["body"])
        return {}


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(notice, "gh_api", fake.gh_api)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    return fake


def _comment(
    login: str = "contributor", association: str = "NONE", user_type: str = "User"
) -> dict[str, Any]:
    return {
        "body": "I'd like to take this issue.",
        "author_association": association,
        "user": {"login": login, "type": user_type},
    }


def _run(
    github: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    on_pull_request: bool = False,
    action: str = "created",
    **comment: Any,
) -> int:
    """Comment on the issue as the REST listing would then show it, and run the script."""
    issue: dict[str, Any] = {"number": ISSUE}
    if on_pull_request:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    trigger = _comment(**comment)
    github.comments.append(trigger)
    event = {"action": action, "issue": issue, "comment": trigger}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(path))
    return notice.main()


def test_an_outside_comment_on_an_issue_with_an_open_pr_gets_a_notice(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _run(github, monkeypatch, tmp_path) == 0

    assert len(github.posted) == 1
    body = github.posted[0]
    assert body.startswith(notice.MARKER + "\n")
    assert body.splitlines()[1] == (
        "This issue already has an open pull request that will close it: #3360."
    )
    assert set(github.tokens) == {TOKEN}
    assert github.graphql_fields[github.graphql_fields.index("-F") + 1] == f"number={ISSUE}"


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_a_pr_that_is_no_longer_open_gets_no_notice(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    github.closing = [_pr(3360, state=state)]

    assert _run(github, monkeypatch, tmp_path) == 0

    assert github.posted == []


def test_only_open_prs_are_named(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.closing = [
        _pr(1, state="MERGED"),
        _pr(2),
        None,
        _pr(3, author=None, draft=True),
        _pr(4, repo="someone/fork-of-memtomem"),
    ]

    assert _run(github, monkeypatch, tmp_path) == 0

    assert github.posted[0].splitlines()[1] == (
        "This issue already has open pull requests that will close it: "
        "#2, #3 (draft), someone/fork-of-memtomem#4."
    )


def test_the_author_of_the_only_open_pr_is_not_told_about_it(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.closing = [_pr(3361, author="Contributor")]

    assert _run(github, monkeypatch, tmp_path, login="contributor") == 0

    assert github.posted == []


def test_a_pr_author_is_told_about_someone_elses_open_pr(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.closing = [_pr(3360), _pr(3361, author="contributor")]

    assert _run(github, monkeypatch, tmp_path) == 0

    assert github.posted[0].splitlines()[1].endswith("will close it: #3360, #3361.")


def test_a_run_answers_an_earlier_comment_whose_own_run_was_dropped(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The per-issue queue replaced the contributor's pending run with the PR author's,
    # and the PR author alone would get no notice.
    github.closing = [_pr(3361, author="author")]
    github.comments.append(_comment("contributor"))

    assert _run(github, monkeypatch, tmp_path, login="author") == 0

    assert len(github.posted) == 1


@pytest.mark.parametrize(
    "trigger",
    [
        {"association": "OWNER", "login": "memtomem"},
        {"association": "MEMBER", "login": "member"},
        {"association": "COLLABORATOR", "login": "collaborator"},
        {"user_type": "Bot", "login": "dependabot[bot]"},
    ],
)
def test_a_maintainers_or_bots_run_answers_an_earlier_outside_comment(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trigger: dict[str, Any]
) -> None:
    github.comments.append(_comment("contributor"))

    assert _run(github, monkeypatch, tmp_path, **trigger) == 0

    assert len(github.posted) == 1


@pytest.mark.parametrize(
    "trigger",
    [
        {"association": "OWNER", "login": "memtomem"},
        {"association": "MEMBER", "login": "member"},
        {"association": "COLLABORATOR", "login": "collaborator"},
        {"user_type": "Bot", "login": "dependabot[bot]"},
    ],
)
def test_maintainers_and_bots_alone_get_no_notice(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trigger: dict[str, Any]
) -> None:
    assert _run(github, monkeypatch, tmp_path, **trigger) == 0

    assert github.posted == []


@pytest.mark.parametrize("association", ["CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "NONE"])
def test_every_outside_association_is_answered(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, association: str
) -> None:
    assert _run(github, monkeypatch, tmp_path, association=association) == 0

    assert len(github.posted) == 1


def test_an_issue_gets_one_notice(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.comments = [{"user": {"login": BOT}, "body": f"{notice.MARKER}\nearlier notice"}]

    assert _run(github, monkeypatch, tmp_path) == 0

    assert github.posted == []


def test_a_marker_quoted_by_a_person_does_not_count_as_a_notice(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.comments = [{"user": {"login": "contributor"}, "body": notice.MARKER}]

    assert _run(github, monkeypatch, tmp_path) == 0

    assert len(github.posted) == 1


def test_a_notice_past_the_first_page_of_comments_still_counts(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An outside comment on page one, the notice on page two.
    github.comments = [_comment("contributor")]
    github.comments += [_comment(f"member{i}", "MEMBER") for i in range(99)]
    github.comments.append({"user": {"login": BOT}, "body": notice.MARKER})

    assert _run(github, monkeypatch, tmp_path) == 0

    assert github.posted == []


def test_an_outside_comment_past_the_first_page_still_counts(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github.comments = [_comment(f"member{i}", "MEMBER") for i in range(100)]

    assert _run(github, monkeypatch, tmp_path, login="memtomem", association="OWNER") == 0
    assert github.posted == []

    github.comments.insert(100, _comment("contributor"))
    assert _run(github, monkeypatch, tmp_path, login="memtomem", association="OWNER") == 0
    assert len(github.posted) == 1


@pytest.mark.parametrize("overrides", [{"on_pull_request": True}, {"action": "edited"}])
def test_no_lookup_for_events_it_does_not_answer(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, overrides: dict[str, Any]
) -> None:
    assert _run(github, monkeypatch, tmp_path, **overrides) == 0

    assert github.posted == []
    assert github.tokens == []


def test_the_notice_never_quotes_the_comment(
    github: FakeGitHub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _run(github, monkeypatch, tmp_path)

    assert "take this" not in github.posted[0]


# --- workflow -----------------------------------------------------------------------------


def _workflow() -> dict[Any, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_runs_on_new_comments_only() -> None:
    # PyYAML reads the bare `on` key as True.
    assert _workflow()[True] == {"issue_comment": {"types": ["created"]}}


def test_the_workflow_grants_only_what_the_notice_needs() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {}
    (job,) = workflow["jobs"].values()
    assert job["permissions"] == {"contents": "read", "issues": "write", "pull-requests": "read"}


def test_the_workflow_runs_the_default_branchs_script_without_credentials_left_behind() -> None:
    (job,) = _workflow()["jobs"].values()
    checkout, run = job["steps"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "${{ github.event.repository.default_branch }}",
        "persist-credentials": False,
    }
    assert run["run"] == "python3 .github/linked-pr-notice.py"
    assert run["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert "comment.body" not in _WORKFLOW.read_text(encoding="utf-8")


def test_the_workflow_skips_only_pull_request_comments() -> None:
    # Anything narrower would let a skipped run replace a pending one that needed to post.
    (job,) = _workflow()["jobs"].values()
    assert job["if"] == "${{ !github.event.issue.pull_request }}"


def test_the_workflow_serialises_runs_per_issue() -> None:
    (job,) = _workflow()["jobs"].values()
    assert job["concurrency"] == {
        "group": "linked-pr-notice-${{ github.event.issue.number }}",
        "cancel-in-progress": False,
    }
