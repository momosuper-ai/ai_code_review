"""Project canonical knowledge into the only schema allowed on public volumes."""

import hashlib
import json
import math
import re
from collections.abc import Callable
from urllib.parse import quote, urlsplit

from src.core.project_brain.public_schema import (
    PublicBrainManifest, PublicBundle, PublicEntry, PublicFact, PublicFile,
    PublicKnowledgeCard, PublicMetadata, PublicProjectionInfo, PublicSourceRef, PublicValue,
)
from src.core.project_brain.schema import BrainManifest, FactBundle, KnowledgeCard, Relation
from src.core.project_brain.snapshot import safe_relative
from src.core.project_brain.privacy_projection import (
    _ATTRIBUTE_ENUMS, _ATTRIBUTE_KINDS, _FACT_KINDS, _FRAMEWORKS, _ID,
    _LANGUAGES, _LOCATOR, _METHODS, _RELATION_KINDS, _SCALAR_ATTRIBUTE_KEYS, _SHA,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def project_value(value: object, sanitize: Callable[[str], str]) -> PublicValue:
    """Sanitize every source-derived key and value without merging collisions."""
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError("公开属性键类型无效")
        return PublicValue(kind="object", entries=[PublicEntry(
            id=f"entry:{index}", key=sanitize(key), value=project_value(child, sanitize))
            for index, (key, child) in enumerate(value.items())])
    if type(value) in {list, tuple}:
        return PublicValue(kind="array", items=[project_value(child, sanitize) for child in value])
    if type(value) is str:
        return PublicValue(kind="string", text=sanitize(value))
    if value is None:
        return PublicValue(kind="null")
    if type(value) is bool:
        literal = _json(value)
        safe = sanitize(literal)
        if safe != literal:
            return PublicValue(kind="string", text=safe)
        return PublicValue(kind="boolean", boolean=value)
    if type(value) in {int, float} and (not isinstance(value, float) or math.isfinite(value)):
        literal = _json(value)
        safe = sanitize(literal)
        if safe != literal:
            return PublicValue(kind="string", text=safe)
        return PublicValue(kind="number", number=value)
    raise ValueError("公开属性值类型无效")


def _machine_value(value: object) -> PublicValue:
    return project_value(value, lambda text: text)


def _object_value(mapping: dict, sanitize: Callable[[str], str], member) -> PublicValue:
    result = []
    for index, (key, value) in enumerate(mapping.items()):
        if type(key) is not str:
            raise ValueError("公开属性键类型无效")
        machine, projected = member(key, value)
        result.append(PublicEntry(id=f"entry:{index}", key=key if machine else sanitize(key), value=projected))
    return PublicValue(kind="object", entries=result)


def _fact_attributes(fact, sanitize) -> PublicValue:
    def member(key, value):
        machine = ((key == "path" and type(value) is str and _LOCATOR.fullmatch(value))
            or (key == "call_arities" and fact.kind == "symbol" and type(value) is dict
                and all(type(ref) is str and _ID.fullmatch(ref) and type(count) is int and count >= 0 for ref, count in value.items()))
            or (key in _ATTRIBUTE_ENUMS and fact.kind in _ATTRIBUTE_KINDS[key]
                and type(value) is str and value in _ATTRIBUTE_ENUMS[key])
            or (key in _SCALAR_ATTRIBUTE_KEYS and type(value) in {bool, int}))
        return bool(machine), _machine_value(value) if machine else project_value(value, sanitize)
    return _object_value(fact.attributes, sanitize, member)


def _file_metadata(metadata: dict, sanitize) -> PublicValue:
    def member(key, value):
        machine = ((key == "blob_sha" and type(value) is str and _SHA.fullmatch(value))
            or (key == "mode" and type(value) is str and value in {"100644", "100755", "120000", "160000"})
            or (key in {"size", "lines"} and type(value) is int and value >= 0)
            or (key == "available" and type(value) is bool))
        return bool(machine), _machine_value(value) if machine else project_value(value, sanitize)
    return _object_value(metadata, sanitize, member)


def _observation(value: dict, facts: dict, refs: dict, relations: dict,
                 modules: set[str], locators: set[str], sanitize) -> PublicValue:
    def member(key, item):
        if key in {"facts", "refs", "relations"}:
            canonical = {"facts": facts, "refs": refs, "relations": relations}[key]
            if type(item) is not list or any(type(raw) is not dict or raw.get("id") not in canonical for raw in item):
                raise ValueError("公开观察镜像身份无效")
            return True, _machine_value([canonical[raw["id"]].model_dump() for raw in item])
        if key == "module" and type(item) is str and item in modules:
            return True, _machine_value(item)
        if key == "calls":
            rows = []
            for row in item:
                if (not isinstance(row, (list, tuple)) or len(row) != 4 or row[0] not in locators
                        or row[1] not in facts or row[3] not in refs):
                    raise ValueError("公开调用观察身份无效")
                rows.append(PublicValue(kind="array", items=[_machine_value(row[0]), _machine_value(row[1]),
                    project_value(row[2], sanitize), _machine_value(row[3])]))
            return True, PublicValue(kind="array", items=rows)
        if key == "routes":
            rows = []
            for route in item:
                def route_member(name, field):
                    machine = ((name == "owner" and type(field) is str and field in facts)
                        or (name == "path" and type(field) is str and field in locators)
                        or (name == "line" and type(field) is int and field >= 1)
                        or (name == "methods" and type(field) is list and all(type(method) is str and method in _METHODS for method in field)))
                    return bool(machine), _machine_value(field) if machine else project_value(field, sanitize)
                rows.append(_object_value(route, sanitize, route_member))
            return True, PublicValue(kind="array", items=rows)
        if key == "accesses":
            rows = []
            for row in item:
                if (not isinstance(row, (list, tuple)) or len(row) != 5 or row[0] not in facts
                        or not isinstance(row[3], (list, tuple)) or not set(row[3]) <= refs.keys()):
                    raise ValueError("公开数据访问观察身份无效")
                rows.append(PublicValue(kind="array", items=[_machine_value(row[0]), project_value(row[1], sanitize),
                    _machine_value(row[2]) if row[2] in {"READS_TABLE", "WRITES_TABLE"} else project_value(row[2], sanitize),
                    _machine_value(list(row[3])), _machine_value(row[4]) if row[4] in {"model", "table"} else project_value(row[4], sanitize)]))
            return True, PublicValue(kind="array", items=rows)
        if key == "frameworks" and type(item) is list:
            return True, _machine_value([framework if framework in _FRAMEWORKS else sanitize(framework) for framework in item])
        return False, project_value(item, sanitize)
    return _object_value(value, sanitize, member)


def safe_public_url(value: str | None, privacy) -> str | None:
    """Do not return a substituted URL or smuggle credentials through authority."""
    if not value or type(value) is not str or any(ord(char) < 33 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"https", "http"} or not parsed.netloc or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0 or "\\" in value
                or privacy.sanitize_text(value) != value):
            return None
    except ValueError:
        return None
    return value


def projection_digest(bundle: PublicBundle, cards: list[PublicKnowledgeCard], manifest: PublicBrainManifest) -> str:
    """Bind all safe content before finalization adds deterministic checksums."""
    metadata = manifest.model_dump()
    for key in ("files", "card_paths", "service_files"):
        metadata[key] = {}
    metadata["projection"]["content_digest"] = ""
    payload = {"bundle": bundle.model_dump(), "cards": [card.model_dump() for card in cards], "manifest": metadata}
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def verify_projection(bundle: PublicBundle, cards: list[PublicKnowledgeCard], manifest: PublicBrainManifest) -> None:
    """Reject changed typed data before it can enter an immutable directory."""
    if (type(bundle) is not PublicBundle or type(manifest) is not PublicBrainManifest
            or any(type(card) is not PublicKnowledgeCard for card in cards)
            or manifest.projection.content_digest != projection_digest(bundle, cards, manifest)):
        raise ValueError("公开知识投影认证无效")
    _validate_graph(bundle, cards, manifest)


def _validate_graph(bundle: PublicBundle, cards: list[PublicKnowledgeCard], manifest: PublicBrainManifest) -> None:
    """Public schemas retain complete graph ownership and evidence membership."""
    facts = {fact.id: fact for fact in bundle.facts}
    refs = {ref.id: ref for ref in bundle.source_refs}
    relations = {relation.id: relation for relation in bundle.relations}
    card_ids = {card.id for card in cards}
    if (len(facts) != len(bundle.facts) or len(refs) != len(bundle.source_refs)
            or len(relations) != len(bundle.relations) or len(card_ids) != len(cards)
            or bundle.source_commit != manifest.source_commit
            or bundle.source_fingerprint != manifest.source_fingerprint):
        raise ValueError("公开知识身份集合无效")
    projection = manifest.projection
    direct = sum(ref.source_url is not None for ref in bundle.source_refs)
    if (projection.identity_version != bundle.identity_version or projection.key_version != bundle.key_version
            or projection.redacted_source_paths != sum(file.path_redacted for file in bundle.files.values())
            or projection.direct_source_links != direct or projection.source_reference_count != len(refs)
            or projection.direct_link_ratio != (direct / len(refs) if refs else 0)):
        raise ValueError("公开知识投影统计或身份版本无效")
    for locator, file in bundle.files.items():
        safe_relative(locator)
        if locator != file.source_file_id:
            raise ValueError("公开文件身份无效")
    for ref in refs.values():
        file = bundle.files.get(ref.source_file_id)
        if (file is None or ref.commit_sha != bundle.source_commit
                or ref.symbol_id is not None and ref.symbol_id not in facts
                or ref.display_path != file.display_path or ref.path_redacted != file.path_redacted):
            raise ValueError("公开来源归属无效")
    for fact in facts.values():
        if fact.module_id not in bundle.modules or not fact.source_ref_ids or not set(fact.source_ref_ids) <= refs.keys():
            raise ValueError("公开事实缺少来源")
    for relation in relations.values():
        if (relation.from_id not in facts or relation.to_id is not None and relation.to_id not in facts
                or not set(relation.candidates) <= facts.keys() or not relation.source_ref_ids
                or not set(relation.source_ref_ids) <= refs.keys()
                or (relation.resolution == "resolved") != bool(relation.to_id)
                or relation.resolution == "candidate" and not relation.candidates):
            raise ValueError("公开关系状态或来源无效")
    for card in cards:
        if (card.source_commit != bundle.source_commit or card.module_id is not None and card.module_id not in bundle.modules
                or not set(card.links) <= card_ids):
            raise ValueError("公开卡片归属无效")
        for claim in [*card.claims, *card.detail_claims]:
            if not set(claim.evidence_ids) <= refs.keys() or not set(claim.relation_ids) <= relations.keys():
                raise ValueError("公开卡片引用无效")
            if any(relations[rid].resolution != "resolved" for rid in claim.relation_ids) and claim.knowledge_type == "DERIVED":
                raise ValueError("公开候选关系不能冒充确定路径")
            scope = claim.source_scope
            if scope and (claim.knowledge_type != "AI_INFERRED" or set(scope.source_ref_ids) != set(claim.evidence_ids)
                          or not set(scope.relation_ids) <= relations.keys()
                          or scope.fact_id is not None and scope.fact_id not in facts
                          or not set(scope.module_ids) <= bundle.modules.keys()):
                raise ValueError("公开推断来源范围无效")


def _paths(cards: list[PublicKnowledgeCard], sanitize: Callable[[str], str]) -> None:
    # Suggested paths are public filenames, not source identities. A changed
    # path, reserved name, or collision falls back deterministically for every
    # member of the collision, never overwriting a card or dropping a claim.
    from src.core.project_brain.artifacts import card_path

    suggested: dict[str, list[PublicKnowledgeCard]] = {}
    fallback = set()
    for card in cards:
        if card.kind == "project":
            card.document_path = None
        if card.document_path:
            try:
                safe_relative(card.document_path)
                if (not card.document_path.startswith("docs/project-brain/")
                        or sanitize(card.document_path) != card.document_path
                        or card.document_path == "docs/project-brain/manifest.json"):
                    fallback.add(card.id)
            except ValueError:
                fallback.add(card.id)
        path = card_path(card) if card.id not in fallback else ""
        json_path = "docs/project-brain/project.json" if card.kind == "project" else (path[:-3] if path.endswith(".md") else path) + ".json"
        for name in (path, json_path):
            suggested.setdefault(name, []).append(card)
    for values in suggested.values():
        if len(values) > 1:
            fallback.update(card.id for card in values)
    for card in cards:
        if card.id in fallback and card.kind != "project":
            # Card identity is already service-owned; hash it so punctuation
            # cannot turn an opaque ID into an invalid repository path.
            suffix = hashlib.sha256(card.id.encode()).hexdigest()
            card.document_path = f"docs/project-brain/cards/{suffix}.md"


def project_public(bundle: FactBundle, cards: list[KnowledgeCard], manifest: BrainManifest,
                   project_url: str, privacy, *, source_paths: dict[str, str] | None = None,
                   branch_id: str | None = None) -> tuple[PublicBundle, list[PublicKnowledgeCard], PublicBrainManifest]:
    """Create a schema 2 graph from authenticated opaque canonical identities.

    source_paths is a Worker-only ephemeral map. It is used solely to decide
    whether a real fixed-SHA source link is safe, and is never serialized.
    """
    identity = getattr(bundle, "source_identity", None)
    if not isinstance(identity, dict) or not identity:
        raise ValueError("公开投影缺少规范来源身份")
    branch_id = branch_id or identity.get("branch_id")
    if type(branch_id) is not str or not branch_id:
        raise ValueError("公开投影缺少分支身份")
    ledger = getattr(privacy, "ledger", None)
    if ledger is None:
        raise ValueError("公开投影缺少完整隐私账本")
    ledger.refresh()
    revision = ledger.revision() if callable(getattr(ledger, "revision", None)) else ledger.digest("public-projection:privacy:v1:" + _json(ledger.entries))
    sanitize = privacy.sanitize_text
    paths = source_paths or {}
    if set(bundle.observations) - set(bundle.files) or set(bundle.files) - set(getattr(bundle, "display_paths", {})):
        raise ValueError("公开投影文件或观察归属无效")

    def display(locator: str) -> tuple[str, bool]:
        if locator not in bundle.display_paths:
            raise ValueError("公开投影缺少展示路径")
        original = paths.get(locator)
        text = sanitize(original if original is not None else bundle.display_paths[locator])
        return text, original is None or text != original

    base_url = safe_public_url(project_url, privacy)
    public_refs = []
    for ref in bundle.source_refs:
        text, redacted = display(ref.path)
        original = paths.get(ref.path)
        url = None
        if not redacted and original is not None and base_url is not None:
            candidate = f"{base_url.rstrip('/')}/-/blob/{ref.commit_sha}/{quote(original, safe='/')}#L{ref.line_start}-{ref.line_end}"
            # A source fragment is service-built from validated numeric bounds.
            before_fragment, fragment = candidate.rsplit("#", 1)
            if safe_public_url(before_fragment, privacy) and sanitize(candidate) == candidate:
                url = before_fragment + "#" + fragment
        public_refs.append(PublicSourceRef(**ref.model_dump(exclude={"path", "display_path"}),
            path=ref.path, source_file_id=ref.path, display_path=text, path_redacted=redacted,
            source_url=url, link_status="redacted" if redacted else "direct" if url else "unavailable"))

    public_facts = [PublicFact(**fact.model_dump(exclude={"attributes", "name", "kind", "extractor"}),
        name=sanitize(fact.name), kind=fact.kind if fact.kind in _FACT_KINDS else sanitize(fact.kind),
        extractor=fact.extractor if fact.extractor in {f"brain.{language}/1" for language in _LANGUAGES} else sanitize(fact.extractor),
        attributes=_fact_attributes(fact, sanitize)) for fact in bundle.facts]
    public_relations = [Relation(**relation.model_dump(exclude={"target_name", "type"}),
        target_name=sanitize(relation.target_name), type=relation.type if relation.type in _RELATION_KINDS else sanitize(relation.type)) for relation in bundle.relations]
    fact_map, ref_map, relation_map = ({fact.id: fact for fact in public_facts}, {ref.id: ref for ref in public_refs},
                                      {relation.id: relation for relation in public_relations})
    public = PublicBundle(source_commit=bundle.source_commit, source_fingerprint=bundle.source_fingerprint,
        identity_version=str(identity["version"]), key_version=str(identity["key_version"]),
        files={locator: PublicFile(source_file_id=locator, display_path=display(locator)[0],
            path_redacted=display(locator)[1], metadata=_file_metadata(metadata, sanitize)) for locator, metadata in bundle.files.items()},
        modules={module: display(locator)[0] for module, locator in bundle.modules.items()},
        facts=public_facts, relations=public_relations, source_refs=public_refs,
        issues=[sanitize(value) for value in bundle.issues], frameworks=[value if value in _FRAMEWORKS else sanitize(value) for value in bundle.frameworks],
        observations={locator: _observation(value, fact_map, ref_map, relation_map, set(bundle.modules), set(bundle.files), sanitize)
                      for locator, value in bundle.observations.items()})
    public_cards = []
    for card in cards:
        values = card.model_dump()
        values.update(schema_version="2.0", title=sanitize(card.title),
            unknowns=[sanitize(value) for value in card.unknowns], detail_unknowns=[sanitize(value) for value in card.detail_unknowns],
            coverage=card.coverage if card.coverage in {"partial", "complete"} else sanitize(card.coverage))
        for name in ("claims", "detail_claims"):
            for claim in values[name]:
                claim["text"] = sanitize(claim["text"])
        public_cards.append(PublicKnowledgeCard.model_validate(values))
    _paths(public_cards, sanitize)
    direct = sum(ref.source_url is not None for ref in public_refs)
    metadata = manifest.model_dump(exclude={"schema_version"})
    metadata.update(branch=sanitize(manifest.branch), issues=[sanitize(value) for value in manifest.issues],
                    coverage=_coverage(manifest.coverage, sanitize))
    public_manifest = PublicBrainManifest(**metadata,
        public_metadata=PublicMetadata(branch_display=sanitize(manifest.branch), branch_id=branch_id,
            project_url=base_url, mr_url_base=(safe_public_url(base_url.rstrip("/") + "/-/merge_requests/", privacy) if base_url else None)),
        projection=PublicProjectionInfo(identity_version=public.identity_version, key_version=public.key_version,
            privacy_seal=revision, content_digest="", redacted_source_paths=sum(value.path_redacted for value in public.files.values()),
            direct_source_links=direct, source_reference_count=len(public_refs),
            direct_link_ratio=direct / len(public_refs) if public_refs else 0))
    public_manifest.projection.content_digest = projection_digest(public, public_cards, public_manifest)
    _validate_graph(public, public_cards, public_manifest)
    ledger.refresh()
    latest = ledger.revision() if callable(getattr(ledger, "revision", None)) else ledger.digest("public-projection:privacy:v1:" + _json(ledger.entries))
    if revision != latest:
        raise ValueError("公开投影期间隐私账本已变化")
    return public, public_cards, public_manifest


def _safe_fixed_tree(value: object, sanitize: Callable[[str], str]) -> object:
    """Unknown coverage extensions are text, including their keys."""
    if type(value) is dict:
        result = {}
        for key, child in value.items():
            if type(key) is not str:
                raise ValueError("公开清单字段类型无效")
            safe = sanitize(key)
            if safe in result:
                raise ValueError("公开清单未知字段脱敏碰撞")
            result[safe] = _safe_fixed_tree(child, sanitize)
        return result
    if type(value) is list:
        return [_safe_fixed_tree(child, sanitize) for child in value]
    if type(value) is str:
        return sanitize(value)
    if value is None:
        return value
    if type(value) in {int, bool} or type(value) is float and math.isfinite(value):
        literal = _json(value)
        safe = sanitize(literal)
        return value if safe == literal else safe
    raise ValueError("公开清单值类型无效")


_COUNT_COVERAGE = {"files", "available_files", "facts", "relations", "resolved_relations", "evidence", "cards", "limitations", "reused_files", "cache_hits", "privacy_history_parents", "privacy_history_legacy_unavailable", "privacy_policy_findings",
                   "model_calls_settled", "model_calls_closed_http_errors", "model_usage_unknown_bound_tokens",
                   "model_usage_settled_tokens", "model_usage_budget_occupied_tokens"}
_RATIO_COVERAGE = {"valid_reference_ratio", "evidence_coverage_ratio", "source_reference_coverage_ratio"}
_PLAN_COUNTS = {"semantic_packets", "canonical_bytes", "wire_bytes", "estimated_semantic_input_tokens", "reservation_envelope", "budget_tokens"}
_PLAN_CONSTANTS = {
    "estimate_method": "characters/LLM_CHARS_PER_TOKEN; not provider tokenizer",
    "quality_policy": "all facts, relations, candidate states and source evidence retained",
}
_EXCLUDED_COSTS = {"semantic_output", "privacy", "retries", "project_summary", "assembly"}
_UNIT_KEY = re.compile(r"(?:compile:\d+|project_summary:\d+:\d+|module_summary:[a-f0-9]{24}:\d+:\d+)")


def _coverage(value: dict, sanitize) -> dict:
    """Preserve only exact service positions; unknown extensions are filtered."""
    def is_count(child):
        return type(child) is int and child >= 0

    def nested(mapping, namespace):
        result = {}
        for key, child in mapping.items():
            if type(key) is not str:
                raise ValueError("公开清单字段类型无效")
            machine = False
            projected = None
            if namespace == "coverage":
                machine = ((key in _COUNT_COVERAGE and is_count(child))
                    or (key in _RATIO_COVERAGE and type(child) in {int, float} and 0 <= child <= 1)
                    or (key in {"incremental", "model_usage_financially_exact"} and type(child) is bool)
                    or (key == "public_schema_version" and child == "2.0")
                    or (key == "semantic_support_status" and child == "unreviewed")
                    or (key == "coverage_metric_meaning" and child == "引用存在和输入范围有效性，不是业务语义支持率")
                    or (key in {"manifest_sha256", "export_sha256", "privacy_history_digest"} and type(child) is str and re.fullmatch(r"[a-f0-9]{64}", child)))
                if key in {"cost_plan", "summary_hierarchy"} and type(child) is dict:
                    machine, projected = True, nested(child, key)
            elif namespace == "cost_plan":
                machine = ((key in _PLAN_COUNTS and is_count(child))
                    or (key in _PLAN_CONSTANTS and child == _PLAN_CONSTANTS[key])
                    or (key == "excluded_costs" and type(child) is list and all(type(item) is str and item in _EXCLUDED_COSTS for item in child)))
            elif namespace == "summary_hierarchy":
                machine = ((key == "module_ids" and type(child) is list and all(type(item) is str and _ID.fullmatch(item) for item in child))
                    or (key == "leaf_ids" and type(child) is list and all(type(item) is str and _UNIT_KEY.fullmatch(item) for item in child))
                    or (key == "root" and type(child) is str and _UNIT_KEY.fullmatch(child))
                    or (key == "summary_nodes" and is_count(child))
                    or (key == "details_preserved" and type(child) is bool)
                    or (key == "semantic_support_status" and child == "unreviewed"))
            safe_key = key if machine else sanitize(key)
            if safe_key in result:
                raise ValueError("公开清单未知字段脱敏碰撞")
            result[safe_key] = projected if projected is not None else child if machine else _safe_fixed_tree(child, sanitize)
        return result
    return nested(value, "coverage")
