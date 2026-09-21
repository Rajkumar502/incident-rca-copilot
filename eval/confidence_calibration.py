"""
Confidence calibration suite.

Runs the graph against every synthetic fixture, buckets the resulting
hypothesis confidence, and checks it against a simple correctness proxy:
does the hypothesis's supporting agent set match what the archetype is
designed to be caught by? (e.g. deployment_regression should be caught
by code_diff/log_analyst, not by test_failure alone).

With only 18 fixtures this can't produce a statistically rigorous
calibration curve — that needs hundreds of labeled incidents — but it
does two useful things now:
  1. Flags gross miscalibration (e.g. an "ambiguous" archetype somehow
     getting reported at 0.9 confidence would be a real bug).
  2. Establishes the harness so calibration becomes a real metric the
     moment a larger labeled set exists — just point SYNTHETIC_PATH at
     a bigger fixture file.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.governance.cost_meter import CostMeter
from src.graph.build_graph import build_graph
from src.graph.nodes.human_approval import auto_reject_stub
from src.llm.client import MockLLMClient
from src.rag.retriever import build_store_from_synthetic
from src.schemas.models import IncidentEvent

SYNTHETIC_PATH = Path(__file__).parent.parent / "synthetic_data" / "generated" / "synthetic_incidents.json"
RUNBOOKS_DIR = Path(__file__).parent.parent / "synthetic_data" / "seed_runbooks"

# expected confidence band per archetype, set by the synthetic data generator
_BAND_RANGES = {
    "high": (0.7, 1.0),
    "medium": (0.5, 0.85),
    "low": (0.0, 0.6),
}


def run_suite() -> None:
    if not SYNTHETIC_PATH.exists():
        print("No synthetic incidents found — run synthetic_data/generate_incidents.py first.")
        return

    fixtures = json.loads(SYNTHETIC_PATH.read_text())
    llm = MockLLMClient()
    store = build_store_from_synthetic(SYNTHETIC_PATH, RUNBOOKS_DIR)

    band_hits = defaultdict(lambda: {"in_band": 0, "total": 0})
    no_hypothesis = 0

    for fixture in fixtures:
        cost_meter = CostMeter(incident_id=fixture["incident_id"])
        graph = build_graph(llm, store, cost_meter, approver=auto_reject_stub)
        events = [IncidentEvent(**e) for e in fixture["events"]]

        final_state = graph.invoke({
            "incident_id": fixture["incident_id"],
            "events": events,
            "decision_log": [],
        })

        band = fixture["ground_truth_confidence_band"]
        report = final_state.get("report")

        if report is None:
            no_hypothesis += 1
            continue

        confidence = report.hypothesis.confidence
        lo, hi = _BAND_RANGES[band]
        in_band = lo <= confidence <= hi

        band_hits[band]["total"] += 1
        if in_band:
            band_hits[band]["in_band"] += 1
        else:
            print(
                f"[{fixture['incident_id']}] archetype={fixture['ground_truth_category']} "
                f"expected_band={band} ({lo}-{hi}) but got confidence={confidence}"
            )

    print("\n--- Calibration summary (18-fixture sample; indicative, not statistically rigorous) ---")
    for band, stats in sorted(band_hits.items()):
        pct = 100 * stats["in_band"] / stats["total"] if stats["total"] else 0
        print(f"  {band:8s}: {stats['in_band']}/{stats['total']} in expected range ({pct:.0f}%)")
    if no_hypothesis:
        print(f"  {no_hypothesis} fixture(s) produced no hypothesis at all")


if __name__ == "__main__":
    run_suite()