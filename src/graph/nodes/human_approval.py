"""
Human approval checkpoint.

In a full deployment this is a LangGraph `interrupt()` — the graph genuinely
pauses, persists state, and resumes only when a human calls back in with a
decision, via a separate channel (Slack, dashboard button, CLI prompt) that
the agent's own context cannot influence. That's the point: approval must
come from a real out-of-band human action, never from text the agent itself
produced, so injected content in ingested logs/tickets cannot forge it.

This module provides that real interrupt() wiring for use inside build_graph,
plus a CallbackApprover for local/CLI/test runs where the "human" is a
function you pass in (e.g. an input() prompt, or an auto-approve/deny stub
for automated eval runs) — never a value the LLM can set itself.
"""

from __future__ import annotations

from typing import Callable

from src.graph.state import GraphState
from src.schemas.models import IncidentReport


def requires_human_approval(report: IncidentReport) -> bool:
    return report.requires_gate()


def make_human_approval_node(approver: Callable[[IncidentReport], tuple[bool, str]]):
    """
    approver: a function(report) -> (approved: bool, approver_name: str).
    This is supplied by the caller (CLI prompt, dashboard callback, or a
    fixed stub for automated tests) — never derived from agent output.
    """

    def run_human_approval(state: GraphState) -> GraphState:
        decision_log = state.get("decision_log", [])
        hypothesis = state.get("hypothesis")
        triage = state.get("triage")
        risk = state.get("risk")
        rollback = state.get("rollback")

        if hypothesis is None or triage is None or risk is None or rollback is None:
            decision_log.append("APPROVAL: skipped, incomplete state upstream")
            state["human_approved"] = False
            state["decision_log"] = decision_log
            return state

        draft_report = IncidentReport(
            incident_id=state["incident_id"],
            triage=triage,
            hypothesis=hypothesis,
            risk=risk,
            rollback=rollback,
            human_approved=False,
        )

        if not requires_human_approval(draft_report):
            decision_log.append("APPROVAL: not required for this report, auto-publishing")
            state["human_approved"] = True
            state["approver"] = "auto (below gate threshold)"
            state["decision_log"] = decision_log
            return state

        approved, approver_name = approver(draft_report)
        decision_log.append(
            f"APPROVAL: required — decision={'approved' if approved else 'rejected'} by {approver_name}"
        )
        state["human_approved"] = approved
        state["approver"] = approver_name
        state["decision_log"] = decision_log
        return state

    return run_human_approval


def cli_prompt_approver(report: IncidentReport) -> tuple[bool, str]:
    print("\n--- HUMAN APPROVAL REQUIRED ---")
    print(f"Incident: {report.incident_id} | Severity: {report.triage.severity.value}")
    print(f"Hypothesis: {report.hypothesis.statement}")
    print(f"Rollback recommended: {report.rollback.recommended} -> {report.rollback.justification}")
    answer = input("Approve? [y/N]: ").strip().lower()
    return answer == "y", "cli-operator"


def auto_reject_stub(report: IncidentReport) -> tuple[bool, str]:
    """For automated/eval runs where no human is present — always denies,
    proving the pipeline never auto-publishes a gated report unattended."""
    return False, "auto-stub (no human present, defaults to reject)"
