"""Record real provider responses into tests/fixtures/providers/ (ADR 0026).

    python tools/record_fixtures.py            # needs ANTHROPIC_API_KEY and OPENAI_API_KEY

Costs a few cents: six small requests (a tool call each, max 300 output tokens), straight
to the providers, not through the gateway. Model names come from config/models.yaml (the
first Anthropic model in the `smart` chain, the first OpenAI chat and Responses models).
Response ids are kept: they identify nothing outside the provider account. API keys never
reach the files: only response bodies are written.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

ROOT = Path(__file__).parent.parent
OUT = ROOT / "tests" / "fixtures" / "providers"
ASK = "What's the weather in Paris? Use the tool."
TOOL = {
    "name": "get_weather",
    "description": "Current weather for a city.",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}, "unit": {"type": "string"}},
        "required": ["city"],
    },
}


def models() -> tuple[str, str, str]:
    cfg = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())
    providers = cfg["providers"]
    anthropic = next(
        t.split("/", 1)[1] for t in cfg["aliases"]["smart"]["chain"] if t.startswith("anthropic/")
    )
    openai_models = providers["openai"].get("models", {})
    responses = next(
        m for m, spec in openai_models.items() if (spec or {}).get("api") == "responses"
    )
    chat = next(
        t.split("/", 1)[1]
        for a in cfg["aliases"].values()
        for t in a.get("chain", [])
        if t.startswith("openai/") and t.split("/", 1)[1] != responses
    )
    return anthropic, chat, responses


def write(name: str, data: str) -> None:
    (OUT / name).write_text(data)
    print(f"  wrote {name} ({len(data)} bytes)")


def main() -> int:
    a_key, o_key = os.environ.get("ANTHROPIC_API_KEY"), os.environ.get("OPENAI_API_KEY")
    if not a_key or not o_key:
        print("set ANTHROPIC_API_KEY and OPENAI_API_KEY (this spends a few cents)", file=sys.stderr)
        return 2
    claude, chat_model, responses_model = models()
    anthropic_h = {"x-api-key": a_key, "anthropic-version": "2023-06-01"}
    openai_h = {"Authorization": f"Bearer {o_key}"}
    a_body: dict[str, Any] = {
        "model": claude,
        "max_tokens": 2048,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "tools": [
            {
                "name": TOOL["name"],
                "description": TOOL["description"],
                "input_schema": TOOL["parameters"],
            }
        ],
        "messages": [{"role": "user", "content": ASK}],
    }
    o_body: dict[str, Any] = {
        "model": chat_model,
        "max_completion_tokens": 300,
        "tools": [{"type": "function", "function": TOOL}],
        "messages": [{"role": "user", "content": ASK}],
    }
    r_body: dict[str, Any] = {
        "model": responses_model,
        "max_output_tokens": 300,
        "store": False,
        "tools": [{"type": "function", **TOOL}],
        "input": [{"role": "user", "content": ASK}],
    }
    with httpx.Client(timeout=120) as http:
        print(f"Anthropic {claude}")
        r = http.post("https://api.anthropic.com/v1/messages", headers=anthropic_h, json=a_body)
        r.raise_for_status()
        write("anthropic_message_tool_use.json", json.dumps(r.json(), indent=1))
        r = http.post(
            "https://api.anthropic.com/v1/messages",
            headers=anthropic_h,
            json={**a_body, "stream": True},
        )
        r.raise_for_status()
        write("anthropic_stream_thinking_tool.sse", r.text)

        print(f"OpenAI chat {chat_model}")
        r = http.post("https://api.openai.com/v1/chat/completions", headers=openai_h, json=o_body)
        r.raise_for_status()
        write("openai_chat_tool.json", json.dumps(r.json(), indent=1))
        r = http.post(
            "https://api.openai.com/v1/chat/completions",
            headers=openai_h,
            json={**o_body, "stream": True, "stream_options": {"include_usage": True}},
        )
        r.raise_for_status()
        write("openai_chat_stream_tool.sse", r.text)

        print(f"OpenAI Responses {responses_model}")
        r = http.post(
            "https://api.openai.com/v1/responses", headers=openai_h, json={**r_body, "stream": True}
        )
        r.raise_for_status()
        write("responses_stream_tool.sse", r.text)
    print("Re-run `make test`: tests/test_golden.py now replays real responses. The test")
    print("assertions name the fixture's tool call; adjust them if a model answered differently.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
