from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models import Base


class ReviewStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"


class FeedbackType(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class Review(Base):
    __tablename__ = "reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    merge_request_iid: Mapped[int] = mapped_column(Integer, nullable=False, comment="GitLab MR IID")
    merge_request_title: Mapped[str] = mapped_column(String(500), nullable=False)
    author: Mapped[str] = mapped_column(String(255), nullable=False, comment="MR 作者")
    source_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    target_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[ReviewStatus] = mapped_column(Enum(ReviewStatus), default=ReviewStatus.PENDING)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="AI 审查总结")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True, comment="失败原因")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    comments: Mapped[list["ReviewComment"]] = relationship(back_populates="review", cascade="all, delete-orphan")
    feedbacks: Mapped[list["ReviewFeedback"]] = relationship(back_populates="review", cascade="all, delete-orphan")


class ReviewComment(Base):
    __tablename__ = "review_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="文件路径")
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="行号")
    severity: Mapped[Severity] = mapped_column(Enum(Severity), default=Severity.INFO)
    category: Mapped[str] = mapped_column(String(50), nullable=False, comment="问题类型：security/performance/maintainability/style/bug")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="审查意见（中文）")
    suggestion: Mapped[str | None] = mapped_column(Text, nullable=True, comment="修改建议")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    review: Mapped["Review"] = relationship(back_populates="comments")


class ReviewFeedback(Base):
    __tablename__ = "review_feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_id: Mapped[int] = mapped_column(ForeignKey("reviews.id"), nullable=False)
    comment_id: Mapped[int] = mapped_column(ForeignKey("review_comments.id"), nullable=False)
    feedback_type: Mapped[FeedbackType] = mapped_column(Enum(FeedbackType), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True, comment="反馈原因")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    review: Mapped["Review"] = relationship(back_populates="feedbacks")


class SonarResult(Base):
    __tablename__ = "sonar_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    task_id: Mapped[str] = mapped_column(String(255), nullable=False, comment="SonarQube 分析任务 ID")
    status: Mapped[str] = mapped_column(String(50), nullable=False, comment="Quality Gate 状态")
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    metrics: Mapped[str | None] = mapped_column(Text, nullable=True, comment="指标数据 (JSON)")
    raw_payload: Mapped[str | None] = mapped_column(Text, nullable=True, comment="原始 Webhook 数据")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
