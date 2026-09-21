"""Provider parsing and streaming against mocked HTTP (Spec §8, §10, §107)."""

from __future__ import annotations

import httpx
import pytest

from core.errors import ConfigurationError, ProviderError, RateLimited
from providers.base import Message
from providers.ollama import OllamaProvider
from providers.openrouter import FREE_ROUTER_ID, OpenRouterProvider
from tests.fakes import ollama_transport, openrouter_transport


def _openrouter(**kwargs) -> OpenRouterProvider:
    transport = kwargs.pop("transport", None) or openrouter_transport(**kwargs)
    return OpenRouterProvider(api_key="sk-or-v1-test", client=httpx.AsyncClient(transport=transport))


def _ollama(**kwargs) -> OllamaProvider:
    transport = kwargs.pop("transport", None) or ollama_transport(**kwargs)
    return OllamaProvider(client=httpx.AsyncClient(transport=transport))


# --- OpenRouter ---------------------------------------------------------------------------


async def test_openrouter_parses_the_documented_schema() -> None:
    models = {m.id: m for m in await _openrouter().list_models()}

    free = models["meta-llama/llama-3.3-70b-instruct:free"]
    assert free.is_free is True
    assert free.context_length == 131072
    assert free.supports_tools is True
    assert free.supports_vision is False          # input_modalities lists text only
    assert free.supports_structured is False      # response_format not advertised

    vision = models["google/gemini-flash-1.5:free"]
    assert vision.supports_vision is True
    assert vision.supports_structured is True


async def test_paid_models_are_not_marked_free() -> None:
    models = {m.id: m for m in await _openrouter().list_models()}
    assert models["openai/gpt-4o"].is_free is False
    # openrouter/auto routes across paid models, so it must never count as free (Spec §9).
    assert models["openrouter/auto"].is_free is False
    assert models["openrouter/auto"].is_router is True


async def test_missing_metadata_becomes_unknown_not_a_guess() -> None:
    """Spec §8: when information is missing, report Unknown — never invent it."""
    models = {m.id: m for m in await _openrouter().list_models()}
    mystery = models["mystery/model-x"]
    assert mystery.supports_tools is None
    assert mystery.supports_vision is None
    assert mystery.context_length is None
    assert mystery.price_prompt is None
    display = mystery.to_dict()["display"]
    assert display["tools"] == "Unknown"
    assert display["price_input"] == "Unknown"
    assert display["context"] == "Unknown"


async def test_free_router_is_always_offered() -> None:
    """The free router is the documented cloud fallback and must stay selectable (Spec §9)."""
    models = await _openrouter().list_models()
    router = next(m for m in models if m.id == FREE_ROUTER_ID)
    assert router.is_free is True
    assert router.is_router is True


async def test_pricing_beats_the_free_suffix() -> None:
    """A ':free' suffix on a priced model must not unlock it under FREE_ONLY."""
    payload = {"data": [{"id": "sneaky/model:free", "pricing": {"prompt": "0.001", "completion": "0.002"}}]}
    models = await _openrouter(models=payload).list_models()
    assert models[0].is_free is False


async def test_zero_priced_model_without_suffix_is_free() -> None:
    payload = {"data": [{"id": "stealth/model", "pricing": {"prompt": "0", "completion": "0"}}]}
    models = await _openrouter(models=payload).list_models()
    assert models[0].is_free is True


async def test_openrouter_streams_text_and_usage() -> None:
    provider = _openrouter()
    result = await provider.chat([Message("user", "Hallo")], "meta-llama/llama-3.3-70b-instruct:free")
    assert result.text == "Hallo, ich bin JARVIS."
    assert result.usage.total_tokens == 19
    assert result.finish_reason == "stop"


async def test_openrouter_streams_tool_calls_across_fragments() -> None:
    frames = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"launch_app","arguments":"{\\"app\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"discord\\"}"}}]}}]}',
        'data: {"choices":[{"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    result = await _openrouter(stream_frames=frames).chat([Message("user", "öffne discord")], "x")
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "launch_app"
    assert result.tool_calls[0].arguments == {"app": "discord"}


async def test_missing_api_key_is_a_configuration_error() -> None:
    provider = OpenRouterProvider(api_key=None, client=httpx.AsyncClient(transport=openrouter_transport()))
    available, reason = await provider.is_available()
    assert available is False
    assert "Schlüssel" in reason
    with pytest.raises(ConfigurationError):
        await provider.list_models()


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, ConfigurationError), (429, RateLimited), (500, ProviderError), (400, ProviderError)],
)
async def test_http_errors_map_to_typed_exceptions(status: int, expected: type[Exception]) -> None:
    with pytest.raises(expected):
        await _openrouter(status=status).list_models()


async def test_error_frame_inside_a_stream_is_raised() -> None:
    frames = ['data: {"error":{"message":"model is overloaded"}}', "data: [DONE]"]
    with pytest.raises(ProviderError, match="overloaded"):
        await _openrouter(stream_frames=frames).chat([Message("user", "hi")], "x")


async def test_malformed_tool_arguments_are_surfaced_not_dropped() -> None:
    frames = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"write_file","arguments":"{not json"}}]}}]}',
        'data: {"choices":[{"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    result = await _openrouter(stream_frames=frames).chat([Message("user", "x")], "y")
    assert "__malformed_arguments__" in result.tool_calls[0].arguments


# --- Ollama -------------------------------------------------------------------------------


async def test_ollama_reads_capabilities_from_api_show() -> None:
    models = {m.id: m for m in await _ollama().list_models()}
    coder = models["qwen2.5-coder:7b"]
    assert coder.supports_tools is True
    assert coder.supports_vision is False
    assert coder.is_local is True
    assert coder.is_free is True          # local inference costs nothing
    assert models["llava:13b"].supports_vision is True


async def test_ollama_reports_unknown_context_instead_of_guessing() -> None:
    models = await _ollama().list_models()
    assert all(m.context_length is None for m in models)
    assert models[0].to_dict()["display"]["context"] == "Unknown"


async def test_ollama_streams_ndjson() -> None:
    result = await _ollama().chat([Message("user", "Hallo")], "qwen2.5-coder:7b")
    assert result.text == "Guten Abend."
    assert result.usage.prompt_tokens == 9
    assert result.usage.cost_usd == 0.0


async def test_ollama_tool_calls_have_object_arguments() -> None:
    import json as json_module

    frames = [
        json_module.dumps({"message": {"tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "/tmp/a"}}}
        ]}, "done": False}),
        json_module.dumps({"done": True, "done_reason": "stop"}),
    ]
    result = await _ollama(chat_frames=frames).chat([Message("user", "lies")], "qwen2.5-coder:7b")
    assert result.tool_calls[0].arguments == {"path": "/tmp/a"}


async def test_unreachable_ollama_reports_a_helpful_reason() -> None:
    available, reason = await _ollama(reachable=False).is_available()
    assert available is False
    assert "nicht erreichbar" in reason


async def test_ollama_without_models_is_available_but_says_so() -> None:
    available, reason = await _ollama(tags={"models": []}).is_available()
    assert available is True
    assert "kein Modell" in reason
