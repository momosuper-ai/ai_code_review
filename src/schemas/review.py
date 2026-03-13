from datetime import datetime

from pydantic import BaseModel


class ReviewCommentOut(BaseModel):
    id: int
    file_path: str
    line_number: int | None
    severity: str
    category: str
    content: str
    suggestion: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReviewOut(BaseModel):
    id: int
    project_id: int
    merge_request_iid: int
    merge_request_title: str
    author: str
    source_branch: str
    target_branch: str
    status: str
    summary: str | None
    created_at: datetime
    completed_at: datetime | None
    comments: list[ReviewCommentOut] = []

    model_config = {"from_attributes": True}


class ReviewListOut(BaseModel):
    id: int
    project_id: int
    merge_request_iid: int
    merge_request_title: str
    author: str
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedbackCreate(BaseModel):
    comment_id: int
    feedback_type: str  # accepted / rejected
    reason: str | None = None


class ProjectCreate(BaseModel):
    name: str
    gitlab_project_id: int
    sonarqube_project_key: str | None = None
    enabled: bool = True


class ProjectOut(BaseModel):
    id: int
    name: str
    gitlab_project_id: int
    sonarqube_project_key: str | None
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class RuleCreate(BaseModel):
    language: str
    rule_content: str
    enabled: bool = True


class RuleOut(BaseModel):
    id: int
    project_id: int
    language: str
    rule_content: str
    enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}
