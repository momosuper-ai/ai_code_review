"""Batch explanations in immutable source scopes, without model-created IDs."""

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from pydantic import Field, field_validator
from pydantic_core import PydanticCustomError

from src.core.project_brain.context import CompactContext
from src.core.project_brain.schema import Compilation, Contract, InferenceScope, InferredClaim, Relation, SourceRef, stable_id
from src.core.project_brain.synthesis_wire import compact_synthesis_wire, resolution_counts_wire, synthesis_relationship_view


def _encoded(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SourceBoundContractCategory(str, Enum):
    """Fixed diagnostic categories, independent of model-created content."""

    DUPLICATE_JSON_KEYS = "duplicate_json_keys"
    SLOT_SET_MISMATCH = "slot_set_mismatch"


class SourceBoundContractError(ValueError):
    """Expose only a fixed category and counts, never rejected keys or values."""

    def __init__(
        self,
        category: SourceBoundContractCategory,
        *,
        missing_count: int = 0,
        extra_count: int = 0,
        duplicate_count: int = 0,
    ) -> None:
        if not isinstance(category, SourceBoundContractCategory):
            raise ValueError("来源绑定诊断类别无效")
        counts = (missing_count, extra_count, duplicate_count)
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("来源绑定诊断计数无效")
        self.category = category
        self.missing_count = missing_count
        self.extra_count = extra_count
        self.duplicate_count = duplicate_count
        super().__init__(
            f"来源绑定输出契约无效: {category.value} "
            f"missing={missing_count} extra={extra_count} duplicate={duplicate_count}"
        )


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    duplicates = 0
    for key, value in pairs:
        if key in result:
            duplicates += 1
        result[key] = value
    if duplicates:
        # json.loads visits nested objects first. Count the duplicate
        # occurrences in the first rejected object; do not retain its keys.
        raise SourceBoundContractError(
            SourceBoundContractCategory.DUPLICATE_JSON_KEYS,
            duplicate_count=duplicates,
        )
    return result


class SlotText(Contract):
    """The model can write prose, never provenance or source identifiers."""

    texts: list[str] = Field(min_length=1)
    unknowns: list[str]

    @field_validator("texts")
    @classmethod
    def meaningful_texts(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise PydanticCustomError("source_bound_blank_text", "来源绑定解释不得为空白")
        return values


class BoundResponse(Contract):
    """Fixed slots are additionally checked against the private source plan."""

    title: str = Field(min_length=1)
    slots: dict[str, SlotText]
    unknowns: list[str]
    document_path: str | None = Field(default=None, pattern=r"^docs/project-brain/.+")


class SourceBoundPathError(ValueError):
    """A fixed path error category, with no untrusted output in the message."""


@dataclass(frozen=True)
class SourceBoundPlan:
    """Only the service chooses the exact evidence scope of every slot."""

    scopes: dict[str, InferenceScope]
    item_slots: tuple[str, ...]
    synthesis_wire: dict | None = None

    @classmethod
    def from_packet(cls, packet: dict, context: CompactContext, *, canonical_digest: str | None = None,
                    canonical_relation_digest: str | None = None) -> "SourceBoundPlan":
        if "synthesis" in packet:
            return cls._synthesis(packet, context, canonical_digest=canonical_digest,
                                  canonical_relation_digest=canonical_relation_digest)
        normalized = copy.deepcopy(packet)
        for item in normalized["items"]:
            for ref in item.get("evidence", []):
                ref.pop("commit_sha", None)
        if canonical_digest is not None and (type(canonical_digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", canonical_digest)):
            raise ValueError("来源绑定规范摘要无效")
        digest = canonical_digest or hashlib.sha256(_encoded(normalized).encode()).hexdigest()
        all_refs = set(context.evidence_ids.values())
        scopes = {}
        for index, item in enumerate(packet["items"], 1):
            slot = f"S{index}"
            fact = item["fact"]
            relations = item.get("relations", [])
            supplied = {ref["id"] for ref in item.get("evidence", [])}
            refs = set(fact.get("source_ref_ids", []))
            kind = "declaration_fragment"
            if not fact.get("id"):
                kind = "summary_group"
                refs.update(rid for claim in fact.get("claims", []) for rid in claim["evidence_ids"])
                if not refs:
                    # A summary fragment containing only limitations retains
                    # the complete parent group's explicitly supplied scope.
                    refs = supplied
            refs.update(rid for relation in relations for rid in relation["source_ref_ids"])
            if not refs or not refs <= supplied or not refs <= all_refs:
                raise ValueError("来源绑定槽位缺少有效的主体证据")
            if kind == "declaration_fragment" and supplied != refs:
                raise ValueError("来源绑定声明包含范围外证据")
            fragment = item.get("fragment", {"index": 0, "total": 1})
            scope = InferenceScope(id=stable_id("scope", digest, slot), kind=kind,
                packet_digest=digest, fact_id=fact.get("id"),
                relation_ids=sorted(relation["id"] for relation in relations) if kind == "declaration_fragment" else [],
                source_ref_ids=sorted(refs), fragment_index=fragment["index"],
                fragment_total=fragment["total"])
            scopes[slot] = scope
        if not scopes:
            raise ValueError("来源绑定知识包没有可解释主体")
        return cls(scopes, tuple(scopes))

    @classmethod
    def _synthesis(cls, packet: dict, context: CompactContext, *, canonical_digest: str | None = None,
                   canonical_relation_digest: str | None = None) -> "SourceBoundPlan":
        """Bind a complete child collection to one service-owned synthesis slot.

        The canonical packet remains intact and is scanned before transport.
        Its provenance and raw graph stay in the service. Initial module layers
        see explicit symbol relationships; upper layers see exact resolution
        counts, not endpoints or dependencies. Every child overview remains.
        """
        try:
            return cls._validated_synthesis(packet, context, canonical_digest=canonical_digest,
                                            canonical_relation_digest=canonical_relation_digest)
        except (KeyError, TypeError, ValueError):
            # Pydantic failures and arbitrary keys/values must not escape into
            # task diagnostics. All malformed synthesis input shares one code.
            raise ValueError("综合知识输入契约无效") from None

    @classmethod
    def _validated_synthesis(cls, packet: dict, context: CompactContext, *, canonical_digest: str | None = None,
                             canonical_relation_digest: str | None = None) -> "SourceBoundPlan":
        if set(packet) != {"module_id", "module", "items", "coverage", "synthesis"}:
            raise ValueError("invalid synthesis envelope")
        meta = packet["synthesis"]
        required = {"purpose", "level", "index", "child_ids", "module_ids", "leaf_ids", "child_hashes", "all_unknowns", "selections"}
        if not isinstance(meta, dict) or set(meta) != required or meta["purpose"] not in {"module", "project"}:
            raise ValueError("invalid synthesis metadata")
        if any(type(meta[key]) is not int or meta[key] < 0 for key in ("level", "index")):
            raise ValueError("invalid synthesis ordinal")

        def strings(value: object, *, unique: bool = False, nonempty: bool = False) -> list[str]:
            if not isinstance(value, list) or any(type(item) is not str for item in value):
                raise ValueError("invalid synthesis strings")
            if nonempty and (not value or any(not item.strip() for item in value)):
                raise ValueError("empty synthesis identities")
            if unique and len(set(value)) != len(value):
                raise ValueError("duplicate synthesis identities")
            return value

        child_ids = strings(meta["child_ids"], unique=True, nonempty=True)
        module_ids = strings(meta["module_ids"], unique=True, nonempty=True)
        strings(meta["leaf_ids"], unique=True, nonempty=True)
        strings(meta["all_unknowns"])
        strings(packet["coverage"])
        if (type(packet["module_id"]) is not str or type(packet["module"]) is not str
                or not packet["module_id"].strip() or not packet["module"].strip()
                or (meta["purpose"] == "module" and module_ids != [packet["module_id"]])):
            raise ValueError("invalid synthesis module")
        hashes = meta["child_hashes"]
        if (not isinstance(hashes, dict) or set(hashes) != set(child_ids)
                or any(type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest) for digest in hashes.values())):
            raise ValueError("invalid synthesis checkpoint hashes")
        selections = meta["selections"]
        selection_fields = {"source_key", "claim_start", "claim_end", "unknown_start", "unknown_end"}
        if not isinstance(selections, dict) or set(selections) != set(child_ids):
            raise ValueError("invalid synthesis selections")
        for child_id, selection in selections.items():
            if (not isinstance(selection, dict) or set(selection) != selection_fields
                    or type(selection["source_key"]) is not str or not selection["source_key"].strip()
                    or any(type(selection[key]) is not int or selection[key] < 0
                           for key in selection_fields - {"source_key"})
                    or selection["claim_start"] > selection["claim_end"]
                    or selection["unknown_start"] > selection["unknown_end"]):
                raise ValueError("invalid synthesis selection range")
            expected = stable_id("summary-input", selection["source_key"],
                                *(str(selection[key]) for key in ("claim_start", "claim_end", "unknown_start", "unknown_end")))
            if expected != child_id:
                raise ValueError("invalid synthesis selection identity")
        items = packet["items"]
        if not isinstance(items, list) or len(items) != len(child_ids):
            raise ValueError("invalid synthesis child count")
        refs = set()
        children = []
        relation_records: dict[str, dict] = {}
        source_records: dict[str, dict] = {}
        symbol_children: dict[str, set[str]] = {}
        for index, (item, child_id) in enumerate(zip(items, child_ids, strict=True), 1):
            if not isinstance(item, dict) or set(item) != {"fact", "relations", "evidence"}:
                raise ValueError("invalid synthesis item")
            fact = item["fact"]
            if (not isinstance(fact, dict) or set(fact) != {"module", "title", "claims", "unknowns", "child_id"}
                    or fact["child_id"] != child_id or type(fact["module"]) is not str
                    or type(fact["title"]) is not str or not fact["title"].strip()):
                raise ValueError("invalid synthesis child")
            strings(fact["unknowns"])
            selection = selections[child_id]
            if (not isinstance(fact["claims"], list) or not fact["claims"] and not fact["unknowns"]
                    or len(fact["claims"]) != selection["claim_end"] - selection["claim_start"]
                    or len(fact["unknowns"]) != selection["unknown_end"] - selection["unknown_start"]):
                raise ValueError("invalid synthesis claims")
            claims = [InferredClaim.model_validate(claim, strict=True) for claim in fact["claims"]]
            if not isinstance(item["evidence"], list) or not item["evidence"]:
                raise ValueError("invalid synthesis evidence")
            supplied = set()
            alias = f"C{index}"
            for raw in item["evidence"]:
                source = SourceRef.model_validate(raw, strict=True)
                value = source.model_dump()
                if source.id in source_records and source_records[source.id] != value:
                    raise ValueError("conflicting synthesis source")
                source_records[source.id] = value
                supplied.add(source.id)
                if source.symbol_id:
                    symbol_children.setdefault(source.symbol_id, set()).add(alias)
            refs.update(supplied)
            if any(not set(claim.evidence_ids) <= supplied for claim in claims):
                raise ValueError("invalid synthesis claim evidence")
            for claim in claims:
                if claim.source_scope and not set(claim.source_scope.source_ref_ids) <= supplied:
                    raise ValueError("invalid synthesis inherited scope")
            if not isinstance(item["relations"], list):
                raise ValueError("invalid synthesis relations")
            for raw in item["relations"]:
                relation = Relation.model_validate(raw, strict=True)
                if ((relation.resolution == "resolved") != bool(relation.to_id)
                        or relation.resolution == "candidate" and not relation.candidates
                        or not set(relation.source_ref_ids) <= supplied):
                    raise ValueError("invalid synthesis relationship state")
                value = relation.model_dump()
                if relation.id in relation_records and relation_records[relation.id] != value:
                    raise ValueError("conflicting synthesis relationship")
                relation_records[relation.id] = value
            children.append({"id": alias, "checkpoint_digest": hashes[child_id],
                             "module": fact["module"], "title": fact["title"],
                             "claims": [{"text": claim.text, "knowledge_type": "AI_INFERRED"} for claim in claims],
                             "unknowns": copy.deepcopy(fact["unknowns"]),
                             "selection": {key: selection[key] for key in ("claim_start", "claim_end", "unknown_start", "unknown_end")}})
        if not refs or not refs <= set(context.evidence_ids.values()):
            raise ValueError("invalid synthesis complete evidence pool")
        if canonical_digest is not None and (type(canonical_digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", canonical_digest)):
            raise ValueError("invalid synthesis canonical digest")
        if canonical_relation_digest is not None and (type(canonical_relation_digest) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", canonical_relation_digest)):
            raise ValueError("invalid synthesis canonical relationship digest")
        digest = canonical_digest or hashlib.sha256(_encoded(packet).encode()).hexdigest()
        view = synthesis_relationship_view(meta["purpose"], meta["level"])
        scope = InferenceScope(id=stable_id("scope", digest, "S1"), kind="summary_synthesis",
            packet_digest=digest, fact_id=None, relation_ids=[], source_ref_ids=sorted(refs),
            fragment_index=0, fragment_total=1, child_ids=list(child_ids), module_ids=list(module_ids), relationship_view=view)
        common = {"purpose": meta["purpose"], "level": meta["level"], "module": packet["module"], "aggregate_slot": "S1",
                "children": children,
                "coverage": copy.deepcopy(packet["coverage"]),
                "inherited_limitations": {"count": len(meta["all_unknowns"]), "detail_retained": True},
                "semantic_support": "Child checkpoint identities certify input provenance, not semantic truth. All output is AI_INFERRED and unreviewed."}
        if view == "resolution_counts":
            wire = resolution_counts_wire(common, list(relation_records.values()), canonical_digest=canonical_relation_digest)
            return cls({"S1": scope}, ("S1",), wire)
        entities = {value for relation in relation_records.values()
                    for value in [relation["from_id"], relation["to_id"], *relation["candidates"]] if value is not None}
        aliases = {value: f"F{index}" for index, value in enumerate(sorted(entities), 1)}
        relationships = [{"from": aliases[relation["from_id"]], "to": aliases.get(relation["to_id"]),
                          "type": relation["type"], "resolution": relation["resolution"],
                          "target_name": relation["target_name"], "knowledge_type": relation["knowledge_type"],
                          "candidates": [aliases[value] for value in relation["candidates"]]}
                         for _, relation in sorted(relation_records.items())]
        wire = {**common, "protocol": "project-brain-synthesis/1", "relationships": relationships,
                "entities": {alias: {"child_candidates": sorted(symbol_children.get(identifier, set())),
                                     "identity": "input topology only; module ownership not independently proven"}
                             for identifier, alias in aliases.items()}}
        return cls({"S1": scope}, ("S1",), compact_synthesis_wire(wire))

    def schema(self) -> dict:
        """Require every predetermined slot, with no model-selected IDs."""
        schema = BoundResponse.model_json_schema()
        schema["required"] = list(schema["properties"])
        schema["properties"]["document_path"].pop("default", None)
        schema["properties"]["slots"] = {"type": "object", "additionalProperties": False,
            "required": list(self.scopes),
            "properties": {slot: {"$ref": "#/$defs/SlotText"} for slot in self.scopes}}
        return schema

    def wire(self, wire: dict) -> dict:
        """Add fixed slot identity; retain every table, candidate and source."""
        if self.synthesis_wire is not None:
            return copy.deepcopy(self.synthesis_wire)
        result = copy.deepcopy(wire)
        if len(result["items"]) != len(self.item_slots):
            raise ValueError("来源绑定输入主体数量改变")
        for item, slot in zip(result["items"], self.item_slots, strict=True):
            item["slot"] = slot
        result["explanation_protocol"] = "source-bound/1"
        result["semantic_support"] = "input scope only; inference support requires independent review"
        return result

    def bind(self, content: str) -> Compilation:
        """Validate all prose, then assign provenance solely from the plan."""
        parsed = BoundResponse.model_validate(json.loads(content, object_pairs_hook=_unique_keys))
        if parsed.document_path:
            from src.core.project_brain.artifacts import owned_path

            try:
                if not owned_path(parsed.document_path):
                    raise ValueError("invalid path")
            except ValueError:
                raise SourceBoundPathError("来源绑定建议文档路径无效") from None
        if set(parsed.slots) != set(self.scopes):
            raise SourceBoundContractError(
                SourceBoundContractCategory.SLOT_SET_MISMATCH,
                missing_count=len(set(self.scopes) - set(parsed.slots)),
                extra_count=len(set(parsed.slots) - set(self.scopes)),
            )
        claims = []
        unknowns = list(parsed.unknowns)
        for slot, scope in self.scopes.items():
            value = parsed.slots[slot]
            claims.extend(InferredClaim(text=text, evidence_ids=scope.source_ref_ids,
                source_scope=scope.model_copy(deep=True)) for text in value.texts)
            unknowns.extend(value.unknowns)
        return Compilation(title=parsed.title, claims=claims, unknowns=sorted(set(unknowns)),
                           document_path=parsed.document_path)
