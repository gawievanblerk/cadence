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
    StructuredRequest,
    StructuredResult,
    Usage,
)


class OpenAIProvider(Provider):
    """OpenAI Chat Completions and Embeddings, over plain HTTP.

    Structured output uses `response_format: json_schema` with `strict: true`,
    which is the only mode that actually guarantees shape. Strict mode has
    conditions the general schema draft does not - every property must be
    required and `additionalProperties` must be false - so schemas are adapted
    on the way out rather than authored twice.
    """

    id = "openai"
    default_model = "gpt-4o-mini"
    base_url = "https://api.openai.com/v1"
    embedding_model = "text-embedding-3-small"

    def __init__(self, model: str | None = None, **options: Any):
        super().__init__(model, **options)
        self.api_key = options.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = options.get("base_url") or self.base_url
        self.timeout = float(options.get("timeout", 60))

    def capabilities(self) -> set[Capability]:
        return {
            Capability.CHAT,
            Capability.NATIVE_STRUCTURED,
            Capability.EMBEDDINGS,
            Capability.STREAMING,
            Capability.SYSTEM_PROMPT,
        }

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ProviderError(self.id, "no API key configured", retryable=False)
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, request: CompletionRequest) -> dict[str, Any]:
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages += [{"role": m.role, "content": m.content} for m in request.messages]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.stop:
            payload["stop"] = request.stop
        return payload

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
                retryable=response.status_code in (408, 409, 429) or response.status_code >= 500,
            )
        return response.json()

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        body = self._post("/chat/completions", self._payload(request))
        choice = body["choices"][0]
        usage = body.get("usage") or {}
        return CompletionResult(
            text=choice["message"]["content"] or "",
            provider=self.id,
            model=body.get("model", self.model),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
            raw_finish_reason=choice.get("finish_reason"),
        )

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        payload = self._payload(
            CompletionRequest(
                messages=request.messages,
                system=request.system,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
        )
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": request.schema_name,
                "strict": True,
                "schema": strictify(request.schema),
            },
        }
        body = self._post("/chat/completions", payload)
        usage = body.get("usage") or {}
        return StructuredResult(
            data=json.loads(body["choices"][0]["message"]["content"]),
            provider=self.id,
            model=body.get("model", self.model),
            usage=Usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)),
        )

    def _embed(self, texts: list[str]) -> EmbeddingResult:
        model = self.options.get("embedding_model", self.embedding_model)
        body = self._post("/embeddings", {"model": model, "input": texts})
        vectors = [item["embedding"] for item in body["data"]]
        return EmbeddingResult(vectors, self.id, model, len(vectors[0]) if vectors else 0)


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a general JSON Schema to OpenAI's strict subset.

    Strict mode requires every property to appear in `required` and every
    object to forbid additional properties. Optionality is expressed by adding
    `null` to the type instead, which is semantically what our extraction
    schemas mean anyway: the field is always present, and its value is null
    when the conversation did not establish it.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)

    if out.get("type") == "object" and "properties" in out:
        props = {k: strictify(v) for k, v in out["properties"].items()}
        previously_required = set(out.get("required", list(props)))
        for name, prop in props.items():
            if name not in previously_required:
                prop_type = prop.get("type")
                if isinstance(prop_type, str) and prop_type != "null":
                    prop["type"] = [prop_type, "null"]
                elif isinstance(prop_type, list) and "null" not in prop_type:
                    prop["type"] = [*prop_type, "null"]
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False

    if "items" in out:
        out["items"] = strictify(out["items"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in out:
            out[key] = [strictify(s) for s in out[key]]
    return out
