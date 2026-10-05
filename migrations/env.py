"""Alembic environment: async engine from DATABASE_URL, metadata from app.db."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection

from app.config import settings
from app.db import Base, make_engine

target_metadata = Base.metadata


def url() -> str:
    # Tests pass their own URL via `config.attributes["url"]`.
    return str(context.config.attributes.get("url") or settings.database_url)


def run_sync(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async() -> None:
    engine = make_engine(url())
    async with engine.connect() as conn:
        await conn.run_sync(run_sync)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        run_sync(connection)
    else:
        asyncio.run(run_async())
