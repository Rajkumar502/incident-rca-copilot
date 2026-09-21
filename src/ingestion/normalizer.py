"""
Normalizer: converts raw tool-call responses (Jira issue, GitHub commit,
Grafana alert, Splunk log line) into IncidentEvent objects.

This is the layer that guarantees the citation-grounding property holds
for real data, not just synthetic fixtures: every event gets a stable,
real `raw_id` derived from something the source system actually returns
(the Jira key, the commit sha, an alert fingerprint, a log-line hash) —
never a random or synthetic id — so a specialist's citation is always
traceable back to something the person can look up themselves.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from src.schemas.models import IncidentEvent


def event_from_jira(issue: dict, service: str | None = None) -> IncidentEvent:
    return IncidentEvent(
        source="jira",
        raw_id=issue["key"],
        timestamp=datetime.now(),
        service=service,
        payload={
            "summary": issue.get("summary"),
            "status": issue.get("status"),
            "description": _truncate(issue.get("description")),
        },
    )


def event_from_commit(commit: dict, service: str | None = None) -> IncidentEvent:
    return IncidentEvent(
        source="git",
        raw_id=commit["sha"],
        timestamp=datetime.now(),
        service=service,
        payload={
            "message": commit.get("message"),
            "author": commit.get("author"),
            "files_changed": commit.get("files_changed", []),
        },
    )


def event_from_workflow_run(run: dict, service: str | None = None) -> IncidentEvent:
    return IncidentEvent(
        source="cicd",
        raw_id=f"run-{run['id']}",
        timestamp=_parse_ts(run.get("created_at")),
        service=service,
        payload={
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "deployed_sha": run.get("head_sha"),
        },
    )


def event_from_grafana_alert(alert: dict, service: str | None = None) -> IncidentEvent:
    # Grafana alerts don't always carry a stable id in the alertmanager API
    # response, so derive a deterministic fingerprint from its content —
    # stable across repeated fetches of the same alert, never random.
    fingerprint = hashlib.sha256(
        f"{alert.get('metric')}{alert.get('starts_at')}{service}".encode()
    ).hexdigest()[:10]
    return IncidentEvent(
        source="grafana",
        raw_id=f"alert-{fingerprint}",
        timestamp=_parse_ts(alert.get("starts_at")),
        service=service,
        payload={
            "metric": alert.get("metric"),
            "severity": alert.get("severity"),
            "annotations": alert.get("annotations"),
        },
    )


def event_from_splunk_log(log_entry: dict, service: str | None = None) -> IncidentEvent:
    fingerprint = hashlib.sha256(str(log_entry).encode()).hexdigest()[:10]
    return IncidentEvent(
        source="splunk",
        raw_id=f"log-{fingerprint}",
        timestamp=datetime.now(),
        service=service,
        payload=log_entry,
    )


def _truncate(text, limit: int = 500) -> str | None:
    if text is None:
        return None
    if not isinstance(text, str):
        # Defensive: a well-formed IncidentEvent should never be built from
        # a raw dict/other structure here — this shouldn't happen if
        # upstream tools (e.g. jira_tool.fetch_incident) correctly convert
        # rich-text fields to plain strings before returning, but coercing
        # rather than crashing keeps one malformed field from taking down
        # the whole ingestion run.
        text = str(text)
    return text[:limit] + ("...[truncated]" if len(text) > limit else "")


def _parse_ts(value: str | None) -> datetime:
    if not value:
        return datetime.now()
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now()