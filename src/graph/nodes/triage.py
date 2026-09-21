"""
Triage node: classifies severity and affected services from raw events.

This is a deliberately simple, transparent heuristic — keyword matching
against ingested event text — not a learned classifier. It exists to
give the rest of the pipeline (risk scoring, the human-approval gate) a
severity signal to act on, not to be a sophisticated NLP system. See the
word-boundary note below for a real bug this simplicity already caused
once.
"""

from __future__ import annotations

import re

from src.graph.state import GraphState
from src.schemas.models import Severity, TriageResult

# Single words matched with \b word boundaries, not substring containment.
# This matters concretely: earlier code matched "down" as a plain
# substring, which meant "downstream analytics job holding long
# transactions" (a real synthetic fixture describing a database
# connection-pool issue, not an outage) matched "down" inside
# "downstream" and got misclassified as sev1. Word-boundary matching
# fixes that class of false positive; phrases (2+ words) are left as
# substring checks since a false match inside a longer word isn't
# realistically possible for a multi-word phrase.
_SEV1_WORDS = ["outage", "down", "unreachable", "unavailable", "crash", "crashed"]
_SEV1_PHRASES = ["data loss", "complete failure", "total outage"]

_SEV2_WORDS = ["degraded", "timeouterror", "timeout"]
_SEV2_PHRASES = [
    "p99 latency", "error_rate", "error rate", "connection pool exhausted",
    "elevated error", "latency spike", "500 error", "5xx", "high latency",
]


def _matches_any(blob: str, words: list[str], phrases: list[str]) -> bool:
    word_pattern = r"\b(" + "|".join(re.escape(w) for w in words) + r")\b"
    if words and re.search(word_pattern, blob):
        return True
    return any(p in blob for p in phrases)


def run_triage(state: GraphState) -> GraphState:
    events = state.get("events", [])
    decision_log = state.get("decision_log", [])

    services = sorted({e.service for e in events if e.service})
    blob = " ".join(str(e.payload) for e in events).lower()

    if _matches_any(blob, _SEV1_WORDS, _SEV1_PHRASES):
        severity = Severity.SEV1
    elif _matches_any(blob, _SEV2_WORDS, _SEV2_PHRASES):
        severity = Severity.SEV2
    elif events:
        severity = Severity.SEV3
    else:
        severity = Severity.SEV4

    triage = TriageResult(
        severity=severity,
        affected_services=services,
        confidence=0.8 if events else 0.3,
        rationale=f"classified from {len(events)} ingested event(s) across {len(services)} service(s)",
    )
    decision_log.append(f"TRIAGE: {severity.value}, services={services}")

    state["triage"] = triage
    state["decision_log"] = decision_log
    return state
