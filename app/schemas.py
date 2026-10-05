"""OpenAI chat-completions wire format — the gateway's internal format.

Only the fields the gateway itself reads are declared. Everything else
(temperature, tools, response_format, …) is kept via `extra="allow"` and
forwarded untouched, so new OpenAI parameters work without a code change.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    # str, a list of content parts (text/image), or None for assistant tool calls.
    content: str | list[dict[str, Any]] | None = None


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    # Read by the gateway for token estimates, so they must be valid numbers.
    max_tokens: int | None = Field(default=None, ge=1)
    max_completion_tokens: int | None = Field(default=None, ge=1)

    def upstream_body(self, model: str) -> dict[str, Any]:
        """What the client sent, with the alias swapped for the real upstream model."""
        body = self.model_dump(exclude_unset=True)
        body["model"] = model
        return body
