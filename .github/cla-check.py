#!/usr/bin/env python3
"""Small CLA gate for GitHub Actions.

This replaces contributor-assistant/github-action while preserving the
existing signatures/v1/cla.json storage format on the cla-signatures branch.
It intentionally never checks out or executes pull request code.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


SIGNATURES_KEY = "signedContributors"
CLA_BOT_LOGINS = {"github-actions[bot]", "cla-assistant[bot]"}
MISSING_MARKER = "Before we can merge, please sign"
SIGNED_MARKER = "All contributors have signed the CLA."
# Every status comment carries this, so one comment per pull request is kept
# current instead of a new one posted per state. The two markers above still
# identify comments written before it existed.
STATUS_MARKER = "<!-- cla-status -->"
# The CLA result is a commit status on the pull request's head commit, so a
# signing comment (whose own run belongs to the default branch) can turn it green.
# The pull request number keeps two pull requests that share a head commit from
# overwriting each other's result; both still show on that commit.
STATUS_CONTEXT = "CLA / PR #{number}"
STATUS_DESCRIPTION_LIMIT = 140  # code points; GitHub rejects longer descriptions


class GitHubApiError(RuntimeError):
    def __init__(self, command: list[str], stderr: str) -> None:
        super().__init__(stderr.strip() or "gh api failed")
        self.command = command
        self.stderr = stderr

    @property
    def is_not_found(self) -> bool:
        return "HTTP 404" in self.stderr or "Not Found" in self.stderr

    @property
    def is_conflict(self) -> bool:
        return "HTTP 409" in self.stderr or "Conflict" in self.stderr


def env_required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def gh_api(
    endpoint: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    command = ["gh", "api", endpoint, "--method", method]
    stdin = None
    if payload is not None:
        command.extend(["--input", "-"])
        stdin = json.dumps(payload)

    env = os.environ.copy()
    if token:
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
        raise GitHubApiError(command, result.stderr)
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def gh_paginated(endpoint: str, *, token: str) -> list[Any]:
    items: list[Any] = []
    page = 1
    separator = "&" if "?" in endpoint else "?"
    while True:
        batch = gh_api(f"{endpoint}{separator}per_page=100&page={page}", token=token)
        if not isinstance(batch, list):
            raise RuntimeError(f"Expected a list response from {endpoint}")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def load_event() -> dict[str, Any]:
    with Path(env_required("GITHUB_EVENT_PATH")).open(encoding="utf-8") as handle:
        return json.load(handle)


def comma_set(value: str) -> set[str]:
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def load_signatures(repo: str, branch: str, path: str, token: str) -> tuple[dict[str, Any], str | None]:
    try:
        response = gh_api(f"repos/{repo}/contents/{path}?ref={branch}", token=token)
    except GitHubApiError as exc:
        if exc.is_not_found:
            return {SIGNATURES_KEY: []}, None
        raise

    raw_content = base64.b64decode(response["content"]).decode("utf-8")
    signatures = json.loads(raw_content) if raw_content.strip() else {SIGNATURES_KEY: []}
    signatures.setdefault(SIGNATURES_KEY, [])
    return signatures, response.get("sha")


def write_signatures(
    repo: str,
    branch: str,
    path: str,
    token: str,
    signatures: dict[str, Any],
    sha: str | None,
    message: str,
) -> None:
    raw_content = json.dumps(signatures, indent=2, ensure_ascii=False) + "\n"
    payload = {
        "message": message,
        "content": base64.b64encode(raw_content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    gh_api(f"repos/{repo}/contents/{path}", method="PUT", token=token, payload=payload)


def record_signature_with_retry(
    *,
    repo: str,
    branch: str,
    path: str,
    token: str,
    login: str,
    user_id: int,
    comment_id: int,
    created_at: str,
    repo_id: int,
    pull_request_no: int,
) -> tuple[dict[str, Any], bool]:
    for attempt in range(2):
        signatures, sha = load_signatures(repo, branch, path, token)
        changed = record_signature(
            signatures,
            login=login,
            user_id=user_id,
            comment_id=comment_id,
            created_at=created_at,
            repo_id=repo_id,
            pull_request_no=pull_request_no,
        )
        if not changed:
            return signatures, False
        try:
            write_signatures(
                repo,
                branch,
                path,
                token,
                signatures,
                sha,
                f"Add CLA signature for {login}",
            )
            return signatures, True
        except GitHubApiError as exc:
            if attempt == 0 and exc.is_conflict:
                print("Signature store changed concurrently; retrying once.")
                continue
            raise
    raise RuntimeError("unreachable")


def signed_logins(signatures: dict[str, Any]) -> set[str]:
    return {
        contributor.get("name", "").lower()
        for contributor in signatures.get(SIGNATURES_KEY, [])
        if contributor.get("name")
    }


def record_signature(
    signatures: dict[str, Any],
    *,
    login: str,
    user_id: int,
    comment_id: int,
    created_at: str,
    repo_id: int,
    pull_request_no: int,
) -> bool:
    if login.lower() in signed_logins(signatures):
        return False
    signatures[SIGNATURES_KEY].append(
        {
            "name": login,
            "id": user_id,
            "comment_id": comment_id,
            "created_at": created_at,
            "repoId": repo_id,
            "pullRequestNo": pull_request_no,
        }
    )
    return True


def pr_number_from_event(event: dict[str, Any]) -> int | None:
    if "pull_request" in event:
        return int(event["pull_request"]["number"])
    issue = event.get("issue", {})
    if issue.get("pull_request"):
        return int(issue["number"])
    return None


def comment_command(event: dict[str, Any], signature_text: str) -> str | None:
    if "comment" not in event:
        return None
    body = event["comment"].get("body", "").strip()
    # The workflow `if` already filters comments. This keeps direct script
    # invocations safe, and quoted instructions do not equal the bare signature.
    if body == signature_text:
        return "sign"
    if body.lower() == "recheck":
        return "recheck"
    return "ignore"


def compare_commits(repo: str, base_sha: str, head_sha: str, token: str) -> list[dict[str, Any]]:
    commits: list[dict[str, Any]] = []
    page = 1
    while True:
        response = gh_api(
            f"repos/{repo}/compare/{base_sha}...{head_sha}?per_page=100&page={page}",
            token=token,
        )
        batch = response.get("commits") or []
        commits.extend(batch)
        if not batch or len(commits) >= int(response.get("total_commits", 0)):
            return commits
        page += 1


def pr_contributors(repo: str, pr: dict[str, Any], allowlist: set[str], token: str) -> set[str]:
    """The PR author plus the authors of the commits between its base and head.

    Both ends are commit SHAs taken from one snapshot of the pull request, so the
    result describes exactly the head commit the status is written to, even if the
    branch moves while this runs.
    """
    contributors: set[str] = set()

    def add_user(user: dict[str, Any] | None) -> None:
        if not user:
            return
        login = user.get("login")
        if login and login.lower() not in allowlist:
            contributors.add(login)

    add_user(pr.get("user"))

    commits = compare_commits(repo, pr["base"]["sha"], pr["head"]["sha"], token)
    for commit in commits:
        add_user(commit.get("author"))

    return contributors


def cla_bot_comments(repo: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    comments = gh_paginated(f"repos/{repo}/issues/{pr_number}/comments", token=token)
    return [
        comment
        for comment in comments
        if (comment.get("user") or {}).get("login") in CLA_BOT_LOGINS
    ]


def status_comments(repo: str, pr_number: int, token: str) -> list[dict[str, Any]]:
    markers = (STATUS_MARKER, MISSING_MARKER, SIGNED_MARKER)
    return [
        comment
        for comment in cla_bot_comments(repo, pr_number, token)
        if any(marker in (comment.get("body") or "") for marker in markers)
    ]


def upsert_status_comment(repo: str, pr_number: int, token: str, body: str) -> None:
    existing = sorted(
        status_comments(repo, pr_number, token),
        key=lambda comment: (comment.get("created_at") or "", int(comment["id"])),
    )
    if not existing:
        gh_api(
            f"repos/{repo}/issues/{pr_number}/comments",
            method="POST",
            token=token,
            payload={"body": body},
        )
        return

    keep, *extras = existing
    if (keep.get("body") or "").strip() != body.strip():
        gh_api(
            f"repos/{repo}/issues/comments/{keep['id']}",
            method="PATCH",
            token=token,
            payload={"body": body},
        )
    for comment in extras:
        gh_api(
            f"repos/{repo}/issues/comments/{comment['id']}",
            method="DELETE",
            token=token,
        )


def missing_comment_body(names: str, document_url: str, signature_text: str) -> str:
    return (
        f"Thank you for your contribution! {MISSING_MARKER} "
        f"the [Contributor License Agreement]({document_url}).\n\n"
        f"Missing signature(s): {names}\n\n"
        f"To sign, comment on this pull request with the statement below. "
        f"You only need to sign once per GitHub account.\n\n"
        f"> {signature_text}\n\n"
        f"{STATUS_MARKER}"
    )


def signed_comment_body() -> str:
    return f"{SIGNED_MARKER}\n\n{STATUS_MARKER}"


def status_description(missing: list[str]) -> str:
    if not missing:
        return "All contributors have signed the CLA"
    text = "Missing: " + ", ".join(missing)
    if len(text) <= STATUS_DESCRIPTION_LIMIT:
        return text
    return text[: STATUS_DESCRIPTION_LIMIT - 1] + "\u2026"


def post_status(
    repo: str, pr_number: int, sha: str, state: str, description: str, url: str, token: str
) -> None:
    gh_api(
        f"repos/{repo}/statuses/{sha}",
        method="POST",
        token=token,
        payload={
            "state": state,
            "target_url": url,
            "description": description,
            "context": STATUS_CONTEXT.format(number=pr_number),
        },
    )


def write_commit_status(
    repo: str, pr_number: int, sha: str, missing: list[str], document_url: str, token: str
) -> None:
    state = "failure" if missing else "success"
    post_status(repo, pr_number, sha, state, status_description(missing), document_url, token)


def mark_evaluation_failed(repo: str, pr_number: int, sha: str, token: str) -> None:
    """Try to replace a possibly stale result with ``error`` before the run fails.

    Without this, a run that cannot finish evaluating (say, after a retarget
    exposed an unsigned contributor) would leave the previous ``success`` standing.
    If this write is rejected too, the previous status stays; the run still fails.
    """
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID")
    url = f"{server}/{repo}/actions/runs/{run_id}" if run_id else f"{server}/{repo}/actions"
    try:
        post_status(
            repo, pr_number, sha, "error", "The CLA check failed; see the workflow run", url, token
        )
    except GitHubApiError as exc:
        warn_api_failure("write the CLA commit status", exc)


def warn_api_failure(action: str, exc: GitHubApiError) -> None:
    print(
        f"Warning: unable to {action}: {' '.join(exc.command)}",
        file=sys.stderr,
    )
    print(exc.stderr, file=sys.stderr)


def main() -> int:
    event = load_event()
    repo = env_required("GITHUB_REPOSITORY")
    github_token = env_required("GITHUB_TOKEN")
    signatures_branch = os.environ.get("SIGNATURES_BRANCH", "cla-signatures")
    signatures_path = os.environ.get("SIGNATURES_PATH", "signatures/v1/cla.json")
    signature_text = os.environ.get(
        "CLA_SIGNATURE_TEXT",
        "I have read the CLA Document and I hereby sign the CLA",
    )
    document_url = os.environ.get(
        "CLA_DOCUMENT_URL",
        f"https://github.com/{repo}/blob/main/CLA.md",
    )
    allowlist = comma_set(os.environ.get("CLA_ALLOWLIST", ""))

    command = comment_command(event, signature_text)
    if command == "ignore":
        print("Ignoring non-CLA issue comment.")
        return 0

    pr_number = pr_number_from_event(event)
    if pr_number is None:
        print("Ignoring event without a pull request.")
        return 0

    # A pull_request_target payload is the snapshot this run was triggered for; a
    # comment has none, so take the pull request as it is now.
    pr = event.get("pull_request") or gh_api(f"repos/{repo}/pulls/{pr_number}", token=github_token)
    repo_id = int(event["repository"]["id"])
    head_sha = pr["head"]["sha"]
    # Anything that stops the evaluation should not leave an earlier result standing,
    # so every step up to the verdict is guarded as one.
    try:
        signatures, _sha = load_signatures(repo, signatures_branch, signatures_path, github_token)

        if command == "sign":
            comment = event["comment"]
            user = comment["user"]
            signatures, changed = record_signature_with_retry(
                repo=repo,
                branch=signatures_branch,
                path=signatures_path,
                token=github_token,
                login=user["login"],
                user_id=int(user["id"]),
                comment_id=int(comment["id"]),
                created_at=comment["created_at"],
                repo_id=repo_id,
                pull_request_no=pr_number,
            )
            if changed:
                print(f"Recorded CLA signature for {user['login']}.")
            else:
                print(f"CLA signature for {user['login']} is already recorded.")

        contributors = pr_contributors(repo, pr, allowlist, github_token)
        signed = signed_logins(signatures)
        missing = sorted(login for login in contributors if login.lower() not in signed)
    except Exception:
        mark_evaluation_failed(repo, pr_number, head_sha, github_token)
        raise

    if missing:
        names = ", ".join(f"`{name}`" for name in missing)
        body = missing_comment_body(names, document_url, signature_text)
        print(f"Missing CLA signature(s): {', '.join(missing)}")
    else:
        body = signed_comment_body()
        print(SIGNED_MARKER)

    # The status is the CLA result, so failing to write it fails the run; the
    # comment only explains the result, so failing to update it does not.
    exit_code = 0
    try:
        write_commit_status(repo, pr_number, head_sha, missing, document_url, github_token)
    except GitHubApiError as exc:
        warn_api_failure("write the CLA commit status", exc)
        exit_code = 1
    try:
        upsert_status_comment(repo, pr_number, github_token, body)
    except GitHubApiError as exc:
        warn_api_failure("update the CLA status comment", exc)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GitHubApiError as exc:
        print(f"GitHub API call failed: {' '.join(exc.command)}", file=sys.stderr)
        print(exc.stderr, file=sys.stderr)
        raise SystemExit(2)
    except Exception as exc:
        print(f"CLA check failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
