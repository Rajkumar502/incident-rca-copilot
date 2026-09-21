from __future__ import annotations

import httpx
import pytest

from src.mcp_tools import github_tool, observability_tool


@pytest.fixture(autouse=True)
def github_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")


def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_commit_works_without_token_unauthenticated(monkeypatch):
    """GitHub allows unauthenticated reads of public repo data (just at a
    lower rate limit) — no token should not be an error."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "sha": "abc123", "commit": {"message": "x", "author": {"name": "y"}}, "files": [],
        })

    result = github_tool.fetch_commit("acme", "checkout", "abc123", client=_mock_client(handler))
    assert result["sha"] == "abc123"
    assert "authorization" not in captured["headers"]


def test_fetch_commit_sends_bearer_token_when_configured():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json={
            "sha": "abc123", "commit": {"message": "x", "author": {"name": "y"}}, "files": [],
        })

    github_tool.fetch_commit("acme", "checkout", "abc123", client=_mock_client(handler))
    assert captured["headers"]["authorization"] == "Bearer fake-gh-token"


def test_fetch_commit_parses_files_changed():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "repos/acme/checkout/commits/abc123" in str(request.url)
        return httpx.Response(200, json={
            "sha": "abc123",
            "commit": {"message": "fix retry logic", "author": {"name": "dev1"}},
            "files": [{"filename": "src/payments/retry.py", "patch": "@@ ..."}],
        })

    result = github_tool.fetch_commit("acme", "checkout", "abc123", client=_mock_client(handler))
    assert result["sha"] == "abc123"
    assert result["files_changed"] == ["src/payments/retry.py"]


def test_fetch_workflow_runs_limits_to_ten():
    def handler(request: httpx.Request) -> httpx.Response:
        runs = [
            {"id": i, "status": "completed", "conclusion": "success",
             "head_sha": f"sha{i}", "created_at": "2026-01-01T00:00:00Z"}
            for i in range(15)
        ]
        return httpx.Response(200, json={"workflow_runs": runs})

    result = github_tool.fetch_workflow_runs("acme", "checkout", client=_mock_client(handler))
    assert len(result) == 10


@pytest.fixture(autouse=True)
def obs_env(monkeypatch):
    monkeypatch.setenv("GRAFANA_URL", "https://grafana.example.com")
    monkeypatch.setenv("GRAFANA_API_KEY", "fake-key")
    monkeypatch.setenv("SPLUNK_URL", "https://splunk.example.com")
    monkeypatch.setenv("SPLUNK_TOKEN", "fake-token")


def test_query_grafana_alerts_raises_without_config(monkeypatch):
    monkeypatch.delenv("GRAFANA_URL", raising=False)
    with pytest.raises(observability_tool.ObservabilityConfigError):
        observability_tool.query_grafana_alerts("checkout-api")


def test_query_grafana_alerts_parses_response():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"labels": {"alertname": "HighLatency", "severity": "critical"},
             "startsAt": "2026-01-01T00:00:00Z", "annotations": {"summary": "p99 spike"}}
        ])

    result = observability_tool.query_grafana_alerts("checkout-api", client=_mock_client(handler))
    assert result[0]["metric"] == "HighLatency"
    assert result[0]["severity"] == "critical"


def test_query_splunk_logs_parses_ndjson():
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            '{"result": {"message": "timeout error"}}\n'
            '{"result": {"message": "retry failed"}}\n'
        )
        return httpx.Response(200, text=body)

    result = observability_tool.query_splunk_logs(
        "checkout-api", "level=ERROR", client=_mock_client(handler)
    )
    assert len(result) == 2
    assert result[0]["message"] == "timeout error"
