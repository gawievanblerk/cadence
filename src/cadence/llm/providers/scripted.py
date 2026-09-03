"""The offline reference provider.

This is not a mock in the testing sense. It is a real implementation of the
`Provider` interface that happens to run on rules instead of weights, and it
earns its place three times over:

  * The whole platform runs, end to end, with no API key and no network. That
    is what makes `make demo` honest and CI cheap.
  * Content changes can be regression-tested deterministically. When a
    clinician edits a threshold, the test suite can assert that the pathway
    changed, without a model in the loop adding variance to the answer.
  * It is the control in the provider-swap comparison. If the engine produces
    the same clinical decisions under `scripted` as under a hosted model, the
    decisions are not coming from the model.

Its counsellor output is deliberately plain: it delivers the clinician's
authored text with minimal framing. That is the floor the system degrades to,
and a floor worth being able to see.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from cadence.llm.base import Provider
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    StructuredRequest,
    StructuredResult,
    Usage,
)

_YES = re.compile(r"\b(yes|yeah|yep|ja|i did|once|twice|a few|one|only one|afraid so)\b", re.I)
_NO = re.compile(r"\b(no|nope|not at all|none|nothing|never|not once|clean)\b", re.I)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_SPLIT = re.compile(r"\s*(?:;|,\s*(?:and\s+)?|\.\s+|\band then\b|\balso\b|\n[-*]\s*|\n)\s*")


class ScriptedProvider(Provider):
    id = "scripted"
    default_model = "demo"

    def capabilities(self) -> set[Capability]:
        return {Capability.CHAT, Capability.NATIVE_STRUCTURED, Capability.SYSTEM_PROMPT}

    # -- conversation --------------------------------------------------------

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        authored = request.metadata.get("authored_text")
        recall = request.metadata.get("recall_summary")
        kind = request.metadata.get("step_kind", "say")

        if authored:
            text = str(authored).strip()
            if kind == "reflect" and recall:
                text = str(recall).strip()
        elif recall:
            text = str(recall).strip()
        else:
            text = _last_user(request) or "Go on."

        return CompletionResult(
            text=text,
            provider=self.id,
            model=self.model,
            usage=Usage(_tokens(request), _tokens_text(text)),
            raw_finish_reason="stop",
        )

    # -- extraction ----------------------------------------------------------

    def _structured_native(self, request: StructuredRequest) -> StructuredResult:
        utterance = request.metadata.get("client_utterance") or _last_user(request) or ""
        data = _extract(request.schema, utterance)
        return StructuredResult(
            data=data,
            provider=self.id,
            model=self.model,
            usage=Usage(_tokens_text(utterance), 40),
        )


def _last_user(request: CompletionRequest | StructuredRequest) -> str | None:
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content
    return None


def _tokens(request: CompletionRequest | StructuredRequest) -> int:
    return sum(_tokens_text(m.content) for m in request.messages)


def _tokens_text(text: str) -> int:
    return max(1, len(text) // 4)


def _extract(schema: dict[str, Any], utterance: str) -> dict[str, Any]:
    """Heuristic structured extraction, driven by the schema rather than by step ids.

    The heuristics are crude on purpose. They are here to exercise the
    pipeline's shape, not to be a substitute for a model - the point is that
    the orchestrator cannot tell the difference, which is the property the
    architecture is claiming.
    """
    out: dict[str, Any] = {}
    for name, spec in (schema.get("properties") or {}).items():
        if name == "_confidence":
            # A rule-based extractor knows exactly how sure it is: reasonably,
            # for values it matched, and never enough to be treated as certain.
            out[name] = 0.85
        elif name == "_verbatim":
            out[name] = utterance.strip()[:300] or None
        else:
            out[name] = _extract_field(name, spec, utterance)
    return out


def _extract_field(name: str, spec: dict[str, Any], utterance: str) -> Any:
    kinds = spec.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    kinds = [k for k in kinds if k and k != "null"]
    kind = kinds[0] if kinds else "string"
    text = utterance.strip()

    if not text:
        return None

    if kind == "boolean":
        # Order matters: "no, not once" must not match on the "once" in _YES.
        if _NO.search(text):
            return False
        if _YES.search(text):
            return True
        return None

    if kind in ("integer", "number"):
        found = _NUMBER.findall(text)
        value = float(found[0]) if found else _word_number(text)
        if value is None:
            return None
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if lo is not None:
            value = max(float(lo), value)
        if hi is not None:
            value = min(float(hi), value)
        return int(value) if kind == "integer" else value

    if kind == "array":
        parts = [p.strip(" .") for p in _SPLIT.split(text) if len(p.strip(" .")) > 3]
        return parts[:6] or [text]

    if spec.get("format") == "date" or "date" in name:
        iso = _ISO_DATE.search(text)
        if iso:
            return iso.group(1)
        relative = _relative_date(text)
        if relative:
            return relative.isoformat()
        return None

    if spec.get("enum"):
        for option in spec["enum"]:
            if str(option).lower() in text.lower():
                return option
        return None

    return text


_WORD_NUMBERS = {
    "none": 0, "zero": 0, "nought": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "a couple": 2, "a few": 3, "half a dozen": 6, "a dozen": 12, "a packet": 20, "a pack": 20,
}


def _word_number(text: str) -> float | None:
    """People say 'about twenty a day' far more often than they say '20'."""
    lowered = text.lower()
    for phrase, value in sorted(_WORD_NUMBERS.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered):
            return float(value)
    return None


def _relative_date(text: str) -> date | None:
    match = re.search(r"in\s+(\d+)\s+(day|week)s?", text, re.I)
    if match:
        n = int(match.group(1))
        return date.today() + timedelta(days=n * (7 if match.group(2).lower() == "week" else 1))
    if re.search(r"\btomorrow\b", text, re.I):
        return date.today() + timedelta(days=1)
    if re.search(r"\bnext (monday|week)\b", text, re.I):
        return date.today() + timedelta(days=7)
    return None


class HashingEmbedder(Provider):
    """A deterministic, offline embedder.

    Hashed character n-grams projected onto a fixed-width vector. It is not
    semantic in the way a trained embedding model is, but it is stable, free,
    dependency-light, and good enough for the retrieval layer's own tests. In
    any real deployment the embedder role is bound to a hosted or local model;
    binding it here is what lets the suite run offline.
    """

    id = "hashing"
    default_model = "local-384"

    def __init__(self, model: str | None = None, **options: Any):
        super().__init__(model, **options)
        self.dimensions = int(options.get("dimensions", 384))

    def capabilities(self) -> set[Capability]:
        return {Capability.EMBEDDINGS}

    def _complete(self, request: CompletionRequest) -> CompletionResult:  # pragma: no cover
        raise NotImplementedError("the hashing provider only produces embeddings")

    def _embed(self, texts: list[str]) -> EmbeddingResult:
        import hashlib
        import math

        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            tokens = re.findall(r"[a-z0-9]+", text.lower())
            grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:], strict=False)]
            for gram in grams:
                digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return EmbeddingResult(vectors, self.id, self.model, self.dimensions)
