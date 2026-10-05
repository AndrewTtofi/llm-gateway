"""Teams: api_keys.team (budgets in limits.yaml) and usage_log.team (spend history)

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("team", sa.String(100)))
    op.create_index("ix_api_keys_team", "api_keys", ["team"])
    op.add_column("usage_log", sa.Column("team", sa.String(100)))


def downgrade() -> None:
    op.drop_column("usage_log", "team")
    op.drop_index("ix_api_keys_team", table_name="api_keys")
    op.drop_column("api_keys", "team")
