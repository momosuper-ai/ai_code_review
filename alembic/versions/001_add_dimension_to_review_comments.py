"""Add dimension column to review_comments.

Revision ID: 001_add_dimension
Revises:
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa

revision = "001_add_dimension"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "review_comments",
        sa.Column("dimension", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("review_comments", "dimension")
