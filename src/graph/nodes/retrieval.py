"""Retrieval node: pulls similar past incidents + runbook chunks via the RAG store."""

from __future__ import annotations

from src.graph.state import GraphState
from src.rag.retriever import VectorStore


def make_retrieval_node(store: VectorStore):
    def run_retrieval(state: GraphState) -> GraphState:
        events = state.get("events", [])
        decision_log = state.get("decision_log", [])
        incident_id = state.get("incident_id")

        query = " ".join(str(e.payload) for e in events)
        raw_results = store.query(query, top_k=4) if query else []
        # exclude the incident's own record from "similar past incidents" —
        # querying against a corpus that includes yourself trivially matches
        # yourself, wasting a retrieval slot and the tokens spent injecting it
        results = [r for r in raw_results if r.doc_id != incident_id][:3]

        state["retrieved_context"] = [
            {
                "doc_id": r.doc_id,
                "doc_type": r.doc_type,
                "text": r.text[:400],
                "score": r.score,
            }
            for r in results
        ]
        decision_log.append(
            f"RETRIEVAL: found {len(results)} similar doc(s): "
            f"{[r.doc_id for r in results]}"
        )
        state["decision_log"] = decision_log
        return state

    return run_retrieval
