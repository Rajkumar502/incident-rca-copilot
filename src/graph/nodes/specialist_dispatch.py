"""Dispatch node: runs all specialist agents, keeps only grounded findings."""

from __future__ import annotations

from src.governance.cost_meter import CostMeter
from src.graph.nodes.specialists.code_diff import CodeDiffAgent
from src.graph.nodes.specialists.log_analyst import LogAnalystAgent
from src.graph.nodes.specialists.metrics import MetricsAgent
from src.graph.nodes.specialists.test_failure import TestFailureAgent
from src.graph.state import GraphState
from src.llm.client import LLMClient


def make_specialist_dispatch_node(llm: LLMClient, cost_meter: CostMeter):
    agents = [
        LogAnalystAgent(llm, cost_meter),
        CodeDiffAgent(llm, cost_meter),
        TestFailureAgent(llm, cost_meter),
        MetricsAgent(llm, cost_meter),
    ]

    def run_specialists(state: GraphState) -> GraphState:
        events = state.get("events", [])
        retrieved_context = state.get("retrieved_context", [])
        decision_log = state.get("decision_log", [])
        findings = []

        for agent in agents:
            finding = agent.run(events, retrieved_context, decision_log)
            if finding is None:
                decision_log.append(f"SPECIALIST[{agent.agent_name}]: no relevant evidence, skipped")
                continue
            if not finding.is_grounded():
                decision_log.append(f"SPECIALIST[{agent.agent_name}]: produced ungrounded finding, discarded")
                continue
            findings.append(finding)
            decision_log.append(
                f"SPECIALIST[{agent.agent_name}]: confidence={finding.confidence} "
                f"citations={[c.reference for c in finding.citations]}"
            )

        state["specialist_findings"] = findings
        state["decision_log"] = decision_log
        return state

    return run_specialists
