"""
GitHub API client.

Stage 3: get_file_content — fetches a file's source at a specific commit ref.
Stage 4: get_pr_diff, get_commit_diff, create_pr, post_comment (not yet implemented).
"""

import base64
import httpx


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
