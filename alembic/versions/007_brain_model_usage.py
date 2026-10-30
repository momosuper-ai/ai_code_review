"""Persist body-free model accounting alongside each budget reservation.

Revision ID: 007_brain_usage
Revises: 006_brain_auto
"""
from alembic import op
import sqlalchemy as sa

revision = "007_brain_usage"
down_revision = "006_brain_auto"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing totals cannot be attributed retrospectively. ORM new builds use
    # version 1 explicitly; the server default remains 0 for old applications.
    op.add_column("brain_builds", sa.Column("usage_tracking_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_table("brain_model_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("build_id", sa.String(36), sa.ForeignKey("brain_builds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("unit_id", sa.Integer(), sa.ForeignKey("brain_units.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lease_token", sa.String(36), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("requested_model", sa.String(255), nullable=False),
        sa.Column("response_model", sa.String(255)),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime()))
    op.create_index("ix_brain_model_calls_build_id", "brain_model_calls", ["build_id"])
    op.create_index("ix_brain_model_calls_unit_id", "brain_model_calls", ["unit_id"])


def downgrade() -> None:
    op.drop_index("ix_brain_model_calls_unit_id", table_name="brain_model_calls")
    op.drop_index("ix_brain_model_calls_build_id", table_name="brain_model_calls")
    op.drop_table("brain_model_calls")
    op.drop_column("brain_builds", "usage_tracking_version")
