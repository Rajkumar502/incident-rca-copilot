"""
Registers the ingestion/action tools as an MCP server, so specialist agents
call them as structured tools (least-privilege, per agent) rather than
importing the HTTP wrappers directly.

Run standalone: `python -m src.mcp_tools.server`
Real, runnable against the `mcp` SDK; not exercised live here since it needs
a running MCP client and live Jira/GitHub/Grafana/Splunk credentials, none
of which exist in this sandbox. The graph itself calls the tool modules
directly (src/mcp_tools/jira_tool.py etc.) for the offline demo path — this
server is the production wiring for when agents run behind a real MCP host.

Error envelope: every tool always returns {"ok": true, "data": ...} or
{"ok": false, "error_type": ..., "status_code": ..., "message": ...} rather
than letting exceptions propagate raw. This exists because testing this
server against a real failure (see src/ingestion/collector.py's retry
logic) revealed that the MCP protocol layer collapses ANY exception raised
inside a tool — a 503, a 404, a missing-credentials error — into the same
generic "Error executing tool X" on the client side, with no way to tell
them apart. That makes smart retry (retry a 503, don't retry a 404)
impossible from the client. Catching and classifying here, before the
exception ever crosses the protocol boundary, is what makes that possible.
"""

from __future__ import annotations

import httpx

try:
    # mcp >= 2.0 (released 2026-07-28): FastMCP was renamed to MCPServer and
    # moved to mcp.server.mcpserver. The decorator API (@mcp.tool(), .run())
    # is unchanged, so we alias it back to the name this module was written
    # against and everything below just works on either major version.
    from mcp.server.mcpserver import MCPServer as FastMCP
except ImportError:
    # mcp < 2.0
    from mcp.server.fastmcp import FastMCP

from src.mcp_tools import github_tool, jira_tool, observability_tool
from src.mcp_tools.github_tool import GitHubConfigError
from src.mcp_tools.jira_tool import JiraConfigError
from src.mcp_tools.observability_tool import ObservabilityConfigError

mcp = FastMCP("incident-rca-tools")

_CONFIG_ERRORS = (JiraConfigError, GitHubConfigError, ObservabilityConfigError)


def _classify_and_wrap(fn, *args, **kwargs) -> dict:
    """Calls fn, returns the success/error envelope described above."""
    try:
        data = fn(*args, **kwargs)
        return {"ok": True, "data": data}
    except _CONFIG_ERRORS as e:
        # missing credentials — will never succeed on retry
        return {"ok": False, "error_type": "config", "message": str(e)}
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 429:
            error_type = "rate_limited"  # retryable, with backoff
        elif status >= 500:
            error_type = "http_5xx"  # retryable — likely transient on the server side
        else:
            error_type = "http_4xx"  # not retryable — e.g. 404 (bad ticket key), 401/403 (bad creds)
        return {"ok": False, "error_type": error_type, "status_code": status, "message": str(e)}
    except (httpx.TransportError, httpx.TimeoutException) as e:
        # connection refused, DNS failure, read timeout, etc. — classic
        # transient network conditions, worth retrying
        return {"ok": False, "error_type": "network", "message": str(e)}
    except Exception as e:  # noqa: BLE001 — genuinely unexpected; surfaced,
        # not retried, since we don't know what it is or whether retrying helps
        return {"ok": False, "error_type": "unknown", "message": f"{type(e).__name__}: {e}"}


@mcp.tool()
def jira_fetch_incident(issue_key: str) -> dict:
    """Fetch a Jira incident's summary, description, and status."""
    return _classify_and_wrap(jira_tool.fetch_incident, issue_key)


@mcp.tool()
def jira_create_issue(project_key: str, summary: str, description: str = "", issue_type: str = "Bug") -> dict:
    """Create a new Jira issue. Returns its key, id, and browse URL."""
    return _classify_and_wrap(jira_tool.create_issue, project_key, summary, description, issue_type)


@mcp.tool()
def github_fetch_commit(owner: str, repo: str, sha: str) -> dict:
    """Fetch a commit's message, author, and changed files."""
    return _classify_and_wrap(github_tool.fetch_commit, owner, repo, sha)


@mcp.tool()
def github_fetch_workflow_runs(owner: str, repo: str, branch: str | None = None) -> dict:
    """Fetch recent CI/CD workflow runs for a repo/branch."""
    return _classify_and_wrap(github_tool.fetch_workflow_runs, owner, repo, branch)


@mcp.tool()
def grafana_query_alerts(service: str, since_minutes: int = 60) -> dict:
    """Fetch active Grafana alerts for a service."""
    return _classify_and_wrap(observability_tool.query_grafana_alerts, service, since_minutes)


@mcp.tool()
def splunk_query_logs(service: str, search_query: str, since_minutes: int = 60) -> dict:
    """Search Splunk logs for a service."""
    return _classify_and_wrap(observability_tool.query_splunk_logs, service, search_query, since_minutes)


if __name__ == "__main__":
    mcp.run()
