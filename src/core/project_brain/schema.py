"""Canonical knowledge and compilation contracts."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KnowledgeType = Literal["FACT", "DERIVED", "AI_INFERRED"]
CardKind = Literal["project", "architecture", "module", "interface", "data", "flow"]


def stable_id(kind: str, *parts: str) -> str:
    """以结构化输入生成无歧义、可重复的实体 ID。"""
    raw = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


class Contract(BaseModel):
    """禁止未知字段的共同模型。"""

    model_config = ConfigDict(extra="forbid")


class SourceRef(Contract):
    """一个固定提交中的源码证据。"""

    id: str
    commit_sha: str
    path: str
    blob_sha: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol_id: str | None = None
    display_path: str | None = None

    @model_validator(mode="after")
    def valid_range(self) -> "SourceRef":
        """校验行范围。"""
        if self.line_end < self.line_start:
            raise ValueError("源码证据行范围无效")
        return self


class Fact(Contract):
    """提取器观察到的事实，不含源码全文和配置秘密值。"""

    id: str
    kind: str
    name: str
    module_id: str
    source_ref_ids: list[str]
    extractor: str
    attributes: dict = Field(default_factory=dict)


class Relation(Contract):
    """已解析、候选或未解析的关系。"""

    id: str
    from_id: str
    type: str
    to_id: str | None
    target_name: str
    resolution: Literal["resolved", "candidate", "unresolved"]
    knowledge_type: Literal["FACT", "DERIVED"] = "FACT"
    source_ref_ids: list[str]
    candidates: list[str] = Field(default_factory=list)


class InferenceScope(Contract):
    """Service-owned input provenance; it does not certify semantic support."""

    id: str
    kind: Literal["declaration_fragment", "summary_group", "summary_synthesis"]
    packet_digest: str
    fact_id: str | None
    relation_ids: list[str]
    source_ref_ids: list[str] = Field(min_length=1)
    fragment_index: int = Field(ge=0, strict=True)
    fragment_total: int = Field(ge=1, strict=True)
    child_ids: list[str] = Field(default_factory=list)
    module_ids: list[str] = Field(default_factory=list)
    relationship_view: Literal["symbol_graph", "resolution_counts"] | None = None

    @model_validator(mode="after")
    def valid_fragment(self) -> "InferenceScope":
        if self.fragment_index >= self.fragment_total:
            raise ValueError("推断来源分片无效")
        if self.kind == "summary_synthesis":
            if (self.fact_id is not None or self.relation_ids or not self.child_ids or not self.module_ids
                    or len(set(self.child_ids)) != len(self.child_ids)
                    or len(set(self.module_ids)) != len(self.module_ids)
                    or any(not value.strip() for value in self.child_ids + self.module_ids)):
                raise ValueError("综合来源范围无效")
        elif self.child_ids or self.module_ids or self.relationship_view is not None:
            raise ValueError("声明或局部汇总不得伪造综合来源身份")
        return self


class Claim(Contract):
    """带证据的知识结论。"""

    text: str = Field(min_length=1)
    knowledge_type: KnowledgeType
    evidence_ids: list[str] = Field(min_length=1)
    relation_ids: list[str] = Field(default_factory=list)
    source_scope: InferenceScope | None = None

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        """Empty whitespace cannot be published as an evidenced conclusion."""
        if not value.strip():
            raise ValueError("知识结论不得为空白")
        return value


class InferredClaim(Contract):
    """模型只能提供推断文本和已有证据 ID。"""

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    source_scope: InferenceScope | None = None

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        """Reject model output that has no actual conclusion."""
        if not value.strip():
            raise ValueError("知识结论不得为空白")
        return value


class Compilation(Contract):
    """语义编译的唯一模型输出格式。"""

    title: str = Field(min_length=1)
    claims: list[InferredClaim] = Field(min_length=1)
    unknowns: list[str]
    document_path: str | None = Field(
        default=None, pattern=r"^docs/project-brain/.+",
        description="包含 docs/project-brain/ 前缀的仓库根目录相对路径，目录内自由命名；无需建议时为 null。",
    )

    @field_validator("title")
    @classmethod
    def meaningful_title(cls, value: str) -> str:
        """Reject an empty semantic unit title."""
        if not value.strip():
            raise ValueError("知识标题不得为空白")
        return value


class KnowledgeCard(Contract):
    """用于机器消费及确定性 Markdown 渲染的知识卡片。"""

    id: str
    kind: CardKind
    title: str
    module_id: str | None
    source_commit: str
    schema_version: str = "1.0"
    claims: list[Claim]
    detail_claims: list[Claim] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    detail_unknowns: list[str] = Field(default_factory=list)
    coverage: str = "partial"
    document_path: str | None = None


class FactBundle(Contract):
    """一个提交的完整事实与证据集合。"""

    source_commit: str
    files: dict[str, dict]
    modules: dict[str, str]
    facts: list[Fact]
    relations: list[Relation]
    source_refs: list[SourceRef]
    issues: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    source_fingerprint: str
    observations: dict[str, dict] = Field(default_factory=dict)
    source_identity: dict | None = None
    display_paths: dict[str, str] = Field(default_factory=dict)


class BrainManifest(Contract):
    """已发布目录清单，所有权和文件完整性依据。"""

    owner: Literal["project-brain"] = "project-brain"
    schema_version: str = "1.0"
    generator: str
    build_id: str
    project_id: int
    branch: str
    source_commit: str
    brain_version: int
    recipe_hash: str
    source_fingerprint: str
    files: dict[str, str]
    coverage: dict
    issues: list[str]
    card_paths: dict[str, str] = Field(default_factory=dict)
    service_files: dict[str, str] = Field(default_factory=dict)
