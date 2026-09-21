"""
Full integration test: proves the "not mocked" production path actually
works end to end — real MCP client/server protocol round trip, real
ingestion/normalization code, real graph — with only the HTTP transport
layer mocked (since this sandbox has no live Jira/GitHub instance).

This is the strongest verification available without live credentials:
every layer above the network call is exercised for real, including the
MCP wire protocol itself (JSON-RPC tool discovery + invocation), not
just direct Python function calls.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.ingestion.collector import collect_incident_events


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")
    monkeypatch.setenv("GITHUB_TOKEN", "fake-gh-token")


def _install_mock_transport(monkeypatch):
    """Patches httpx.Client's default construction so every call inside
    jira_tool/github_tool — even the ones the MCP server invokes on our
    behalf, with no client injected — goes through a mock transport
    instead of a real socket."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "issue/OPS-1" in url and request.method == "GET":
            return httpx.Response(200, json={
                "key": "OPS-1",
                "fields": {
                    "summary": "Checkout API returning 500s after deploy",
                    "status": {"name": "In Progress"},
                    "description": "Started right after commit a1b2c3d deployed to prod.",
                },
            })
        if "commits/a1b2c3d" in url:
            return httpx.Response(200, json={
                "sha": "a1b2c3d",
                "commit": {"message": "refactor: sync retry path", "author": {"name": "dev1"}},
                "files": [{"filename": "src/payments/retry.py", "patch": "@@ ..."}],
            })
        if "actions/runs" in url:
            return httpx.Response(200, json={"workflow_runs": [
                {"id": 555, "status": "completed", "conclusion": "success",
                 "head_sha": "a1b2c3d", "created_at": "2026-01-01T00:00:00Z"}
            ]})
        return httpx.Response(404, json={"error": "not found in mock"})

    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


def test_full_ingestion_over_real_mcp_protocol(monkeypatch):
    """
    Real MCP client connects to the real MCP server, which calls the real
    jira_tool/github_tool functions, which make real httpx calls (mocked
    at the transport layer only) — proving the whole chain from MCP
    protocol down to tool execution works, not just isolated units.
    """
    _install_mock_transport(monkeypatch)

    events, log = asyncio.run(
        collect_incident_events("OPS-1", github_owner="acme", github_repo="checkout", service="checkout-api")
    )

    # a Jira event and at least one GitHub event should have been collected
    sources = [e.source for e in events]
    assert "jira" in sources
    assert "git" in sources or "cicd" in sources

    jira_event = next(e for e in events if e.source == "jira")
    assert jira_event.raw_id == "OPS-1"
    assert "500s" in jira_event.payload["summary"]

    # the commit sha was correctly extracted from free-text ticket
    # description and used to correlate a real GitHub call
    git_events = [e for e in events if e.source == "git"]
    assert any(e.raw_id == "a1b2c3d" for e in git_events)

    assert any("fetched Jira issue OPS-1" in line for line in log)


def test_ingestion_degrades_gracefully_on_missing_ticket(monkeypatch):
    """A Jira 404 should abort collection cleanly, not crash."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    original_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    events, log = asyncio.run(collect_incident_events("OPS-DOES-NOT-EXIST"))
    assert events == []
    assert any("aborting collection" in line for line in log)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
