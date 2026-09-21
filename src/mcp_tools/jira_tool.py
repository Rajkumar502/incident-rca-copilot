"""
Jira tool — read incidents, write structured root-cause comments back.

Ported from the TS repo's `jira-client.ts` auth pattern (Basic auth via
email + API token, Jira Cloud REST API v3) into a Python MCP-style tool.
Exposed as plain functions so they can be registered either as MCP server
tools (via the `mcp` SDK) or called directly by build_graph's writeback node.

Requires JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN in the environment. Not
exercised against a live Jira instance in this sandbox (no network access
to *.atlassian.net here) — this is real, runnable code, verified only via
the unit tests in tests/test_jira_tool.py which mock the HTTP layer.
"""

from __future__ import annotations

import base64

import httpx

from src import config

from src.schemas.models import IncidentReport


class JiraConfigError(RuntimeError):
    pass


def _auth_headers() -> dict:
    email = config.get("JIRA_EMAIL")
    token = config.get("JIRA_API_TOKEN")
    if not email or not token:
        raise JiraConfigError("JIRA_EMAIL / JIRA_API_TOKEN missing in environment")
    basic = base64.b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _base_url() -> str:
    url = config.get("JIRA_URL")
    if not url:
        raise JiraConfigError("JIRA_URL missing in environment")
    return url.rstrip("/")


def create_issue(
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Bug",
    client: httpx.Client | None = None,
) -> dict:
    """
    Creates a new Jira issue and returns its key + URL.

    `project_key` is your Jira project's short code (e.g. "OPS", "ENG") —
    visible in your Jira project's settings, or in the URL of any existing
    ticket in that project (https://yoursite.atlassian.net/browse/OPS-42
    -> project key is "OPS"). `issue_type` must match a type your project
    actually has configured (commonly "Bug", "Task", or "Incident" if your
    Jira instance has an incident-management scheme) — an invalid type
    name will cause Jira to reject the request with a 400.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.post(
            f"{_base_url()}/rest/api/3/issue",
            headers=_auth_headers(),
            json={
                "fields": {
                    "project": {"key": project_key},
                    "summary": summary,
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": description}]}
                        ],
                    } if description else None,
                    "issuetype": {"name": issue_type},
                }
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "key": data.get("key"),
            "id": data.get("id"),
            "url": f"{_base_url()}/browse/{data.get('key')}",
        }
    finally:
        if own_client:
            client.close()


def _adf_to_plain_text(node) -> str:
    """
    Jira Cloud REST API v3 returns rich-text fields (description, comments)
    as Atlassian Document Format — a nested JSON tree, not a plain string.
    This walks that tree and extracts just the text content, so callers
    always get a plain string regardless of how the ticket was authored
    (typed directly, pasted from another tool, etc.).

    Handles the legacy case too: older Jira instances / API versions can
    still return description as a plain string directly.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        parts = [_adf_to_plain_text(child) for child in node.get("content", [])]
        joined = " ".join(p for p in parts if p)
        # paragraphs/hardBreaks in ADF are block-level; a blank line between
        # them keeps multi-paragraph descriptions readable as plain text
        return joined + ("\n" if node.get("type") in ("paragraph", "hardBreak") else "")
    if isinstance(node, list):
        return " ".join(_adf_to_plain_text(child) for child in node)
    return ""


def fetch_incident(issue_key: str, client: httpx.Client | None = None) -> dict:
    """Fetch a Jira issue's summary, description, and status.

    `description` is always returned as a plain string — Jira's v3 API
    returns it as a structured Atlassian Document Format tree, which is
    converted here so downstream code never has to know about ADF."""
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(
            f"{_base_url()}/rest/api/3/issue/{issue_key}",
            headers=_auth_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "key": data.get("key"),
            "summary": data.get("fields", {}).get("summary"),
            "status": data.get("fields", {}).get("status", {}).get("name"),
            "description": _adf_to_plain_text(data.get("fields", {}).get("description")).strip(),
        }
    finally:
        if own_client:
            client.close()


def format_report_comment(report: IncidentReport) -> dict:
    """Builds a Jira Atlassian Document Format comment body from a report."""
    text = (
        f"Root cause hypothesis (confidence {report.hypothesis.confidence}): "
        f"{report.hypothesis.statement}\n\n"
        f"Risk score: {report.risk.score}/100 — {', '.join(report.risk.factors)}\n"
        f"Rollback recommended: {report.rollback.recommended} — {report.rollback.justification}\n"
        f"Human approved: {report.human_approved} by {report.approver or 'n/a'}\n"
        f"Citations: {len(report.hypothesis.citations)} grounded evidence reference(s)"
    )
    return {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
        }
    }


def write_incident_comment(
    issue_key: str, report: IncidentReport, client: httpx.Client | None = None
) -> None:
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.post(
            f"{_base_url()}/rest/api/3/issue/{issue_key}/comment",
            headers=_auth_headers(),
            json=format_report_comment(report),
        )
        resp.raise_for_status()
    finally:
        if own_client:
            client.close()


def transition_issue(
    issue_key: str, target_status_name: str, client: httpx.Client | None = None
) -> None:
    """Moves a Jira ticket to a named status (e.g. 'Done', 'In Review')."""
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        transitions_resp = client.get(
            f"{_base_url()}/rest/api/3/issue/{issue_key}/transitions",
            headers=_auth_headers(),
        )
        transitions_resp.raise_for_status()
        transitions = transitions_resp.json().get("transitions", [])
        match = next(
            (t for t in transitions if t["name"].lower() == target_status_name.lower()),
            None,
        )
        if match is None:
            return
        client.post(
            f"{_base_url()}/rest/api/3/issue/{issue_key}/transitions",
            headers=_auth_headers(),
            json={"transition": {"id": match["id"]}},
        )
    finally:
        if own_client:
            client.close()


# Maps this project's internal severity (Section triage.py) to Jira's
# default priority scheme names. Jira Cloud's default scheme is
# Highest/High/Medium/Low/Lowest — a custom scheme with different names
# will cause update_priority() to fail with a 400 from Jira (the priority
# name has to match something the project actually has configured); this
# mapping covers the out-of-the-box default only.
SEVERITY_TO_PRIORITY = {
    "sev1": "Highest",
    "sev2": "High",
    "sev3": "Medium",
    "sev4": "Low",
}


def update_priority(issue_key: str, priority_name: str, client: httpx.Client | None = None) -> None:
    """
    Sets a Jira issue's Priority field directly (a PUT on the issue's
    `fields.priority`, distinct from `transition_issue`'s workflow-status
    transitions). Raises the underlying httpx.HTTPStatusError on failure
    (e.g. a 400 if `priority_name` doesn't match a name your Jira
    project's priority scheme actually has) rather than silently no-op'ing
    — unlike transition_issue's "skip if status not found" behavior,
    since a caller setting priority explicitly wants to know if it failed.
    """
    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.put(
            f"{_base_url()}/rest/api/3/issue/{issue_key}",
            headers=_auth_headers(),
            json={"fields": {"priority": {"name": priority_name}}},
        )
        resp.raise_for_status()
    finally:
        if own_client:
            client.close()


def _cli() -> None:
    """python -m src.mcp_tools.jira_tool create <PROJECT_KEY> "<summary>" ["<description>"] [issue_type]"""
    import sys

    if len(sys.argv) < 4 or sys.argv[1] != "create":
        print('Usage: python -m src.mcp_tools.jira_tool create <PROJECT_KEY> "<summary>" ["<description>"] [issue_type]')
        print('Example: python -m src.mcp_tools.jira_tool create OPS "Checkout API 500s" "Started after deploy" Bug')
        sys.exit(1)

    project_key = sys.argv[2]
    summary = sys.argv[3]
    description = sys.argv[4] if len(sys.argv) > 4 else ""
    issue_type = sys.argv[5] if len(sys.argv) > 5 else "Bug"

    result = create_issue(project_key, summary, description, issue_type)
    print(f"Created {result['key']}: {result['url']}")


if __name__ == "__main__":
    _cli()
