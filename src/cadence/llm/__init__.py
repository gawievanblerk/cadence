from cadence.llm.base import Provider
from cadence.llm.redaction import EgressPolicy, redact
from cadence.llm.router import ModelRouter, RouterFactory, available_providers, register
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    Message,
    ProviderError,
    SchemaViolation,
    StructuredRequest,
    StructuredResult,
    Usage,
)

# Importing the adapters registers them. Kept last so the registry is populated
# for anyone who imports `cadence.llm` and nothing else.
from cadence.llm import providers as _providers  # noqa: E402,F401

__all__ = [
    "Capability",
    "CompletionRequest",
    "CompletionResult",
    "EgressPolicy",
    "EmbeddingResult",
    "Message",
    "ModelRouter",
    "Provider",
    "ProviderError",
    "RouterFactory",
    "SchemaViolation",
    "StructuredRequest",
    "StructuredResult",
    "Usage",
    "available_providers",
    "redact",
    "register",
]
