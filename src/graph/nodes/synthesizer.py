"""Synthesizer node: merges grounded specialist findings into one cited
RootCauseHypothesis. Confidence is derived, never invented — it's bounded
by the strongest grounded finding, enforced again downstream by the gate."""

from __future__ import annotations

from src.graph.state import GraphState
from src.schemas.models import RootCauseHypothesis


def run_synthesizer(state: GraphState) -> GraphState:
    findings = state.get("specialist_findings", [])
    decision_log = state.get("decision_log", [])

    grounded = [f for f in findings if f.is_grounded()]
    if not grounded:
        state["hypothesis"] = None
        decision_log.append("SYNTHESIS: no grounded findings available, no hypothesis produced")
        state["decision_log"] = decision_log
        return state

    # rank by confidence, lead with the strongest signal
    grounded.sort(key=lambda f: -f.confidence)
    lead = grounded[0]
    others = grounded[1:]

    statement = lead.summary
    if others:
        statement += " Corroborating signals: " + " ".join(f.summary for f in others)

    all_citations = []
    for f in grounded:
        all_citations.extend(f.citations)

    # hypothesis confidence bounded by strongest finding — never inflated
    # purely by multiple agents agreeing (re-checked by the gate too)
    confidence = lead.confidence

    alternatives = [f.summary for f in others] if len(grounded) > 1 else []

    hypothesis = RootCauseHypothesis(
        statement=statement,
        supporting_findings=grounded,
        citations=all_citations,
        confidence=confidence,
        alternative_hypotheses=alternatives,
    )

    decision_log.append(
        f"SYNTHESIS: hypothesis confidence={confidence} from {len(grounded)} finding(s)"
    )
    state["hypothesis"] = hypothesis
    state["decision_log"] = decision_log
    return state
