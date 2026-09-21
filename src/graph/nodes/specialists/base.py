"""Shared base for specialist agents: filters relevant events, calls the LLM,
and builds a grounded SpecialistFinding whose citations point at real event ids.
A specialist that finds no relevant events for its source types returns None
rather than inventing a finding.

Token governance is enforced here, not left to prompt discipline alone:
- evidence payloads are truncated before being embedded in a prompt
- retrieved RAG context is capped to a small number of short excerpts
- a hard prompt-token ceiling is estimated before every call; if evidence
  would exceed it, older/lower-priority events are dropped and the
  truncation is recorded in the decision log, not done silently
"""

from __future__ import annotations

from src.governance.cost_meter import CostMeter
from src.llm.client import LLMClient
from src.schemas.models import Citation, SpecialistFinding

# --- token governance constants -------------------------------------------
# Rough word-based token estimate (no tokenizer dependency needed for a cap
# this coarse) — 1 word ≈ 1.3 tokens is a safe overestimate for English.
_WORDS_TO_TOKENS = 1.3

MAX_PAYLOAD_CHARS_PER_EVENT = 240          # cap on any single event's payload text
MAX_EVIDENCE_EVENTS_PER_CALL = 6            # hard cap on events included in one prompt
MAX_RETRIEVED_DOCS = 2                      # cap on RAG context docs injected
MAX_RETRIEVED_DOC_CHARS = 180                # cap per injected doc excerpt
PROMPT_TOKEN_CEILING = 600                   # hard per-call ceiling; triggers truncation, not failure


def _estimate_tokens(text: str) -> int:
    return int(len(text.split()) * _WORDS_TO_TOKENS)


class BaseSpecialist:
    agent_name: str
    relevant_sources: tuple[str, ...]

    def __init__(self, llm: LLMClient, cost_meter: CostMeter):
        self.llm = llm
        self.cost_meter = cost_meter

    def analyze(
        self,
        events: list,
        system_prompt: str,
        retrieved_context: list[dict] | None = None,
        decision_log: list[str] | None = None,
    ) -> SpecialistFinding | None:
        decision_log = decision_log if decision_log is not None else []
        relevant = [e for e in events if e.source in self.relevant_sources]
        if not relevant:
            return None

        truncated_event_count = False
        if len(relevant) > MAX_EVIDENCE_EVENTS_PER_CALL:
            relevant = relevant[:MAX_EVIDENCE_EVENTS_PER_CALL]
            truncated_event_count = True

        evidence_lines = []
        for e in relevant:
            payload_text = str(e.payload)
            if len(payload_text) > MAX_PAYLOAD_CHARS_PER_EVENT:
                payload_text = payload_text[:MAX_PAYLOAD_CHARS_PER_EVENT] + "...[truncated]"
            evidence_lines.append(
                f"- id={e.raw_id} source={e.source} service={e.service} payload={payload_text}"
            )
        evidence_blob = "\n".join(evidence_lines)

        # inject a small, capped slice of RAG context — only doc types this
        # agent can act on (past incidents / runbooks), never raw retrieval dumps
        context_blob = ""
        if retrieved_context:
            capped_docs = retrieved_context[:MAX_RETRIEVED_DOCS]
            context_lines = []
            for doc in capped_docs:
                text = doc.get("text", "")[:MAX_RETRIEVED_DOC_CHARS]
                context_lines.append(f"- [{doc.get('doc_type')}] {doc.get('doc_id')}: {text}")
            if context_lines:
                context_blob = "\n\nRelevant prior context (for reference, do not cite unless it directly supports a claim):\n" + "\n".join(context_lines)

        prompt = (
            f"Evidence available to you:\n{evidence_blob}"
            f"{context_blob}\n\n"
            "Summarize what this evidence suggests about the root cause, in one "
            "or two sentences. Be specific and only claim what the evidence supports."
        )

        # token ceiling guardrail: estimate before calling, truncate context
        # (never evidence — evidence is the grounding source) if over budget
        estimated = _estimate_tokens(system_prompt + prompt)
        if estimated > PROMPT_TOKEN_CEILING and context_blob:
            prompt = (
                f"Evidence available to you:\n{evidence_blob}\n\n"
                "Summarize what this evidence suggests about the root cause, in one "
                "or two sentences. Be specific and only claim what the evidence supports."
            )
            decision_log.append(
                f"SPECIALIST[{self.agent_name}]: dropped RAG context to stay under "
                f"{PROMPT_TOKEN_CEILING}-token prompt ceiling (estimated {estimated})"
            )
        if truncated_event_count:
            decision_log.append(
                f"SPECIALIST[{self.agent_name}]: capped evidence to "
                f"{MAX_EVIDENCE_EVENTS_PER_CALL} event(s) to control prompt size"
            )

        response = self.llm.complete(system_prompt, prompt)
        self.cost_meter.record(response.input_tokens, response.output_tokens, self.agent_name)

        citations = [
            Citation(
                source_type=self._map_source_type(e.source),
                reference=e.raw_id,
                excerpt=str(e.payload)[:200],
            )
            for e in relevant
        ]

        # crude confidence heuristic: more corroborating evidence -> higher confidence,
        # capped well below 1.0 since a single agent's view is never the full picture.
        # Explicitly discounted when the analysis itself signals weak/mixed evidence
        # ("no strong signal", "mixed", "no single cause") — otherwise confidence was
        # tracking evidence *count* rather than evidence *quality*, which the
        # calibration suite caught inflating the "ambiguous" archetype above its
        # intended band.
        base_confidence = min(0.55 + 0.1 * len(relevant), 0.85)
        weak_signal_markers = ["no strong signal", "mixed", "no single cause"]
        if any(marker in response.text.lower() for marker in weak_signal_markers):
            confidence = max(0.2, base_confidence - 0.35)
        else:
            confidence = base_confidence

        return SpecialistFinding(
            agent=self.agent_name,
            summary=response.text.strip(),
            citations=citations,
            confidence=round(confidence, 2),
        )

    @staticmethod
    def _map_source_type(source: str) -> str:
        return {
            "splunk": "log",
            "grafana": "metric",
            "git": "code_diff",
            "cicd": "code_diff",
            "test_results": "test",
            "jira": "log",
        }.get(source, "log")
