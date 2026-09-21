"""
Tests the Jira tool's request construction and response parsing against a
mocked HTTP transport — no live Jira instance required or contacted.
"""

from __future__ import annotations

import base64
import os

import httpx
import pytest

from src.mcp_tools import jira_tool


@pytest.fixture(autouse=True)
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")


def _mock_client(handler) -> httpx.Client:
    transport = httpx.MockTransport(handler)
    return httpx.Client(transport=transport)


def test_auth_headers_are_correctly_base64_encoded():
    headers = jira_tool._auth_headers()
    expected = base64.b64encode(b"bot@example.com:fake-token").decode()
    assert headers["Authorization"] == f"Basic {expected}"


def test_fetch_incident_raises_without_config(monkeypatch):
    monkeypatch.delenv("JIRA_URL", raising=False)
    with pytest.raises(jira_tool.JiraConfigError):
        jira_tool.fetch_incident("OPS-1")


def test_fetch_incident_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/OPS-1"
        return httpx.Response(200, json={
            "key": "OPS-1",
            "fields": {
                "summary": "Checkout API elevated latency",
                "status": {"name": "In Progress"},
                "description": "p99 latency spike after deploy",
            },
        })

    client = _mock_client(handler)
    result = jira_tool.fetch_incident("OPS-1", client=client)
    assert result["key"] == "OPS-1"
    assert result["summary"] == "Checkout API elevated latency"
    assert result["status"] == "In Progress"
    assert result["description"] == "p99 latency spike after deploy"


def test_fetch_incident_converts_real_adf_description_to_plain_text():
    """
    Jira Cloud's REST API v3 returns `description` as Atlassian Document
    Format (a nested JSON tree), not a plain string — this is the actual
    shape a real Jira instance returns, caught only once this project was
    run against a real ticket (see project history: a bare .env-driven run
    against a real Jira instance crashed with KeyError: slice(...) because
    the normalizer assumed description was already a string).
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "key": "SCRUM-6",
            "fields": {
                "summary": "Checkout API returning 500s",
                "status": {"name": "To Do"},
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Started after commit "},
                                {"type": "text", "text": "a1b2c3d"},
                                {"type": "text", "text": " was deployed."},
                            ],
                        },
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Errors show TimeoutError."}],
                        },
                    ],
                },
            },
        })

    client = _mock_client(handler)
    result = jira_tool.fetch_incident("SCRUM-6", client=client)

    # must be a plain string, never the raw ADF dict
    assert isinstance(result["description"], str)
    assert "Started after commit" in result["description"]
    assert "a1b2c3d" in result["description"]
    assert "TimeoutError" in result["description"]


def test_fetch_incident_handles_missing_description():
    """A ticket with no description at all (description: null) shouldn't crash."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "key": "OPS-2",
            "fields": {"summary": "No description ticket", "status": {"name": "Open"}, "description": None},
        })

    client = _mock_client(handler)
    result = jira_tool.fetch_incident("OPS-2", client=client)
    assert result["description"] == ""


def test_write_incident_comment_posts_expected_body():
    from src.schemas.models import (
        IncidentReport, RiskScore, RollbackRecommendation,
        RootCauseHypothesis, Severity, TriageResult,
    )

    report = IncidentReport(
        incident_id="OPS-1",
        triage=TriageResult(severity=Severity.SEV2, affected_services=["checkout-api"],
                             confidence=0.8, rationale="latency spike"),
        hypothesis=RootCauseHypothesis(
            statement="deploy caused latency spike",
            supporting_findings=[], citations=[], confidence=0.7,
        ),
        risk=RiskScore(score=60, factors=["latency"]),
        rollback=RollbackRecommendation(recommended=True, justification="revert deploy"),
        human_approved=True, approver="alice",
    )

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/issue/OPS-1/comment"
        captured["body"] = request.content
        return httpx.Response(201, json={"id": "10001"})

    client = _mock_client(handler)
    jira_tool.write_incident_comment("OPS-1", report, client=client)
    assert b"deploy caused latency spike" in captured["body"]


def test_update_priority_sends_correct_put_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/rest/api/3/issue/OPS-1"
        captured["body"] = request.content
        return httpx.Response(204)

    client = _mock_client(handler)
    jira_tool.update_priority("OPS-1", "High", client=client)
    assert captured["body"] == b'{"fields":{"priority":{"name":"High"}}}'


def test_update_priority_raises_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"errors": {"priority": "Invalid priority name"}})

    client = _mock_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        jira_tool.update_priority("OPS-1", "NotARealPriority", client=client)


def test_severity_to_priority_mapping_covers_all_severities():
    from src.schemas.models import Severity
    for severity in Severity:
        assert severity.value in jira_tool.SEVERITY_TO_PRIORITY


def test_transition_issue_no_op_when_status_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"transitions": [{"id": "1", "name": "Done"}]})
        raise AssertionError("should not POST when target status not found")

    client = _mock_client(handler)
    # "Nonexistent Status" isn't in the transitions list -> should silently no-op
    jira_tool.transition_issue("OPS-1", "Nonexistent Status", client=client)
