from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Capability(str, Enum):
    """What a provider can do, declared rather than assumed.

    The router uses these to decide whether a role can be bound to a provider
    at all, and the adapters use them to decide whether structured output needs
    the compatibility path. Adding a provider means declaring capabilities, not
    editing the orchestrator.
    """

    CHAT = "chat"
    NATIVE_STRUCTURED = "native_structured"
    EMBEDDINGS = "embeddings"
    STREAMING = "streaming"
    SYSTEM_PROMPT = "system_prompt"


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class CompletionRequest:
    """Everything a provider needs, in vendor-neutral terms.

    Note what is *not* here: no provider-specific parameters, no vendor message
    formats, no tool schemas in anyone's dialect. If a feature cannot be
    expressed here it does not get used, because the moment orchestration code
    reaches for a vendor-specific field the portability claim is over.
    """

    messages: list[Message]
    system: str | None = None
    max_tokens: int = 800
    temperature: float = 0.4
    stop: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructuredRequest:
    """A completion whose answer must conform to a JSON Schema."""

    messages: list[Message]
    schema: dict[str, Any]
    system: str | None = None
    schema_name: str = "response"
    max_tokens: int = 800
    temperature: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class CompletionResult:
    text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    raw_finish_reason: str | None = None


@dataclass
class StructuredResult:
    data: dict[str, Any]
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0
    repaired: bool = False
    """True when the provider's first answer did not validate and was re-asked."""


@dataclass
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dimensions: int


class ProviderError(Exception):
    """Any failure attributable to the provider rather than to our own request."""

    def __init__(self, provider: str, message: str, *, retryable: bool = True):
        self.provider = provider
        self.retryable = retryable
        super().__init__(f"[{provider}] {message}")


class SchemaViolation(ProviderError):
    """The provider answered, but not with the shape we asked for."""

    def __init__(self, provider: str, message: str, payload: str = ""):
        self.payload = payload
        super().__init__(provider, message, retryable=True)
