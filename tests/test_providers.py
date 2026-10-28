from __future__ import annotations

import json

import httpx
import pytest
import respx

from llm_finops_gateway.providers.azure_openai import AzureOpenAIProvider
from llm_finops_gateway.providers.base import ProviderError
from llm_finops_gateway.providers.ollama import OllamaProvider

MESSAGES = [{"role": "user", "content": "hi"}]


@respx.mock
async def test_ollama_maps_tokens_and_options() -> None:
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "hey"},
                "prompt_eval_count": 7,
                "eval_count": 3,
                "done_reason": "stop",
            },
        )
    )
    provider = OllamaProvider(base_url="http://ollama.test")
    result = await provider.chat("llama3.1:8b", MESSAGES, temperature=0.1, max_tokens=20)
    await provider.aclose()

    assert (result.content, result.input_tokens, result.output_tokens) == ("hey", 7, 3)
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "llama3.1:8b"
    assert payload["stream"] is False
    assert payload["options"] == {"temperature": 0.1, "num_predict": 20}


@respx.mock
async def test_ollama_upstream_error_becomes_provider_error() -> None:
    respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(404, text="model not found")
    )
    provider = OllamaProvider(base_url="http://ollama.test")
    with pytest.raises(ProviderError) as exc:
        await provider.chat("missing", MESSAGES)
    assert exc.value.status_code == 404
    await provider.aclose()


@respx.mock
async def test_ollama_connection_error_becomes_provider_error() -> None:
    respx.post("http://ollama.test/api/chat").mock(side_effect=httpx.ConnectError("down"))
    provider = OllamaProvider(base_url="http://ollama.test")
    with pytest.raises(ProviderError):
        await provider.chat("m", MESSAGES)
    await provider.aclose()


@respx.mock
async def test_azure_openai_uses_deployment_mapping_and_api_key() -> None:
    route = respx.post(
        "https://res.openai.azure.com/openai/deployments/gpt4o-prod/chat/completions"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
            },
        )
    )
    provider = AzureOpenAIProvider(
        endpoint="https://res.openai.azure.com/",
        api_key="secret",
        api_version="2024-10-21",
        deployments={"gpt-4o": "gpt4o-prod"},
    )
    result = await provider.chat("gpt-4o", MESSAGES, temperature=0.5)
    await provider.aclose()

    assert (result.content, result.input_tokens, result.output_tokens) == ("hi", 11, 4)
    sent = route.calls.last.request
    assert sent.headers["api-key"] == "secret"
    assert sent.url.params["api-version"] == "2024-10-21"
    assert json.loads(sent.content)["temperature"] == 0.5


@respx.mock
async def test_azure_openai_unmapped_model_is_used_as_deployment_name() -> None:
    respx.post("https://res.openai.azure.com/openai/deployments/gpt-4o-mini/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}], "usage": {}}
        )
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    result = await provider.chat("gpt-4o-mini", MESSAGES)
    await provider.aclose()
    assert result.content == "x" and result.input_tokens == 0


@respx.mock
async def test_azure_openai_error_status_becomes_provider_error() -> None:
    respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        return_value=httpx.Response(429, json={"error": {"message": "rate limited"}})
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    with pytest.raises(ProviderError) as exc:
        await provider.chat("m", MESSAGES)
    assert exc.value.status_code == 429
    await provider.aclose()
