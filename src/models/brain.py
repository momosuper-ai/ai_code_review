"""Project Brain lifecycle, checkpoints, and publication records."""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base


def utcnow() -> datetime:
    """返回无时区的 UTC 时间，与数据库的时间比较保持一致。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BrainProject(Base):
    """按 GitLab 项目和分支登记知识维护配置。"""

    __tablename__ = "brain_projects"
    __table_args__ = (UniqueConstraint("gitlab_project_id", "branch"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gitlab_project_id: Mapped[int] = mapped_column(Integer, index=True)
    project_name: Mapped[str] = mapped_column(String(500))
    branch: Mapped[str] = mapped_column(String(255))
    auto_update: Mapped[bool] = mapped_column(Boolean, default=True)
    maintenance_state: Mapped[str] = mapped_column(String(16), default="active")
    event_seq: Mapped[int] = mapped_column(Integer, default=0)
    source_status: Mapped[str] = mapped_column(String(16), default="unknown")
    last_observed_head: Mapped[str | None] = mapped_column(String(64))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    gitlab_sync: Mapped[bool] = mapped_column(Boolean, default=True)
    request_seq: Mapped[int] = mapped_column(Integer, default=0)
    next_version: Mapped[int] = mapped_column(Integer, default=1)
    active_build_id: Mapped[str | None] = mapped_column(String(36))
    desired_sha: Mapped[str | None] = mapped_column(String(64))
    observed_fingerprint: Mapped[str | None] = mapped_column(String(64))
    dirty_at: Mapped[datetime | None] = mapped_column(DateTime)
    dirty_since: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BrainBuild(Base):
    """一次绑定源码 SHA 和生成配方的构建。"""

    __tablename__ = "brain_builds"
    __table_args__ = (
        UniqueConstraint("project_id", "source_commit", "recipe_hash", "mode", "generation", name="uq_brain_build_generation"),
        CheckConstraint("execution_state IN ('running', 'pausing', 'paused')", name="ck_brain_build_execution_state"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    project_id: Mapped[int] = mapped_column(ForeignKey("brain_projects.id"), index=True)
    source_commit: Mapped[str] = mapped_column(String(64))
    recipe_hash: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(10), default="auto")
    generation: Mapped[int] = mapped_column(Integer, default=0)
    request_seq: Mapped[int] = mapped_column(Integer)
    brain_version: Mapped[int] = mapped_column(Integer)
    historical: Mapped[bool] = mapped_column(Boolean, default=False)
    base_build_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    execution_state: Mapped[str] = mapped_column(String(16), default="running", server_default="running")
    pause_requested_at: Mapped[datetime | None] = mapped_column(DateTime)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))
    artifact_path: Mapped[str | None] = mapped_column(String(500))
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)
    total_prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    usage_tracking_version: Mapped[int] = mapped_column(Integer, default=1, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class BrainUnit(Base):
    """独立阶段/编译单元的可恢复检查点和带令牌租约。"""

    __tablename__ = "brain_units"
    __table_args__ = (UniqueConstraint("build_id", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[str] = mapped_column(ForeignKey("brain_builds.id"), index=True)
    key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24))
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)


class BrainPublication(Base):
    """独立于知识就绪状态的 GitLab 发布状态。"""

    __tablename__ = "brain_publications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    build_id: Mapped[str] = mapped_column(ForeignKey("brain_builds.id"), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    target_branch: Mapped[str] = mapped_column(String(255))
    temp_branch: Mapped[str | None] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    merge_commit_sha: Mapped[str | None] = mapped_column(String(64))
    mr_iid: Mapped[int | None] = mapped_column(Integer)
    mr_url: Mapped[str | None] = mapped_column(String(1000))
    lease_token: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    error_code: Mapped[str | None] = mapped_column(String(40))
    policy_version: Mapped[str] = mapped_column(String(16), default="2")


class BrainModelCall(Base):
    """A budget reservation and its numeric settlement, without request bodies."""

    __tablename__ = "brain_model_calls"
    __table_args__ = (UniqueConstraint("transport_group", "transport_attempt", name="uq_brain_call_transport_attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    build_id: Mapped[str] = mapped_column(ForeignKey("brain_builds.id", ondelete="CASCADE"), index=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("brain_units.id", ondelete="CASCADE"), index=True)
    lease_token: Mapped[str] = mapped_column(String(36))
    stage: Mapped[str] = mapped_column(String(24))
    role: Mapped[str] = mapped_column(String(16))
    purpose: Mapped[str] = mapped_column(String(32))
    requested_model: Mapped[str] = mapped_column(String(255))
    response_model: Mapped[str | None] = mapped_column(String(255))
    attempt: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="reserved")
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime)
    transport_group: Mapped[str | None] = mapped_column(String(36))
    transport_attempt: Mapped[int | None] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)


class BrainRecovery(Base):
    """A frozen, body-free checkpoint inheritance plan for a new build.

    ``plan`` is a closed-schema set of machine identities and hashes, validated
    by the recovery service. It must never contain model responses, source
    excerpts, credentials, or reversible privacy mappings. Neither foreign key
    cascades: a recovery dependency must be handled explicitly before retention
    can remove either build or its authenticated private checkpoints.
    """

    __tablename__ = "brain_recoveries"
    __table_args__ = (
        CheckConstraint("child_build_id <> parent_build_id", name="ck_brain_recovery_distinct_builds"),
    )
    child_build_id: Mapped[str] = mapped_column(ForeignKey("brain_builds.id", ondelete="RESTRICT"), primary_key=True)
    parent_build_id: Mapped[str] = mapped_column(ForeignKey("brain_builds.id", ondelete="RESTRICT"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    plan_version: Mapped[str] = mapped_column(String(32))
    compatibility_hash: Mapped[str] = mapped_column(String(64))
    manifest_hash: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    imports: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(48))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
