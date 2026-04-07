import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models import Base


class TriggerType(str, enum.Enum):
    merge_request = "merge_request"
    push = "push"


class ReviewStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


class Severity(str, enum.Enum):
    critical = "critical"
    major = "major"
    minor = "minor"
    info = "info"


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gitlab_project_id: Mapped[int] = mapped_column(Integer, index=True)
    project_name: Mapped[str] = mapped_column(String(500))
    trigger_type: Mapped[TriggerType] = mapped_column(Enum(TriggerType))
    merge_request_iid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    merge_request_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    commit_sha: Mapped[str] = mapped_column(String(40))
    branch: Mapped[str] = mapped_column(String(255))
    author: Mapped[str] = mapped_column(String(255))
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(ReviewStatus), default=ReviewStatus.pending
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    files_reviewed: Mapped[int] = mapped_column(Integer, default=0)
    issues_found: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    comments: Mapped[list["ReviewComment"]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


class ReviewComment(Base):
    __tablename__ = "review_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"))
    file_path: Mapped[str] = mapped_column(String(500))
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    severity: Mapped[Severity] = mapped_column(Enum(Severity))
    category: Mapped[str] = mapped_column(String(50))
    content: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    dimension: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    review: Mapped["Review"] = relationship(back_populates="comments")
