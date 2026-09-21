"""
Three-tier confidence gate.

1. Per-finding gate  — ungrounded findings (no citations) never reach synthesis.
2. Synthesis gate    — hypothesis confidence cannot exceed the strongest
                        grounded finding; every claim must trace to a citation.
3. Action gate       — rollback recommendation OR sev1/sev2 always routes to
                        human approval, regardless of confidence.

This mirrors the TS repo's split between auto-merge-on-pass (safe path) and
mandatory-halt-for-review (maintenance PRs): confidence changes *what* gets
recommended, never *whether* a risky action gets a human sign-off.
"""

from __future__ import annotations

from src.graph.state import GraphState
from src.schemas.models import Severity

MIN_PUBLISHABLE_CONFIDENCE = 0.55


def run_confidence_gate(state: GraphState) -> GraphState:
    hypothesis = state.get("hypothesis")
    decision_log = state.get("decision_log", [])

    if hypothesis is None:
        state["confidence_gate_passed"] = False
        state["gate_reason"] = "no hypothesis produced"
        decision_log.append("GATE: failed — no hypothesis")
        state["decision_log"] = decision_log
        return state

    # Tier 1: strip ungrounded supporting findings defensively (belt & braces —
    # the synthesizer should already have excluded these).
    grounded = [f for f in hypothesis.supporting_findings if f.is_grounded()]
    if len(grounded) < len(hypothesis.supporting_findings):
        decision_log.append(
            f"GATE: dropped {len(hypothesis.supporting_findings) - len(grounded)} "
            "ungrounded finding(s) before evaluation"
        )

    if not grounded or not hypothesis.citations:
        state["confidence_gate_passed"] = False
        state["gate_reason"] = "hypothesis has no grounded citations"
        decision_log.append("GATE: failed — ungrounded hypothesis")
        state["decision_log"] = decision_log
        return state

    # Tier 2: hypothesis confidence cannot exceed the strongest finding's
    # confidence — prevents confidence inflation from mere agreement.
    max_finding_confidence = max(f.confidence for f in grounded)
    if hypothesis.confidence > max_finding_confidence + 1e-6:
        state["confidence_gate_passed"] = False
        state["gate_reason"] = (
            f"hypothesis confidence {hypothesis.confidence:.2f} exceeds max "
            f"grounded finding confidence {max_finding_confidence:.2f}"
        )
        decision_log.append(f"GATE: failed — {state['gate_reason']}")
        state["decision_log"] = decision_log
        return state

    if hypothesis.confidence < MIN_PUBLISHABLE_CONFIDENCE:
        state["confidence_gate_passed"] = False
        state["gate_reason"] = (
            f"confidence {hypothesis.confidence:.2f} below publishable "
            f"threshold {MIN_PUBLISHABLE_CONFIDENCE} — routing to human review"
        )
        decision_log.append(f"GATE: below threshold — {state['gate_reason']}")
        state["decision_log"] = decision_log
        return state

    state["confidence_gate_passed"] = True
    state["gate_reason"] = "passed all tiers"
    decision_log.append("GATE: passed")
    state["decision_log"] = decision_log
    return state


def requires_human_approval(state: GraphState, severity: Severity, rollback_recommended: bool) -> bool:
    """Tier 3: action gate. Always true for rollback or high severity."""
    return rollback_recommended or severity in (Severity.SEV1, Severity.SEV2)
