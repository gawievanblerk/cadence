"""Provider adapters.

Registration happens here so that adding a vendor is one file plus one line,
and so that no orchestration module ever imports a vendor adapter directly.
"""

from cadence.llm.providers.anthropic import AnthropicProvider
from cadence.llm.providers.deepseek import DeepSeekProvider
from cadence.llm.providers.gemini import GeminiProvider
from cadence.llm.providers.ollama import OllamaProvider
from cadence.llm.providers.openai import OpenAIProvider
from cadence.llm.providers.scripted import HashingEmbedder, ScriptedProvider
from cadence.llm.router import register

for _cls in (
    OpenAIProvider,
    AnthropicProvider,
    GeminiProvider,
    DeepSeekProvider,
    OllamaProvider,
    ScriptedProvider,
    HashingEmbedder,
):
    register(_cls)

__all__ = [
    "AnthropicProvider",
    "DeepSeekProvider",
    "GeminiProvider",
    "HashingEmbedder",
    "OllamaProvider",
    "OpenAIProvider",
    "ScriptedProvider",
]
