from __future__ import annotations

from src.graph.nodes.specialists.base import BaseSpecialist

SYSTEM_PROMPT = (
    "You are a log analysis specialist. You only see log and alert evidence. "
    "Never speculate beyond what the evidence shows."
)


class LogAnalystAgent(BaseSpecialist):
    agent_name = "log_analyst"
    relevant_sources = ("splunk", "jira")

    def run(self, events: list, retrieved_context: list[dict] | None = None,
            decision_log: list[str] | None = None):
        return self.analyze(events, SYSTEM_PROMPT, retrieved_context, decision_log)
