"""OpenAI chat-completions wire format — the gateway's internal format.

Only the fields the gateway itself reads are declared. Everything else
(temperature, tools, response_format, …) is kept via `extra="allow"` and
forwarded untouched, so new OpenAI parameters work without a code change.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Bounds on what one request may ask for (ADR 0023). Generous for real use; they keep
# validation time, token estimates and usage-log values bounded.
MAX_MESSAGES = 10_000
MAX_CONTENT_PARTS = 1_000  # per message
MAX_OUTPUT_TOKENS = 1_000_000  # above any model's output limit


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "developer", "user", "assistant", "tool"]
    # str, a list of content parts (text/image), or None for assistant tool calls.
    content: str | Annotated[list[dict[str, Any]], Field(max_length=MAX_CONTENT_PARTS)] | None = (
        None
    )


class StreamOptions(BaseModel):
    model_config = ConfigDict(extra="allow")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1, max_length=256)
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    stream: bool = False
    stream_options: StreamOptions | None = None
    # Read by the gateway for token estimates, so they must be valid numbers.
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=MAX_OUTPUT_TOKENS)
    n: int | None = Field(default=None, ge=1, le=128)  # read by the cache and the judge

    @model_validator(mode="before")
    @classmethod
    def _one_output_limit(cls, data: Any) -> Any:
        """Both limits sent: keep `max_completion_tokens` (OpenAI's current name). Then the
        estimate and what the provider gets can't disagree after a rename rule."""
        if isinstance(data, dict) and data.get("max_completion_tokens") is not None:
            if "max_tokens" in data:
                data = {k: v for k, v in data.items() if k != "max_tokens"}
        return data

    def upstream_body(self, model: str) -> dict[str, Any]:
        """What the client sent, with the alias swapped for the real upstream model."""
        body = self.model_dump(exclude_unset=True)
        body["model"] = model
        return body
