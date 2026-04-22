"""ORM model for AI_README generation tracking."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class ReadmeRecord(Base):
    __tablename__ = "readme_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gitlab_project_id: Mapped[int] = mapped_column(Integer, index=True)
    project_name: Mapped[str] = mapped_column(String(500))
    branch: Mapped[str] = mapped_column(String(255))
    commit_sha: Mapped[str] = mapped_column(String(40))
    result_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    root_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    subdirs_generated: Mapped[int] = mapped_column(Integer, default=0)
    structure_fingerprint: Mapped[str] = mapped_column(String(64))
    total_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # 仅 completed 状态下 (project_id, branch, commit_sha) 唯一；
    # failed 记录不受约束，保留排查历史
    __table_args__ = (
        Index(
            "ix_readme_records_unique_completed",
            "gitlab_project_id",
            "branch",
            "commit_sha",
            unique=True,
            postgresql_where="status = 'completed'",
        ),
    )
