"""
Contract-first Pydantic schemas for the incident RCA copilot.

Every LangGraph node reads and writes these models. A node that cannot
populate a required field with real, resolvable evidence must fail closed
(raise / route to "insufficient evidence") rather than inventing a value.
"""

from __future__ import annotations

from datetime import datetime, UTC
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

class IncidentEvent(BaseModel):
    """A single normalized event from any ingested source."""

    source: Literal["jira", "cicd", "grafana", "splunk", "git", "test_results"]
    raw_id: str
    timestamp: datetime
    service: str | None = None
    payload: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Triage
# --------------------------------------------------------------------------

class Severity(str, Enum):
    SEV1 = "sev1"  # full outage / data loss risk
    SEV2 = "sev2"  # major degradation
    SEV3 = "sev3"  # partial / limited-blast-radius issue
    SEV4 = "sev4"  # minor / cosmetic


class TriageResult(BaseModel):
    severity: Severity
    affected_services: list[str]
    confidence: float = Field(ge=0, le=1)
    rationale: str


# --------------------------------------------------------------------------
# Evidence & specialist findings
# --------------------------------------------------------------------------

class Citation(BaseModel):
    """A pointer back to real, checkable evidence. Never free text alone."""

    source_type: Literal[
        "log", "code_diff", "test", "metric", "past_incident", "runbook"
    ]
    reference: str  # e.g. log line id, commit sha, incident id, doc anchor
    excerpt: str    # short, must be traceable back to the raw evidence


class SpecialistFinding(BaseModel):
    agent: Literal["log_analyst", "code_diff", "test_failure", "metrics"]
    summary: str
    citations: list[Citation]
    confidence: float = Field(ge=0, le=1)

    def is_grounded(self) -> bool:
        """A finding with no citations cannot be trusted downstream."""
        return len(self.citations) > 0


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------

class RootCauseHypothesis(BaseModel):
    statement: str
    supporting_findings: list[SpecialistFinding]
    citations: list[Citation]
    confidence: float = Field(ge=0, le=1)
    alternative_hypotheses: list[str] = Field(default_factory=list)


class RiskScore(BaseModel):
    score: int = Field(ge=0, le=100)
    factors: list[str]


class RollbackRecommendation(BaseModel):
    recommended: bool
    target_ref: str | None = None  # commit / deploy id to roll back to
    justification: str
    requires_human_approval: bool = True


# --------------------------------------------------------------------------
# Final report
# --------------------------------------------------------------------------

class IncidentReport(BaseModel):
    incident_id: str
    triage: TriageResult
    hypothesis: RootCauseHypothesis
    risk: RiskScore
    rollback: RollbackRecommendation
    human_approved: bool = False
    approver: str | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def requires_gate(self) -> bool:
        """Mirrors governance rule: severity or rollback always needs a human."""
        return (
            self.rollback.recommended
            or self.triage.severity in (Severity.SEV1, Severity.SEV2)
        )