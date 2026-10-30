"""Persist authenticated checkpoint recovery dependencies without bodies.

Revision ID: 009_brain_recovery
Revises: 008_brain_http
"""
from alembic import op
import sqlalchemy as sa

revision = "009_brain_recovery"
down_revision = "008_brain_http"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "brain_recoveries",
        sa.Column("child_build_id", sa.String(36), sa.ForeignKey("brain_builds.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("parent_build_id", sa.String(36), sa.ForeignKey("brain_builds.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("plan_version", sa.String(32), nullable=False),
        sa.Column("compatibility_hash", sa.String(64), nullable=False),
        sa.Column("manifest_hash", sa.String(64)),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.Column("imports", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(48)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("frozen_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
        sa.CheckConstraint("child_build_id <> parent_build_id", name="ck_brain_recovery_distinct_builds"),
    )
    op.create_index("ix_brain_recoveries_parent_build_id", "brain_recoveries", ["parent_build_id"])
    op.create_index("ix_brain_recoveries_status", "brain_recoveries", ["status"])


def downgrade() -> None:
    # Removing dependency records could silently make retention delete inherited
    # private checkpoints or hide recovery lineage. Do not erase that history.
    bind = op.get_bind()
    recoveries = sa.table("brain_recoveries", sa.column("child_build_id", sa.String(36)))
    if bind.execute(sa.select(recoveries.c.child_build_id).limit(1)).first() is not None:
        raise RuntimeError("brain_recovery_downgrade_requires_empty_history")
    op.drop_index("ix_brain_recoveries_status", table_name="brain_recoveries")
    op.drop_index("ix_brain_recoveries_parent_build_id", table_name="brain_recoveries")
    op.drop_table("brain_recoveries")
