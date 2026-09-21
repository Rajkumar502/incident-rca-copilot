from __future__ import annotations

from src.graph.nodes.specialists.base import BaseSpecialist

SYSTEM_PROMPT = (
    "You are a code-change analysis specialist. You only see git commit and "
    "CI/CD deploy evidence. Assess whether a recent change plausibly caused "
    "the incident. Never speculate beyond what the evidence shows."
)


class CodeDiffAgent(BaseSpecialist):
    agent_name = "code_diff"
    relevant_sources = ("git", "cicd")

    def run(self, events: list, retrieved_context: list[dict] | None = None,
            decision_log: list[str] | None = None):
        return self.analyze(events, SYSTEM_PROMPT, retrieved_context, decision_log)
