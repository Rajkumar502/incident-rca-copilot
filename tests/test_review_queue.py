"""Tests for the review queue: listing, approving (publishes + records
history), and rejecting (does neither, archives with an audit trail)."""

from __future__ import annotations

import json

import pytest

from src.graph.nodes.writeback import MockJiraClient, _persist_report
from src.rag.history import load_history
from src.review.queue import QueueEntryNotFound, approve, list_blocked, reject
from src.schemas.models import (
    Citation, IncidentReport, RiskScore, RollbackRecommendation,
    RootCauseHypothesis, Severity, SpecialistFinding, TriageResult,
)


def _make_report(incident_id: str, severity: Severity = Severity.SEV2) -> IncidentReport:
    citation = Citation(source_type="log", reference=f"log-{incident_id}", excerpt="timeout")
    finding = SpecialistFinding(agent="log_analyst", summary="timeout observed", citations=[citation], confidence=0.7)
    return IncidentReport(
        incident_id=incident_id,
        triage=TriageResult(severity=severity, affected_services=["checkout-api"], confidence=0.8, rationale="spike"),
        hypothesis=RootCauseHypothesis(
            statement="deploy caused latency spike", supporting_findings=[finding], citations=[citation], confidence=0.7,
        ),
        risk=RiskScore(score=66, factors=["latency"]),
        rollback=RollbackRecommendation(recommended=True, justification="revert deploy"),
        human_approved=False,
    )


@pytest.fixture()
def reports_dir(tmp_path, monkeypatch):
    d = tmp_path / "reports"
    d.mkdir()
    # writeback.py's REPORTS_DIR is a module-level constant used by
    # _persist_report — point it at our tmp dir for the setup step too
    monkeypatch.setattr("src.graph.nodes.writeback.REPORTS_DIR", d)
    return d


def test_list_blocked_empty_when_no_reports_dir(tmp_path):
    assert list_blocked(tmp_path / "does-not-exist") == []


def test_list_blocked_returns_only_blocked_reports(reports_dir):
    _persist_report(_make_report("OPS-1"), blocked=True)
    _persist_report(_make_report("OPS-2"), blocked=False)  # published, not blocked

    entries = list_blocked(reports_dir)
    assert len(entries) == 1
    assert entries[0].incident_id == "OPS-1"


def test_list_blocked_sorted_oldest_first(reports_dir):
    import time
    r1 = _make_report("OPS-OLD")
    _persist_report(r1, blocked=True)
    time.sleep(0.01)
    r2 = _make_report("OPS-NEW")
    _persist_report(r2, blocked=True)

    entries = list_blocked(reports_dir)
    assert [e.incident_id for e in entries] == ["OPS-OLD", "OPS-NEW"]


def test_list_blocked_skips_corrupt_files(reports_dir):
    _persist_report(_make_report("OPS-GOOD"), blocked=True)
    (reports_dir / "OPS-BAD_BLOCKED.json").write_text("{not valid json")

    entries = list_blocked(reports_dir)
    assert len(entries) == 1
    assert entries[0].incident_id == "OPS-GOOD"


def test_approve_raises_for_unknown_incident(reports_dir):
    with pytest.raises(QueueEntryNotFound):
        approve("OPS-NONEXISTENT", "alice", reports_dir)


def test_approve_publishes_records_history_and_removes_blocked_file(reports_dir, monkeypatch):
    history_path = reports_dir.parent / "history.jsonl"
    monkeypatch.setattr("src.rag.history.DEFAULT_HISTORY_PATH", history_path)
    monkeypatch.setattr("src.review.queue.record_published_incident",
                         lambda report, history_path=history_path: __import__("src.rag.history", fromlist=["record_published_incident"]).record_published_incident(report, history_path))

    jira_log = reports_dir.parent / "mockjira.json"
    monkeypatch.setattr(
        "src.review.queue.select_jira_client",
        lambda: MockJiraClient(log_path=jira_log),
    )

    _persist_report(_make_report("OPS-APPROVE"), blocked=True)
    blocked_path = reports_dir / "OPS-APPROVE_BLOCKED.json"
    assert blocked_path.exists()

    report = approve("OPS-APPROVE", "alice", reports_dir)

    assert report.human_approved is True
    assert report.approver == "alice"
    assert not blocked_path.exists()
    assert (reports_dir / "OPS-APPROVE_published.json").exists()
    assert jira_log.exists()
    posted = json.loads(jira_log.read_text())
    assert posted[0]["incident_id"] == "OPS-APPROVE"

    recorded = load_history(history_path)
    assert len(recorded) == 1
    assert recorded[0]["incident_id"] == "OPS-APPROVE"


def test_reject_does_not_publish_or_record_history_and_archives_with_reason(reports_dir, monkeypatch):
    history_path = reports_dir.parent / "history.jsonl"
    jira_log = reports_dir.parent / "mockjira.json"
    monkeypatch.setattr(
        "src.review.queue.select_jira_client",
        lambda: MockJiraClient(log_path=jira_log),
    )

    _persist_report(_make_report("OPS-REJECT"), blocked=True)
    blocked_path = reports_dir / "OPS-REJECT_BLOCKED.json"
    assert blocked_path.exists()

    report = reject("OPS-REJECT", "bob", "not enough evidence", reports_dir)

    assert report.human_approved is False
    assert report.approver == "bob"
    assert not blocked_path.exists()
    assert not jira_log.exists()  # never published
    assert load_history(history_path) == []  # never recorded

    rejected_path = reports_dir / "OPS-REJECT_REJECTED.json"
    assert rejected_path.exists()
    record = json.loads(rejected_path.read_text())
    assert record["rejected_by"] == "bob"
    assert record["reason"] == "not enough evidence"


def test_reject_raises_for_unknown_incident(reports_dir):
    with pytest.raises(QueueEntryNotFound):
        reject("OPS-NONEXISTENT", "bob", reports_dir=reports_dir)


def test_approved_incident_no_longer_appears_in_list_blocked(reports_dir, monkeypatch):
    jira_log = reports_dir.parent / "mockjira.json"
    monkeypatch.setattr(
        "src.review.queue.select_jira_client",
        lambda: MockJiraClient(log_path=jira_log),
    )
    _persist_report(_make_report("OPS-1"), blocked=True)
    _persist_report(_make_report("OPS-2"), blocked=True)

    assert len(list_blocked(reports_dir)) == 2
    approve("OPS-1", "alice", reports_dir)
    remaining = list_blocked(reports_dir)
    assert len(remaining) == 1
    assert remaining[0].incident_id == "OPS-2"
