"""LangGraph state definition — the single object threaded through every node."""

from __future__ import annotations

from typing import TypedDict

from src.schemas.models import (
    IncidentEvent,
    IncidentReport,
    RiskScore,
    RollbackRecommendation,
    RootCauseHypothesis,
    SpecialistFinding,
    TriageResult,
)


class GraphState(TypedDict, total=False):
    incident_id: str
    events: list[IncidentEvent]

    triage: TriageResult
    retrieved_context: list[dict]  # similar past incidents / runbook chunks

    specialist_findings: list[SpecialistFinding]

    hypothesis: RootCauseHypothesis | None
    confidence_gate_passed: bool
    gate_reason: str | None

    risk: RiskScore | None
    rollback: RollbackRecommendation | None

    human_approved: bool
    approver: str | None

    report: IncidentReport | None

    # governance / audit
    tokens_used: int
    cost_usd: float
    decision_log: list[str]
