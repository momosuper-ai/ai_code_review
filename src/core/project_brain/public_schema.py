"""Versioned, non-reversible public knowledge contracts.

These views are deliberately not FactBundle subclasses: a public graph cannot
be restored into the parser or used to resolve a source path.
"""

import json
from typing import Literal

from pydantic import Field, StrictBool, StrictFloat, StrictInt, model_validator

from src.core.project_brain.schema import BrainManifest, Contract, KnowledgeCard, Relation


class PublicEntry(Contract):
    """An object member keeps its identity when display keys collide."""

    id: str
    key: str
    value: "PublicValue"


class PublicValue(Contract):
    """A strict JSON tree; object names never become dictionary identities."""

    kind: Literal["object", "array", "string", "number", "boolean", "null"]
    entries: list[PublicEntry] | None = None
    items: list["PublicValue"] | None = None
    text: str | None = None
    number: StrictInt | StrictFloat | None = None
    boolean: StrictBool | None = None

    @model_validator(mode="after")
    def valid_shape(self) -> "PublicValue":
        present = {name for name in ("entries", "items", "text", "number", "boolean")
                   if getattr(self, name) is not None}
        expected = {"object": {"entries"}, "array": {"items"}, "string": {"text"},
                    "number": {"number"}, "boolean": {"boolean"}, "null": set()}[self.kind]
        if present != expected or (self.kind == "number" and type(self.number) not in {int, float}):
            raise ValueError("公开属性树结构无效")
        if self.entries is not None and len({entry.id for entry in self.entries}) != len(self.entries):
            raise ValueError("公开属性成员身份重复")
        return self


class PublicSourceRef(Contract):
    """An opaque locator is not a filesystem path or a clickable URL."""

    id: str
    source_file_id: str
    path: str
    commit_sha: str
    blob_sha: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    symbol_id: str | None = None
    display_path: str
    path_redacted: bool
    link_status: Literal["direct", "redacted", "unavailable"]
    source_url: str | None = None
    verification: Literal["service_verified"] = "service_verified"

    @model_validator(mode="after")
    def valid_reference(self) -> "PublicSourceRef":
        if (self.line_end < self.line_start or self.path != self.source_file_id
                or (self.link_status == "direct") != bool(self.source_url)
                or self.path_redacted and (self.source_url is not None or self.link_status != "redacted")):
            raise ValueError("公开来源展示或链接状态无效")
        return self


class PublicFile(Contract):
    source_file_id: str
    display_path: str
    path_redacted: bool
    metadata: PublicValue


class PublicFact(Contract):
    id: str
    kind: str
    name: str
    module_id: str
    source_ref_ids: list[str]
    extractor: str
    attributes: PublicValue


class PublicBundle(Contract):
    """Safe schema 2 graph, never a canonical extraction checkpoint."""

    schema_version: Literal["2.0"] = "2.0"
    projection_version: Literal["project-brain-public/1"] = "project-brain-public/1"
    source_commit: str
    source_fingerprint: str
    identity_version: str
    key_version: str
    files: dict[str, PublicFile]
    modules: dict[str, str]
    facts: list[PublicFact]
    relations: list[Relation]
    source_refs: list[PublicSourceRef]
    issues: list[str]
    frameworks: list[str]
    observations: dict[str, PublicValue]


class PublicKnowledgeCard(KnowledgeCard):
    schema_version: Literal["2.0"] = "2.0"


class PublicMetadata(Contract):
    branch_display: str | None = None
    branch_id: str
    project_url: str | None = None
    mr_url_base: str | None = None


class PublicProjectionInfo(Contract):
    version: Literal["project-brain-public/1"] = "project-brain-public/1"
    identity_version: str
    key_version: str
    privacy_seal: str
    content_digest: str
    redacted_source_paths: int = Field(ge=0)
    direct_source_links: int = Field(ge=0)
    source_reference_count: int = Field(ge=0)
    direct_link_ratio: float = Field(ge=0, le=1)


class PublicBrainManifest(BrainManifest):
    schema_version: Literal["2.0"] = "2.0"
    projection: PublicProjectionInfo
    public_metadata: PublicMetadata


def parse_manifest(value: str | bytes | dict) -> BrainManifest | PublicBrainManifest:
    """Dispatch an explicit public schema, preserving legacy contracts."""
    def unique(pairs):
        result = {}
        for key, child in pairs:
            if key in result:
                raise ValueError("知识清单字段重复")
            result[key] = child
        return result
    data = json.loads(value, object_pairs_hook=unique) if isinstance(value, (str, bytes)) else value
    if type(data) is not dict:
        raise ValueError("知识清单结构无效")
    version = data.get("schema_version", "1.0")
    if version == "2.0":
        return PublicBrainManifest.model_validate(data)
    if version == "1.0":
        return BrainManifest.model_validate(data)
    raise ValueError("不支持的公开知识版本")


PublicValue.model_rebuild()
