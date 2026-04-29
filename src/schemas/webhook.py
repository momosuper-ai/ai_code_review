from pydantic import BaseModel


class MergeRequestAttributes(BaseModel):
    iid: int
    title: str
    action: str | None = None
    target_branch: str
    source_branch: str
    state: str | None = None
    last_commit: dict | None = None


class MergeRequestEvent(BaseModel):
    object_kind: str
    project: dict
    object_attributes: MergeRequestAttributes
    user: dict | None = None


class PushEvent(BaseModel):
    object_kind: str
    project: dict
    ref: str
    before: str
    after: str
    total_commits_count: int
    user_name: str | None = None
    user_username: str | None = None
    commits: list[dict] = []
