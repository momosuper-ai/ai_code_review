"""Automatic maintenance and independently versioned publication policy."""

from alembic import op
import sqlalchemy as sa

revision = "006_brain_auto"
down_revision = "005_project_brain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保守迁移既有开关，历史归档不自动接管文档。"""
    op.add_column("brain_projects", sa.Column("maintenance_state", sa.String(16), nullable=False, server_default="unmanaged"))
    op.add_column("brain_projects", sa.Column("event_seq", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("brain_projects", sa.Column("source_status", sa.String(16), nullable=False, server_default="unknown"))
    op.add_column("brain_projects", sa.Column("last_observed_head", sa.String(64), nullable=True))
    op.add_column("brain_projects", sa.Column("last_checked_at", sa.DateTime(), nullable=True))
    op.execute("UPDATE brain_projects SET maintenance_state = CASE WHEN auto_update THEN 'active' ELSE 'paused' END "
               "WHERE EXISTS (SELECT 1 FROM brain_builds WHERE brain_builds.project_id = brain_projects.id AND historical = false)")
    op.add_column("brain_publications", sa.Column("error_code", sa.String(40), nullable=True))
    op.add_column("brain_publications", sa.Column("policy_version", sa.String(16), nullable=False, server_default="2"))
    op.add_column("brain_builds", sa.Column("generation", sa.Integer(), nullable=False, server_default="0"))
    # 005 的未命名唯一约束在 PostgreSQL 使用该默认名；SQLite 反射时赋同名。
    with op.batch_alter_table("brain_builds", naming_convention={"uq": "brain_builds_project_id_source_commit_recipe_hash_mode_key"}) as batch:
        batch.drop_constraint("brain_builds_project_id_source_commit_recipe_hash_mode_key", type_="unique")
        batch.create_unique_constraint("uq_brain_build_generation", ["project_id", "source_commit", "recipe_hash", "mode", "generation"])


def downgrade() -> None:
    """移除维护调度字段，保留原有知识版本。"""
    # 005 无法表示已过期产物后的多代重建；有此类记录时拒绝丢失历史，默认保留表回滚应用。
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM brain_builds WHERE generation > 0 LIMIT 1")).first():
        raise RuntimeError("存在同输入的多代构建，请保留 Brain 表回滚应用；不可自动降级 005")
    with op.batch_alter_table("brain_builds") as batch:
        batch.drop_constraint("uq_brain_build_generation", type_="unique")
        batch.drop_column("generation")
        batch.create_unique_constraint("brain_builds_project_id_source_commit_recipe_hash_mode_key", ["project_id", "source_commit", "recipe_hash", "mode"])
    for name in ("policy_version", "error_code"):
        op.drop_column("brain_publications", name)
    for name in ("last_checked_at", "last_observed_head", "source_status", "event_seq", "maintenance_state"):
        op.drop_column("brain_projects", name)
