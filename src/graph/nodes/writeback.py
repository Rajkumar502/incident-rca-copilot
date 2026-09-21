"""
Writeback node: assembles the final IncidentReport and writes it out.

Two Jira clients share the same `write_comment(incident_id, report)` shape:
- MockJiraClient (default) — appends to a local JSON file, no credentials needed
- RealJiraClient — posts an actual comment to a real Jira issue via
  src/mcp_tools/jira_tool.py

select_jira_client() picks the real one automatically when JIRA_URL,
JIRA_EMAIL, and JIRA_API_TOKEN are all set — mirroring select_llm_client()'s
pattern for Gemini. Nothing else in the graph needs to change either way.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

from src import config
from src.graph.state import GraphState
from src.schemas.models import IncidentReport

REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)


class MockJiraClient:
    """Stand-in for the real MCP Jira write-back tool. No credentials needed."""

    def __init__(self, log_path: Path = Path("mock_jira_writeback.json")):
        self.log_path = log_path

    def write_comment(self, incident_id: str, report: IncidentReport) -> None:
        entry = {
            "incident_id": incident_id,
            "written_at": datetime.now(UTC).isoformat(),
            "comment": self._format_comment(report),
        }
        existing = []
        if self.log_path.exists():
            try:
                existing = json.loads(self.log_path.read_text())
            except json.JSONDecodeError:
                existing = []
        existing.append(entry)
        self.log_path.write_text(json.dumps(existing, indent=2))
        print(f"[MockJira] wrote comment to {incident_id} -> {self.log_path}")

    @staticmethod
    def _format_comment(report: IncidentReport) -> str:
        return (
            f"Root cause hypothesis (confidence {report.hypothesis.confidence}): "
            f"{report.hypothesis.statement}\n"
            f"Risk score: {report.risk.score}/100\n"
            f"Rollback recommended: {report.rollback.recommended} "
            f"({report.rollback.justification})\n"
            f"Human approved: {report.human_approved} by {report.approver}"
        )


class RealJiraClient:
    """
    Posts an actual comment to a real Jira issue.

    Thin wrapper around src/mcp_tools/jira_tool.write_incident_comment() so
    it exposes the same write_comment(incident_id, report) shape as
    MockJiraClient — make_writeback_node doesn't care which one it got.

    `incident_id` is treated as the Jira issue key (e.g. "OPS-123"), which
    is the natural mapping when incidents originate from real Jira tickets
    via src/ingestion/collector.py.
    """

    def write_comment(self, incident_id: str, report: IncidentReport) -> None:
        from src.mcp_tools import jira_tool
        jira_tool.write_incident_comment(incident_id, report)
        print(f"[RealJira] posted comment to {incident_id}")

        priority_name = jira_tool.SEVERITY_TO_PRIORITY.get(report.triage.severity.value)
        if priority_name:
            try:
                jira_tool.update_priority(incident_id, priority_name)
                print(f"[RealJira] set priority of {incident_id} to {priority_name}")
            except Exception as e:  # noqa: BLE001 — a failed priority update
                # (e.g. this Jira project uses a custom priority scheme
                # without a "Highest"/"High"/etc. name) should never block
                # the comment that already posted successfully; the report
                # is still published either way, just without this one
                # secondary side effect
                print(f"[RealJira] failed to set priority on {incident_id}: {e}")


def select_jira_client() -> "MockJiraClient | RealJiraClient":
    """Real Jira if JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN are all set, else the mock."""
    if config.has_jira_credentials():
        return RealJiraClient()
    return MockJiraClient()


def make_writeback_node(jira_client=None):
    jira_client = jira_client or select_jira_client()

    def run_writeback(state: GraphState) -> GraphState:
        decision_log = state.get("decision_log", [])
        hypothesis = state.get("hypothesis")
        triage = state.get("triage")
        risk = state.get("risk")
        rollback = state.get("rollback")

        if hypothesis is None or triage is None or risk is None or rollback is None:
            decision_log.append("WRITEBACK: skipped, incomplete state")
            state["decision_log"] = decision_log
            return state

        report = IncidentReport(
            incident_id=state["incident_id"],
            triage=triage,
            hypothesis=hypothesis,
            risk=risk,
            rollback=rollback,
            human_approved=state.get("human_approved", False),
            approver=state.get("approver"),
        )

        gated = report.requires_gate()
        if gated and not report.human_approved:
            decision_log.append(
                "WRITEBACK: BLOCKED — report requires human approval and was not approved. "
                "Routed to review queue instead of publishing."
            )
            state["report"] = report
            state["decision_log"] = decision_log
            _persist_report(report, blocked=True)
            return state

        jira_client.write_comment(state["incident_id"], report)
        _persist_report(report, blocked=False)
        _record_history(report)
        decision_log.append("WRITEBACK: published incident report")

        state["report"] = report
        state["decision_log"] = decision_log
        return state

    return run_writeback


def _persist_report(report: IncidentReport, blocked: bool) -> None:
    suffix = "BLOCKED" if blocked else "published"
    path = REPORTS_DIR / f"{report.incident_id}_{suffix}.json"
    path.write_text(report.model_dump_json(indent=2))


def _record_history(report: IncidentReport) -> None:
    """
    Only called for reports that actually got published — a report stuck
    in the blocked/pending-approval queue isn't a confirmed outcome and
    shouldn't seed future retrieval as if it were. Failure to record
    history is logged, never allowed to fail the writeback itself; a
    missing history entry is a smaller problem than a publish that
    silently didn't happen because of an unrelated I/O error.
    """
    try:
        from src.rag.history import record_published_incident
        record_published_incident(report)
    except Exception as e:  # noqa: BLE001
        print(f"[history] failed to record {report.incident_id} to history: {e}")
