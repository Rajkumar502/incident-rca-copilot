"""
Automatic trigger: a small HTTP server that receives Jira webhook events
and calls analyze_real_incident() without a human running a CLI command.

This closes the gap the architecture doc called out as the highest-priority
remaining piece: everything downstream of ingestion (analysis, gating,
history) works and improves itself, but until now nothing reached for it
automatically.

Run: uvicorn src.webhook.server:app --host 0.0.0.0 --port 8000
     (or: python -m src.webhook.server)

Jira Cloud setup: Project settings -> Automation -> create a rule
triggered on "Issue created" or "Issue updated", condition "Labels
contains <WEBHOOK_TRIGGER_LABEL>" (default "incident"), action "Send web
request" to this server's /webhook/jira URL, with a header
X-Webhook-Secret: <your WEBHOOK_SECRET>. Jira's built-in "Automation for
Jira" is the recommended path since raw Jira Cloud webhooks don't support
a custom auth header; a reverse proxy adding the header also works.

Design choices, and why:
- Runs analysis in a background task, so the webhook responds immediately
  (2xx) rather than making Jira/Automation wait for a full graph run —
  most webhook senders time out and retry if a response takes too long,
  which would otherwise cause duplicate triggers.
- Idempotency: an issue key that's already been triggered in this
  process's lifetime is not re-triggered, since Jira webhooks are
  at-least-once delivery and commonly fire more than once for the same
  event (e.g. an automation rule re-evaluating after its own comment
  write-back triggers "Issue updated" again).
- The secret check is a deliberately simple shared-secret header, not a
  cryptographic HMAC signature — Jira Cloud's native webhooks don't sign
  payloads the way some other providers do, so a shared secret checked at
  the application layer (paired with HTTPS in any real deployment) is the
  practical baseline here, not a compromise from a stronger scheme.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from pydantic import BaseModel

from src import config

logger = logging.getLogger("incident_rca_webhook")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Incident RCA Copilot — Jira Webhook Trigger")

# In-memory de-dupe set. A real multi-instance deployment would use a
# shared store (Redis, a database row) instead of process memory — this
# is documented as a known limitation for that reason, not overlooked.
_already_triggered: set[str] = set()
_lock = asyncio.Lock()


class WebhookAck(BaseModel):
    status: str
    issue_key: str | None = None
    triggered: bool = False
    reason: str | None = None


def _extract_issue_key(payload: dict[str, Any]) -> str | None:
    """Jira's standard webhook payload nests the key under issue.key."""
    return payload.get("issue", {}).get("key")


def _extract_labels(payload: dict[str, Any]) -> list[str]:
    return payload.get("issue", {}).get("fields", {}).get("labels", []) or []


def _extract_service(payload: dict[str, Any]) -> str | None:
    """Reads the service name from a configured custom field, if set.
    Jira has no standard "service" field, so this is opt-in via
    WEBHOOK_SERVICE_FIELD (a customfield_XXXXX id) rather than guessed."""
    if not config.WEBHOOK_DEFAULT_SERVICE_FIELD:
        return None
    value = payload.get("issue", {}).get("fields", {}).get(config.WEBHOOK_DEFAULT_SERVICE_FIELD)
    return value if isinstance(value, str) else None


async def _run_analysis(issue_key: str, service: str | None) -> None:
    """Background task: the actual analyze_real_incident() call, isolated
    so its own exceptions can't crash the webhook server or leave the
    request hanging — logged instead."""
    from src.ingestion.collector import analyze_real_incident

    try:
        logger.info("Starting automatic analysis for %s", issue_key)
        final_state = await analyze_real_incident(
            issue_key,
            github_owner=config.WEBHOOK_GITHUB_OWNER,
            github_repo=config.WEBHOOK_GITHUB_REPO,
            service=service,
            interactive=False,  # a webhook-triggered run has no human to prompt;
            # the safe default (auto_reject_stub) applies — any report that
            # needs approval stays blocked in the review queue, never
            # auto-published just because it arrived via webhook instead
            # of a CLI invocation
        )
        if final_state is None:
            logger.warning("Analysis for %s produced no result (no evidence collected)", issue_key)
        else:
            report = final_state.get("report")
            logger.info(
                "Analysis for %s complete: report_produced=%s human_approved=%s",
                issue_key, report is not None, getattr(report, "human_approved", None),
            )
    except Exception:
        logger.exception("Automatic analysis failed for %s", issue_key)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/jira", response_model=WebhookAck)
async def jira_webhook(
    payload: dict[str, Any],
    background_tasks: BackgroundTasks,
    x_webhook_secret: str | None = Header(default=None),
) -> WebhookAck:
    if config.WEBHOOK_SECRET:
        if x_webhook_secret != config.WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="invalid or missing X-Webhook-Secret header")
    else:
        logger.warning(
            "WEBHOOK_SECRET is not set — accepting unauthenticated requests. "
            "Set WEBHOOK_SECRET in .env before exposing this endpoint publicly."
        )

    issue_key = _extract_issue_key(payload)
    if not issue_key:
        raise HTTPException(status_code=400, detail="payload missing issue.key")

    labels = _extract_labels(payload)
    if config.WEBHOOK_TRIGGER_LABEL not in labels:
        return WebhookAck(
            status="skipped", issue_key=issue_key, triggered=False,
            reason=f"issue does not have the '{config.WEBHOOK_TRIGGER_LABEL}' label",
        )

    async with _lock:
        if issue_key in _already_triggered:
            return WebhookAck(
                status="skipped", issue_key=issue_key, triggered=False,
                reason="already triggered for this issue in this server's lifetime",
            )
        _already_triggered.add(issue_key)

    service = _extract_service(payload)
    background_tasks.add_task(_run_analysis, issue_key, service)

    return WebhookAck(status="accepted", issue_key=issue_key, triggered=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
