"""
Real ingestion: gathers evidence for a Jira incident key by calling the
MCP tool server over the actual MCP client-server protocol — not by
importing jira_tool/github_tool/observability_tool directly.

This is the production wiring path described in the architecture doc:
`analyze_incident` (src/cli.py, offline demo) reads pre-built synthetic
`IncidentEvent`s from a JSON fixture. This module instead calls out to
real systems through MCP and builds those same `IncidentEvent` objects
from live data, so the rest of the graph — triage, retrieval, specialists,
gates, approval, write-back — runs completely unchanged.

Usage:
    python -m src.ingestion.collector OPS-123

Requires JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN at minimum; GITHUB_TOKEN and
GRAFANA_URL/GRAFANA_API_KEY/SPLUNK_URL/SPLUNK_TOKEN are optional — if
unset, those tool calls are skipped and logged rather than failing the
whole collection (a missing observability integration shouldn't block
triage on the Jira ticket that IS available).
"""

from __future__ import annotations

import asyncio
import re

from mcp.client.client import Client

from src import config
from src.governance.retry import call_with_retry
from src.ingestion.normalizer import (
    event_from_commit,
    event_from_grafana_alert,
    event_from_jira,
    event_from_splunk_log,
    event_from_workflow_run,
)
from src.mcp_tools.server import mcp
from src.schemas.models import IncidentEvent

# Best-effort extraction of a deploy commit sha mentioned in a Jira
# description/summary (e.g. "deployed sha abc1234" or "commit abc1234def").
_SHA_PATTERN = re.compile(r"\b([0-9a-f]{7,40})\b")

# error_type values (set by src/mcp_tools/server.py's _classify_and_wrap)
# worth retrying — transient by nature, likely to succeed on a later
# attempt. Anything else (config, http_4xx, unknown) is NOT retried:
# retrying a missing-credentials error or a 404 for a ticket that
# doesn't exist only adds latency with zero chance of success.
_RETRYABLE_ERROR_TYPES = {"http_5xx", "network", "rate_limited"}


class ToolCallFailed(Exception):
    """Raised internally by _call_tool_safely to carry the classified
    error_type through to call_with_retry's is_retryable check. Never
    escapes _call_tool_safely itself — callers see None on final failure,
    same as before this retry logic existed."""

    def __init__(self, error_type: str, message: str):
        self.error_type = error_type
        super().__init__(f"[{error_type}] {message}")


def _is_retryable(e: Exception) -> bool:
    if isinstance(e, ToolCallFailed):
        return e.error_type in _RETRYABLE_ERROR_TYPES
    # An exception that isn't even our classified ToolCallFailed means
    # something failed before the tool's own error handling ran (e.g. the
    # MCP transport itself, or the client session) — genuinely unexpected,
    # but still plausibly transient, so one or two retries is reasonable
    # rather than failing fast on the very first hiccup.
    return True


def _parse_envelope(result) -> dict:
    """
    The mcp SDK represents a dict-returning tool's result as JSON text in
    content[0].text — every tool here always returns a dict-shaped
    envelope now (see server.py), so this is the only parsing path
    needed; no more branching on structured_content vs content for
    list-vs-dict return shapes, since the envelope unified that.
    """
    import json
    if result.content:
        return json.loads(result.content[0].text)
    return {"ok": False, "error_type": "unknown", "message": "empty tool result"}


async def _call_tool_safely(client: Client, tool_name: str, arguments: dict, log: list[str]) -> dict | list | None:
    """
    Calls an MCP tool with retry-with-backoff on transient failures, and
    returns its result as plain Python data, or None if it never
    succeeds — logging what happened either way, so one missing or
    flaky integration doesn't take down the whole collection.

    Every tool now returns an envelope — {"ok": true, "data": ...} or
    {"ok": false, "error_type": ..., ...} — set up in
    src/mcp_tools/server.py specifically so this function can tell a
    transient failure (worth retrying) from a permanent one (not) rather
    than treating every failure identically.
    """

    async def attempt() -> dict | list:
        result = await client.call_tool(tool_name, arguments)
        envelope = _parse_envelope(result)
        if not envelope.get("ok", False):
            raise ToolCallFailed(envelope.get("error_type", "unknown"), envelope.get("message", "unknown error"))
        return envelope["data"]

    def _on_retry(attempt_num: int, error: Exception, delay: float) -> None:
        log.append(
            f"INGEST: {tool_name} failed on attempt {attempt_num} "
            f"({error}), retrying in {delay:.1f}s"
        )

    outcome = await call_with_retry(attempt, is_retryable=_is_retryable, on_retry=_on_retry)

    if outcome.succeeded:
        if outcome.attempts > 1:
            log.append(f"INGEST: {tool_name} succeeded on attempt {outcome.attempts}")
        return outcome.value

    log.append(
        f"INGEST: {tool_name} failed after {outcome.attempts} attempt(s) "
        f"({outcome.last_error}), skipping"
    )
    return None


async def collect_incident_events(
    jira_issue_key: str,
    github_owner: str | None = None,
    github_repo: str | None = None,
    service: str | None = None,
) -> tuple[list[IncidentEvent], list[str]]:
    """
    Gathers real evidence for a Jira incident over the MCP protocol.
    Returns (events, ingestion_log) — the log records what succeeded,
    what was skipped, and why, so a partial collection is auditable
    rather than silently incomplete.

    `service` scopes the Grafana/Splunk queries (both need a service name
    to search against). In production this typically comes from a Jira
    custom field or the ticket's component/label — pass it explicitly if
    you have it; Grafana/Splunk collection is skipped and logged if not.
    """
    log: list[str] = []
    events: list[IncidentEvent] = []

    async with Client(mcp) as client:
        # 1. Always try Jira first — it's the anchor for everything else
        jira_data = await _call_tool_safely(
            client, "jira_fetch_incident", {"issue_key": jira_issue_key}, log
        )
        if jira_data:
            events.append(event_from_jira(jira_data, service))
            log.append(f"INGEST: fetched Jira issue {jira_issue_key}")
        else:
            log.append(f"INGEST: could not fetch Jira issue {jira_issue_key} — aborting collection")
            return events, log

        # 2. If a GitHub repo is known, try to correlate a deploy commit
        # mentioned in the ticket description
        description = (jira_data.get("description") or "") + " " + (jira_data.get("summary") or "")
        sha_match = _SHA_PATTERN.search(description)
        if github_owner and github_repo and sha_match:
            commit_sha = sha_match.group(1)
            commit_data = await _call_tool_safely(
                client, "github_fetch_commit",
                {"owner": github_owner, "repo": github_repo, "sha": commit_sha}, log,
            )
            if commit_data:
                events.append(event_from_commit(commit_data, service))
                log.append(f"INGEST: correlated commit {commit_sha} from ticket description")

            runs_data = await _call_tool_safely(
                client, "github_fetch_workflow_runs",
                {"owner": github_owner, "repo": github_repo}, log,
            )
            if runs_data:
                for run in runs_data[:3]:
                    events.append(event_from_workflow_run(run, service))
        elif github_owner and github_repo:
            log.append("INGEST: no commit sha found in ticket text, skipping GitHub correlation")

        # 3. Grafana alerts for the affected service, if we know it and have config
        if not service:
            log.append("INGEST: no service name provided, skipping Grafana/Splunk collection")
        elif config.get("GRAFANA_URL"):
            alerts = await _call_tool_safely(
                client, "grafana_query_alerts", {"service": service}, log
            )
            if alerts:
                for alert in alerts[:5]:
                    events.append(event_from_grafana_alert(alert, service))

        # 4. Splunk logs for the affected service, if we know it and have config
        if service and config.get("SPLUNK_URL"):
            logs = await _call_tool_safely(
                client, "splunk_query_logs",
                {"service": service, "search_query": "level=ERROR"}, log,
            )
            if logs:
                for entry in logs[:10]:
                    events.append(event_from_splunk_log(entry, service))

    return events, log


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m src.ingestion.collector <JIRA_ISSUE_KEY> [service] [github_owner] [github_repo]")
        sys.exit(1)

    jira_key = sys.argv[1]
    service = sys.argv[2] if len(sys.argv) > 2 else None
    owner = sys.argv[3] if len(sys.argv) > 3 else None
    repo = sys.argv[4] if len(sys.argv) > 4 else None

    events, log = asyncio.run(collect_incident_events(jira_key, owner, repo, service))

    print(f"\n=== Ingestion result for {jira_key} ===")
    for line in log:
        print(f"  {line}")
    print(f"\nCollected {len(events)} event(s):")
    for e in events:
        print(f"  [{e.source}] {e.raw_id}: {e.payload}")

    if events:
        print(
            "\nTo analyze these events, feed them into build_graph the same way "
            "src/cli.py does for synthetic incidents — see analyze_real_incident() "
            "in this module for a ready-made example."
        )


async def analyze_real_incident(
    jira_issue_key: str,
    github_owner: str | None = None,
    github_repo: str | None = None,
    service: str | None = None,
    interactive: bool = False,
):
    """
    End-to-end: collect real evidence over MCP, then run it through the
    exact same graph used for synthetic incidents. This is the function
    a real ingestion trigger (webhook handler, poller, scheduled job)
    would call in production.
    """
    from src.governance.cost_meter import CostCircuitBreakerTripped, CostMeter
    from src.graph.build_graph import build_graph
    from src.graph.nodes.human_approval import auto_reject_stub, cli_prompt_approver
    from src.llm.client import select_llm_client
    from src.rag.history import DEFAULT_HISTORY_PATH
    from src.rag.retriever import build_store

    events, ingest_log = await collect_incident_events(jira_issue_key, github_owner, github_repo, service)
    if not events:
        print(f"No evidence could be collected for {jira_issue_key}; aborting analysis.")
        return None

    llm = select_llm_client()
    # No synthetic fixtures here — real incidents shouldn't retrieve demo
    # data as if it were institutional memory. Seeded only from real
    # published history (src/rag/history.py), which starts empty and
    # grows by one entry every time a report from THIS function gets
    # published — so retrieval quality improves the more real incidents
    # this system actually analyzes.
    store = build_store(history_path=DEFAULT_HISTORY_PATH)
    cost_meter = CostMeter(incident_id=jira_issue_key)
    approver = cli_prompt_approver if interactive else auto_reject_stub
    graph = build_graph(llm, store, cost_meter, approver=approver)

    try:
        final_state = graph.invoke({
            "incident_id": jira_issue_key,
            "events": events,
            "decision_log": list(ingest_log),
        })
    except CostCircuitBreakerTripped as e:
        print(f"CIRCUIT BREAKER TRIPPED: {e}")
        return None

    return final_state


if __name__ == "__main__":
    main()
