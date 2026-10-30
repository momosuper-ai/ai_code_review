"""Persist safe Project Brain unit and build diagnostics.

Revision ID: 011_brain_diagnostics
Revises: 010_brain_control
"""
from alembic import op
import sqlalchemy as sa

revision = "011_brain_diagnostics"
down_revision = "010_brain_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("brain_units", sa.Column("diagnostic", sa.JSON(), nullable=True))
    op.add_column("brain_builds", sa.Column("diagnostic", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("brain_builds", "diagnostic")
    op.drop_column("brain_units", "diagnostic")
