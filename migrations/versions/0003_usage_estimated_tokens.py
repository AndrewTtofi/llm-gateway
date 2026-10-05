"""usage_log.estimated_tokens: how good the pre-call token estimate is (Phase 6)

Revision ID: 0003
Revises: 0002
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.add_column("usage_log", sa.Column("estimated_tokens", sa.Integer()))


def downgrade() -> None:
    op.drop_column("usage_log", "estimated_tokens")
