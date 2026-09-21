"""CLI entrypoint: python -m src.cli analyze-incident <incident_id>"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from src.governance.cost_meter import CostCircuitBreakerTripped, CostMeter
from src.graph.build_graph import build_graph
from src.graph.nodes.human_approval import auto_reject_stub, cli_prompt_approver
from src.llm.client import select_llm_client
from src.rag.retriever import build_store_from_synthetic
from src.schemas.models import IncidentEvent

SYNTHETIC_PATH = Path("synthetic_data/generated/synthetic_incidents.json")
RUNBOOKS_DIR = Path("synthetic_data/seed_runbooks")


def load_incident(incident_id: str) -> list[IncidentEvent]:
    incidents = json.loads(SYNTHETIC_PATH.read_text())
    match = next((i for i in incidents if i["incident_id"] == incident_id), None)
    if match is None:
        available = [i["incident_id"] for i in incidents]
        raise SystemExit(
            f"Incident {incident_id} not found. Available: {available[:10]}..."
        )
    return [IncidentEvent(**e) for e in match["events"]]


def analyze_incident(incident_id: str, interactive: bool = False) -> None:
    events = load_incident(incident_id)
    llm = select_llm_client()
    store = build_store_from_synthetic(SYNTHETIC_PATH, RUNBOOKS_DIR)
    cost_meter = CostMeter(incident_id=incident_id)
    approver = cli_prompt_approver if interactive else auto_reject_stub

    graph = build_graph(llm, store, cost_meter, approver=approver)

    initial_state = {
        "incident_id": incident_id,
        "events": events,
        "decision_log": [],
    }

    print(f"\n=== Analyzing {incident_id} (llm={type(llm).__name__}) ===\n")
    try:
        final_state = graph.invoke(initial_state)
    except CostCircuitBreakerTripped as e:
        print(f"CIRCUIT BREAKER TRIPPED: {e}")
        return

    for line in final_state.get("decision_log", []):
        print(f"  {line}")

    report = final_state.get("report")
    print("\n--- FINAL STATE ---")
    if report:
        print(report.model_dump_json(indent=2))
    else:
        print("No report produced.")
    print(f"\nCost so far: ${cost_meter.cost_usd():.6f} over {cost_meter.iterations} LLM call(s)")


def main() -> None:
    if len(sys.argv) < 3 or sys.argv[1] != "analyze-incident":
        print("Usage: python -m src.cli analyze-incident <INCIDENT_ID> [--interactive]")
        sys.exit(1)
    incident_id = sys.argv[2]
    interactive = "--interactive" in sys.argv
    analyze_incident(incident_id, interactive=interactive)


if __name__ == "__main__":
    main()
