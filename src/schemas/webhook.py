from pydantic import BaseModel


class GitLabMRAttributes(BaseModel):
    iid: int
    title: str
    source_branch: str
    target_branch: str
    action: str | None = None
    state: str | None = None


class GitLabUser(BaseModel):
    username: str
    name: str | None = None


class GitLabProject(BaseModel):
    id: int
    name: str
    path_with_namespace: str


class GitLabMRWebhook(BaseModel):
    object_kind: str
    event_type: str | None = None
    user: GitLabUser
    project: GitLabProject
    object_attributes: GitLabMRAttributes


class SonarQubeProject(BaseModel):
    key: str
    name: str | None = None


class SonarQubeQualityGate(BaseModel):
    status: str
    conditions: list[dict] | None = None


class SonarQubeWebhook(BaseModel):
    serverUrl: str | None = None
    taskId: str
    status: str
    project: SonarQubeProject
    branch: dict | None = None
    qualityGate: SonarQubeQualityGate | None = None
