"""Add token usage columns to reviews.

Revision ID: 002_add_tokens
Revises: 001_add_dimension
Create Date: 2026-04-15
"""

from alembic import op
import sqlalchemy as sa

revision = "002_add_tokens"
down_revision = "001_add_dimension"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reviews", sa.Column("total_prompt_tokens", sa.Integer(), server_default="0"))
    op.add_column("reviews", sa.Column("total_completion_tokens", sa.Integer(), server_default="0"))
    op.add_column("reviews", sa.Column("total_cached_tokens", sa.Integer(), server_default="0"))


def downgrade() -> None:
    op.drop_column("reviews", "total_cached_tokens")
    op.drop_column("reviews", "total_completion_tokens")
    op.drop_column("reviews", "total_prompt_tokens")
