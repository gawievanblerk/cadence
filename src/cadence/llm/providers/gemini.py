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


class GeminiProvider(Provider):
    """Google Gemini via the Generative Language API.

    Gemini differs from the other two in three ways that all get absorbed here:
    messages are `contents` with `parts`, the assistant role is called `model`,
    and its structured-output schema dialect is OpenAPI-flavoured rather than
    JSON Schema - so `to_gemini_schema` translates on the way out.
    """

    id = "gemini"
    default_model = "gemini-2.0-flash"
    base_url = "https://generativelanguage.googleapis.com/v1beta"
    embedding_model = "text-embedding-004"

    def __init__(self, model: str | None = None, **options: Any):
        super().__init__(model, **options)
        self.api_key = options.get("api_key") or os.environ.get("GEMINI_API_KEY", "")
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

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError(self.id, "no API key configured", retryable=False)
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
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
    def _contents(messages: Any) -> list[dict[str, Any]]:
        return [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
        ]

    def _body(self, request: CompletionRequest | StructuredRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "contents": self._contents(request.messages),
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
                "temperature": request.temperature,
            },
        }
        if request.system:
            body["systemInstruction"] = {"parts": [{"text": request.system}]}
        return body

    @staticmethod
    def _text(body: dict[str, Any]) -> str:
        candidates = body.get("candidates") or []
        if not candidates:
            raise SchemaViolation("gemini", "response contained no candidates")
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    @staticmethod
    def _usage(body: dict[str, Any]) -> Usage:
        meta = body.get("usageMetadata") or {}
        return Usage(meta.get("promptTokenCount", 0), meta.get("candidatesTokenCount", 0))

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        body = self._post(f"/models/{self.model}:generateContent", self._body(request))
        return CompletionResult(
            text=self._text(body),
            provider=self.id,
            model=self.model,
            usage=self._usage(body),
            raw_finish_reason=(body.get("candidates") or [{}])[0].get("finishReason"),
        )

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        payload = self._body(request)
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = to_gemini_schema(request.schema)
        body = self._post(f"/models/{self.model}:generateContent", payload)
        text = self._text(body)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaViolation(self.id, f"responseSchema output was not JSON: {exc.msg}", text) from exc
        return StructuredResult(
            data=data, provider=self.id, model=self.model, usage=self._usage(body)
        )

    def _embed(self, texts: list[str]) -> EmbeddingResult:
        model = self.options.get("embedding_model", self.embedding_model)
        vectors = []
        for text in texts:
            body = self._post(
                f"/models/{model}:embedContent",
                {"model": f"models/{model}", "content": {"parts": [{"text": text}]}},
            )
            vectors.append(body["embedding"]["values"])
        return EmbeddingResult(vectors, self.id, model, len(vectors[0]) if vectors else 0)


_GEMINI_TYPES = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


def to_gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate JSON Schema into Gemini's OpenAPI-subset dialect.

    Gemini rejects keys it does not know, so this is a whitelist translation
    rather than a passthrough with edits. Nullability is expressed with the
    `nullable` flag instead of a union type.
    """
    if not isinstance(schema, dict):
        return schema

    types = schema.get("type")
    nullable = False
    if isinstance(types, list):
        nullable = "null" in types
        concrete = [t for t in types if t != "null"]
        types = concrete[0] if concrete else "string"

    out: dict[str, Any] = {}
    if types:
        out["type"] = _GEMINI_TYPES.get(types, "STRING")
    if nullable:
        out["nullable"] = True
    if "description" in schema:
        out["description"] = schema["description"]
    if "enum" in schema:
        out["enum"] = [str(v) for v in schema["enum"]]
        out["type"] = "STRING"
    if "properties" in schema:
        out["properties"] = {k: to_gemini_schema(v) for k, v in schema["properties"].items()}
        required = [k for k in schema.get("required", []) if k in schema["properties"]]
        if required:
            out["required"] = required
    if "items" in schema:
        out["items"] = to_gemini_schema(schema["items"])
    return out
