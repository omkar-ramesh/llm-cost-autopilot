"""Thin LiteLLM wrapper. Isolated so tests can patch provider calls without touching the network."""

import json
import time
from typing import Any

import litellm

from app.config import get_settings

litellm.drop_params = True

MOCK_TEXT = "This is a mock completion from LLM Cost Autopilot."


def as_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return json.loads(response.json())


def _is_judge_prompt(messages: list[dict]) -> bool:
    return any("REFERENCE ANSWER" in str(m.get("content", "")) for m in messages)


def _mock_response(model: str, messages: list[dict]) -> dict[str, Any]:
    # The judge expects JSON back; plain mock prose would score 0 and escalate everything.
    text = '{"score": 0.9, "reason": "mock judge"}' if _is_judge_prompt(messages) else MOCK_TEXT
    prompt_tokens = sum(len(str(m.get("content", ""))) // 4 for m in messages) or 1
    completion_tokens = len(text) // 4
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _mock_stream(model: str, messages: list[dict]):
    full = _mock_response(model, messages)
    for word in MOCK_TEXT.split():
        yield {
            "id": full["id"],
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": word + " "}}],
        }
    yield {
        "id": full["id"],
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": full["usage"],
    }


async def acompletion(**kwargs: Any) -> Any:
    if get_settings().mock_providers:
        model = kwargs["model"]
        messages = kwargs.get("messages", [])
        if kwargs.get("stream"):
            return _mock_stream(model, messages)
        return _mock_response(model, messages)

    kwargs.setdefault("timeout", get_settings().request_timeout_s)
    if kwargs.get("stream"):
        kwargs.setdefault("stream_options", {"include_usage": True})
    return await litellm.acompletion(**kwargs)
