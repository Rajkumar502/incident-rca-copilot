"""
Pluggable LLM client.

GeminiClient wraps the real API (requires GEMINI_API_KEY + network access).
MockLLMClient is deterministic and heuristic-based — no network call — so the
full graph can be exercised end-to-end in CI or in a sandboxed environment
without live credentials. select_llm_client() picks Gemini if a key is
present, otherwise falls back to the mock so `analyze-incident` always works.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from src import config


@dataclass
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system: str, prompt: str) -> LLMResponse:
        ...


class GeminiClient(LLMClient):
    """Real Gemini Flash-Lite client. Requires GEMINI_API_KEY and network access."""

    def __init__(self, model: str = "gemini-flash-lite-latest"):
        api_key = config.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY not set")
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise RuntimeError(
                "google-generativeai not installed. pip install google-generativeai"
            ) from e
        genai.configure(api_key=api_key)
        self._genai = genai
        self._model = genai.GenerativeModel(model)

    def complete(self, system: str, prompt: str) -> LLMResponse:
        full_prompt = f"{system}\n\n{prompt}"
        result = self._model.generate_content(full_prompt)
        text = result.text
        # Gemini's usage_metadata gives real token counts when available.
        usage = getattr(result, "usage_metadata", None)
        input_tokens = getattr(usage, "prompt_token_count", len(full_prompt.split()))
        output_tokens = getattr(usage, "candidates_token_count", len(text.split()))
        return LLMResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


class MockLLMClient(LLMClient):
    """
    Deterministic, heuristic-based stand-in for the LLM.

    Doesn't call any network API — inspects the prompt's embedded evidence
    (event payloads) with simple keyword rules and returns a plausible,
    grounded-looking analysis. This exists so the graph, schemas, and gates
    can be tested end-to-end without a live API key or network access —
    swap in GeminiClient for real runs; nothing else in the graph changes.
    """

    def complete(self, system: str, prompt: str) -> LLMResponse:
        text = self._heuristic_response(prompt)
        return LLMResponse(
            text=text,
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
        )

    @staticmethod
    def _heuristic_response(prompt: str) -> str:
        lowered = prompt.lower()
        if "timeouterror" in lowered or "blocked event loop" in lowered:
            return (
                "The deploy introduced a synchronous call that blocks the event "
                "loop, causing request timeouts and the observed latency spike."
            )
        if "switch payment retry to sync call" in lowered:
            return (
                "This commit changed the retry path from async to a synchronous "
                "call, which is the kind of change known to block the event loop "
                "under load."
            )
        if "bump feature-flag defaults" in lowered:
            return (
                "This commit changed feature-flag defaults, which can expose an "
                "undertested flag-off code path."
            )
        if "connection pool exhausted" in lowered:
            return (
                "A downstream job is holding long-running transactions, "
                "exhausting the database connection pool."
            )
        if "assertionerror: discount applied twice" in lowered:
            return (
                "The discount-stacking logic double-applies a discount under "
                "a specific cart state — this looks like a genuine application bug."
            )
        if "rerun_pass" in lowered and "true" in lowered:
            return (
                "The failing test passed on rerun with no related code or "
                "infra change nearby — consistent with test flakiness, not a regression."
            )
        if "dns resolution slow" in lowered and "docs update" in lowered:
            return (
                "Evidence is mixed: an unrelated documentation commit and a slow "
                "upstream DNS warning near the error-rate spike. No single cause "
                "is clearly supported by the available evidence."
            )
        return "No strong signal found in the available evidence for this source."


def select_llm_client() -> LLMClient:
    """Gemini if a key is present and the SDK is importable, else the mock."""
    if config.has_gemini_credentials():
        try:
            return GeminiClient()
        except Exception:
            pass
    return MockLLMClient()
