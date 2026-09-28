from __future__ import annotations

import json
from urllib import request

from contractops.infrastructure.document_parsing import (
    FindingCandidate,
    validate_model_candidates,
)


class OpenAICompatibleFindingSuggester:
    """Optional advisory-only model boundary; failures always degrade to no suggestions."""

    def __init__(self, base_url: str, model_name: str, *, timeout_seconds: float = 15) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model_name = model_name
        self._timeout_seconds = timeout_seconds

    def suggest(self, source_text: str) -> tuple[FindingCandidate, ...]:
        prompt = (
            "Return a JSON array only. Each item may contain kind (RISK or OBLIGATION), "
            "title, source_text, confidence, and optional normalized_value. source_text "
            "must be an exact substring of the supplied contract text. Do not make decisions "
            "or infer missing terms.\n\nContract text:\n" + source_text
        )
        body = json.dumps(
            {
                "model": self._model_name,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            },
            separators=(",", ":"),
        ).encode()
        call = request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(call, timeout=self._timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read())
            content = payload["choices"][0]["message"]["content"]
            raw = json.loads(content)
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return ()
        return validate_model_candidates(
            raw,
            source_text=source_text,
            model_name=self._model_name,
        )
