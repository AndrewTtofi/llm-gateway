"""usage_log.variant: the A/B arm a request was assigned to (ADR 0020)

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"


def upgrade() -> None:
    op.add_column("usage_log", sa.Column("variant", sa.String(32)))


def downgrade() -> None:
    op.drop_column("usage_log", "variant")
