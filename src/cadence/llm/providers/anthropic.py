from __future__ import annotations

import os
from typing import Any

import httpx

from cadence.llm.base import Provider
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    ProviderError,
    SchemaViolation,
    StructuredRequest,
    StructuredResult,
    Usage,
)


class AnthropicProvider(Provider):
    """Anthropic Messages API.

    Two vendor specifics are absorbed here and nowhere else:

      * the system prompt is a top-level parameter, not a message with
        `role: system`;
      * structured output is expressed as a single-tool forced tool call, which
        is Anthropic's native mechanism and is stricter than asking for JSON.

    Neither of those facts is visible above `Provider`.
    """

    id = "anthropic"
    default_model = "claude-opus-5"
    base_url = "https://api.anthropic.com/v1"
    api_version = "2023-06-01"

    # Two generations of the Messages API meet here. From Opus 4.7 onward
    # (Opus 4.7, 4.8 and 5, Sonnet 5, Fable) the model rejects sampling
    # parameters and thinks by default, and the thinking shares `max_tokens`
    # with the words the client reads. The models below still take a
    # temperature and do not think unless asked. Anything not listed is
    # treated as current, so a new release works without an edit here.
    _SAMPLING_MODEL_PREFIXES = (
        "claude-3",
        "claude-haiku-4-5",
        "claude-sonnet-4-",
        "claude-opus-4-0",
        "claude-opus-4-1",
        "claude-opus-4-2",
        "claude-opus-4-5",
        "claude-opus-4-6",
    )

    def __init__(self, model: str | None = None, **options: Any):
        super().__init__(model, **options)
        self.api_key = options.get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        self.base_url = options.get("base_url") or self.base_url
        self.timeout = float(options.get("timeout", 60))

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.NATIVE_STRUCTURED,
            Capability.STREAMING,
            Capability.SYSTEM_PROMPT,
        }

    def _takes_temperature(self) -> bool:
        return self.model.startswith(self._SAMPLING_MODEL_PREFIXES)

    def _sampling(self, temperature: float) -> dict[str, Any]:
        """The generation-specific part of a request.

        Older models get the temperature the caller asked for. Current models
        get low effort instead: the turns this system asks for are short and
        the clinical decisions are made elsewhere, so the token budget should
        go on the reply, not on deliberation.
        """
        if self._takes_temperature():
            return {"temperature": temperature}
        return {"output_config": {"effort": "low"}}

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderError(self.id, "no API key configured", retryable=False)
        return {
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.id, f"transport failure: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                self.id,
                f"HTTP {response.status_code}: {response.text[:400]}",
                retryable=response.status_code in (429,) or response.status_code >= 500,
            )
        return response.json()

    @staticmethod
    def _messages(request: CompletionRequest | StructuredRequest) -> list[dict[str, Any]]:
        # Anthropic rejects consecutive same-role messages; merge them rather
        # than making callers think about it.
        merged: list[dict[str, Any]] = []
        for m in request.messages:
            role = "assistant" if m.role == "assistant" else "user"
            if merged and merged[-1]["role"] == role:
                merged[-1]["content"] += "\n\n" + m.content
            else:
                merged.append({"role": role, "content": m.content})
        return merged or [{"role": "user", "content": "(no input)"}]

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(request),
            "max_tokens": request.max_tokens,
            **self._sampling(request.temperature),
        }
        if request.system:
            payload["system"] = request.system
        if request.stop:
            payload["stop_sequences"] = request.stop

        body = self._post("/messages", payload)
        text = "".join(block.get("text", "") for block in body.get("content", []))
        usage = body.get("usage") or {}
        return CompletionResult(
            text=text,
            provider=self.id,
            model=body.get("model", self.model),
            usage=Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
            raw_finish_reason=body.get("stop_reason"),
        )

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        tool_name = request.schema_name or "record"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(request),
            "max_tokens": request.max_tokens,
            **self._sampling(request.temperature),
            "tools": [
                {
                    "name": tool_name,
                    "description": "Record the extracted values.",
                    "input_schema": request.schema,
                }
            ],
            "tool_choice": {"type": "tool", "name": tool_name},
        }
        if request.system:
            payload["system"] = request.system

        body = self._post("/messages", payload)
        for block in body.get("content", []):
            if block.get("type") == "tool_use":
                usage = body.get("usage") or {}
                return StructuredResult(
                    data=block.get("input") or {},
                    provider=self.id,
                    model=body.get("model", self.model),
                    usage=Usage(usage.get("input_tokens", 0), usage.get("output_tokens", 0)),
                )
        raise SchemaViolation(self.id, "forced tool call returned no tool_use block")
