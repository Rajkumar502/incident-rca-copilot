"""Tests for RealJiraClient.write_comment(): posts a comment AND sets
Jira's own priority field from this project's internal severity — with
a failed priority update never blocking the comment that already posted."""

from __future__ import annotations

import httpx
import pytest

from src.graph.nodes.writeback import RealJiraClient
from src.schemas.models import (
    IncidentReport, RiskScore, RollbackRecommendation,
    RootCauseHypothesis, Severity, TriageResult,
)


@pytest.fixture(autouse=True)
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")


def _patch_transport(monkeypatch, handler) -> None:
    orig_init = httpx.Client.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        orig_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


def _report(severity: Severity) -> IncidentReport:
    return IncidentReport(
        incident_id="OPS-1",
        triage=TriageResult(severity=severity, affected_services=["checkout-api"], confidence=0.9, rationale="x"),
        hypothesis=RootCauseHypothesis(statement="deploy caused it", supporting_findings=[], citations=[], confidence=0.8),
        risk=RiskScore(score=80, factors=["x"]),
        rollback=RollbackRecommendation(recommended=True, justification="revert"),
        human_approved=True, approver="alice",
    )


@pytest.mark.parametrize("severity,expected_priority", [
    (Severity.SEV1, "Highest"),
    (Severity.SEV2, "High"),
    (Severity.SEV3, "Medium"),
    (Severity.SEV4, "Low"),
])
def test_write_comment_sets_matching_jira_priority(monkeypatch, severity, expected_priority):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url), request.content))
        if request.method == "POST":
            return httpx.Response(201, json={"id": "1"})
        return httpx.Response(204)

    _patch_transport(monkeypatch, handler)

    RealJiraClient().write_comment("OPS-1", _report(severity))

    methods = [c[0] for c in calls]
    assert methods == ["POST", "PUT"]
    put_body = calls[1][2]
    assert expected_priority.encode() in put_body


def test_write_comment_succeeds_even_if_priority_update_fails(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json={"id": "1"})
        return httpx.Response(400, json={"errors": {"priority": "invalid"}})

    _patch_transport(monkeypatch, handler)

    # must NOT raise — the comment already succeeded, a failed priority
    # update is a secondary effect, not a reason to fail the whole publish
    RealJiraClient().write_comment("OPS-1", _report(Severity.SEV1))
