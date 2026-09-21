"""Computes RiskScore and RollbackRecommendation from the hypothesis + triage."""

from __future__ import annotations

from src.graph.state import GraphState
from src.schemas.models import RiskScore, RollbackRecommendation, Severity

_SEVERITY_BASE_RISK = {
    Severity.SEV1: 80,
    Severity.SEV2: 60,
    Severity.SEV3: 35,
    Severity.SEV4: 10,
}

_ROLLBACK_KEYWORDS = ["deploy", "commit", "config", "flag", "sync call", "recent change"]
_NO_ROLLBACK_KEYWORDS = ["flaky", "flakiness", "rerun", "downstream job", "pool", "dns"]


def run_risk_and_rollback(state: GraphState) -> GraphState:
    hypothesis = state.get("hypothesis")
    triage = state.get("triage")
    decision_log = state.get("decision_log", [])

    if hypothesis is None or triage is None:
        decision_log.append("RISK/ROLLBACK: skipped, no hypothesis or triage available")
        state["decision_log"] = decision_log
        return state

    base = _SEVERITY_BASE_RISK[triage.severity]
    confidence_adjustment = int((hypothesis.confidence - 0.5) * 40)
    score = max(0, min(100, base + confidence_adjustment))

    factors = [f"severity={triage.severity.value}", f"hypothesis_confidence={hypothesis.confidence}"]
    if len(hypothesis.alternative_hypotheses) > 0:
        factors.append("multiple competing explanations present")

    statement_lower = hypothesis.statement.lower()
    rollback_signal = any(k in statement_lower for k in _ROLLBACK_KEYWORDS)
    no_rollback_signal = any(k in statement_lower for k in _NO_ROLLBACK_KEYWORDS)

    recommended = rollback_signal and not no_rollback_signal and hypothesis.confidence >= 0.6

    justification = (
        "hypothesis points to a recent deploy/config/commit as the likely cause "
        "with sufficient confidence to justify reverting it"
        if recommended
        else "evidence does not clearly implicate a specific recent change, or "
             "confidence is too low to justify a rollback"
    )

    risk = RiskScore(score=score, factors=factors)
    rollback = RollbackRecommendation(
        recommended=recommended,
        target_ref=None,  # would be populated from the CodeDiff finding's commit sha in a fuller build
        justification=justification,
        requires_human_approval=True,
    )

    decision_log.append(f"RISK: score={score}, ROLLBACK recommended={recommended}")
    state["risk"] = risk
    state["rollback"] = rollback
    state["decision_log"] = decision_log
    return state
