"""Assembles the LangGraph StateGraph from the node implementations."""

from __future__ import annotations

from typing import Callable

from langgraph.graph import END, StateGraph

from src.governance.cost_meter import CostMeter
from src.graph.nodes.confidence_gate import run_confidence_gate
from src.graph.nodes.human_approval import auto_reject_stub, make_human_approval_node
from src.graph.nodes.retrieval import make_retrieval_node
from src.graph.nodes.risk_rollback import run_risk_and_rollback
from src.graph.nodes.specialist_dispatch import make_specialist_dispatch_node
from src.graph.nodes.synthesizer import run_synthesizer
from src.graph.nodes.triage import run_triage
from src.graph.nodes.writeback import make_writeback_node
from src.graph.state import GraphState
from src.llm.client import LLMClient
from src.rag.retriever import VectorStore


def build_graph(
    llm: LLMClient,
    vector_store: VectorStore,
    cost_meter: CostMeter,
    approver: Callable | None = None,
):
    approver = approver or auto_reject_stub

    retrieval_node = make_retrieval_node(vector_store)
    specialist_node = make_specialist_dispatch_node(llm, cost_meter)
    approval_node = make_human_approval_node(approver)
    writeback_node = make_writeback_node()

    graph = StateGraph(GraphState)

    graph.add_node("triage", run_triage)
    graph.add_node("retrieval", retrieval_node)
    graph.add_node("specialists", specialist_node)
    graph.add_node("synthesizer", run_synthesizer)
    graph.add_node("confidence_gate", run_confidence_gate)
    graph.add_node("risk_rollback", run_risk_and_rollback)
    graph.add_node("human_approval", approval_node)
    graph.add_node("writeback", writeback_node)

    graph.set_entry_point("triage")
    graph.add_edge("triage", "retrieval")
    graph.add_edge("retrieval", "specialists")
    graph.add_edge("specialists", "synthesizer")
    graph.add_edge("synthesizer", "confidence_gate")

    def route_after_gate(state: GraphState) -> str:
        if state.get("confidence_gate_passed"):
            return "risk_rollback"
        return "writeback"  # low-confidence/ungrounded -> straight to blocked/insufficient-evidence path

    graph.add_conditional_edges(
        "confidence_gate", route_after_gate, {"risk_rollback": "risk_rollback", "writeback": "writeback"}
    )
    graph.add_edge("risk_rollback", "human_approval")
    graph.add_edge("human_approval", "writeback")
    graph.add_edge("writeback", END)

    return graph.compile()
