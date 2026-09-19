"""Thin LiteLLM wrapper. Isolated so tests can patch provider calls without touching the network."""

from typing import Any

import litellm

from app.config import get_settings

litellm.drop_params = True


async def acompletion(**kwargs: Any) -> Any:
    kwargs.setdefault("timeout", get_settings().request_timeout_s)
    if kwargs.get("stream"):
        kwargs.setdefault("stream_options", {"include_usage": True})
    return await litellm.acompletion(**kwargs)
