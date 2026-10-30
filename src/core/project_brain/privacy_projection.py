"""Project canonical extraction data into complete semantic privacy records.

This module does not sanitize data or declare it safe. It only removes repeated
classification of the same full string in the same parser-defined role. Callers
must run ordinary rules/HMAC sanitization over the complete original structure.
Canonical bundles must come from the fixed-SHA extractor; completed receipts can
only be reused within that build/recipe, never on a caller-provided trust flag.
"""

import json
import re
from collections import defaultdict

from src.core.project_brain.schema import Compilation, Fact, FactBundle, InferredClaim, InferenceScope, KnowledgeCard, Relation, SourceRef, stable_id

_ID = re.compile(r"[a-z_]+:(?:[a-f0-9]{24}|[a-f0-9]{64})")
_EVIDENCE_ID = re.compile(r"evidence:(?:[a-f0-9]{24}|[a-f0-9]{64})")
_LOCATOR = re.compile(r"sources(?:/s[a-f0-9]{64})*")
_SHA = re.compile(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})")
_PLACEHOLDER = re.compile(r"<<[A-Z]+_\d+>>")
_IDENTIFIER = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_LANGUAGES = {"python", "java", "javascript", "typescript", "go", "engineering"}
_FACT_KINDS = {"file", "symbol", "test", "table", "interface", "config", "entrypoint", "dependency", "sql_statement"}
_RELATION_KINDS = {
    "CALLS", "EXTENDS", "MAPS_TO", "HANDLED_BY", "READS_TABLE", "WRITES_TABLE",
    "TESTED_BY", "IMPLEMENTED_BY",
}
_CODE_RELATIONS = {"CALLS", "EXTENDS", "HANDLED_BY", "TESTED_BY", "IMPLEMENTED_BY"}
_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD", "TRACE", "CONNECT", "ANY"}
_FRAMEWORKS = {"FastAPI", "SQLAlchemy", "Spring", "JPA", "MyBatis"}
_ATTRIBUTE_ENUMS = {
    "language": _LANGUAGES,
    "symbol_kind": {"function", "method", "class", "interface", "constructor"},
    "interface_type": {"http", "task", "cli"},
    "entry_type": {"deployment", "npm_script"},
    "package_manager": {"npm", "python", "maven", "pip", "gradle", "go"},
    "usage": {"environment", "settings", "declaration", "yaml_key"},
    "declaration": {"__tablename__", "@Table", "DDL"},
}
_ATTRIBUTE_KINDS = {
    "language": {"symbol", "file"}, "symbol_kind": {"symbol"},
    "interface_type": {"interface"}, "entry_type": {"entrypoint"},
    "package_manager": {"dependency"}, "usage": {"config"}, "declaration": {"table"},
}
_SCALAR_ATTRIBUTE_KEYS = {
    "dispatch_fixed", "is_static", "parameter_count", "varargs", "cli_main", "redefined",
}
_OBSERVATION_KEYS = {
    "module", "facts", "refs", "relations", "imports", "globals", "java_imports",
    "calls", "routes", "routers", "mounts", "apps", "accesses", "issues", "frameworks",
}


class _Records:
    def __init__(self) -> None:
        self.values: set[tuple[str, str]] = set()

    def add(self, field: str, value: object) -> None:
        if isinstance(value, str):
            if value and not _PLACEHOLDER.fullmatch(value):
                self.values.add((field, value))
        else:
            self.generic(value, (field,))

    def generic(self, value: object, path: tuple[str, ...]) -> None:
        # Unknown dict keys themselves are source data, even when their values
        # are numeric. No content heuristic can turn them into trusted metadata.
        if isinstance(value, str):
            self.add(".".join(path[-2:]) or "value", value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.generic(item, path)
        elif isinstance(value, dict):
            for key, item in sorted(value.items()):
                if not isinstance(key, str):
                    raise ValueError("隐私投影字段名必须是字符串")
                self.add("field_name", key)
                self.generic(item, (*path, key))
        elif type(value) in {bool, int, float}:
            self.add(".".join(path[-2:]) or "value", json.dumps(value, allow_nan=False))
        elif value is not None:
            raise ValueError("隐私投影包含无法识别的数据类型")

    def identifier(self, value: object, fallback: str) -> None:
        if isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            self.add("code_identifier", value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                self.identifier(item, fallback)
        else:
            self.add(fallback, value)

    def machine(self, value: object, field: str, pattern: re.Pattern[str] = _ID) -> None:
        # Only fixed Schema positions use this check. A hash-shaped description
        # is still free text and goes through generic(), not this method.
        if isinstance(value, str) and pattern.fullmatch(value):
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                self.machine(item, field, pattern)
        elif value is not None:
            self.add(field, value)

    def finish(self) -> list[dict[str, str]]:
        return [{"field": field, "text": text} for field, text in sorted(self.values)]


def _attributes(fact: Fact, records: _Records, *, opaque: bool = False) -> None:
    for key, value in sorted(fact.attributes.items()):
        if key == "path":
            records.machine(value, "source_path", _LOCATOR) if opaque else records.add("source_path", value)
        elif key == "signature" and fact.kind == "symbol":
            # Do not tokenise or strip literals here. Signatures from a future
            # adapter can contain defaults/annotations and need full scanning.
            records.add("source_signature", value)
        elif key in {"assigned_names", "shadowed_names", "closure_bindings"} and fact.kind == "symbol":
            records.identifier(value, "attributes." + key)
        elif (key, fact.kind) in {("model", "table"), ("namespace", "sql_statement"), ("value_type", "config")}:
            records.identifier(value, "attributes." + key)
        elif key in _ATTRIBUTE_ENUMS and fact.kind in _ATTRIBUTE_KINDS[key] and isinstance(value, str) and value in _ATTRIBUTE_ENUMS[key]:
            continue
        elif key == "call_arities" and fact.kind == "symbol" and isinstance(value, dict):
            for ref, count in sorted(value.items()):
                records.machine(ref, "attributes.call_arities")
                if not isinstance(count, int) or isinstance(count, bool):
                    records.generic(count, ("attributes", "call_arities"))
        elif key in _SCALAR_ATTRIBUTE_KEYS and isinstance(value, (bool, int)):
            continue
        else:
            # A new adapter attribute, or an unexpected enum value, is never
            # silently removed from model classification.
            records.add("field_name", key)
            records.generic(value, ("attributes", key))


def _fact(fact: Fact, records: _Records, *, opaque: bool = False) -> None:
    records.machine(fact.id, "fact_id")
    records.machine(fact.module_id, "module_id")
    records.machine(fact.source_ref_ids, "evidence_reference")
    if fact.kind not in _FACT_KINDS:
        records.add("fact_kind", fact.kind)
    if fact.extractor not in {f"brain.{language}/1" for language in _LANGUAGES}:
        records.add("extractor", fact.extractor)
    if fact.kind in {"symbol", "test", "sql_statement"}:
        records.identifier(fact.name, "declaration_name")
    elif fact.kind == "file":
        records.add("source_path", fact.name)
    else:
        role = {"config": "config_name", "table": "table_name", "dependency": "dependency_name"}.get(fact.kind, "declaration_name")
        records.add(role, fact.name)
    _attributes(fact, records, opaque=opaque)


def _relation(relation: Relation, records: _Records) -> None:
    for value in (relation.id, relation.from_id, relation.to_id, relation.candidates):
        records.machine(value, "relation_reference")
    records.machine(relation.source_ref_ids, "evidence_reference")
    if relation.type not in _RELATION_KINDS:
        records.add("relation_type", relation.type)
    if relation.type in _CODE_RELATIONS:
        records.identifier(relation.target_name, "relation_target")
    elif relation.type in {"MAPS_TO", "READS_TABLE", "WRITES_TABLE"}:
        records.add("table_name", relation.target_name)
    else:
        records.add("relation_target", relation.target_name)


def _mirror_equal(raw: dict, canonical: dict) -> bool:
    # These fields are sets in the fact contract. Equivalent evidence ordering
    # may differ between observation creation and relation coalescing.
    def normalized(value: dict) -> dict:
        return {key: sorted(item) if key in {"source_ref_ids", "candidates"} and isinstance(item, list) else item
                for key, item in value.items()}
    return normalized(raw) == normalized(canonical)


def _observation(path: str, value: dict, bundle: FactBundle, records: _Records, canonical: dict[str, dict[str, dict]], *, opaque: bool = False) -> None:
    records.machine(path, "source_path", _LOCATOR) if opaque else records.add("source_path", path)
    for collection in ("facts", "refs", "relations"):
        for raw in value.get(collection, []):
            if not isinstance(raw, dict) or raw.get("id") not in canonical[collection] or not _mirror_equal(raw, canonical[collection][raw["id"]]):
                raise ValueError("隐私投影中的观察副本与规范事实不一致")
    module = value.get("module")
    if module not in bundle.modules:
        records.add("observation.module", module)
    for key in ("imports", "java_imports"):
        for alias, target in value.get(key, {}).items():
            records.identifier(alias, "import_alias")
            records.identifier(target, "import_target")
    records.identifier(value.get("globals", []), "global_name")
    for call in value.get("calls", []):
        if not isinstance(call, (list, tuple)) or len(call) != 4:
            raise ValueError("隐私投影中的调用观察结构无效")
        records.machine(call[0], "source_path", _LOCATOR) if opaque else records.add("source_path", call[0])
        records.machine(call[1], "call_owner")
        records.identifier(call[2], "call_expression")
        records.machine(call[3], "evidence_reference")
    for access in value.get("accesses", []):
        if not isinstance(access, (list, tuple)) or len(access) != 5:
            raise ValueError("隐私投影中的数据访问观察结构无效")
        owner, name, kind, refs, adapter = access
        records.machine(owner, "access_owner")
        records.machine(refs, "evidence_reference")
        if adapter == "model":
            records.identifier(name, "access_name")
        elif adapter == "table":
            records.add("table_name", name)
        else:
            records.add("access_name", name)
            records.add("access_adapter", adapter)
        if kind not in {"READS_TABLE", "WRITES_TABLE"}:
            records.add("access_kind", kind)
    for route in value.get("routes", []):
        for key, item in route.items():
            if key == "owner":
                records.machine(item, "route_owner")
            elif key == "path":
                records.machine(item, "source_path", _LOCATOR) if opaque else records.add("source_path", item)
            elif key == "methods" and isinstance(item, list) and all(isinstance(method, str) and method in _METHODS for method in item):
                continue
            elif key == "line" and isinstance(item, int):
                continue
            elif key in {"router", "route", "methods"}:
                records.add("route." + key, item)
            else:
                records.add("field_name", key)
                records.generic(item, ("route", key))
    # Composite router keys and prefixes are source values; scan their complete
    # strings instead of trying to reconstruct a trusted partial key.
    for key in ("routers", "mounts", "apps"):
        records.generic(value.get(key, []), ("observation", key))
    records.add("source_issue", value.get("issues", []))
    for framework in value.get("frameworks", []):
        if framework not in _FRAMEWORKS:
            records.add("framework", framework)
    for key, item in value.items():
        if key not in _OBSERVATION_KEYS:
            records.add("field_name", key)
            records.generic(item, ("observation", key))


def bundle_privacy_records(bundle: FactBundle) -> list[dict[str, str]]:
    """Return deterministic complete records for the canonical fixed-SHA bundle.

    Identifier roles are proven by extractor positions and full syntax, not by
    similarity to another safe string. Unknown adapter fields/keys fall back to
    scanning. Observation mirrors are reused only after complete equality.
    """
    records = _Records()
    identity = getattr(bundle, "source_identity", None)
    opaque = identity is not None
    if opaque:
        if (not isinstance(identity, dict)
                or set(identity) != {"version", "key_version", "project_id", "canonical_binding", "branch_id"}
                or identity["version"] != "opaque-v1"
                or type(identity["key_version"]) is not int or identity["key_version"] != 1
                or type(identity["project_id"]) is not int or identity["project_id"] <= 0
                or type(identity["canonical_binding"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", identity["canonical_binding"])
                or type(identity["branch_id"]) is not str or not re.fullmatch(r"branch:[a-f0-9]{64}", identity["branch_id"])):
            # A flag or hash-shaped unknown wrapper never grants an exemption.
            # The worker additionally authenticates this exact metadata and
            # all locators against its private SourceCatalog before reuse.
            raise ValueError("隐私投影来源身份元数据无效")
    records.machine(bundle.source_commit, "source_commit", _SHA)
    records.machine(bundle.source_fingerprint, "source_fingerprint", re.compile(r"[a-f0-9]{64}"))
    for path, metadata in bundle.files.items():
        records.machine(path, "source_path", _LOCATOR) if opaque else records.add("source_path", path)
        for key, value in metadata.items():
            if key == "blob_sha":
                records.machine(value, "blob_sha", _SHA)
            elif key == "mode" and isinstance(value, str) and value in {"100644", "100755", "120000", "160000"}:
                continue
            elif key in {"size", "lines", "available"} and isinstance(value, (int, bool)):
                continue
            else:
                records.add("field_name", key)
                records.generic(value, ("file", key))
    for mid, path in bundle.modules.items():
        records.machine(mid, "module_id")
        records.machine(path, "source_path", _LOCATOR) if opaque else records.add("source_path", path)
    for locator, display in getattr(bundle, "display_paths", {}).items():
        records.machine(locator, "source_path", _LOCATOR) if opaque else records.add("source_path", locator)
        records.add("source_path", display)
    for ref in bundle.source_refs:
        records.machine(ref.id, "evidence_reference")
        records.machine(ref.commit_sha, "source_commit", _SHA)
        records.machine(ref.blob_sha, "blob_sha", _SHA)
        records.machine(ref.symbol_id, "symbol_reference")
        records.machine(ref.path, "source_path", _LOCATOR) if opaque else records.add("source_path", ref.path)
        if getattr(ref, "display_path", None) is not None:
            records.add("source_path", ref.display_path)
    for fact in bundle.facts:
        _fact(fact, records, opaque=opaque)
    for relation in bundle.relations:
        _relation(relation, records)
    records.add("source_issue", bundle.issues)
    for framework in bundle.frameworks:
        if framework not in _FRAMEWORKS:
            records.add("framework", framework)
    canonical = {
        "facts": {item.id: item.model_dump() for item in bundle.facts},
        "refs": {item.id: item.model_dump() for item in bundle.source_refs},
        "relations": {item.id: item.model_dump() for item in bundle.relations},
    }
    for path, observation in bundle.observations.items():
        _observation(path, observation, bundle, records, canonical, opaque=opaque)
    return records.finish()


def compilation_privacy_records(compilation: Compilation) -> list[dict[str, str]]:
    """Classify new narratives and conservatively include future Schema fields.

    Only valid evidence IDs at the existing claims[].evidence_ids position are
    machine references. An ID-shaped string anywhere else remains free text.
    ``serialize_as_any`` also retains new fields on nested model subclasses;
    nominal base-type serialization could otherwise silently remove them.
    """
    records = _Records()
    data = compilation.model_dump(serialize_as_any=True)
    for key, value in sorted(data.items()):
        if key in {"title", "unknowns", "document_path"}:
            records.add(key, value)
        elif key == "claims":
            if not isinstance(value, list) or any(not isinstance(claim, dict) for claim in value):
                raise ValueError("知识结论的隐私投影结构无效")
            for claim in value:
                for field, item in sorted(claim.items()):
                    if field == "text":
                        records.add("claims.text", item)
                    elif field == "evidence_ids":
                        records.machine(item, "evidence_reference", _EVIDENCE_ID)
                    elif field == "source_scope":
                        _scope_records(records, item)
                    else:
                        records.add("field_name", field)
                        records.generic(item, ("claims", field))
        else:
            records.add("field_name", key)
            records.generic(value, (key,))
    return records.finish()


def summary_privacy_records(packet: dict) -> list[dict[str, str]]:
    """Classify all prose of an independently authenticated summary packet.

    This pure projection does not establish trust. Its caller must first
    authenticate the complete canonical packet and create this safe display
    copy through the worker-owned catalog. Only explicit Schema positions are
    metadata; a similarly shaped value in any prose field is still classified.
    Unknown wrappers and missing nullable/default fields fail closed rather
    than acquiring metadata status through Pydantic defaults.
    """
    error = "汇总隐私投影契约无效"
    try:
        def shape(value, fields):
            if type(value) is not dict or set(value) != set(fields):
                raise ValueError

        def strings(value, *, unique=False, nonempty=False):
            if type(value) is not list or any(type(item) is not str for item in value):
                raise ValueError
            if nonempty and (not value or any(not item.strip() for item in value)):
                raise ValueError
            if unique and len(value) != len(set(value)):
                raise ValueError
            return value

        def identifier(value, pattern=_ID):
            if type(value) is not str or pattern.fullmatch(value) is None:
                raise ValueError

        def identifiers(value, pattern=_ID, *, nonempty=False):
            for entry in strings(value, unique=True, nonempty=nonempty):
                identifier(entry, pattern)

        shape(packet, {"module_id", "module", "items", "coverage", "synthesis"})
        if type(packet["module"]) is not str or not packet["module"].strip():
            raise ValueError
        strings(packet["coverage"])
        meta = packet["synthesis"]
        shape(meta, {"purpose", "level", "index", "child_ids", "module_ids", "leaf_ids",
                     "child_hashes", "all_unknowns", "selections"})
        if (type(meta["purpose"]) is not str or meta["purpose"] not in {"module", "project"}
                or any(type(meta[key]) is not int or meta[key] < 0 for key in ("level", "index"))):
            raise ValueError
        children = strings(meta["child_ids"], unique=True, nonempty=True)
        identifiers(meta["module_ids"], re.compile(r"module:(?:[a-f0-9]{24}|[a-f0-9]{64})"), nonempty=True)
        strings(meta["leaf_ids"], unique=True, nonempty=True)
        if any(re.fullmatch(r"compile:\d+", leaf) is None for leaf in meta["leaf_ids"]):
            raise ValueError
        if meta["purpose"] == "module":
            identifier(packet["module_id"], re.compile(r"module:(?:[a-f0-9]{24}|[a-f0-9]{64})"))
            if meta["module_ids"] != [packet["module_id"]]:
                raise ValueError
        elif packet["module_id"] != "project":
            raise ValueError
        shape(meta["child_hashes"], children)
        shape(meta["selections"], children)
        strings(meta["all_unknowns"])
        if type(packet["items"]) is not list or len(packet["items"]) != len(children):
            raise ValueError
        records = _Records()
        records.add("module_name", packet["module"])
        records.add("source_issue", packet["coverage"])
        records.add("unknowns", meta["all_unknowns"])
        source_index, relation_index = {}, {}
        for item, child in zip(packet["items"], children, strict=True):
            shape(item, {"fact", "relations", "evidence"})
            fact = item["fact"]
            shape(fact, {"module", "title", "claims", "unknowns", "child_id"})
            if (fact["child_id"] != child or type(fact["module"]) is not str
                    or not fact["module"].strip() or type(fact["title"]) is not str or not fact["title"].strip()):
                raise ValueError
            strings(fact["unknowns"])
            if type(fact["claims"]) is not list or not fact["claims"] and not fact["unknowns"]:
                raise ValueError
            digest = meta["child_hashes"][child]
            if type(digest) is not str or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
                raise ValueError
            selection = meta["selections"][child]
            shape(selection, {"source_key", "claim_start", "claim_end", "unknown_start", "unknown_end"})
            if (type(selection["source_key"]) is not str or not selection["source_key"].strip()
                    or any(type(selection[key]) is not int or selection[key] < 0
                           for key in ("claim_start", "claim_end", "unknown_start", "unknown_end"))
                    or selection["claim_start"] > selection["claim_end"]
                    or selection["unknown_start"] > selection["unknown_end"]
                    or selection["claim_end"] - selection["claim_start"] != len(fact["claims"])
                    or selection["unknown_end"] - selection["unknown_start"] != len(fact["unknowns"])
                    or child != stable_id("summary-input", selection["source_key"],
                        *(str(selection[key]) for key in ("claim_start", "claim_end", "unknown_start", "unknown_end")))):
                raise ValueError
            if type(item["evidence"]) is not list or not item["evidence"]:
                raise ValueError
            supplied = set()
            for raw in item["evidence"]:
                shape(raw, SourceRef.model_fields)
                ref = SourceRef.model_validate(raw, strict=True)
                identifier(ref.id, _EVIDENCE_ID)
                identifier(ref.commit_sha, _SHA)
                identifier(ref.blob_sha, _SHA)
                if ref.symbol_id is not None:
                    identifier(ref.symbol_id)
                if not ref.path.strip():
                    raise ValueError
                if ref.id in source_index and json.dumps(source_index[ref.id], sort_keys=True) != json.dumps(raw, sort_keys=True):
                    raise ValueError
                source_index[ref.id] = raw
                if ref.id in supplied:
                    raise ValueError
                supplied.add(ref.id)
                # Schema2 path is a bound opaque locator; its display string
                # remains ordinary source data. Legacy null-display packets
                # still classify the original path instead of dropping it.
                records.add("source_path", ref.path if ref.display_path is None else ref.display_path)
            claims = []
            for raw in fact["claims"]:
                shape(raw, InferredClaim.model_fields)
                claim = InferredClaim.model_validate(raw, strict=True)
                identifiers(claim.evidence_ids, _EVIDENCE_ID, nonempty=True)
                if not set(claim.evidence_ids) <= supplied:
                    raise ValueError
                if claim.source_scope is not None:
                    shape(raw["source_scope"], InferenceScope.model_fields)
                    scope = claim.source_scope
                    identifier(scope.id)
                    identifier(scope.packet_digest, re.compile(r"[a-f0-9]{64}"))
                    if scope.fact_id is not None:
                        identifier(scope.fact_id)
                    identifiers(scope.relation_ids)
                    identifiers(scope.source_ref_ids, _EVIDENCE_ID, nonempty=True)
                    if not set(scope.source_ref_ids) <= supplied:
                        raise ValueError
                    identifiers(scope.child_ids, re.compile(r"summary-input:(?:[a-f0-9]{24}|[a-f0-9]{64})"))
                    identifiers(scope.module_ids, re.compile(r"module:(?:[a-f0-9]{24}|[a-f0-9]{64})"))
                claims.append(claim)
            records.add("module_name", fact["module"])
            if claims:
                # Reuse the exact existing narrative roles and complete-field
                # deduplication, not generic scanning of machine provenance.
                compilation = Compilation(title=fact["title"], claims=claims, unknowns=fact["unknowns"], document_path=None)
                records.values.update((entry["field"], entry["text"]) for entry in compilation_privacy_records(compilation))
            else:
                records.add("title", fact["title"])
                records.add("unknowns", fact["unknowns"])
            if type(item["relations"]) is not list:
                raise ValueError
            for raw in item["relations"]:
                shape(raw, Relation.model_fields)
                relation = Relation.model_validate(raw, strict=True)
                for value in (relation.id, relation.from_id):
                    identifier(value)
                if relation.to_id is not None:
                    identifier(relation.to_id)
                identifiers(relation.candidates)
                identifiers(relation.source_ref_ids, _EVIDENCE_ID, nonempty=True)
                if (relation.type not in _RELATION_KINDS or not set(relation.source_ref_ids) <= supplied
                        or (relation.resolution == "resolved") != bool(relation.to_id)
                        or relation.resolution == "candidate" and not relation.candidates):
                    raise ValueError
                if relation.id in relation_index and json.dumps(relation_index[relation.id], sort_keys=True) != json.dumps(raw, sort_keys=True):
                    raise ValueError
                relation_index[relation.id] = raw
                _relation(relation, records)
        return records.finish()
    except (KeyError, TypeError, ValueError):
        raise ValueError(error) from None


def card_privacy_records(card: KnowledgeCard) -> list[dict[str, str]]:
    """Reuse narrative roles while scanning every unknown card/claim field."""
    records = _Records()
    data = card.model_dump(serialize_as_any=True)
    for key, value in sorted(data.items()):
        if key in {"title", "unknowns", "detail_unknowns", "document_path"}:
            records.add("unknowns" if key == "detail_unknowns" else key, value)
        elif key in {"id", "module_id", "links"}:
            records.machine(value, "card_reference")
        elif key == "source_commit":
            records.machine(value, "source_commit", _SHA)
        elif (key == "kind" and value in {"project", "architecture", "module", "interface", "data", "flow"}
              or key == "schema_version" and value == "1.0"
              or key == "coverage" and value in {"partial", "complete"}):
            continue
        elif key in {"claims", "detail_claims"}:
            for claim in value:
                for field, item in sorted(claim.items()):
                    if field == "text":
                        records.add("claims.text", item)
                    elif field == "evidence_ids":
                        records.machine(item, "evidence_reference", _EVIDENCE_ID)
                    elif field == "relation_ids":
                        records.machine(item, "relation_reference", re.compile(r"relation:(?:[a-f0-9]{24}|[a-f0-9]{64})"))
                    elif field == "knowledge_type" and item in {"FACT", "DERIVED", "AI_INFERRED"}:
                        continue
                    elif field == "source_scope":
                        _scope_records(records, item)
                    else:
                        records.add("field_name", field)
                        records.generic(item, ("claims", field))
        else:
            records.add("field_name", key)
            records.generic(value, (key,))
    return records.finish()


def _scope_records(records: _Records, scope: dict | None) -> None:
    """Only typed, service-owned provenance fields are machine metadata."""
    if scope is None:
        return
    for key, value in sorted(scope.items()):
        if key == "child_ids":
            records.machine(value, "scope_child_reference", re.compile(r"summary-input:(?:[a-f0-9]{24}|[a-f0-9]{64})"))
        elif key == "module_ids":
            records.machine(value, "scope_module_reference", re.compile(r"module:(?:[a-f0-9]{24}|[a-f0-9]{64})"))
        elif key in {"id", "fact_id", "relation_ids"}:
            records.machine(value, "scope_reference")
        elif key == "source_ref_ids":
            records.machine(value, "evidence_reference", _EVIDENCE_ID)
        elif key == "packet_digest":
            records.machine(value, "scope_digest", _SHA)
        elif key == "kind" and value in {"declaration_fragment", "summary_group", "summary_synthesis"}:
            continue
        elif key == "relationship_view" and (value is None or type(value) is str and value in {"symbol_graph", "resolution_counts"}):
            continue
        elif key in {"fragment_index", "fragment_total"} and type(value) is int:
            continue
        else:
            records.add("field_name", key)
            records.generic(value, ("source_scope", key))


def project_records(records: list[dict[str, str]]) -> dict[str, list[str]]:
    """Group records for the existing structured scanner without losing roles."""
    grouped: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if set(record) != {"field", "text"} or not isinstance(record["field"], str) or not isinstance(record["text"], str) or not record["field"]:
            raise ValueError("隐私投影记录结构无效")
        if record["text"]:
            grouped[record["field"]].add(record["text"])
    return {field: sorted(values) for field, values in sorted(grouped.items())}


def untrusted_privacy_records(value: object, field: str) -> list[dict[str, str]]:
    """Classify all values of an untrusted wrapper; no machine exemptions."""
    if type(field) is not str or not field:
        raise ValueError("隐私投影记录结构无效")
    records = _Records()
    records.generic(value, (field,))
    return records.finish()
