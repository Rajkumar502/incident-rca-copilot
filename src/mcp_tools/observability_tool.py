"""
Observability tool — thin wrapper over Grafana and Splunk query APIs.

Feeds the MetricsAgent (Grafana) and LogAnalystAgent (Splunk). Both real
API shapes; not exercised live in this sandbox (no network access to a
Grafana/Splunk instance here, and no instance URL configured) — covered
by tests/test_observability_tool.py with a mocked client. Both functions
return already-normalized dicts matching the `payload` shape used by
IncidentEvent, so ingestion code doesn't need source-specific branching
beyond picking which function to call.
"""

from __future__ import annotations

import httpx

from src import config


class ObservabilityConfigError(RuntimeError):
    pass


def query_grafana_alerts(
    service: str, since_minutes: int = 60, client: httpx.Client | None = None
) -> list[dict]:
    base_url = config.get("GRAFANA_URL")
    api_key = config.get("GRAFANA_API_KEY")
    if not base_url or not api_key:
        raise ObservabilityConfigError("GRAFANA_URL / GRAFANA_API_KEY missing in environment")

    own_client = client is None
    client = client or httpx.Client(timeout=15)
    try:
        resp = client.get(
            f"{base_url.rstrip('/')}/api/alertmanager/grafana/api/v2/alerts",
            headers={"Authorization": f"Bearer {api_key}"},
            params={"filter": f'service="{service}"'},
        )
        resp.raise_for_status()
        alerts = resp.json()
        return [
            {
                "metric": a.get("labels", {}).get("alertname"),
                "severity": a.get("labels", {}).get("severity"),
                "starts_at": a.get("startsAt"),
                "annotations": a.get("annotations", {}),
            }
            for a in alerts
        ]
    finally:
        if own_client:
            client.close()


def query_splunk_logs(
    service: str, search_query: str, since_minutes: int = 60,
    client: httpx.Client | None = None,
) -> list[dict]:
    base_url = config.get("SPLUNK_URL")
    token = config.get("SPLUNK_TOKEN")
    if not base_url or not token:
        raise ObservabilityConfigError("SPLUNK_URL / SPLUNK_TOKEN missing in environment")

    own_client = client is None
    client = client or httpx.Client(timeout=15, verify=True)
    try:
        spl = f'search index=* service="{service}" {search_query} earliest=-{since_minutes}m'
        resp = client.post(
            f"{base_url.rstrip('/')}/services/search/jobs/export",
            headers={"Authorization": f"Bearer {token}"},
            data={"search": spl, "output_mode": "json"},
        )
        resp.raise_for_status()
        # Splunk returns newline-delimited JSON events
        events = []
        for line in resp.text.splitlines():
            if not line.strip():
                continue
            import json as _json
            try:
                events.append(_json.loads(line).get("result", {}))
            except _json.JSONDecodeError:
                continue
        return events
    finally:
        if own_client:
            client.close()
