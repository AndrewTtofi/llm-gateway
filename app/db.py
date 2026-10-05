"""Postgres: async engine, session factory and table definitions (schema via Alembic)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, String, Uuid, func
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))  # first chars, for humans
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256 hex
    tier: Mapped[str] = mapped_column(String(50))
    # Per-key overrides of the tier (NULL = use the tier's value).
    requests_per_minute: Mapped[int | None] = mapped_column(Integer)
    tokens_per_minute: Mapped[int | None] = mapped_column(Integer)
    monthly_budget_usd: Mapped[float | None] = mapped_column(Float)
    allowed_aliases: Mapped[list[str] | None] = mapped_column(JSON(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def make_engine(url: str, timeout: float | None = None, **kwargs: Any) -> AsyncEngine:
    """`timeout` bounds waiting for a pooled connection, connecting and each query — on
    the request path a hung Postgres must fail fast, not stall auth for a minute."""
    if timeout is not None:
        kwargs.setdefault("pool_timeout", timeout)
        kwargs.setdefault("connect_args", {"timeout": timeout, "command_timeout": timeout})
    return create_async_engine(url, pool_pre_ping=True, **kwargs)


def make_sessions(engine: AsyncEngine) -> async_sessionmaker[Any]:
    return async_sessionmaker(engine, expire_on_commit=False)
