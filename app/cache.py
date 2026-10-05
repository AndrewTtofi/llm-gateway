"""Response cache (ADR 0018): opt-in per alias, exact and semantic.

    aliases:
      faq-bot:
        chain: [...]
        cache: { mode: semantic, ttl_seconds: 3600, threshold: 0.95, scope: key,
                 embedding: openai/text-embedding-3-small }

- **exact**: the answer for an identical request (same messages, tools and sampling
  parameters), keyed by a SHA-256 of their canonical JSON.
- **semantic**: also the answer for a *similar* conversation. Its text is embedded with
  the configured OpenAI-compatible embedding model and looked up in a Redis 8 vector set;
  a match at or above `threshold` (cosine similarity, 0–1) is a hit.

**Scope** decides who shares answers: `key` (default), `team`, or `global`. Sharing is a
data-isolation decision: a semantic hit hands one caller an answer generated for someone
else's similar prompt, which may contain that prompt's details.

Hits are free: no provider call, no cost, tokens refunded. Streams are replayed from the
stored completion. Clients can skip the cache with `x-gateway-cache: bypass` (no read, no
write) or `refresh` (no read, write the new answer). Only clean, complete answers are
stored (finish reason `stop` or `tool_calls`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis

from app import config, providers
from app.auth import ApiKey
from app.config import CacheConfig
from app.observability import metrics

log = logging.getLogger(__name__)

# Request fields that change the answer; everything else (stream, stream_options, user,
# routing hints) doesn't.
KEY_FIELDS = (
    "messages",
    "tools",
    "tool_choice",
    "response_format",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "seed",
    "reasoning_effort",
    "thinking",
    "parallel_tool_calls",
    "n",
)
STORABLE_FINISH = ("stop", "tool_calls")


def scope_id(cfg: CacheConfig, key: ApiKey) -> str:
    if cfg.scope == "global":
        return "global"
    if cfg.scope == "team" and key.team:
        return f"team:{key.team}"
    return f"key:{key.id}"  # team scope without a team falls back to the key


def request_hash(alias: str, request: dict[str, Any]) -> str:
    material = {"alias": alias, **{k: request[k] for k in KEY_FIELDS if k in request}}
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def text_for_embedding(request: dict[str, Any]) -> str:
    """The conversation as text, role-tagged, for semantic lookups."""
    lines = []
    for msg in request.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        if content:
            lines.append(f"{msg.get('role')}: {content}")
    return "\n".join(lines)


def storable(result: dict[str, Any]) -> bool:
    choices = result.get("choices") or []
    return bool(choices) and all(c.get("finish_reason") in STORABLE_FINISH for c in choices)


def replay(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A stored chat.completion as stream chunks (role, content, tool calls, finish, usage)."""
    base = {
        "id": result.get("id", ""),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": result.get("model", ""),
    }
    choice = (result.get("choices") or [{}])[0]
    msg = choice.get("message") or {}

    def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
        return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    out = [chunk({"role": "assistant", "content": ""})]
    if msg.get("thinking_blocks"):  # extension (ADR 0013): replay for Anthropic clients
        for i, block in enumerate(msg["thinking_blocks"]):
            start = {"type": block.get("type")}
            if block.get("type") == "redacted_thinking":
                start["data"] = block.get("data", "")
            out.append(chunk({"thinking": {"index": i, "start": start}}))
            if block.get("thinking"):
                out.append(chunk({"thinking": {"index": i, "thinking": block["thinking"]}}))
            if block.get("signature"):
                out.append(chunk({"thinking": {"index": i, "signature": block["signature"]}}))
    if msg.get("content"):
        out.append(chunk({"content": msg["content"]}))
    for i, call in enumerate(msg.get("tool_calls") or []):
        out.append(chunk({"tool_calls": [{"index": i, **call}]}))
    out.append(chunk({}, choice.get("finish_reason") or "stop"))
    usage = {
        **base,
        "choices": [],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    out.append(usage)
    return out


class Collector:
    """Assembles a streamed answer into a chat.completion, for storing after the stream."""

    def __init__(self) -> None:
        self.id, self.model = "", ""
        self.content: list[str] = []
        self.calls: dict[int, dict[str, Any]] = {}
        self.finish: str | None = None

    def feed(self, chunk: dict[str, Any]) -> None:
        self.id = self.id or chunk.get("id", "")
        self.model = self.model or chunk.get("model", "")
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if text := delta.get("content"):
                self.content.append(text)
            for call in delta.get("tool_calls") or []:
                i = int(call.get("index") or 0)
                slot = self.calls.setdefault(
                    i, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                slot["function"]["arguments"] += fn.get("arguments") or ""
            if choice.get("finish_reason"):
                self.finish = choice["finish_reason"]

    def result(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self.content) or None}
        if self.calls:
            message["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return {
            "id": self.id,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{"index": 0, "message": message, "finish_reason": self.finish}],
        }


# --- stores ---------------------------------------------------------------------


class CacheStore(Protocol):
    async def get(self, key: str) -> dict[str, Any] | None: ...
    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None: ...
    async def similar(self, index: str, vector: list[float], threshold: float) -> str | None: ...
    async def add_vector(
        self, index: str, vector: list[float], element: str, max_entries: int
    ) -> None: ...
    async def forget(self, index: str, element: str) -> None: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class MemoryCacheStore:
    """Tests and single-process dev. Bounded LRU; brute-force similarity."""

    def __init__(self, max_items: int = 1000) -> None:
        self._items: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._vectors: dict[str, dict[str, list[float]]] = {}
        self._max = max_items

    async def get(self, key: str) -> dict[str, Any] | None:
        hit = self._items.get(key)
        if hit is None or hit[0] < time.monotonic():
            self._items.pop(key, None)
            return None
        self._items.move_to_end(key)
        return hit[1]

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        self._items[key] = (time.monotonic() + ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    async def similar(self, index: str, vector: list[float], threshold: float) -> str | None:
        best, best_score = None, -1.0
        for element, v in self._vectors.get(index, {}).items():
            # Same scale as Redis VSIM with cosine: 1 identical … 0 opposite.
            score = (1 + _cosine(vector, v)) / 2
            if score > best_score:
                best, best_score = element, score
        return best if best is not None and best_score >= threshold else None

    async def add_vector(
        self, index: str, vector: list[float], element: str, max_entries: int
    ) -> None:
        vectors = self._vectors.setdefault(index, {})
        if len(vectors) < max_entries:
            vectors[element] = vector

    async def forget(self, index: str, element: str) -> None:
        self._vectors.get(index, {}).pop(element, None)


class RedisCacheStore:
    """Answers as JSON strings with a TTL; the semantic index as Redis 8 vector sets.
    Every error is swallowed and logged: a cache must never fail a request."""

    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def get(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await self.redis.get(f"rcache:{key}")
        except Exception as exc:
            log.warning("response cache read failed: %s", type(exc).__name__)
            return None
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    async def set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        try:
            await self.redis.set(f"rcache:{key}", json.dumps(value), ex=ttl)
        except Exception as exc:
            log.warning("response cache write failed: %s", type(exc).__name__)

    async def similar(self, index: str, vector: list[float], threshold: float) -> str | None:
        try:
            result = await self.redis.vset().vsim(
                f"vcache:{index}", vector, with_scores=True, count=1
            )
        except AttributeError:  # redis-py's reply parsing when the index doesn't exist yet
            return None
        except Exception as exc:
            log.warning("semantic cache lookup failed: %s", type(exc).__name__)
            return None
        if not isinstance(result, dict) or not result:
            return None
        element, score = next(iter(result.items()))  # {element: similarity 0..1}
        element = element.decode() if isinstance(element, bytes) else str(element)
        return element if float(score) >= threshold else None

    async def add_vector(
        self, index: str, vector: list[float], element: str, max_entries: int
    ) -> None:
        name = f"vcache:{index}"
        try:
            if await self.redis.vset().vcard(name) >= max_entries:
                return  # full: answers still expire; the index stops growing
            await self.redis.vset().vadd(name, vector, element)
        except Exception as exc:
            log.warning("semantic cache write failed: %s", type(exc).__name__)

    async def forget(self, index: str, element: str) -> None:
        try:
            await self.redis.vset().vrem(f"vcache:{index}", element)
        except Exception as exc:
            log.warning("semantic cache cleanup failed: %s", type(exc).__name__)


# --- lookup and store -------------------------------------------------------------


@dataclass
class Lookup:
    """One request's cache state: where its answer lives and whether to write it."""

    cfg: CacheConfig
    store: CacheStore
    scope: str
    key_hash: str
    index: str  # semantic index name (scope + alias)
    vector: list[float] | None
    write: bool

    @property
    def entry_key(self) -> str:
        return f"{self.scope}:{self.key_hash}"

    async def save(self, result: dict[str, Any]) -> bool:
        """Store a finished answer. Never raises: a cache must never fail a request."""
        if not self.write or not storable(result):
            return False
        try:
            if len(json.dumps(result)) > self.cfg.max_entry_bytes:
                return False
            await self.store.set(self.entry_key, result, self.cfg.ttl_seconds)
            if self.vector is not None:
                await self.store.add_vector(
                    self.index, self.vector, self.key_hash, self.cfg.max_entries
                )
            metrics.cache.labels(self.cfg.mode, "store").inc()
            return True
        except Exception:
            log.exception("response cache store failed")
            return False


async def _embed(target: str, text: str) -> list[float] | None:
    provider, _, model = target.partition("/")
    cfg = config.registry.providers.get(provider)
    if cfg is None:
        log.warning("cache embedding provider %s is not configured", provider)
        return None
    try:
        adapter = providers.pool.get(provider, cfg)
        embed = getattr(adapter, "embed", None)
        if embed is None:
            log.warning("provider %s can't make embeddings; semantic cache off", provider)
            return None
        vectors = await embed(model, [text])
        return list(vectors[0])
    except Exception as exc:
        log.warning("cache embedding failed: %s", type(exc).__name__)
        return None


async def lookup(
    store: CacheStore, cfg: CacheConfig, key: ApiKey, alias: str, request: dict[str, Any], mode: str
) -> tuple[dict[str, Any] | None, Lookup | None, str]:
    """→ (cached answer or None, the lookup for storing later, result label).

    `mode`: "" (normal), "bypass" (no read, no write), "refresh" (no read, write)."""
    if mode == "bypass":
        metrics.cache.labels(cfg.mode, "bypass").inc()
        return None, None, "bypass"
    scope = scope_id(cfg, key)
    key_hash = request_hash(alias, request)
    index = f"{scope}:{alias}"
    vector = None
    if cfg.mode == "semantic" and cfg.embedding and (text := text_for_embedding(request)):
        vector = await _embed(cfg.embedding, text)
    ctx = Lookup(cfg, store, scope, key_hash, index, vector, write=True)
    if mode == "refresh":
        metrics.cache.labels(cfg.mode, "refresh").inc()
        return None, ctx, "refresh"
    if (hit := await store.get(ctx.entry_key)) is not None:
        metrics.cache.labels(cfg.mode, "hit_exact").inc()
        return hit, ctx, "hit"
    if vector is not None and (element := await store.similar(index, vector, cfg.threshold)):
        if (hit := await store.get(f"{scope}:{element}")) is not None:
            metrics.cache.labels(cfg.mode, "hit_semantic").inc()
            return hit, ctx, "hit"
        await store.forget(index, element)  # its answer expired
    metrics.cache.labels(cfg.mode, "miss").inc()
    return None, ctx, "miss"
