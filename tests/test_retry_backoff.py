"""
Tests for retry-with-backoff on transient tool failures.

Covers: a transient 503 that succeeds on retry, a permanent 404 that
fails fast with zero retries, a missing-credentials error that also
fails fast, and full exhaustion (always-failing) still degrading
gracefully rather than raising. All exercised through the real MCP
client-server round trip (mocked only at the HTTP transport layer),
same pattern as the other real-ingestion tests.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.governance.retry import call_with_retry
from src.ingestion.collector import _call_tool_safely
from src.mcp_tools.server import mcp
from mcp.client.client import Client


@pytest.fixture(autouse=True)
def jira_env(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "fake-token")
    # fast tests: don't actually wait through real backoff delays
    monkeypatch.setattr("src.governance.retry.config.RETRY_BASE_DELAY_SECONDS", 0.01)


def _patch_transport(monkeypatch, handler) -> None:
    orig_init = httpx.Client.__init__

    def patched_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        orig_init(self, *a, **kw)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


# --------------------------------------------------------------------------
# Unit-level: call_with_retry itself
# --------------------------------------------------------------------------

def test_call_with_retry_succeeds_on_first_attempt():
    async def always_succeeds():
        return "ok"

    outcome = asyncio.run(call_with_retry(always_succeeds, is_retryable=lambda e: True, max_attempts=3, base_delay=0.01))
    assert outcome.succeeded is True
    assert outcome.attempts == 1
    assert outcome.value == "ok"


def test_call_with_retry_succeeds_after_transient_failures():
    calls = {"count": 0}

    async def fails_twice_then_succeeds():
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    outcome = asyncio.run(call_with_retry(
        fails_twice_then_succeeds, is_retryable=lambda e: True, max_attempts=5, base_delay=0.01,
    ))
    assert outcome.succeeded is True
    assert outcome.attempts == 3
    assert calls["count"] == 3


def test_call_with_retry_does_not_retry_non_retryable_failure():
    calls = {"count": 0}

    async def always_fails():
        calls["count"] += 1
        raise ValueError("permanent")

    outcome = asyncio.run(call_with_retry(
        always_fails, is_retryable=lambda e: False, max_attempts=5, base_delay=0.01,
    ))
    assert outcome.succeeded is False
    assert outcome.attempts == 1  # failed fast, no retries attempted
    assert calls["count"] == 1


def test_call_with_retry_exhausts_max_attempts_and_reports_failure():
    async def always_fails():
        raise ConnectionError("still down")

    outcome = asyncio.run(call_with_retry(
        always_fails, is_retryable=lambda e: True, max_attempts=3, base_delay=0.01,
    ))
    assert outcome.succeeded is False
    assert outcome.attempts == 3
    assert isinstance(outcome.last_error, ConnectionError)


# --------------------------------------------------------------------------
# Integration-level: _call_tool_safely through the real MCP protocol
# --------------------------------------------------------------------------

def test_transient_503_succeeds_after_retry(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={
            "key": "OPS-1",
            "fields": {"summary": "ok now", "status": {"name": "Open"}, "description": ""},
        })

    _patch_transport(monkeypatch, handler)

    async def run():
        async with Client(mcp) as client:
            log: list[str] = []
            result = await _call_tool_safely(client, "jira_fetch_incident", {"issue_key": "OPS-1"}, log)
            return result, log

    result, log = asyncio.run(run())
    assert result is not None
    assert result["summary"] == "ok now"
    assert call_count["n"] == 3  # failed twice, succeeded on the third
    assert any("retrying" in line.lower() for line in log)
    assert any("succeeded on attempt 3" in line for line in log)


def test_permanent_404_fails_fast_without_retry(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})

    _patch_transport(monkeypatch, handler)

    async def run():
        async with Client(mcp) as client:
            log: list[str] = []
            result = await _call_tool_safely(client, "jira_fetch_incident", {"issue_key": "OPS-GONE"}, log)
            return result, log

    result, log = asyncio.run(run())
    assert result is None
    assert call_count["n"] == 1  # no retries — 404 is not retryable
    assert any("failed after 1 attempt" in line for line in log)
    assert not any("retrying" in line.lower() for line in log)


def test_missing_credentials_fails_fast_without_retry(monkeypatch):
    monkeypatch.delenv("JIRA_URL", raising=False)
    # note: httpx transport isn't even reached in this case, since the
    # config check happens before any HTTP call — no handler needed

    async def run():
        async with Client(mcp) as client:
            log: list[str] = []
            result = await _call_tool_safely(client, "jira_fetch_incident", {"issue_key": "OPS-1"}, log)
            return result, log

    result, log = asyncio.run(run())
    assert result is None
    assert any("failed after 1 attempt" in line for line in log)
    assert not any("retrying" in line.lower() for line in log)


def test_persistent_5xx_exhausts_retries_and_degrades_gracefully(monkeypatch):
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(503, text="Still down")

    _patch_transport(monkeypatch, handler)

    async def run():
        async with Client(mcp) as client:
            log: list[str] = []
            result = await _call_tool_safely(client, "jira_fetch_incident", {"issue_key": "OPS-1"}, log)
            return result, log

    result, log = asyncio.run(run())
    assert result is None  # never crashes — degrades to None after exhausting attempts
    assert call_count["n"] == 3  # default RETRY_MAX_ATTEMPTS
    assert any("failed after 3 attempt(s)" in line for line in log)
