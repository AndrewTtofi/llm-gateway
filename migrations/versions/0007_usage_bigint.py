"""usage_log: 64-bit token counts, and the caller's own request id kept apart (ADR 0023)

A 32-bit column overflowed on one oversized value, and the failed insert took other rows
with it. The gateway's request id is now always its own; a caller's id is stored beside it.

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"

TOKEN_COLUMNS = ("prompt_tokens", "completion_tokens", "cached_tokens", "estimated_tokens")


def upgrade() -> None:
    for col in TOKEN_COLUMNS:
        op.alter_column("usage_log", col, type_=sa.BigInteger(), existing_type=sa.Integer())
    op.add_column("usage_log", sa.Column("client_request_id", sa.String(64)))


def downgrade() -> None:
    op.drop_column("usage_log", "client_request_id")
    for col in TOKEN_COLUMNS:
        op.alter_column("usage_log", col, type_=sa.Integer(), existing_type=sa.BigInteger())
