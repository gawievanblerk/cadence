"""Role-to-provider routing, with fallback, retry and an egress record.

The orchestrator asks for a *role* - "give me a counsellor turn", "extract this
into that schema" - and never for a vendor. Which vendor answers is decided
here from configuration, so the answer to "how do we move from OpenAI to
Gemini" is: change one line of an environment file, and nothing else.

The router is also the only place a request crosses the trust boundary, which
makes it the right place to enforce the egress policy and to write the
processing register. Putting those checks in the adapters would mean every new
adapter re-implements them, and one day one of them will not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cadence.config import Binding, Settings
from cadence.llm.base import Provider
from cadence.llm.redaction import EgressPolicy, EgressRecord
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    EmbeddingResult,
    Message,
    ProviderError,
    StructuredRequest,
    StructuredResult,
)

log = logging.getLogger("cadence.llm")

_REGISTRY: dict[str, type[Provider]] = {}


def register(provider_cls: type[Provider]) -> type[Provider]:
    _REGISTRY[provider_cls.id] = provider_cls
    return provider_cls


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def build_provider(binding: Binding, **options: Any) -> Provider:
    try:
        cls = _REGISTRY[binding.provider]
    except KeyError as exc:
        raise ValueError(
            f"unknown provider {binding.provider!r}; known providers: {', '.join(available_providers())}"
        ) from exc
    return cls(binding.model or None, **options)


ROLE_REQUIREMENTS: dict[str, set[Capability]] = {
    "counsellor": {Capability.CHAT},
    "extractor": {Capability.CHAT},
    "summariser": {Capability.CHAT},
    "embedder": {Capability.EMBEDDINGS},
}


@dataclass
class RouterCall:
    role: str
    provider: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    fell_back: bool = False
    repaired: bool = False


class ModelRouter:
    def __init__(self, settings: Settings, *, max_attempts: int = 2):
        self.settings = settings
        self.max_attempts = max_attempts
        self.policy = EgressPolicy(settings.allowed_egress_providers, settings.redact_egress)
        self.calls: list[RouterCall] = []
        self.egress_log: list[EgressRecord] = []
        self._instances: dict[str, Provider] = {}
        self._verify_bindings()

    # -- configuration -------------------------------------------------------

    def _verify_bindings(self) -> None:
        """Fail at start-up, not mid-session.

        A role bound to a provider that cannot do the job - the embedder bound
        to DeepSeek, say - is a configuration error that should surface when
        the process boots.
        """
        problems = []
        for role, binding in self.settings.bindings.items():
            try:
                provider = self._provider(binding)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            required = ROLE_REQUIREMENTS.get(role, set())
            missing = required - provider.capabilities()
            if missing:
                problems.append(
                    f"role {role!r} is bound to {binding}, which does not support "
                    f"{', '.join(sorted(c.value for c in missing))}"
                )
            try:
                self.policy.check(binding.provider)
            except PermissionError as exc:
                problems.append(str(exc))
        if problems:
            raise ValueError("model routing is misconfigured:\n  - " + "\n  - ".join(problems))

    def _provider(self, binding: Binding) -> Provider:
        key = str(binding)
        if key not in self._instances:
            self._instances[key] = build_provider(binding)
        return self._instances[key]

    def _chain(self, role: str) -> list[Binding]:
        primary = self.settings.bindings.get(role)
        if primary is None:
            raise ValueError(f"no provider bound to role {role!r}")
        return [primary, *self.settings.fallbacks.get(role, [])]

    def binding_for(self, role: str) -> Binding:
        return self._chain(role)[0]

    # -- calls ---------------------------------------------------------------

    def complete(self, role: str, request: CompletionRequest, *, pseudonym: str = "-") -> CompletionResult:
        def run(provider: Provider, req: CompletionRequest) -> CompletionResult:
            return provider.complete(req)

        return self._dispatch(role, request, run, pseudonym)

    def structured(
        self, role: str, request: StructuredRequest, *, pseudonym: str = "-"
    ) -> StructuredResult:
        def run(provider: Provider, req: StructuredRequest) -> StructuredResult:
            return provider.structured(req)

        return self._dispatch(role, request, run, pseudonym)

    def embed(self, texts: list[str], *, role: str = "embedder") -> EmbeddingResult:
        binding = self.binding_for(role)
        self.policy.check(binding.provider)
        provider = self._provider(binding)
        result = provider.embed(texts)
        self.calls.append(
            RouterCall(role, provider.id, provider.model, 0, sum(len(t) // 4 for t in texts), 0)
        )
        return result

    def _dispatch(self, role: str, request: Any, run: Any, pseudonym: str) -> Any:
        chain = self._chain(role)
        last_error: Exception | None = None

        for index, binding in enumerate(chain):
            self.policy.check(binding.provider)
            provider = self._provider(binding)
            prepared = self._prepare(request, provider, role, pseudonym)

            for attempt in range(1, self.max_attempts + 1):
                try:
                    result = run(provider, prepared)
                except ProviderError as exc:
                    last_error = exc
                    log.warning(
                        "provider call failed role=%s provider=%s attempt=%s: %s",
                        role, binding.provider, attempt, exc,
                    )
                    if not exc.retryable:
                        break
                    continue
                self.calls.append(
                    RouterCall(
                        role=role,
                        provider=result.provider,
                        model=result.model,
                        latency_ms=getattr(result, "latency_ms", 0),
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        fell_back=index > 0,
                        repaired=getattr(result, "repaired", False),
                    )
                )
                return result

        raise ProviderError(
            chain[0].provider,
            f"role {role!r} exhausted {len(chain)} binding(s): {last_error}",
            retryable=False,
        )

    # -- privacy -------------------------------------------------------------

    def _prepare(self, request: Any, provider: Provider, role: str, pseudonym: str) -> Any:
        """Redact outbound text and write the processing register entry."""
        masked: dict[str, int] = {}
        messages: list[Message] = []
        for message in request.messages:
            report = self.policy.apply(message.content)
            for key, count in report.counts.items():
                masked[key] = masked.get(key, 0) + count
            messages.append(Message(message.role, report.text))

        prepared = _replace_messages(request, messages)
        self.egress_log.append(
            EgressRecord(
                provider=provider.id,
                model=provider.model,
                role=role,
                characters=sum(len(m.content) for m in messages),
                masked=masked,
                purpose=request.metadata.get("purpose", role),
                client_pseudonym=pseudonym,
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        )
        return prepared

    # -- observability -------------------------------------------------------

    def usage_summary(self) -> dict[str, Any]:
        by_provider: dict[str, dict[str, int]] = {}
        for call in self.calls:
            row = by_provider.setdefault(
                call.provider, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "repairs": 0}
            )
            row["calls"] += 1
            row["input_tokens"] += call.input_tokens
            row["output_tokens"] += call.output_tokens
            row["repairs"] += int(call.repaired)
        return by_provider


def _replace_messages(request: Any, messages: list[Message]) -> Any:
    import copy

    prepared = copy.copy(request)
    prepared.messages = messages
    return prepared


@dataclass
class RouterFactory:
    """Builds a router per request scope, so a swap needs no process restart."""

    settings: Settings
    _cached: ModelRouter | None = field(default=None, repr=False)

    def get(self) -> ModelRouter:
        if self._cached is None:
            self._cached = ModelRouter(self.settings)
        return self._cached

    def with_binding(self, role: str, spec: str) -> ModelRouter:
        return ModelRouter(self.settings.with_binding(role, spec))
