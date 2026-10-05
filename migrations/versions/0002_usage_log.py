"""usage_log table (ADR 0008)

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    op.create_table(
        "usage_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),  # append-only
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("key_id", sa.Uuid()),
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("target", sa.String(200)),
        sa.Column("provider", sa.String(100)),
        sa.Column("model", sa.String(200)),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("streamed", sa.Boolean(), nullable=False),
        sa.Column("fallback", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False),
        sa.Column("cost_usd", sa.Float()),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("ttft_ms", sa.Integer()),
    )
    op.create_index("ix_usage_log_created_at", "usage_log", ["created_at"])
    op.create_index("ix_usage_log_key_created", "usage_log", ["key_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_usage_log_key_created", table_name="usage_log")
    op.drop_index("ix_usage_log_created_at", table_name="usage_log")
    op.drop_table("usage_log")
