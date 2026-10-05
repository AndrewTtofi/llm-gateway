"""api_keys table (ADR 0006)

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("tier", sa.String(50), nullable=False),
        sa.Column("requests_per_minute", sa.Integer()),
        sa.Column("tokens_per_minute", sa.Integer()),
        sa.Column("monthly_budget_usd", sa.Float()),
        sa.Column("allowed_aliases", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
