from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="项目名称")
    gitlab_project_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, comment="GitLab 项目 ID")
    sonarqube_project_key: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="SonarQube 项目 Key")
    review_rules: Mapped[str | None] = mapped_column(Text, nullable=True, comment="审查规则配置 (YAML)")
    enabled: Mapped[bool] = mapped_column(default=True, comment="是否启用 AI 审查")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
