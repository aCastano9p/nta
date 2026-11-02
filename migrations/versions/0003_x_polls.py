"""x_polls: spend and since_id bookkeeping for the optional X poller

Revision ID: 0003
Revises: 0002
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "x_polls",
        sa.Column("author_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("posts_returned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("posts_billable", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False),
        sa.Column("newest_id", sa.String(length=32), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_x_polls_author_time", "x_polls", ["author_id", "first_observed_at"], unique=False)
    op.execute("CREATE TRIGGER x_polls_append_only BEFORE UPDATE OR DELETE ON x_polls "
               "FOR EACH ROW EXECUTE FUNCTION nta_forbid_mutation()")
    op.execute("CREATE TRIGGER x_polls_no_truncate BEFORE TRUNCATE ON x_polls "
               "FOR EACH STATEMENT EXECUTE FUNCTION nta_forbid_mutation()")


def downgrade() -> None:
    op.drop_index("ix_x_polls_author_time", table_name="x_polls")
    op.drop_table("x_polls")
