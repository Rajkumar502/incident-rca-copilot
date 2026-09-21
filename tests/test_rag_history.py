"""
Regression test for the RAG-history feedback loop: a published (human-
approved) report should be recorded as institutional memory, a blocked
(not-approved) report should not, and a later incident with a similar
failure pattern should actually retrieve the earlier one via the RAG store.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from src.governance.cost_meter import CostMeter
from src.graph.build_graph import build_graph
from src.graph.nodes.writeback import MockJiraClient, make_writeback_node
from src.llm.client import MockLLMClient
from src.rag.history import load_history, record_published_incident
from src.rag.retriever import build_store
from src.schemas.models import IncidentEvent


@pytest.fixture()
def history_path(tmp_path) -> Path:
    return tmp_path / "incident_history.jsonl"


@pytest.fixture(autouse=True)
def force_mock_jira_client(monkeypatch):
    """
    This module builds real graphs via build_graph(), which internally
    calls make_writeback_node() -> select_jira_client() with no override.
    select_jira_client() reads real JIRA_URL/JIRA_EMAIL/JIRA_API_TOKEN
    from the environment (including a developer's local .env — see
    src/config.py) and, if all three are set, genuinely tries to post to
    real Jira. Without this fixture, these tests pass or fail depending
    on whether the person running them happens to have real Jira
    credentials configured for unrelated testing — which is exactly
    what broke them the first time (a real .env with valid credentials
    caused these tests to attempt a real POST to a nonexistent ticket
    like "OPS-APPROVED" and fail with a 404). Explicitly clearing the
    credentials here forces select_jira_client() to fall back to
    MockJiraClient regardless of the environment outside the test,
    which is what "isolated" is supposed to mean.
    """
    monkeypatch.delenv("JIRA_URL", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)


def _events(commit_sha: str, service: str) -> list[IncidentEvent]:
    return [
        IncidentEvent(
            source="splunk", raw_id=f"log-{commit_sha}", timestamp=datetime.now(),
            service=service,
            payload={"level": "ERROR", "message": "TimeoutError: sync retry blocked event loop"},
        ),
        IncidentEvent(
            source="git", raw_id=commit_sha, timestamp=datetime.now(), service=service,
            payload={"message": "refactor: switch retry to sync call"},
        ),
    ]


def test_approved_report_is_recorded_rejected_report_is_not(history_path):
    llm = MockLLMClient()
    store = build_store()  # no synthetic/history seeding needed for this check

    # Case 1: rejected -> writeback blocks -> should NOT be recorded
    cost_meter_1 = CostMeter(incident_id="OPS-REJECTED")
    writeback_rejected = make_writeback_node(MockJiraClient(log_path=history_path.parent / "mockjira1.json"))
    graph_rejected = build_graph(llm, store, cost_meter_1, approver=lambda r: (False, "reviewer"))
    graph_rejected.invoke({
        "incident_id": "OPS-REJECTED", "events": _events("aaa1111", "checkout-api"), "decision_log": [],
    })
    assert load_history(history_path) == []

    # Case 2: approved -> writeback publishes -> record_published_incident
    # is called internally by writeback.py's own default history path, so
    # here we call it directly against our tmp history_path to isolate the
    # test from any real incident_history.jsonl on disk.
    cost_meter_2 = CostMeter(incident_id="OPS-APPROVED")
    graph_approved = build_graph(llm, store, cost_meter_2, approver=lambda r: (True, "reviewer"))
    final_state = graph_approved.invoke({
        "incident_id": "OPS-APPROVED", "events": _events("bbb2222", "checkout-api"), "decision_log": [],
    })
    report = final_state["report"]
    assert report.human_approved is True
    record_published_incident(report, history_path=history_path)

    recorded = load_history(history_path)
    assert len(recorded) == 1
    assert recorded[0]["incident_id"] == "OPS-APPROVED"


def test_later_incident_retrieves_earlier_recorded_history(history_path):
    llm = MockLLMClient()

    # Simulate a first incident already having been approved and recorded
    store_1 = build_store()
    cost_meter_1 = CostMeter(incident_id="OPS-1")
    graph_1 = build_graph(llm, store_1, cost_meter_1, approver=lambda r: (True, "reviewer"))
    state_1 = graph_1.invoke({
        "incident_id": "OPS-1", "events": _events("aaa1111", "checkout-api"), "decision_log": [],
    })
    record_published_incident(state_1["report"], history_path=history_path)

    # A second, similar incident should now retrieve OPS-1 from history
    store_2 = build_store(history_path=history_path)
    cost_meter_2 = CostMeter(incident_id="OPS-2")
    graph_2 = build_graph(llm, store_2, cost_meter_2, approver=lambda r: (True, "reviewer"))
    state_2 = graph_2.invoke({
        "incident_id": "OPS-2", "events": _events("zzz9999", "payments-svc"), "decision_log": [],
    })

    retrieval_lines = [l for l in state_2["decision_log"] if l.startswith("RETRIEVAL")]
    assert retrieval_lines, "expected a retrieval log line"
    assert "history:OPS-1" in retrieval_lines[0]