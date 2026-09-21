"""
Real institutional memory: every time writeback.py successfully publishes
a report (not blocked pending approval), it's appended here as durable
history. build_store() then seeds the RAG store from synthetic incidents,
runbooks, AND this file — so the system's "similar past incidents" retrieval
actually gets smarter over time in production, instead of starting empty on
every real run the way it did before this file existed.

Storage format: JSON Lines (one report summary per line) so appending is
O(1) and a corrupt trailing write can't take down the whole file the way
it could with a single JSON array that gets rewritten on every append.
"""

from __future__ import annotations

import json
from datetime import datetime, UTC
from pathlib import Path

DEFAULT_HISTORY_PATH = Path("incident_history.jsonl")


def record_published_incident(report, history_path: Path = DEFAULT_HISTORY_PATH) -> None:
    """
    Appends a published report to the history file. Called only for
    reports that actually got published (writeback.py checks this before
    calling in) — a report stuck in the "blocked, pending approval" queue
    isn't a confirmed outcome yet and shouldn't be treated as institutional
    memory until a human has actually signed off on it.
    """
    citation_refs = [c.reference for c in report.hypothesis.citations]
    entry = {
        "incident_id": report.incident_id,
        "recorded_at": datetime.now(UTC).isoformat(),
        "severity": report.triage.severity.value,
        "affected_services": report.triage.affected_services,
        "summary_text": (
            f"{report.triage.severity.value} on {', '.join(report.triage.affected_services) or 'unknown service'}: "
            f"{report.hypothesis.statement}"
        ),
        "rollback_recommended": report.rollback.recommended,
        "citation_refs": citation_refs,
    }
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def load_history(history_path: Path = DEFAULT_HISTORY_PATH) -> list[dict]:
    """Reads all recorded history entries. Tolerates a corrupt trailing
    line (e.g. from a crash mid-write) by skipping just that line rather
    than failing the whole load."""
    if not history_path.exists():
        return []
    entries = []
    for line in history_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries
