from __future__ import annotations

import pytest

from cadence.config import Binding, Settings
from cadence.llm.base import Provider
from cadence.llm.providers.gemini import to_gemini_schema
from cadence.llm.providers.openai import strictify
from cadence.llm.redaction import EgressPolicy, redact
from cadence.llm.router import ModelRouter, register
from cadence.llm.types import (
    Capability,
    CompletionRequest,
    CompletionResult,
    Message,
    ProviderError,
    SchemaViolation,
    StructuredRequest,
    Usage,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "confidence": {"type": ["integer", "null"], "minimum": 0, "maximum": 10},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["confidence"],
    "additionalProperties": False,
}


class ChattyProvider(Provider):
    """A provider with no native structured output, to exercise the compat path."""

    id = "test-chatty"
    default_model = "v1"

    def __init__(self, model=None, replies: list[str] | None = None, **options):
        super().__init__(model, **options)
        self.replies = replies or []
        self.calls: list[CompletionRequest] = []

    def capabilities(self) -> set[Capability]:
        return {Capability.CHAT}

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        self.calls.append(request)
        text = self.replies.pop(0) if self.replies else "{}"
        return CompletionResult(text, self.id, self.model, Usage(10, 5))


class BrokenProvider(Provider):
    id = "test-broken"
    default_model = "v1"

    def capabilities(self) -> set[Capability]:
        return {Capability.CHAT}

    def _complete(self, request: CompletionRequest) -> CompletionResult:
        raise ProviderError(self.id, "upstream is on fire", retryable=True)


class TestStructuredCompatibility:
    def test_json_in_a_code_fence_is_recovered(self) -> None:
        provider = ChattyProvider(replies=['Here you go:\n```json\n{"confidence": 4, "reasons": []}\n```'])
        result = provider.structured(
            StructuredRequest(messages=[Message("user", "x")], schema=SCHEMA)
        )
        assert result.data["confidence"] == 4
        assert result.repaired is False

    def test_invalid_output_is_repaired_once(self) -> None:
        provider = ChattyProvider(
            replies=['{"confidence": "four"}', '{"confidence": 4, "reasons": []}']
        )
        result = provider.structured(
            StructuredRequest(messages=[Message("user", "x")], schema=SCHEMA)
        )
        assert result.data["confidence"] == 4
        assert result.repaired is True
        assert len(provider.calls) == 2
        assert "not valid" in provider.calls[1].messages[-1].content

    def test_a_second_failure_surfaces_rather_than_looping(self) -> None:
        provider = ChattyProvider(replies=['{"confidence": "four"}', '{"confidence": "still four"}'])
        with pytest.raises(SchemaViolation):
            provider.structured(StructuredRequest(messages=[Message("user", "x")], schema=SCHEMA))


class TestSchemaAdaptation:
    def test_openai_strict_mode_requires_everything_and_nulls_the_optional(self) -> None:
        strict = strictify(SCHEMA)
        assert set(strict["required"]) == {"confidence", "reasons"}
        assert strict["additionalProperties"] is False
        assert "null" in strict["properties"]["reasons"]["type"]

    def test_gemini_dialect_translation(self) -> None:
        translated = to_gemini_schema(SCHEMA)
        assert translated["type"] == "OBJECT"
        assert translated["properties"]["confidence"]["type"] == "INTEGER"
        assert translated["properties"]["confidence"]["nullable"] is True
        assert translated["properties"]["reasons"]["items"]["type"] == "STRING"

    def test_translation_drops_keys_gemini_would_reject(self) -> None:
        translated = to_gemini_schema(SCHEMA)
        assert "additionalProperties" not in translated
        assert "minimum" not in translated["properties"]["confidence"]


class TestRedaction:
    def test_direct_identifiers_are_masked_with_typed_placeholders(self) -> None:
        report = redact(
            "Call me on 082 555 1234 or email sam@example.com, my ID is 8001015009087."
        )
        assert "[PHONE]" in report.text
        assert "[EMAIL]" in report.text
        assert "[SA_ID]" in report.text
        assert report.counts["email"] == 1

    def test_known_names_are_masked_but_the_sentence_survives(self) -> None:
        report = redact("I told Priya about it.", known_names=["Priya"])
        assert report.text == "I told [NAME] about it."

    def test_clean_text_is_untouched(self) -> None:
        report = redact("I had one on Friday after a bad meeting.")
        assert report.clean
        assert report.text == "I had one on Friday after a bad meeting."

    def test_clinical_data_is_not_over_masked(self) -> None:
        """Over-masking is a silent data-loss bug, not a safe default.

        A quit date turned into [PHONE] on the way to the extractor means the
        extraction returns null and the session records nothing, with no error
        anywhere.
        """
        for text in (
            "My quit date is 2026-09-17 and I smoke 20 a day.",
            "I had one on 2026-09-17, about 6 out of 10.",
            "Ten to fifteen a day since 2019-03-01.",
        ):
            assert redact(text).clean, text

    @pytest.mark.parametrize(
        "text", ["082 555 1234", "+27 82 555 1234", "(021) 555-1234", "0825551234"]
    )
    def test_real_phone_numbers_are_still_masked(self, text: str) -> None:
        assert redact(f"you can reach me on {text} any time").counts.get("phone") == 1

    def test_egress_policy_refuses_providers_not_on_the_allowlist(self) -> None:
        policy = EgressPolicy({"scripted"})
        policy.check("scripted")
        with pytest.raises(PermissionError):
            policy.check("some-vendor")


class TestRouting:
    def test_a_role_bound_to_an_incapable_provider_fails_at_startup(self) -> None:
        settings = Settings.from_env({"CADENCE_ROLE_EMBEDDER": "deepseek:deepseek-chat"})
        with pytest.raises(ValueError) as exc:
            ModelRouter(settings)
        assert "embeddings" in str(exc.value)

    def test_fallback_chain_is_used_when_the_primary_fails(self) -> None:
        register(BrokenProvider)
        register(ChattyProvider)
        settings = Settings.from_env(
            {
                "CADENCE_ROLE_COUNSELLOR": "test-broken:v1",
                "CADENCE_ROLE_COUNSELLOR_FALLBACK": "test-chatty:v1",
                "CADENCE_ALLOWED_EGRESS_PROVIDERS": "test-broken,test-chatty,scripted,hashing",
            }
        )
        router = ModelRouter(settings, max_attempts=1)
        result = router.complete("counsellor", CompletionRequest(messages=[Message("user", "hi")]))
        assert result.provider == "test-chatty"
        assert router.calls[-1].fell_back is True

    def test_every_outbound_call_is_recorded_with_what_was_masked(self) -> None:
        settings = Settings.from_env({})
        router = ModelRouter(settings)
        router.complete(
            "counsellor",
            CompletionRequest(
                messages=[Message("user", "my number is 082 555 1234")],
                metadata={"authored_text": "hello", "purpose": "test"},
            ),
            pseudonym="cli_x",
        )
        record = router.egress_log[-1]
        assert record.provider == "scripted"
        assert record.masked == {"phone": 1}
        assert record.client_pseudonym == "cli_x"

    def test_binding_swap_produces_a_new_router_without_touching_content(self) -> None:
        settings = Settings.from_env({})
        assert settings.bindings["counsellor"] == Binding("scripted", "demo")
        swapped = settings.with_binding("counsellor", "test-chatty:v1")
        assert swapped.bindings["counsellor"] == Binding("test-chatty", "v1")
        assert swapped.bindings["extractor"] == settings.bindings["extractor"]
