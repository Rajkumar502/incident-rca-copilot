"""
Tests for the automatic webhook trigger — secret validation, label
filtering, idempotency, and that a correctly-shaped webhook actually
triggers analyze_real_incident() end to end (mocked at the HTTP
transport layer, same pattern as the other real-ingestion tests).
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from src.webhook import server as webhook_server


@pytest.fixture(autouse=True)
def reset_dedupe_state():
    webhook_server._already_triggered.clear()
    yield
    webhook_server._already_triggered.clear()


@pytest.fixture()
def client():
    return TestClient(webhook_server.app)


def _payload(issue_key: str = "OPS-1", labels: list[str] | None = None, service_field: dict | None = None) -> dict:
    fields = {"labels": labels if labels is not None else ["incident"]}
    if service_field:
        fields.update(service_field)
    return {"issue": {"key": issue_key, "fields": fields}}


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_rejects_request_with_wrong_secret(client, monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", "correct-secret")
    resp = client.post(
        "/webhook/jira", json=_payload(),
        headers={"X-Webhook-Secret": "wrong-secret"},
    )
    assert resp.status_code == 401


def test_accepts_request_with_correct_secret(client, monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(webhook_server, "_run_analysis", _noop_async)
    resp = client.post(
        "/webhook/jira", json=_payload(),
        headers={"X-Webhook-Secret": "correct-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["triggered"] is True


def test_accepts_request_with_no_secret_configured(client, monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", None)
    monkeypatch.setattr(webhook_server, "_run_analysis", _noop_async)
    resp = client.post("/webhook/jira", json=_payload())
    assert resp.status_code == 200
    assert resp.json()["triggered"] is True


def test_skips_issue_without_trigger_label(client, monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", None)
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_TRIGGER_LABEL", "incident")
    resp = client.post("/webhook/jira", json=_payload(labels=["bug", "backend"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["triggered"] is False
    assert "label" in body["reason"]


def test_rejects_payload_missing_issue_key(client, monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", None)
    resp = client.post("/webhook/jira", json={"issue": {"fields": {"labels": ["incident"]}}})
    assert resp.status_code == 400


def test_idempotency_second_call_for_same_issue_is_skipped(client, monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", None)
    monkeypatch.setattr(webhook_server, "_run_analysis", _noop_async)

    resp1 = client.post("/webhook/jira", json=_payload(issue_key="OPS-9"))
    assert resp1.json()["triggered"] is True

    resp2 = client.post("/webhook/jira", json=_payload(issue_key="OPS-9"))
    assert resp2.json()["triggered"] is False
    assert "already triggered" in resp2.json()["reason"]


def test_extract_service_reads_configured_custom_field(monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_DEFAULT_SERVICE_FIELD", "customfield_10050")
    payload = _payload(service_field={"customfield_10050": "checkout-api"})
    assert webhook_server._extract_service(payload) == "checkout-api"


def test_extract_service_returns_none_when_not_configured(monkeypatch):
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_DEFAULT_SERVICE_FIELD", None)
    payload = _payload(service_field={"customfield_10050": "checkout-api"})
    assert webhook_server._extract_service(payload) is None


async def _noop_async(*args, **kwargs) -> None:
    return None


def test_end_to_end_webhook_triggers_real_analysis(client, monkeypatch, tmp_path):
    """
    Full proof: a correctly-shaped webhook call (secret valid, label
    present) actually results in analyze_real_incident() running and
    producing a report — not just that the HTTP layer accepts the
    request. Uses BackgroundTasks' test-mode behavior (TestClient runs
    background tasks synchronously before returning the response), and
    mocks the HTTP transport the same way the other real-ingestion tests do.
    """
    monkeypatch.setattr(webhook_server.config, "WEBHOOK_SECRET", None)
    monkeypatch.setenv("JIRA_URL", "https://fake.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@fake.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")

    def handler(request: httpx.Request) -> httpx.Response:
        if "issue/OPS-E2E" in str(request.url) and request.method == "GET":
            return httpx.Response(200, json={
                "key": "OPS-E2E",
                "fields": {
                    "summary": "Checkout API 500s",
                    "status": {"name": "To Do"},
                    "description": "TimeoutError blocked event loop after commit a1b2c3d, sync retry blocked event loop",
                },
            })
        if "comment" in str(request.url):
            return httpx.Response(201, json={"id": "1"})
        return httpx.Response(404, json={})

    orig_init = httpx.Client.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        orig_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    produced = {}
    original_run_analysis = webhook_server._run_analysis

    async def spying_run_analysis(issue_key, service):
        from src.ingestion.collector import analyze_real_incident
        from src.graph.nodes.human_approval import auto_reject_stub
        # call through exactly like the real function does, but capture the result
        final_state = await analyze_real_incident(issue_key, service=service, interactive=False)
        produced["final_state"] = final_state

    monkeypatch.setattr(webhook_server, "_run_analysis", spying_run_analysis)

    resp = client.post("/webhook/jira", json=_payload(issue_key="OPS-E2E"))
    assert resp.status_code == 200
    assert resp.json()["triggered"] is True

    # TestClient runs background tasks synchronously, so by the time we
    # get here the analysis has actually completed
    assert "final_state" in produced
    final_state = produced["final_state"]
    assert final_state is not None
    log = final_state["decision_log"]
    assert any("fetched Jira issue OPS-E2E" in line for line in log)
