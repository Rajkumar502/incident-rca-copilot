"""
Tests for triage severity classification. Covers the specific false-
positive bug found on real synthetic data: substring matching for "down"
incorrectly matched inside "downstream", misclassifying a database
connection-pool incident as sev1 (a full outage) when nothing was
actually down.
"""

from __future__ import annotations

from datetime import datetime

from src.graph.nodes.triage import run_triage
from src.schemas.models import IncidentEvent, Severity


def _event(payload: dict, service: str = "checkout-api") -> IncidentEvent:
    return IncidentEvent(source="splunk", raw_id="log-1", timestamp=datetime.now(), service=service, payload=payload)


def test_downstream_does_not_false_positive_as_sev1():
    """The actual bug: 'downstream analytics job...' must not match 'down'."""
    events = [_event({"message": "Downstream analytics job holding long transactions"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity != Severity.SEV1


def test_real_outage_language_still_classifies_as_sev1():
    events = [_event({"message": "Service is completely down, full outage in progress"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity == Severity.SEV1


def test_data_loss_phrase_classifies_as_sev1():
    events = [_event({"message": "Customer reports of data loss after the migration"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity == Severity.SEV1


def test_connection_pool_exhausted_classifies_as_sev2_not_sev1():
    events = [_event({"message": "connection pool exhausted, requests queuing"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity == Severity.SEV2


def test_timeout_word_classifies_as_sev2():
    events = [_event({"message": "TimeoutError: request timeout after 30s"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity == Severity.SEV2


def test_no_severity_keywords_falls_back_to_sev3():
    events = [_event({"message": "Minor UI glitch reported by one user"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity == Severity.SEV3


def test_no_events_at_all_is_sev4():
    state = run_triage({"events": [], "decision_log": []})
    assert state["triage"].severity == Severity.SEV4


def test_sev1_word_takes_priority_over_sev2_phrase_in_same_blob():
    events = [_event({"message": "Full outage — also seeing elevated error rates"})]
    state = run_triage({"events": events, "decision_log": []})
    assert state["triage"].severity == Severity.SEV1
