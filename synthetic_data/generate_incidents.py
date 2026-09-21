"""
Generates synthetic incidents with known ground-truth root causes.

Used for:
  - RAG seed data (past incidents to retrieve against)
  - Golden eval fixtures (eval/golden_incidents/) with labeled correct answers
  - Local demo / dashboard walkthrough without needing real Jira/Splunk access

Six archetypes are included because each exercises a different specialist
agent and a different point on the confidence spectrum:
  1. deploy_regression       -> CodeDiffAgent should find it, high confidence
  2. db_connection_exhaustion-> MetricsAgent + LogAnalystAgent, medium confidence
  3. bad_config_push         -> CodeDiffAgent, high confidence
  4. flaky_test_noise        -> TestFailureAgent, should NOT trigger rollback
  5. genuine_app_bug         -> TestFailureAgent + LogAnalystAgent, medium
  6. ambiguous_multi_cause   -> low confidence, should route to human review
"""

from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta, UTC
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "generated"
OUTPUT_DIR.mkdir(exist_ok=True)

random.seed(42)

SERVICES = ["checkout-api", "payments-svc", "inventory-svc", "auth-gateway", "notification-worker"]


def _ts(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def deploy_regression(service: str) -> dict:
    commit_sha = uuid.uuid4().hex[:7]
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        "ground_truth_category": "deployment_regression",
        "ground_truth_confidence_band": "high",
        "expected_rollback": True,
        "service": service,
        "events": [
            {"source": "git", "raw_id": commit_sha, "timestamp": _ts(20),
             "service": service,
             "payload": {"message": "refactor: switch payment retry to sync call",
                         "files_changed": ["src/payments/retry.py"]}},
            {"source": "cicd", "raw_id": f"run-{uuid.uuid4().hex[:6]}", "timestamp": _ts(18),
             "service": service, "payload": {"status": "success", "deployed_sha": commit_sha}},
            {"source": "grafana", "raw_id": f"alert-{uuid.uuid4().hex[:6]}", "timestamp": _ts(12),
             "service": service, "payload": {"metric": "p99_latency_ms",
                                              "before": 180, "after": 4200,
                                              "alert": "p99 latency > 2000ms"}},
            {"source": "splunk", "raw_id": f"log-{uuid.uuid4().hex[:6]}", "timestamp": _ts(11),
             "service": service, "payload": {"level": "ERROR",
                                              "message": "TimeoutError: sync retry blocked event loop"}},
        ],
        "runbook_reference": "runbook-sync-vs-async-retry.md",
    }


def db_connection_exhaustion(service: str) -> dict:
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        "ground_truth_category": "infra_external",
        "ground_truth_confidence_band": "medium",
        "expected_rollback": False,
        "service": service,
        "events": [
            {"source": "grafana", "raw_id": f"alert-{uuid.uuid4().hex[:6]}", "timestamp": _ts(30),
             "service": service, "payload": {"metric": "db_connections_active",
                                              "before": 40, "after": 200, "pool_max": 200}},
            {"source": "splunk", "raw_id": f"log-{uuid.uuid4().hex[:6]}", "timestamp": _ts(25),
             "service": service, "payload": {"level": "ERROR",
                                              "message": "connection pool exhausted, waiting"}},
            {"source": "jira", "raw_id": f"OPS-{random.randint(100,999)}", "timestamp": _ts(24),
             "service": service, "payload": {"summary": "Downstream analytics job holding long transactions"}},
        ],
        "runbook_reference": "runbook-db-pool-exhaustion.md",
    }


def bad_config_push(service: str) -> dict:
    commit_sha = uuid.uuid4().hex[:7]
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        "ground_truth_category": "config_drift",
        "ground_truth_confidence_band": "high",
        "expected_rollback": True,
        "service": service,
        "events": [
            {"source": "git", "raw_id": commit_sha, "timestamp": _ts(15),
             "service": service,
             "payload": {"message": "chore: bump feature-flag defaults",
                         "files_changed": ["config/flags.yaml"]}},
            {"source": "cicd", "raw_id": f"run-{uuid.uuid4().hex[:6]}", "timestamp": _ts(14),
             "service": service, "payload": {"status": "success", "deployed_sha": commit_sha}},
            {"source": "test_results", "raw_id": f"suite-{uuid.uuid4().hex[:6]}", "timestamp": _ts(10),
             "service": service, "payload": {"failed_tests": ["test_checkout_flow_flag_off"],
                                              "pass_rate": 0.62}},
        ],
        "runbook_reference": "runbook-feature-flag-rollout.md",
    }


def flaky_test_noise(service: str) -> dict:
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        "ground_truth_category": "flaky_unrelated",
        "ground_truth_confidence_band": "medium",
        "expected_rollback": False,
        "service": service,
        "events": [
            {"source": "test_results", "raw_id": f"suite-{uuid.uuid4().hex[:6]}", "timestamp": _ts(5),
             "service": service, "payload": {"failed_tests": ["test_carousel_hover_animation"],
                                              "pass_rate": 0.98, "rerun_pass": True}},
        ],
        "runbook_reference": "runbook-flaky-test-triage.md",
    }


def genuine_app_bug(service: str) -> dict:
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        "ground_truth_category": "true_app_bug",
        "ground_truth_confidence_band": "medium",
        "expected_rollback": False,
        "service": service,
        "events": [
            {"source": "test_results", "raw_id": f"suite-{uuid.uuid4().hex[:6]}", "timestamp": _ts(40),
             "service": service, "payload": {"failed_tests": ["test_discount_stacking"],
                                              "pass_rate": 0.71, "rerun_pass": False}},
            {"source": "splunk", "raw_id": f"log-{uuid.uuid4().hex[:6]}", "timestamp": _ts(38),
             "service": service, "payload": {"level": "ERROR",
                                              "message": "AssertionError: discount applied twice"}},
        ],
        "runbook_reference": None,
    }


def ambiguous_multi_cause(service: str) -> dict:
    return {
        "incident_id": f"INC-{uuid.uuid4().hex[:6]}",
        "ground_truth_category": "ambiguous",
        "ground_truth_confidence_band": "low",
        "expected_rollback": False,
        "service": service,
        "events": [
            {"source": "grafana", "raw_id": f"alert-{uuid.uuid4().hex[:6]}", "timestamp": _ts(50),
             "service": service, "payload": {"metric": "error_rate", "before": 0.01, "after": 0.06}},
            {"source": "git", "raw_id": uuid.uuid4().hex[:7], "timestamp": _ts(55),
             "service": service, "payload": {"message": "unrelated docs update"}},
            {"source": "splunk", "raw_id": f"log-{uuid.uuid4().hex[:6]}", "timestamp": _ts(48),
             "service": service, "payload": {"level": "WARN", "message": "upstream DNS resolution slow"}},
        ],
        "runbook_reference": None,
    }


GENERATORS = [
    deploy_regression,
    db_connection_exhaustion,
    bad_config_push,
    flaky_test_noise,
    genuine_app_bug,
    ambiguous_multi_cause,
]


def main(n_per_archetype: int = 3) -> None:
    all_incidents = []
    for gen in GENERATORS:
        for _ in range(n_per_archetype):
            service = random.choice(SERVICES)
            all_incidents.append(gen(service))

    out_path = OUTPUT_DIR / "synthetic_incidents.json"
    out_path.write_text(json.dumps(all_incidents, indent=2))
    print(f"Wrote {len(all_incidents)} synthetic incidents to {out_path}")


if __name__ == "__main__":
    main()