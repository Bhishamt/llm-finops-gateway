"""The OpenAI Python SDK against llm-finops-gateway.

    pip install openai
    LFG_API_KEY=lfg_... python chat.py                # api_key mode
    LFG_AUTH_MODE=headers python chat.py             # headers mode (development)

Environment: LFG_URL (default http://localhost:8080/v1), LFG_API_KEY, LFG_MODEL
(default gpt-4.1-mini), LFG_TEAM / LFG_PROJECT / LFG_USE_CASE for the attribution headers.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI, OpenAIError

MODEL = os.environ.get("LFG_MODEL", "gpt-4.1-mini")

client = OpenAI(
    base_url=os.environ.get("LFG_URL", "http://localhost:8080/v1"),
    api_key=os.environ.get("LFG_API_KEY", "lfg_dev"),
    default_headers={
        # X-Team is ignored in api_key mode (the key decides); it is required in headers mode.
        "X-Team": os.environ.get("LFG_TEAM", "platform"),
        "X-Project": os.environ.get("LFG_PROJECT", "sdk-example"),
        "X-Use-Case": os.environ.get("LFG_USE_CASE", "python-example"),
    },
)


def finops(obj: Any) -> dict[str, Any]:
    """The gateway's extension block. The SDK keeps unknown fields in model_extra."""
    return dict((obj.model_extra or {}).get("x_finops") or {})


def plain_completion() -> None:
    completion = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "One sentence on what FinOps is."}],
        max_tokens=60,
    )
    x = finops(completion)
    print("1. completion:", completion.choices[0].message.content)
    print(f"   cost {x.get('cost_usd')} USD, team {x.get('team')}, budget {x['policy']['budget']}")
    if x["policy"]["warnings"]:
        print("   warnings:", x["policy"]["warnings"])


def streamed_completion() -> None:
    print("2. stream: ", end="", flush=True)
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": "Count from one to five, words only."}],
        max_tokens=40,
        stream=True,
    )
    last = None
    for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            print(chunk.choices[0].delta.content, end="", flush=True)
        last = chunk
    # The final chunk has no choices; it carries usage and x_finops.
    x = finops(last) if last is not None else {}
    tokens = last.usage.total_tokens if last is not None and last.usage else "?"
    print(f"\n   usage {tokens} tokens, cost {x.get('cost_usd')} {x.get('currency', '')}")


def tool_call_round_trip() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Current weather for a city",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    messages: list[Any] = [
        {"role": "user", "content": "What is the weather in Budapest? Use the tool."}
    ]
    first = client.chat.completions.create(
        model=MODEL, messages=messages, tools=tools, max_tokens=60
    )
    call = first.choices[0].message.tool_calls[0]
    print(f"3. tool call: {call.function.name}({call.function.arguments})")
    messages.append(first.choices[0].message)
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps({"temp_c": 9, "sky": "cloudy"}),
        }
    )
    second = client.chat.completions.create(
        model=MODEL, messages=messages, tools=tools, max_tokens=60
    )
    print("   answer:", second.choices[0].message.content)


def rejected_call() -> None:
    try:
        client.chat.completions.create(model="gpt-9", messages=[{"role": "user", "content": "hi"}])
    except OpenAIError as exc:
        print("4. rejected as expected:", exc)


if __name__ == "__main__":
    plain_completion()
    streamed_completion()
    tool_call_round_trip()
    rejected_call()
