"""The Anthropic adapter against the two generations of model it can be bound to.

Everything vendor-specific about Anthropic lives in one file, so this is the
one place a model-generation difference can be asserted. From Opus 4.7 onward
the Messages API rejects sampling parameters and thinks by default, and the
thinking shares `max_tokens` with the reply. Older models still take a
temperature and do not think unless asked. The adapter must send each
generation only what it accepts, and orchestration must never know.
"""

from __future__ import annotations

from typing import Any

import pytest

from cadence.llm.providers import anthropic as anthropic_module
from cadence.llm.providers.anthropic import AnthropicProvider
from cadence.llm.types import CompletionRequest, Message, StructuredRequest


class _FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, body: dict[str, Any]):
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture()
def capture(monkeypatch: pytest.MonkeyPatch):
    """Capture the JSON payload the adapter posts, and answer with a canned body."""
    sent: dict[str, Any] = {}
    body: dict[str, Any] = {
        "model": "any",
        "content": [{"type": "text", "text": "Hello."}],
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "stop_reason": "end_turn",
    }

    def fake_post(url: str, *, headers: dict[str, str], json: dict[str, Any], timeout: float):
        sent.clear()
        sent.update(json)
        return _FakeResponse(body)

    monkeypatch.setattr(anthropic_module.httpx, "post", fake_post)
    return sent, body


def _completion() -> CompletionRequest:
    return CompletionRequest(
        messages=[Message("user", "I had a cigarette this morning.")],
        system="You are the counsellor.",
        max_tokens=500,
        temperature=0.5,
    )


def _structured() -> StructuredRequest:
    return StructuredRequest(
        messages=[Message("user", "I smoke about ten a day.")],
        schema={"type": "object", "properties": {"cigarettes_per_day": {"type": "integer"}}},
        schema_name="observations",
        temperature=0.0,
    )


class TestCurrentGeneration:
    """Opus 4.7 onward: Opus 5, Sonnet 5, Fable, and the 4.7 and 4.8 point releases."""

    @pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"])
    def test_a_completion_sends_no_temperature_and_asks_for_low_effort(
        self, capture, model: str
    ) -> None:
        sent, _ = capture
        AnthropicProvider(model=model, api_key="k")._complete(_completion())
        assert "temperature" not in sent
        assert sent["output_config"] == {"effort": "low"}
        assert sent["max_tokens"] == 500
        assert sent["system"] == "You are the counsellor."

    def test_a_structured_call_keeps_the_forced_tool_and_drops_the_temperature(
        self, capture
    ) -> None:
        sent, body = capture
        body["content"] = [
            {"type": "thinking", "thinking": ""},
            {"type": "tool_use", "name": "observations", "input": {"cigarettes_per_day": 10}},
        ]
        result = AnthropicProvider(model="claude-opus-5", api_key="k")._structured_native(
            _structured()
        )
        assert sent["tool_choice"] == {"type": "tool", "name": "observations"}
        assert "temperature" not in sent
        assert sent["output_config"] == {"effort": "low"}
        assert result.data == {"cigarettes_per_day": 10}

    def test_thinking_blocks_do_not_leak_into_the_reply(self, capture) -> None:
        _, body = capture
        body["content"] = [
            {"type": "thinking", "thinking": "private reasoning"},
            {"type": "text", "text": "That sounds hard. "},
            {"type": "text", "text": "What happened just before?"},
        ]
        result = AnthropicProvider(model="claude-opus-5", api_key="k")._complete(_completion())
        assert result.text == "That sounds hard. What happened just before?"
        assert "private reasoning" not in result.text


class TestEarlierGeneration:
    """Haiku 4.5, Sonnet 4.5 and 4.6, Opus 4.6 and before still take a temperature."""

    @pytest.mark.parametrize(
        "model", ["claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-6", "claude-sonnet-4-6"]
    )
    def test_a_completion_sends_the_temperature_and_no_effort(self, capture, model: str) -> None:
        sent, _ = capture
        AnthropicProvider(model=model, api_key="k")._complete(_completion())
        assert sent["temperature"] == 0.5
        assert "output_config" not in sent

    def test_a_structured_call_sends_the_temperature(self, capture) -> None:
        sent, body = capture
        body["content"] = [{"type": "tool_use", "name": "observations", "input": {}}]
        AnthropicProvider(model="claude-haiku-4-5", api_key="k")._structured_native(_structured())
        assert sent["temperature"] == 0.0
        assert "output_config" not in sent


def test_the_default_model_is_a_current_one() -> None:
    assert AnthropicProvider.default_model == "claude-opus-5"
