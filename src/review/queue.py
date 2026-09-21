"""
Review queue: the missing piece once incidents can be detected and
analyzed unattended (src/webhook/server.py) and more resilient to
transient failures (src/governance/retry.py) — a place to actually see
and act on everything sitting blocked, pending a human decision.

Every report requiring approval (see IncidentReport.requires_gate()) that
wasn't approved at analysis time is persisted to
reports/<incident_id>_BLOCKED.json by src/graph/nodes/writeback.py. This
module lists those files and lets a reviewer approve or reject them —
without re-running the graph, since the analysis (hypothesis, citations,
risk, rollback recommendation) was already computed and doesn't change;
only the human decision was missing.

Approving here calls the *same* write-back and history-recording code
paths a graph run would have called had a human approved inline — so a
report approved from the queue is indistinguishable downstream from one
approved during the original run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path

from src.graph.nodes.writeback import select_jira_client
from src.rag.history import record_published_incident
from src.schemas.models import IncidentReport

REPORTS_DIR = Path("reports")
BLOCKED_SUFFIX = "_BLOCKED.json"
PUBLISHED_SUFFIX = "_published.json"
REJECTED_SUFFIX = "_REJECTED.json"


@dataclass
class QueueEntry:
    incident_id: str
    report: IncidentReport
    path: Path


class QueueEntryNotFound(Exception):
    pass


def list_blocked(reports_dir: Path = REPORTS_DIR) -> list[QueueEntry]:
    """Everything currently waiting on a human decision, oldest first —
    so a reviewer works through the backlog in the order it built up,
    not in whatever order the filesystem happens to list files."""
    if not reports_dir.exists():
        return []

    entries = []
    for path in reports_dir.glob(f"*{BLOCKED_SUFFIX}"):
        try:
            report = IncidentReport.model_validate_json(path.read_text())
        except Exception:  # noqa: BLE001 — a corrupt/partial file shouldn't
            # break listing every other entry; skip it, don't crash the queue
            continue
        entries.append(QueueEntry(incident_id=report.incident_id, report=report, path=path))

    entries.sort(key=lambda e: e.report.generated_at)
    return entries


def _find_blocked(incident_id: str, reports_dir: Path = REPORTS_DIR) -> QueueEntry:
    path = reports_dir / f"{incident_id}{BLOCKED_SUFFIX}"
    if not path.exists():
        raise QueueEntryNotFound(
            f"No blocked report found for {incident_id} at {path}. "
            "It may have already been approved/rejected, or never existed."
        )
    report = IncidentReport.model_validate_json(path.read_text())
    return QueueEntry(incident_id=incident_id, report=report, path=path)


def approve(
    incident_id: str, approver: str, reports_dir: Path = REPORTS_DIR,
) -> IncidentReport:
    """
    Approves a blocked report: publishes it via the same Jira client
    selection real graph runs use (real or mock, based on credentials —
    see writeback.py), records it to RAG history exactly as an inline
    approval would, writes the approved copy to *_published.json, and
    removes the blocked file so it stops showing up in list_blocked().
    """
    entry = _find_blocked(incident_id, reports_dir)
    report = entry.report.model_copy(update={"human_approved": True, "approver": approver})

    jira_client = select_jira_client()
    jira_client.write_comment(incident_id, report)

    try:
        record_published_incident(report)
    except Exception as e:  # noqa: BLE001 — same principle as writeback.py's
        # _record_history: a missing history entry is a smaller problem
        # than letting it block the approval that already succeeded
        print(f"[review_queue] failed to record {incident_id} to history: {e}")

    published_path = reports_dir / f"{incident_id}{PUBLISHED_SUFFIX}"
    published_path.write_text(report.model_dump_json(indent=2))
    entry.path.unlink()

    return report


def reject(
    incident_id: str, reviewer: str, reason: str = "", reports_dir: Path = REPORTS_DIR,
) -> IncidentReport:
    """
    Rejects a blocked report: never published, never recorded to history
    (a rejected hypothesis is explicitly not confirmed institutional
    memory — same principle as writeback.py's _record_history only
    firing on actual publication). Moved to *_REJECTED.json rather than
    deleted outright, so there's an audit trail of what was reviewed and
    declined, and why.
    """
    entry = _find_blocked(incident_id, reports_dir)
    report = entry.report.model_copy(update={"human_approved": False, "approver": reviewer})

    rejected_path = reports_dir / f"{incident_id}{REJECTED_SUFFIX}"
    record = {
        "report": json.loads(report.model_dump_json()),
        "rejected_by": reviewer,
        "rejected_at": datetime.now(UTC).isoformat(),
        "reason": reason,
    }
    rejected_path.write_text(json.dumps(record, indent=2))
    entry.path.unlink()

    return report
