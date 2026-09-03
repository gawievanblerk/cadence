from __future__ import annotations

import json
import os
from typing import Any

import httpx

from cadence.llm.base import Provider
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    ProviderError,
    SchemaViolation,
    StructuredRequest,
    StructuredResult,
    Usage,
)


class OllamaProvider(Provider):
    """A locally hosted model, reached over the Ollama HTTP API.

    This adapter exists to make one thing concrete rather than aspirational:
    the option to run the whole platform without client text ever leaving the
    operator's own infrastructure. It is the same interface as the hosted
    providers, so choosing it is a configuration decision.

    Recent Ollama supports a `format` field carrying a JSON Schema, which is
    why it declares native structured output.
    """

    id = "ollama"
    default_model = "llama3.1:8b"
    embedding_model = "nomic-embed-text"

    def __init__(self, model: str | None = None, **options: Any):
        super().__init__(model, **options)
        self.base_url = (
            options.get("base_url")
            or os.environ.get("OLLAMA_BASE_URL")
            or "http://localhost:11434"
        ).rstrip("/")
        self.timeout = float(options.get("timeout", 180))

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.NATIVE_STRUCTURED,
            Capability.EMBEDDINGS,
            Capability.SYSTEM_PROMPT,
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        except httpx.HTTPError as exc:
            raise ProviderError(self.id, f"cannot reach Ollama at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(self.id, f"HTTP {response.status_code}: {response.text[:400]}")
        return response.json()

    def _messages(self, request: CompletionRequest | StructuredRequest) -> list[dict[str, str]]:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        return messages

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        body = self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": self._messages(request),
                "stream": False,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_tokens,
                },
            },
        )
        return CompletionResult(
            text=body.get("message", {}).get("content", ""),
            provider=self.id,
            model=self.model,
            usage=Usage(body.get("prompt_eval_count", 0), body.get("eval_count", 0)),
            raw_finish_reason=body.get("done_reason"),
        )

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        body = self._post(
            "/api/chat",
            {
                "model": self.model,
                "messages": self._messages(request),
                "stream": False,
                "format": request.schema,
                "options": {"temperature": request.temperature},
            },
        )
        text = body.get("message", {}).get("content", "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaViolation(self.id, f"structured output was not JSON: {exc.msg}", text) from exc
        return StructuredResult(
            data=data,
            provider=self.id,
            model=self.model,
            usage=Usage(body.get("prompt_eval_count", 0), body.get("eval_count", 0)),
        )

    def _embed(self, texts: list[str]) -> EmbeddingResult:
        model = self.options.get("embedding_model", self.embedding_model)
        body = self._post("/api/embed", {"model": model, "input": texts})
        vectors = body.get("embeddings") or []
        return EmbeddingResult(vectors, self.id, model, len(vectors[0]) if vectors else 0)
