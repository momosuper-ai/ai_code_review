"""Record bounded HTTP retries without inventing provider usage.

Revision ID: 008_brain_http
Revises: 007_brain_usage
"""
from alembic import op
import sqlalchemy as sa

revision = "008_brain_http"
down_revision = "007_brain_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("brain_model_calls") as batch:
        batch.add_column(sa.Column("transport_group", sa.String(36)))
        batch.add_column(sa.Column("transport_attempt", sa.Integer()))
        batch.add_column(sa.Column("http_status", sa.Integer()))
        batch.add_column(sa.Column("closed_at", sa.DateTime()))
        batch.create_unique_constraint("uq_brain_call_transport_attempt", ["transport_group", "transport_attempt"])


def downgrade() -> None:
    with op.batch_alter_table("brain_model_calls") as batch:
        batch.drop_constraint("uq_brain_call_transport_attempt", type_="unique")
        for name in ("closed_at", "http_status", "transport_attempt", "transport_group"):
            batch.drop_column(name)
