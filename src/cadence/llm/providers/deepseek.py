from __future__ import annotations

import os
from typing import Any

from cadence.llm.providers.openai import OpenAIProvider
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    Message,
    ProviderError,
    StructuredRequest,
    StructuredResult,
    Usage,
)


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek, which speaks the OpenAI wire format with two caveats.

    It supports `response_format: json_object` but not `json_schema`, so it
    gets JSON mode plus our own validation and repair rather than a provider
    guarantee. And it has no embeddings endpoint, so binding the embedder role
    to it is rejected at configuration time rather than at 3am.
    """

    id = "deepseek"
    default_model = "deepseek-chat"
    base_url = "https://api.deepseek.com/v1"

    def __init__(self, model: str | None = None, **options: Any):
        super().__init__(model, **options)
        self.api_key = options.get("api_key") or os.environ.get("DEEPSEEK_API_KEY", "")

    def capabilities(self) -> set[Capability]:
        return {Capability.CHAT, Capability.STREAMING, Capability.SYSTEM_PROMPT}

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        raise NotImplementedError

    def _embed(self, texts: list[str]) -> Any:
        raise ProviderError(self.id, "DeepSeek has no embeddings endpoint", retryable=False)

    def _structured_by_prompt(self, request: StructuredRequest) -> StructuredResult:
        """Use JSON mode where available, then fall back to the generic path.

        JSON mode guarantees parseable JSON but not conformant JSON, so the
        base class's validate-and-repair still runs. Belt and braces, again.
        """
        from cadence.llm.base import _extract_json, _schema_instruction, _validate

        messages = [*request.messages]
        messages.append(Message("user", _schema_instruction(request.schema)))
        payload = self._payload(
            CompletionRequest(
                messages=messages,
                system=request.system,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
        )
        payload["response_format"] = {"type": "json_object"}
        body = self._post("/chat/completions", payload)
        text = body["choices"][0]["message"]["content"] or ""
        usage = body.get("usage") or {}

        try:
            data = _extract_json(self.id, text)
            _validate(self.id, data, request.schema)
        except Exception:
            # Hand off to the generic prompt-and-repair path, which knows how
            # to re-ask with the validator's complaint attached.
            return super()._structured_by_prompt(request)

        return StructuredResult(
            data=data,
            provider=self.id,
            model=body.get("model", self.model),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
        )
