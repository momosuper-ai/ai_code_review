"""Add partial unique index on readme_records for completed rows.

Revision ID: 004_readme_unique
Revises: 003_add_readme_records
Create Date: 2026-04-21
"""

from alembic import op

revision = "004_readme_unique"
down_revision = "003_add_readme_records"
branch_labels = None
depends_on = None


_INDEX_NAME = "ix_readme_records_unique_completed"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "readme_records",
        ["gitlab_project_id", "branch", "commit_sha"],
        unique=True,
        postgresql_where="status = 'completed'",
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="readme_records")
