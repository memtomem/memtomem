#!/usr/bin/env python3
"""Tell an outside commenter that an issue already has an open pull request.

Runs from ``issue_comment`` on the default branch's copy. When someone who is not a
maintainer has commented on an issue that an open pull request will close, it posts
one notice on the issue naming that pull request, so a contributor does not start a
second fix for work that is already under review. It reads a comment's body only in
comments by github-actions[bot], to find its own marker, never puts comment text into
the notice or a command, and never checks out pull request code.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


BOT = "github-actions[bot]"
# Every notice carries this, so an issue gets one notice however many people comment.
MARKER = "<!-- linked-pr-notice -->"
MAINTAINER_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

# `includeClosedPrs: false` still returns merged pull requests (measured on an issue
# whose only closing pull request was merged), so the state is checked below too.
CLOSING_PRS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 100, includeClosedPrs: false) {
        nodes {
          number
          state
          isDraft
          author { login }
          repository { nameWithOwner }
        }
      }
    }
  }
}
"""


class GitHubApiError(RuntimeError):
    pass


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def gh_api(
    endpoint: str,
    *,
    token: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    fields: list[str] | None = None,
) -> Any:
    command = ["gh", "api", endpoint, "--method", method, *(fields or [])]
    stdin = None
    if payload is not None:
        command.extend(["--input", "-"])
        stdin = json.dumps(payload)
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    result = subprocess.run(
        command,
        input=stdin,
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )
    if result.returncode != 0:
        raise GitHubApiError(result.stderr.strip() or "gh api failed")
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def gh_paginated(endpoint: str, *, token: str) -> list[Any]:
    items: list[Any] = []
    page = 1
    while True:
        batch = gh_api(f"{endpoint}?per_page=100&page={page}", token=token)
        if not isinstance(batch, list):
            raise RuntimeError(f"Expected a list response from {endpoint}")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def load_event() -> dict[str, Any]:
    with Path(env_required("GITHUB_EVENT_PATH")).open(encoding="utf-8") as handle:
        return json.load(handle)


def skip_reason(event: dict[str, Any]) -> str | None:
    """Why this event gets no notice, or ``None`` when the issue should be looked at."""
    if event.get("action") != "created":
        return "the comment was not just created"
    if (event.get("issue") or {}).get("pull_request"):
        return "the comment is on a pull request"
    return None


def open_closing_prs(repo: str, issue_number: int, token: str) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    response = gh_api(
        "graphql",
        token=token,
        method="POST",
        fields=[
            "-f",
            f"query={CLOSING_PRS_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={issue_number}",
        ],
    )
    issue = response["data"]["repository"]["issue"]
    nodes = issue["closedByPullRequestsReferences"]["nodes"]
    return [node for node in nodes if node and node.get("state") == "OPEN"]


def pr_login(pr: dict[str, Any]) -> str:
    return ((pr.get("author") or {}).get("login") or "").lower()


def pr_reference(pr: dict[str, Any], repo: str) -> str:
    owner_repo = (pr.get("repository") or {}).get("nameWithOwner") or repo
    prefix = "" if owner_repo.lower() == repo.lower() else owner_repo
    reference = f"{prefix}#{pr['number']}"
    return f"{reference} (draft)" if pr.get("isDraft") else reference


def notice_body(repo: str, prs: list[dict[str, Any]]) -> str:
    references = ", ".join(pr_reference(pr, repo) for pr in prs)
    lead = (
        f"This issue already has an open pull request that will close it: {references}."
        if len(prs) == 1
        else f"This issue already has open pull requests that will close it: {references}."
    )
    labels = f"https://github.com/{repo}/labels"
    return (
        f"{MARKER}\n"
        f"{lead}\n\n"
        "If you were planning to work on it, please look at that first, since two fixes "
        "for the same issue usually can't both be merged. Issues labelled "
        f"[`good first issue`]({labels}/good%20first%20issue) or "
        f"[`help wanted`]({labels}/help%20wanted) are a good place to find "
        "something else to pick up.\n\n"
        "_This is an automated message._\n"
    )


def already_noticed(comments: list[dict[str, Any]]) -> bool:
    return any(
        (comment.get("user") or {}).get("login") == BOT and MARKER in (comment.get("body") or "")
        for comment in comments
    )


def needs_notice(comments: list[dict[str, Any]], prs: list[dict[str, Any]]) -> bool:
    """Whether an outside commenter could still start a second fix.

    An outside commenter is neither a maintainer nor a bot. One whose own pull request is
    the only open one has nothing to be told.
    """
    authors = {pr_login(pr) for pr in prs}
    for comment in comments:
        user = comment.get("user") or {}
        if user.get("type") == "Bot":
            continue
        if comment.get("author_association") in MAINTAINER_ASSOCIATIONS:
            continue
        if authors - {(user.get("login") or "").lower()}:
            return True
    return False


def main() -> int:
    event = load_event()
    repo = env_required("GITHUB_REPOSITORY")
    token = env_required("GITHUB_TOKEN")

    reason = skip_reason(event)
    if reason:
        print(f"No notice: {reason}.")
        return 0

    # The decision reads the issue as a whole, not the comment that started this run:
    # the per-issue queue keeps only the newest pending run, and it must still answer a
    # comment whose own run was dropped.
    issue_number = int(event["issue"]["number"])
    prs = open_closing_prs(repo, issue_number, token)
    if not prs:
        print(f"No notice: issue #{issue_number} has no open pull request that closes it.")
        return 0

    endpoint = f"repos/{repo}/issues/{issue_number}/comments"
    comments = gh_paginated(endpoint, token=token)
    if already_noticed(comments):
        print(f"No notice: issue #{issue_number} already has one.")
        return 0
    if not needs_notice(comments, prs):
        print(f"No notice: no outside commenter on issue #{issue_number} needs one.")
        return 0

    gh_api(endpoint, token=token, method="POST", payload={"body": notice_body(repo, prs)})
    print(f"Posted a notice on issue #{issue_number}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
