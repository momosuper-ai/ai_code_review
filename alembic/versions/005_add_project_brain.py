"""Add durable Project Brain lifecycle tables.

Revision ID: 005_project_brain
Revises: 004_readme_unique
"""

from alembic import op

import sqlalchemy as sa

revision = "005_project_brain"
down_revision = "004_readme_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建知识表，定义不依赖未来 ORM 版本。"""
    op.create_table("brain_projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("gitlab_project_id", sa.Integer(), nullable=False),
        sa.Column("project_name", sa.String(500), nullable=False),
        sa.Column("branch", sa.String(255), nullable=False),
        sa.Column("auto_update", sa.Boolean(), nullable=False),
        sa.Column("gitlab_sync", sa.Boolean(), nullable=False),
        sa.Column("request_seq", sa.Integer(), nullable=False),
        sa.Column("next_version", sa.Integer(), nullable=False),
        sa.Column("active_build_id", sa.String(36)),
        sa.Column("desired_sha", sa.String(64)),
        sa.Column("observed_fingerprint", sa.String(64)),
        sa.Column("dirty_at", sa.DateTime()), sa.Column("dirty_since", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("gitlab_project_id", "branch"))
    op.create_table("brain_builds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("brain_projects.id"), nullable=False),
        sa.Column("source_commit", sa.String(64), nullable=False),
        sa.Column("recipe_hash", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("request_seq", sa.Integer(), nullable=False),
        sa.Column("brain_version", sa.Integer(), nullable=False),
        sa.Column("historical", sa.Boolean(), nullable=False),
        sa.Column("base_build_id", sa.String(36)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("source_fingerprint", sa.String(64)),
        sa.Column("artifact_path", sa.String(500)),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("total_prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("total_completion_tokens", sa.Integer(), nullable=False),
        sa.Column("total_cached_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("project_id", "source_commit", "recipe_hash", "mode"))
    op.create_table("brain_units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("build_id", sa.String(36), sa.ForeignKey("brain_builds.id"), nullable=False),
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("lease_token", sa.String(36)), sa.Column("lease_until", sa.DateTime()),
        sa.Column("dispatched_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result", sa.JSON()), sa.Column("error_message", sa.Text()),
        sa.UniqueConstraint("build_id", "key"))
    op.create_table("brain_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("build_id", sa.String(36), sa.ForeignKey("brain_builds.id"), nullable=False, unique=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("target_branch", sa.String(255), nullable=False),
        sa.Column("temp_branch", sa.String(255)),
        sa.Column("commit_sha", sa.String(64)), sa.Column("merge_commit_sha", sa.String(64)),
        sa.Column("mr_iid", sa.Integer()), sa.Column("mr_url", sa.String(1000)),
        sa.Column("lease_token", sa.String(36)), sa.Column("lease_until", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False))
    for table, columns in {"brain_projects": ["gitlab_project_id"], "brain_builds": ["project_id", "status"],
                           "brain_units": ["build_id", "status"], "brain_publications": ["status"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    """移除知识表；操作前必须备份不可变产物和数据库。"""
    for table in ("brain_publications", "brain_units", "brain_builds", "brain_projects"):
        op.drop_table(table)
