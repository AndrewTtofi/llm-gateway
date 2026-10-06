"""admin_audit: every admin change, with the operator who made it (ADR 0025)

Revision ID: 0008
Revises: 0007
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    op.create_table(
        "admin_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operator", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("target", sa.String(100)),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index("ix_admin_audit_at", "admin_audit", ["at"])


def downgrade() -> None:
    op.drop_index("ix_admin_audit_at", "admin_audit")
    op.drop_table("admin_audit")
