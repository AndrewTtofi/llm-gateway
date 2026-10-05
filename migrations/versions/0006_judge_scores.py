"""judge_scores: LLM-as-judge results, scores and fixed labels only (ADR 0022)

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.create_table(
        "judge_scores",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("target", sa.String(200)),
        sa.Column("variant", sa.String(32)),
        sa.Column("judge_target", sa.String(200)),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
    )
    op.create_index("ix_judge_scores_request_id", "judge_scores", ["request_id"])
    op.create_index("ix_judge_scores_alias_created", "judge_scores", ["alias", "created_at"])


def downgrade() -> None:
    op.drop_table("judge_scores")
