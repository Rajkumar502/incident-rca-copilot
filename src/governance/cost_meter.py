"""
Token/cost tracking and circuit breaker.

Direct descendant of TokenLogger + the $0.05/run circuit breaker in the
TS agentic-playwright-framework repo, generalized here to also trip on
excessive agent iterations (not just dollar cost), since a stuck retry
loop is itself a governance failure.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, UTC
from pathlib import Path

# Pricing for gemini-3.5-flash-lite (the model this project's GeminiClient
# actually defaults to — see src/llm/client.py). Google's per-model pricing
# changes over time and isn't fetched live, so this is a point-in-time
# estimate for cost *tracking* (relative comparison across runs), not a
# verified real-time billing figure — cross-check against
# https://ai.google.dev/pricing for current rates before treating the
# dollar amount this produces as authoritative.
GEMINI_FLASH_LITE_INPUT_PER_1K = 0.0001
GEMINI_FLASH_LITE_OUTPUT_PER_1K = 0.0004

DEFAULT_COST_CEILING_USD = 0.25   # per incident run (higher than the test-gen
                                  # repo's $0.05 since this does more retrieval)
DEFAULT_MAX_ITERATIONS = 12


class CostCircuitBreakerTripped(Exception):
    pass


@dataclass
class CostMeter:
    incident_id: str
    cost_ceiling_usd: float = DEFAULT_COST_CEILING_USD
    max_iterations: int = DEFAULT_MAX_ITERATIONS

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    iterations: int = 0
    log_path: Path = field(default_factory=lambda: Path("token-audit.json"))

    def record(self, input_tokens: int, output_tokens: int, node: str) -> None:
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.iterations += 1

        if self.iterations > self.max_iterations:
            raise CostCircuitBreakerTripped(
                f"[{self.incident_id}] exceeded {self.max_iterations} agent "
                f"iterations at node '{node}' — aborting to prevent a stuck loop."
            )
        if self.cost_usd() > self.cost_ceiling_usd:
            raise CostCircuitBreakerTripped(
                f"[{self.incident_id}] cost ${self.cost_usd():.4f} exceeded "
                f"ceiling ${self.cost_ceiling_usd:.4f} at node '{node}'."
            )

        self._append_audit_record(node)

    def cost_usd(self) -> float:
        return (
            self.total_input_tokens / 1000 * GEMINI_FLASH_LITE_INPUT_PER_1K
            + self.total_output_tokens / 1000 * GEMINI_FLASH_LITE_OUTPUT_PER_1K
        )

    def _append_audit_record(self, node: str) -> None:
        record = {
            "incident_id": self.incident_id,
            "node": node,
            "timestamp": datetime.now(UTC).isoformat(),
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "cumulative_cost_usd": round(self.cost_usd(), 6),
        }
        existing = []
        if self.log_path.exists():
            try:
                existing = json.loads(self.log_path.read_text())
            except json.JSONDecodeError:
                existing = []
        existing.append(record)
        self.log_path.write_text(json.dumps(existing, indent=2))