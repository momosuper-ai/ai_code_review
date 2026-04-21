"""Add readme_records table.

Revision ID: 003_add_readme_records
Revises: 002_add_tokens
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa

revision = "003_add_readme_records"
down_revision = "002_add_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "readme_records",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("gitlab_project_id", sa.Integer(), nullable=False, index=True),
        sa.Column("project_name", sa.String(500), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("commit_sha", sa.String(40), nullable=False),
        sa.Column("result_commit_sha", sa.String(40), nullable=True),
        sa.Column("root_generated", sa.Boolean(), server_default="false"),
        sa.Column("subdirs_generated", sa.Integer(), server_default="0"),
        sa.Column("structure_fingerprint", sa.String(64), nullable=False),
        sa.Column("total_prompt_tokens", sa.Integer(), server_default="0"),
        sa.Column("total_completion_tokens", sa.Integer(), server_default="0"),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("readme_records")
