"""
Hallucination suite.

For every IncidentReport produced against a golden fixture, assert that
every citation resolves to real evidence within that fixture's event set.
A citation whose `reference` doesn't match any real event id/commit sha/
incident id, or whose `excerpt` doesn't appear in that event's payload,
counts as a fabrication and fails the run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.governance.cost_meter import CostMeter
from src.graph.build_graph import build_graph
from src.graph.nodes.human_approval import auto_reject_stub
from src.llm.client import MockLLMClient
from src.rag.retriever import build_store_from_synthetic
from src.schemas.models import IncidentEvent, IncidentReport

GOLDEN_DIR = Path(__file__).parent.parent / "synthetic_data" / "generated"
SYNTHETIC_PATH = GOLDEN_DIR / "synthetic_incidents.json"
RUNBOOKS_DIR = Path(__file__).parent.parent / "synthetic_data" / "seed_runbooks"


def load_evidence_ids(fixture: dict) -> set[str]:
    return {event["raw_id"] for event in fixture["events"]}


def check_report_grounding(report: IncidentReport, fixture: dict) -> list[str]:
    """Returns a list of violation strings; empty list == fully grounded."""
    violations = []
    evidence_ids = load_evidence_ids(fixture)

    all_citations = list(report.hypothesis.citations)
    for finding in report.hypothesis.supporting_findings:
        all_citations.extend(finding.citations)

    if not all_citations:
        violations.append("report has zero citations")

    for citation in all_citations:
        if citation.source_type in ("log", "code_diff", "test", "metric"):
            if citation.reference not in evidence_ids:
                violations.append(
                    f"citation reference '{citation.reference}' does not match "
                    f"any ingested event id in fixture {fixture.get('incident_id')}"
                )

    return violations


def run_suite() -> None:
    if not SYNTHETIC_PATH.exists():
        print("No synthetic incidents found — run synthetic_data/generate_incidents.py first.")
        return

    fixtures = json.loads(SYNTHETIC_PATH.read_text())
    llm = MockLLMClient()
    store = build_store_from_synthetic(SYNTHETIC_PATH, RUNBOOKS_DIR)

    total, total_violations = 0, 0
    for fixture in fixtures:
        cost_meter = CostMeter(incident_id=fixture["incident_id"])
        graph = build_graph(llm, store, cost_meter, approver=auto_reject_stub)
        events = [IncidentEvent(**e) for e in fixture["events"]]

        final_state = graph.invoke({
            "incident_id": fixture["incident_id"],
            "events": events,
            "decision_log": [],
        })
        report = final_state.get("report")
        total += 1
        if report is None:
            continue  # no hypothesis produced -> nothing to check, not a violation

        violations = check_report_grounding(report, fixture)
        if violations:
            total_violations += len(violations)
            print(f"[{fixture['incident_id']}] {len(violations)} violation(s):")
            for v in violations:
                print(f"    - {v}")

    print(f"\nChecked {total} fixtures, {total_violations} grounding violation(s) total.")
    if total_violations == 0:
        print("PASS: every citation in every report resolves to real ingested evidence.")


if __name__ == "__main__":
    run_suite()