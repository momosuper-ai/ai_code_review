"""Persist build pause intent independently from build stage and maintenance.

Revision ID: 010_brain_control
Revises: 009_brain_recovery
"""
from alembic import op
import sqlalchemy as sa

revision = "010_brain_control"
down_revision = "009_brain_recovery"
branch_labels = None
depends_on = None

_CHECK = "ck_brain_build_execution_state"


def upgrade() -> None:
    op.add_column("brain_builds", sa.Column("execution_state", sa.String(16), nullable=False, server_default="running"))
    for name in ("pause_requested_at", "paused_at", "resumed_at"):
        op.add_column("brain_builds", sa.Column(name, sa.DateTime()))
    # SQLite is used only by the isolated migration contract tests. PostgreSQL
    # adds the constraint without rebuilding any existing checkpoint tables.
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("brain_builds") as batch:
            batch.create_check_constraint(_CHECK, "execution_state IN ('running', 'pausing', 'paused')")
    else:
        op.create_check_constraint(_CHECK, "brain_builds", "execution_state IN ('running', 'pausing', 'paused')")


def downgrade() -> None:
    # Losing a durable pause intent could let an older worker start generation.
    builds = sa.table("brain_builds", sa.column("execution_state", sa.String(16)))
    if op.get_bind().execute(sa.select(builds.c.execution_state).where(
            builds.c.execution_state.in_(("pausing", "paused"))).limit(1)).first() is not None:
        raise RuntimeError("brain_control_downgrade_requires_no_pause_intent")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("brain_builds") as batch:
            batch.drop_constraint(_CHECK, type_="check")
            for name in ("resumed_at", "paused_at", "pause_requested_at", "execution_state"):
                batch.drop_column(name)
    else:
        op.drop_constraint(_CHECK, "brain_builds", type_="check")
        for name in ("resumed_at", "paused_at", "pause_requested_at", "execution_state"):
            op.drop_column("brain_builds", name)
