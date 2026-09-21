"""
GitHub tool — fetch commit metadata, diffs, and CI/CD run status.

Feeds the CodeDiffAgent's evidence. Uses the GitHub REST API; a token in
GITHUB_TOKEN is optional (see _headers() below — public repo data works
unauthenticated). This is the one integration in the project that has
actually been exercised against the real live API, not just mocked HTTP:
fetch_commit() correctly fetched a real commit from octocat/Hello-World
with no token and no mocking. A second real call also surfaced GitHub's
actual (403, not 429) rate-limit behavior — see ARCHITECTURE.md Section N
for what that revealed about the retry logic. Unit tests
(tests/test_github_and_observability_tools.py) still cover the mocked
cases for repeatable, network-independent CI runs.
"""

from __future__ import annotations

import httpx

from src import config


class GitHubConfigError(RuntimeError):
    pass


def _headers() -> dict:
    """
    Authorization is optional: GitHub's REST API allows unauthenticated
    reads of public repository data (rate-limited to 60 requests/hour per
    IP instead of 5,000/hour with a token), so a token is only required
    when one is actually configured — this lets fetch_commit/
    fetch_workflow_runs work against public repos with zero setup, while
    still using the token automatically once GITHUB_TOKEN is set (higher
    rate limit, access to private repos).
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = config.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_commit(
    owner: str, repo: str, sha: str, client: httpx.Client | None = None
) -> dict:
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(
            f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}",
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "sha": data.get("sha"),
            "message": data.get("commit", {}).get("message"),
            "author": data.get("commit", {}).get("author", {}).get("name"),
            "files_changed": [f["filename"] for f in data.get("files", [])],
            "patch_summary": [
                {"filename": f["filename"], "patch": f.get("patch", "")[:2000]}
                for f in data.get("files", [])
            ],
        }
    finally:
        if own_client:
            client.close()


def fetch_workflow_runs(
    owner: str, repo: str, branch: str | None = None, client: httpx.Client | None = None
) -> list[dict]:
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        params = {"branch": branch} if branch else {}
        resp = client.get(
            f"https://api.github.com/repos/{owner}/{repo}/actions/runs",
            headers=_headers(),
            params=params,
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs", [])
        return [
            {
                "id": r["id"],
                "status": r["status"],
                "conclusion": r.get("conclusion"),
                "head_sha": r["head_sha"],
                "created_at": r["created_at"],
            }
            for r in runs[:10]
        ]
    finally:
        if own_client:
            client.close()
