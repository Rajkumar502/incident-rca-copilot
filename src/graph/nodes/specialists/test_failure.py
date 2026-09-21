from __future__ import annotations

from src.graph.nodes.specialists.base import BaseSpecialist

SYSTEM_PROMPT = (
    "You are a test-failure analysis specialist. You only see test result "
    "evidence. Distinguish genuine regressions from flaky/noisy failures. "
    "Never speculate beyond what the evidence shows."
)


class TestFailureAgent(BaseSpecialist):
    agent_name = "test_failure"
    relevant_sources = ("test_results",)

    def run(self, events: list, retrieved_context: list[dict] | None = None,
            decision_log: list[str] | None = None):
        return self.analyze(events, SYSTEM_PROMPT, retrieved_context, decision_log)
