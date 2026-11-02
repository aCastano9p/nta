"""Fake Anthropic backend: answers from a function of the request, counts calls."""

import json
from collections.abc import Callable
from typing import Any


def message(payload: Any, *, stop_reason: str = "end_turn", input_tokens: int = 1000,
            output_tokens: int = 200, model: str = "fake") -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return {"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}


GOOD_TRIAGE = {"material": True, "event_type": "guidance", "reason": "Raised full-year guidance."}
DULL_TRIAGE = {"material": False, "event_type": "other", "reason": "Exhibit-only filing."}
GOOD_EXTRACTION = {
    "event_type": "guidance", "direction": "up", "entities": ["Apple Inc."],
    "numbers": [{"label": "FY revenue guidance", "value": 420.0, "unit": "USD billions"}],
    "cited_origin": None, "thesis": "Guidance raised above consensus.",
    "contrary_evidence": "Pre-announced by press.", "invalidation": "Stock below pre-filing price at close.",
    "p_up_at_horizon": 0.7,
}


class FakeBackend:
    def __init__(self, answer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 triage: Any = GOOD_TRIAGE, extraction: Any = GOOD_EXTRACTION) -> None:
        self.requests: list[dict[str, Any]] = []
        self._answer = answer
        self._triage, self._extraction = triage, extraction

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if self._answer:
            return self._answer(request)
        is_triage = "screen" in request["system"]
        return message(self._triage if is_triage else self._extraction, model=request["model"])
