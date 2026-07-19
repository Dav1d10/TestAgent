"""
GitHub API client.

Stage 3: get_file_content — fetches a file's source at a specific commit ref.
Stage 4: get_push_diff, create_pr, post_commit_comment. All push-triggered — this
project has not added `pull_request` event handling to the webhook, so PR-diff and
PR-comment functions are out of scope here (see ImplementationPlan.md Stage 4 revision).
"""

import base64
from typing import List, Tuple

import httpx

_API = "https://api.github.com"


def get_file_content(repo: str, path: str, ref: str, token: str) -> str:
    """
    Fetch the content of a file from the GitHub Contents API at a specific ref.

    Args:
        repo: full repository name, e.g. "owner/repo".
        path: file path relative to the repo root, e.g. "app/utils.py".
        ref: commit SHA or branch name to fetch the file at.
        token: GitHub Personal Access Token with repo read access.

    Returns:
        The decoded file content as a UTF-8 string.

    Raises:
        httpx.HTTPStatusError: if the API returns a non-2xx status.
    """
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    with httpx.Client() as client:
        response = client.get(url, headers=headers, params={"ref": ref})
        response.raise_for_status()
    # GitHub returns content as a base64-encoded string, possibly split with newlines
    return base64.b64decode(response.json()["content"]).decode("utf-8")


def file_exists(repo: str, path: str, ref: str, token: str) -> bool:
    """Returns True if path exists in repo at ref, False on a 404. Other errors still raise."""
    try:
        get_file_content(repo=repo, path=path, ref=ref, token=token)
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return False
        raise


def get_file_content_or_empty(repo: str, path: str, ref: str, token: str) -> str:
    """Like get_file_content, but returns "" when the file does not exist (404)."""
    try:
        return get_file_content(repo=repo, path=path, ref=ref, token=token)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return ""
        raise


def get_push_diff(repo: str, base: str, head: str, token: str) -> str:
    """
    Fetch the cumulative unified diff for an entire push (which may span several
    commits) via the compare API — GitHub's single-commit diff endpoint would only
    cover the last commit and silently miss earlier ones in the same push.

    Args:
        repo: full repository name, e.g. "owner/repo".
        base: the commit SHA before the push (payload["before"]).
        head: the commit SHA after the push (payload["after"]).
        token: GitHub Personal Access Token with repo read access.

    Returns:
        The raw unified diff text for base...head.
    """
    url = f"{_API}/repos/{repo}/compare/{base}...{head}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3.diff",
    }
    with httpx.Client() as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
    return response.text


def post_commit_comment(repo: str, sha: str, body: str, token: str) -> None:
    """Posts a comment on a specific commit — used to report failed_max_attempts on a push."""
    url = f"{_API}/repos/{repo}/commits/{sha}/comments"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    with httpx.Client() as client:
        response = client.post(url, headers=headers, json={"body": body})
        response.raise_for_status()


def create_pr(
    repo: str,
    branch: str,
    title: str,
    body: str,
    files: List[Tuple[str, str]],
    token: str,
) -> str:
    """
    Creates a new branch off the repo's default branch, commits each generated test
    file to it via the Contents API, and opens a PR against the default branch.

    Args:
        repo: full repository name, e.g. "owner/repo".
        branch: name for the new branch, e.g. "test-agent/push/a1b2c3d".
        title: PR title.
        body: PR body (markdown).
        files: list of (path, content) pairs to commit to the new branch.
        token: GitHub Personal Access Token with repo write access.

    Returns:
        The URL of the created PR.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    with httpx.Client() as client:
        repo_info = client.get(f"{_API}/repos/{repo}", headers=headers)
        repo_info.raise_for_status()
        default_branch = repo_info.json()["default_branch"]

        ref_resp = client.get(
            f"{_API}/repos/{repo}/git/ref/heads/{default_branch}", headers=headers
        )
        ref_resp.raise_for_status()
        base_sha = ref_resp.json()["object"]["sha"]

        create_ref_resp = client.post(
            f"{_API}/repos/{repo}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        create_ref_resp.raise_for_status()

        for path, content in files:
            # A module's test file may already exist (a prior TestAgent PR was merged).
            # The Contents API rejects an update PUT without the current blob sha, so
            # look it up on the new branch and include it when present.
            body_json = {
                "message": f"Add or update generated test for {path}",
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": branch,
            }
            existing = client.get(
                f"{_API}/repos/{repo}/contents/{path}",
                headers=headers,
                params={"ref": branch},
            )
            if existing.status_code == 200:
                body_json["sha"] = existing.json()["sha"]
            elif existing.status_code != 404:
                existing.raise_for_status()

            put_resp = client.put(
                f"{_API}/repos/{repo}/contents/{path}",
                headers=headers,
                json=body_json,
            )
            put_resp.raise_for_status()

        pr_resp = client.post(
            f"{_API}/repos/{repo}/pulls",
            headers=headers,
            json={"title": title, "body": body, "head": branch, "base": default_branch},
        )
        pr_resp.raise_for_status()
        return pr_resp.json()["html_url"]
