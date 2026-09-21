from __future__ import annotations

from src.graph.nodes.specialists.base import BaseSpecialist

SYSTEM_PROMPT = (
    "You are a metrics/observability specialist. You only see Grafana alert "
    "evidence. Assess what the metric deviation suggests about root cause. "
    "Never speculate beyond what the evidence shows."
)


class MetricsAgent(BaseSpecialist):
    agent_name = "metrics"
    relevant_sources = ("grafana",)

    def run(self, events: list, retrieved_context: list[dict] | None = None,
            decision_log: list[str] | None = None):
        return self.analyze(events, SYSTEM_PROMPT, retrieved_context, decision_log)
