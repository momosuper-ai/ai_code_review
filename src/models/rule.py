from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


class ReviewRule(Base):
    __tablename__ = "review_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    language: Mapped[str] = mapped_column(String(50), nullable=False, comment="编程语言：java/python/go/javascript 或 *（通用）")
    rule_content: Mapped[str] = mapped_column(Text, nullable=False, comment="审查规则内容（将注入 Prompt）")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
