"""A/B routing: weighted variants of an alias (ADR 0020).

    aliases:
      support:
        sticky: user                 # key (default) | user | request
        variants:
          - { name: control,   weight: 90, chain: [anthropic/claude-sonnet-5-5] }
          - { name: concise,   weight: 10, chain: [anthropic/claude-sonnet-5-5],
              system_prefix: "Answer in at most three sentences." }

Each request gets one variant. With `sticky: key` or `user` the choice is a hash of the
alias and the key id (or the request's `user`), so a caller stays on one variant and the
split follows the weights across callers; `request` picks at random every time. The arm's
chain is used as the fallback chain, and its `system_prefix` (if any) goes in front of
the system prompt, which is how prompt variants are tested.

The variant is returned in `x-gateway-variant`, recorded in the usage log and in
`gateway_variant_*` metrics, so arms can be compared on latency, errors and cost. A
client may pin a variant with `x-gateway-variant: <name>` (e.g. for QA).
"""

from __future__ import annotations

import hashlib
import random

from app.auth import ApiKey
from app.config import Alias, Variant
from app.schemas import ChatCompletionRequest


def _unit(text: str) -> float:
    """A stable number in [0, 1) for a string."""
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") / 2**64


def assign(name: str, alias: Alias, key: ApiKey, user: str | None, pinned: str | None) -> Variant:
    variants = alias.variants
    if pinned:
        for v in variants:
            if v.name == pinned:
                return v
    if alias.sticky == "request":
        point = random.random()  # noqa: S311 — traffic splitting, not security
    else:
        subject = (user or key.id) if alias.sticky == "user" else key.id
        point = _unit(f"{name}:{subject}")
    total = sum(v.weight for v in variants)
    edge = 0.0
    for v in variants:
        edge += v.weight / total
        if point < edge:
            return v
    return variants[-1]  # rounding at the top edge


def with_prefix(body: ChatCompletionRequest, prefix: str) -> ChatCompletionRequest:
    """The request with `prefix` in front of its system prompt (or as one, if it has none)."""
    data = body.model_dump(exclude_unset=True)
    messages = list(data.get("messages") or [])
    first = messages[0] if messages else None
    if isinstance(first, dict) and first.get("role") in ("system", "developer"):
        content = first.get("content")
        if isinstance(content, str):
            messages[0] = {**first, "content": f"{prefix}\n\n{content}"}
        elif isinstance(content, list):
            messages[0] = {**first, "content": [{"type": "text", "text": prefix}, *content]}
        else:
            messages[0] = {**first, "content": prefix}
    else:
        messages.insert(0, {"role": "system", "content": prefix})
    data["messages"] = messages
    return ChatCompletionRequest.model_validate(data)
