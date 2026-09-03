"""The provider seam.

Everything above this module speaks only in `Message`, `CompletionRequest` and
JSON Schema. Everything below it speaks HTTP to a vendor. That is the entire
portability story, and it holds only as long as nothing above the line imports
anything below it except through `Provider`.

Deliberately absent from this interface:

  * vendor SDKs. Every adapter is plain HTTP over httpx. SDKs bring their own
    retry semantics, their own auth handling and their own upgrade cadence, and
    swapping one vendor for another then means swapping a dependency tree too.
  * streaming, for now. It is a transport concern that the API layer can add
    per provider without the orchestrator knowing.
  * tool calling as a first-class concept. Tools are how *we* would let a model
    act; this system does not let the model act. It generates prose and it
    extracts structure, and both of those are expressible as structured output.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any

from jsonschema import Draft202012Validator

from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    Message,
    SchemaViolation,
    StructuredRequest,
    StructuredResult,
)


class Provider(ABC):
    """One vendor, one class, no leakage.

    Subclasses implement `_complete` and, where the vendor supports it,
    `_structured_native`. The compatibility path in `structured` handles every
    provider that does not: a schema-in-prompt instruction, a tolerant JSON
    extraction, validation, and exactly one repair attempt.
    """

    id: str = "abstract"
    default_model: str = ""

    def __init__(self, model: str | None = None, **options: Any):
        self.model = model or self.default_model
        self.options = options

    # -- capability declaration ---------------------------------------------

    @abstractmethod
    def capabilities(self) -> set[Capability]: ...

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities()

    # -- vendor calls to implement ------------------------------------------

    @abstractmethod
    def _complete(self, request: CompletionRequest) -> CompletionResult: ...

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        raise NotImplementedError

    def _embed(self, texts: list[str]) -> EmbeddingResult:
        raise NotImplementedError(f"{self.id} does not provide embeddings")

    # -- public surface ------------------------------------------------------

    def complete(self, request: CompletionRequest) -> CompletionResult:
        started = time.perf_counter()
        result = self._complete(request)
        result.latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
        return result

    def embed(self, texts: list[str]) -> EmbeddingResult:
        return self._embed(texts)

    def structured(self, request: StructuredRequest) -> StructuredResult:
        """Return schema-valid data, whatever the provider's native support.

        The uniform contract matters more than the mechanism: extraction code
        upstream must never branch on which vendor is answering.
        """
        if self.supports(Capability.NATIVE_STRUCTURED):
            result = self._structured_native(request)
            _validate(self.id, result.data, request.schema)
            return result
        return self._structured_by_prompt(request)

    # -- compatibility path --------------------------------------------------

    def _structured_by_prompt(self, request: StructuredRequest) -> StructuredResult:
        instruction = _schema_instruction(request.schema)
        messages = [*request.messages, Message("user", instruction)]
        started = time.perf_counter()

        completion = self._complete(
            CompletionRequest(
                messages=messages,
                system=request.system,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                metadata=request.metadata,
            )
        )
        repaired = False
        try:
            data = _extract_json(self.id, completion.text)
            _validate(self.id, data, request.schema)
        except SchemaViolation as first:
            # One repair attempt, with the validator's own complaint fed back.
            # More than one is a sign the schema is wrong, not the model, and
            # burning three calls to discover that helps nobody.
            repaired = True
            repair = [
                *messages,
                Message("assistant", completion.text),
                Message(
                    "user",
                    "That response was not valid. "
                    f"{first.args[0]}. "
                    "Reply with the corrected JSON object and nothing else.",
                ),
            ]
            completion = self._complete(
                CompletionRequest(
                    messages=repair,
                    system=request.system,
                    max_tokens=request.max_tokens,
                    temperature=0.0,
                    metadata=request.metadata,
                )
            )
            data = _extract_json(self.id, completion.text)
            _validate(self.id, data, request.schema)

        return StructuredResult(
            data=data,
            provider=self.id,
            model=self.model,
            usage=completion.usage,
            latency_ms=int((time.perf_counter() - started) * 1000),
            repaired=repaired,
        )


# ---------------------------------------------------------------------------
# helpers shared by every adapter
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL)


def _extract_json(provider: str, text: str) -> dict[str, Any]:
    """Get a JSON object out of a model's answer without being precious about it.

    Models wrap JSON in fences, prefix it with "Here is the JSON:", and
    occasionally add a closing remark. All three are recoverable and none of
    them is worth a failed session.
    """
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise SchemaViolation(provider, "no JSON object found in the response", text)
        candidate = candidate[start : end + 1]

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise SchemaViolation(provider, f"response was not valid JSON: {exc.msg}", text) from exc
    if not isinstance(data, dict):
        raise SchemaViolation(provider, "response JSON was not an object", text)
    return data


def _validate(provider: str, data: dict[str, Any], schema: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise SchemaViolation(provider, f"field {where}: {first.message}", json.dumps(data))


def _schema_instruction(schema: dict[str, Any]) -> str:
    return (
        "Reply with a single JSON object and nothing else: no prose before or "
        "after it, no code fence, no explanation. It must validate against this "
        "JSON Schema:\n\n"
        f"{json.dumps(schema, indent=2)}\n\n"
        "Use null for anything the conversation does not establish. Do not "
        "invent values to fill required fields."
    )
