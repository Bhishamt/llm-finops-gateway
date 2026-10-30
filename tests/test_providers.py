from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from llm_finops_gateway.providers.azure_openai import AzureOpenAIProvider
from llm_finops_gateway.providers.base import ProviderError, TokenUsage
from llm_finops_gateway.providers.ollama import OllamaProvider
from llm_finops_gateway.providers.openai_compat import OpenAICompatProvider

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


@respx.mock
async def test_openai_compat_sends_bearer_and_maps_model() -> None:
    route = respx.post("https://res.openai.azure.com/openai/v1/chat/completions").mock(
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
    provider = OpenAICompatProvider(
        base_url="https://res.openai.azure.com/openai/v1/",
        api_key="secret",
        models={"gpt-4o-mini": "gpt-4o-mini-eastus"},
    )
    result = await provider.chat("gpt-4o-mini", MESSAGES, temperature=0.5, max_tokens=20)
    await provider.aclose()

    assert (result.content, result.input_tokens, result.output_tokens) == ("hi", 11, 4)
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer secret"
    payload = json.loads(sent.content)
    assert payload["model"] == "gpt-4o-mini-eastus"
    assert payload["stream"] is False
    assert payload["temperature"] == 0.5
    assert payload["max_tokens"] == 20


@respx.mock
async def test_openai_compat_without_key_sends_extra_headers_only() -> None:
    route = respx.post("http://vllm.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}], "usage": {}}
        )
    )
    provider = OpenAICompatProvider(base_url="http://vllm.test/v1", headers={"X-Tenant": "a"})
    result = await provider.chat("llama", MESSAGES)
    await provider.aclose()

    assert result.content == "x" and result.input_tokens == 0 and result.output_tokens == 0
    sent = route.calls.last.request
    assert "Authorization" not in sent.headers
    assert sent.headers["X-Tenant"] == "a"
    assert json.loads(sent.content)["model"] == "llama"


@respx.mock
async def test_openai_compat_error_status_becomes_provider_error() -> None:
    respx.post("http://vllm.test/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad request"}})
    )
    provider = OpenAICompatProvider(base_url="http://vllm.test/v1")
    with pytest.raises(ProviderError) as exc:
        await provider.chat("m", MESSAGES)
    assert exc.value.status_code == 400
    assert exc.value.provider == "openai_compat"
    await provider.aclose()


@respx.mock
async def test_openai_compat_connection_error_becomes_provider_error() -> None:
    respx.post("http://vllm.test/v1/chat/completions").mock(side_effect=httpx.ConnectError("down"))
    provider = OpenAICompatProvider(base_url="http://vllm.test/v1")
    with pytest.raises(ProviderError):
        await provider.chat("m", MESSAGES)
    await provider.aclose()


@respx.mock
async def test_openai_compat_reads_cached_tokens_and_retry_after() -> None:
    respx.post("http://vllm.test/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}, text="slow down"),
            httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "x"}}],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 60},
                    },
                },
            ),
        ]
    )
    provider = OpenAICompatProvider(base_url="http://vllm.test/v1")
    with pytest.raises(ProviderError) as exc:
        await provider.chat("m", MESSAGES)
    assert (exc.value.status_code, exc.value.retry_after) == (429, "7")
    result = await provider.chat("m", MESSAGES)
    await provider.aclose()
    assert (result.input_tokens, result.cached_input_tokens, result.output_tokens) == (100, 60, 5)


@respx.mock
async def test_azure_openai_reads_cached_tokens() -> None:
    respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "x"}}],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 8,
                    "prompt_tokens_details": {"cached_tokens": 1024},
                    "completion_tokens_details": {"reasoning_tokens": 6},
                },
            },
        )
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    result = await provider.chat("m", MESSAGES)
    await provider.aclose()
    assert (result.cached_input_tokens, result.reasoning_tokens) == (1024, 6)


@respx.mock
async def test_azure_openai_sends_max_completion_tokens_for_reasoning_calls() -> None:
    route = respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "x"}}], "usage": {}}
        )
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    await provider.chat("m", MESSAGES, max_completion_tokens=64)
    await provider.aclose()
    payload = json.loads(route.calls.last.request.content)
    assert payload["max_completion_tokens"] == 64
    assert "max_tokens" not in payload and "temperature" not in payload


@respx.mock
async def test_ollama_maps_max_completion_tokens_to_num_predict() -> None:
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"content": "x"}, "prompt_eval_count": 1, "eval_count": 1}
        )
    )
    provider = OllamaProvider(base_url="http://ollama.test")
    await provider.chat("m", MESSAGES, max_completion_tokens=9)
    await provider.aclose()
    assert json.loads(route.calls.last.request.content)["options"] == {"num_predict": 9}


SSE = (
    b'data: {"choices":[{"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"Hel"},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":2,'
    b'"prompt_tokens_details":{"cached_tokens":4}}}\n\n'
    b"data: [DONE]\n\n"
)


@respx.mock
async def test_openai_compat_stream_yields_deltas_then_usage() -> None:
    route = respx.post("http://vllm.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, content=SSE)
    )
    provider = OpenAICompatProvider(base_url="http://vllm.test/v1")
    chunks = [c async for c in provider.stream("m", MESSAGES, max_tokens=5)]
    await provider.aclose()
    payload = json.loads(route.calls.last.request.content)
    assert payload["stream"] is True and payload["stream_options"] == {"include_usage": True}
    assert "".join(c.delta for c in chunks) == "Hello"
    assert [c.finish_reason for c in chunks if c.finish_reason] == ["stop"]
    assert chunks[-1].usage == TokenUsage(9, 2, cached_input_tokens=4)


@respx.mock
async def test_azure_openai_stream_error_status_becomes_provider_error() -> None:
    respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"}, text="slow down")
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    with pytest.raises(ProviderError) as exc:
        async for _ in provider.stream("m", MESSAGES):
            pass
    assert (exc.value.status_code, exc.value.retry_after) == (429, "3")
    await provider.aclose()


@respx.mock
async def test_azure_openai_stream_parses_sse() -> None:
    respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        return_value=httpx.Response(200, content=SSE)
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    chunks = [c async for c in provider.stream("m", MESSAGES)]
    await provider.aclose()
    assert "".join(c.delta for c in chunks) == "Hello" and chunks[-1].usage is not None


@respx.mock
async def test_ollama_stream_reads_ndjson_and_final_counts() -> None:
    body = (
        b'{"message":{"role":"assistant","content":"Hel"},"done":false}\n'
        b'{"message":{"role":"assistant","content":"lo"},"done":false}\n'
        b'{"message":{"role":"assistant","content":""},"done":true,"done_reason":"stop",'
        b'"prompt_eval_count":7,"eval_count":2}\n'
    )
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(200, content=body)
    )
    provider = OllamaProvider(base_url="http://ollama.test")
    chunks = [c async for c in provider.stream("m", MESSAGES)]
    await provider.aclose()
    assert json.loads(route.calls.last.request.content)["stream"] is True
    assert "".join(c.delta for c in chunks) == "Hello"
    assert chunks[-1].finish_reason == "stop" and chunks[-1].usage == TokenUsage(7, 2)


class FakeTokenProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def token(self) -> str:
        self.calls += 1
        return f"tok-{self.calls}"

    async def aclose(self) -> None:
        self.closed = True


@respx.mock
async def test_azure_openai_entra_sends_bearer_per_request_and_no_api_key() -> None:
    route = respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        side_effect=[
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}], "usage": {}}),
            httpx.Response(200, content=SSE),
        ]
    )
    tokens = FakeTokenProvider()
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", token_provider=tokens)
    await provider.chat("m", MESSAGES)
    async for _ in provider.stream("m", MESSAGES):
        pass
    await provider.aclose()
    first, second = route.calls[0].request, route.calls[1].request
    assert first.headers["Authorization"] == "Bearer tok-1"
    assert second.headers["Authorization"] == "Bearer tok-2"
    assert "api-key" not in first.headers
    assert tokens.closed


def test_azure_openai_requires_exactly_one_credential() -> None:
    with pytest.raises(ValueError):
        AzureOpenAIProvider(endpoint="https://res.openai.azure.com")
    with pytest.raises(ValueError):
        AzureOpenAIProvider(
            endpoint="https://res.openai.azure.com", api_key="k", token_provider=FakeTokenProvider()
        )


@respx.mock
async def test_azure_openai_passes_extra_fields_and_returns_tool_calls() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
    route = respx.post("https://res.openai.azure.com/openai/deployments/m/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    provider = AzureOpenAIProvider(endpoint="https://res.openai.azure.com", api_key="k")
    result = await provider.chat(
        "m", MESSAGES, extra={"tools": [{"type": "function"}], "top_p": 0.5, "seed": 1}
    )
    await provider.aclose()
    payload = json.loads(route.calls.last.request.content)
    assert payload["tools"] == [{"type": "function"}] and payload["top_p"] == 0.5
    assert (result.content, result.finish_reason, result.tool_calls) == ("", "tool_calls", [call])


@respx.mock
async def test_ollama_translates_tools_and_tool_calls_both_ways() -> None:
    route = respx.post("http://ollama.test/api/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_weather", "arguments": {"city": "Budapest"}}}
                    ],
                },
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 3,
            },
        )
    )
    provider = OllamaProvider(base_url="http://ollama.test")
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Pecs"}'},
                }
            ],
        },
    ]
    result = await provider.chat(
        "m",
        history,
        extra={
            "tools": [{"type": "function"}],
            "response_format": {"type": "json_object"},
            "top_p": 0.3,
            "stop": "END",
            "tool_choice": "auto",
            "user": "ignored",
        },
    )
    await provider.aclose()
    payload = json.loads(route.calls.last.request.content)
    assert payload["tools"] == [{"type": "function"}] and payload["format"] == "json"
    assert payload["options"] == {"top_p": 0.3, "stop": ["END"]}
    assert payload["messages"][1]["tool_calls"][0]["function"]["arguments"] == {"city": "Pecs"}
    assert "user" not in payload and "tool_choice" not in payload
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        {
            "id": "call_0",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Budapest"}'},
        }
    ]
