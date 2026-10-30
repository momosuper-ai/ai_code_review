"""Explicit synthesis graph views, independent of canonical provenance.

The service still owns and scans the complete canonical packet. These pure
functions encode /2 losslessly or produce an explicitly lossy /3 count view.
They never truncate child strings, choose source evidence, or certify semantics.
The /3 view cannot reconstruct edges; complete raw relationships stay in the
canonical packet and service graph, authenticated before transport is built.
"""

import copy
from collections import Counter
from functools import wraps
import hashlib
import json
import re


ENTITY_IDENTITY = "input topology only; module ownership not independently proven"
_ERROR = "综合知识传输表契约无效"
_FIELDS = frozenset({"protocol", "purpose", "level", "module", "aggregate_slot", "children",
                     "relationships", "entities", "coverage", "inherited_limitations", "semantic_support"})
_RELATION_COLUMNS = ("from", "to", "type", "resolution", "target_name", "knowledge_type", "candidates")
_ENTITY_COLUMNS = ("id", "child_candidates")
_COUNT_COLUMNS = ("type", "resolution", "knowledge_type", "count")
_CANONICAL_RELATION_FIELDS = {"id", "from_id", "to_id", "type", "target_name", "resolution",
                              "knowledge_type", "source_ref_ids", "candidates"}


def _fail() -> None:
    # Every rejected field or value can originate in untrusted source/model text.
    raise ValueError(_ERROR)


def _contract(function):
    """Keep malformed JSON types and rejected data out of all diagnostics."""
    @wraps(function)
    def checked(wire):
        try:
            return function(wire)
        except (KeyError, TypeError, ValueError):
            raise ValueError(_ERROR) from None
    return checked


def _keys(value: object, expected: set | frozenset) -> None:
    if type(value) is not dict or set(value) != expected:
        _fail()


def _strings(value: object) -> list[str]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _fail()
    return value


def _alias(value: object, prefix: str) -> None:
    if type(value) is not str or not re.fullmatch(prefix + r"[1-9]\d*", value):
        _fail()


def _validate_expanded(wire: dict) -> None:
    """Validate all fields and references without repairing or dropping data."""
    _keys(wire, _FIELDS)
    if (wire["protocol"] != "project-brain-synthesis/1" or wire["purpose"] not in {"module", "project"}
            or type(wire["level"]) is not int or wire["level"] < 0
            or type(wire["module"]) is not str or not wire["module"].strip()
            or wire["aggregate_slot"] != "S1" or type(wire["semantic_support"]) is not str):
        _fail()
    _strings(wire["coverage"])
    limits = wire["inherited_limitations"]
    _keys(limits, {"count", "detail_retained"})
    if type(limits["count"]) is not int or limits["count"] < 0 or limits["detail_retained"] is not True:
        _fail()

    children = wire["children"]
    if type(children) is not list or not children:
        _fail()
    child_ids = set()
    for child in children:
        _keys(child, {"id", "checkpoint_digest", "module", "title", "claims", "unknowns", "selection"})
        _alias(child["id"], "C")
        if child["id"] in child_ids:
            _fail()
        child_ids.add(child["id"])
        if (type(child["checkpoint_digest"]) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", child["checkpoint_digest"])
                or type(child["module"]) is not str or type(child["title"]) is not str or not child["title"].strip()):
            _fail()
        if type(child["claims"]) is not list:
            _fail()
        for claim in child["claims"]:
            _keys(claim, {"text", "knowledge_type"})
            if type(claim["text"]) is not str or not claim["text"].strip() or claim["knowledge_type"] != "AI_INFERRED":
                _fail()
        _strings(child["unknowns"])
        if not child["claims"] and not child["unknowns"]:
            _fail()
        selection = child["selection"]
        _keys(selection, {"claim_start", "claim_end", "unknown_start", "unknown_end"})
        if (any(type(value) is not int or value < 0 for value in selection.values())
                or selection["claim_end"] - selection["claim_start"] != len(child["claims"])
                or selection["unknown_end"] - selection["unknown_start"] != len(child["unknowns"])):
            _fail()

    if type(wire["entities"]) is not dict:
        _fail()
    for alias, entity in wire["entities"].items():
        _alias(alias, "F")
        _keys(entity, {"child_candidates", "identity"})
        candidates = _strings(entity["child_candidates"])
        if (entity["identity"] != ENTITY_IDENTITY or len(set(candidates)) != len(candidates)
                or not set(candidates) <= child_ids):
            _fail()

    if type(wire["relationships"]) is not list:
        _fail()
    entity_ids = set(wire["entities"])
    used_entities = set()
    for relation in wire["relationships"]:
        _keys(relation, set(_RELATION_COLUMNS))
        _alias(relation["from"], "F")
        if relation["to"] is not None:
            _alias(relation["to"], "F")
        candidates = _strings(relation["candidates"])
        for candidate in candidates:
            _alias(candidate, "F")
        if (type(relation["type"]) is not str or type(relation["target_name"]) is not str
                or relation["resolution"] not in {"resolved", "candidate", "unresolved"}
                or relation["knowledge_type"] not in {"FACT", "DERIVED"}
                or (relation["resolution"] == "resolved") != (relation["to"] is not None)
                or relation["resolution"] == "candidate" and not candidates):
            _fail()
        endpoints = {relation["from"], *candidates}
        if relation["to"] is not None:
            endpoints.add(relation["to"])
        if not endpoints <= entity_ids:
            _fail()
        used_entities.update(endpoints)
    if used_entities != entity_ids:
        _fail()


def _compact_table(records: list[dict], fields: tuple[str, ...]) -> dict:
    """Move only exactly equal values to defaults; preserve row order/count."""
    defaults = {field: copy.deepcopy(records[0][field]) for field in fields
                if records and all(record[field] == records[0][field] for record in records)}
    columns = [field for field in fields if field not in defaults]
    return {"columns": columns, "defaults": defaults,
            "rows": [[copy.deepcopy(record[field]) for field in columns] for record in records]}


def _expand_table(table: dict, fields: tuple[str, ...]) -> list[dict]:
    _keys(table, {"columns", "defaults", "rows"})
    columns = _strings(table["columns"])
    defaults = table["defaults"]
    if (type(defaults) is not dict or len(set(columns)) != len(columns)
            or set(columns) & set(defaults) or set(columns) | set(defaults) != set(fields)
            or type(table["rows"]) is not list):
        _fail()
    records = []
    for row in table["rows"]:
        if type(row) is not list or len(row) != len(columns):
            _fail()
        records.append({field: copy.deepcopy(defaults[field] if field in defaults else row[columns.index(field)])
                        for field in fields})
    return records


@_contract
def compact_synthesis_wire(wire: dict) -> dict:
    """Encode validated /1 semantics as /2 tables without altering any strings."""
    _validate_expanded(wire)
    result = {key: copy.deepcopy(value) for key, value in wire.items() if key not in {"relationships", "entities"}}
    result["protocol"] = "project-brain-synthesis/2"
    result["relationships"] = _compact_table(wire["relationships"], _RELATION_COLUMNS)
    entities = [{"id": alias, "child_candidates": value["child_candidates"]}
                for alias, value in wire["entities"].items()]
    result["entities"] = _compact_table(entities, _ENTITY_COLUMNS)
    result["entity_identity"] = ENTITY_IDENTITY
    return result


@_contract
def expand_synthesis_wire(wire: dict) -> dict:
    """Strictly reconstruct the full /1 graph; reject every unknown field."""
    _keys(wire, _FIELDS | {"entity_identity"})
    if wire["protocol"] != "project-brain-synthesis/2" or wire["entity_identity"] != ENTITY_IDENTITY:
        _fail()
    result = {key: copy.deepcopy(value) for key, value in wire.items() if key not in {"relationships", "entities"}}
    result["protocol"] = "project-brain-synthesis/1"
    result.pop("entity_identity")
    result["relationships"] = _expand_table(wire["relationships"], _RELATION_COLUMNS)
    entities = {}
    for record in _expand_table(wire["entities"], _ENTITY_COLUMNS):
        _alias(record["id"], "F")
        if record["id"] in entities:
            _fail()
        entities[record["id"]] = {"child_candidates": record["child_candidates"], "identity": ENTITY_IDENTITY}
    result["entities"] = entities
    _validate_expanded(result)
    return result


def synthesis_relationship_view(purpose: str, level: int) -> str:
    """Only the first module layer reads the complete symbol topology.

    Project level zero already consumes module roots rather than declarations,
    so it is an upper synthesis layer despite its independently numbered depth.
    """
    if type(purpose) is not str or purpose not in {"module", "project"} or type(level) is not int or level < 0:
        _fail()
    return "symbol_graph" if purpose == "module" and level == 0 else "resolution_counts"


def _canonical_relations(relations: list[dict]) -> list[dict]:
    if type(relations) is not list:
        _fail()
    records = {}
    for relation in relations:
        _keys(relation, _CANONICAL_RELATION_FIELDS)
        if (any(type(relation[key]) is not str or not relation[key] for key in ("id", "from_id", "type"))
                or type(relation["target_name"]) is not str
                or relation["resolution"] not in {"resolved", "candidate", "unresolved"}
                or relation["knowledge_type"] not in {"FACT", "DERIVED"}
                or relation["to_id"] is not None and (type(relation["to_id"]) is not str or not relation["to_id"])
                or (relation["resolution"] == "resolved") != (relation["to_id"] is not None)):
            _fail()
        candidates = _strings(relation["candidates"])
        refs = _strings(relation["source_ref_ids"])
        if (any(not value for value in [*candidates, *refs])
                or relation["resolution"] == "candidate" and not candidates):
            _fail()
        identifier = relation["id"]
        if identifier in records and records[identifier] != relation:
            _fail()
        records[identifier] = relation
    return [records[key] for key in sorted(records)]


def canonical_relationship_digest(relations: list[dict]) -> str:
    """Hash every complete canonical row, including IDs/evidence/target text."""
    try:
        value = _canonical_relations(relations)
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
    except (KeyError, TypeError, ValueError):
        raise ValueError(_ERROR) from None


@_contract
def validate_resolution_counts_wire(wire: dict) -> dict:
    """Authenticate the view contract, never present counts as dependency edges."""
    _keys(wire, _FIELDS - {"entities"})
    if wire["protocol"] != "project-brain-synthesis/3":
        _fail()
    view = wire["relationships"]
    _keys(view, {"view", "total", "counts", "canonical_digest", "detail_retained"})
    if (view["view"] != "resolution_counts" or view["detail_retained"] is not True
            or type(view["total"]) is not int or view["total"] < 0
            or type(view["canonical_digest"]) is not str or not re.fullmatch(r"[a-f0-9]{64}", view["canonical_digest"])):
        _fail()
    # Reuse exact child/hash/selection validation without inventing any edges.
    common = copy.deepcopy(wire)
    common.update(protocol="project-brain-synthesis/1", relationships=[], entities={})
    _validate_expanded(common)
    if synthesis_relationship_view(wire["purpose"], wire["level"]) != "resolution_counts":
        _fail()
    total = 0
    seen = set()
    for row in _expand_table(view["counts"], _COUNT_COLUMNS):
        if (type(row["type"]) is not str or not row["type"]
                or row["resolution"] not in {"resolved", "candidate", "unresolved"}
                or row["knowledge_type"] not in {"FACT", "DERIVED"}
                or type(row["count"]) is not int or row["count"] <= 0):
            _fail()
        key = row["type"], row["resolution"], row["knowledge_type"]
        if key in seen:
            _fail()
        seen.add(key)
        total += row["count"]
    if total != view["total"]:
        _fail()
    return copy.deepcopy(wire)


def resolution_counts_wire(common: dict, canonical_relations: list[dict], *, canonical_digest: str | None = None) -> dict:
    """Create an explicitly lossy graph view; callers retain/scan all raw rows."""
    try:
        _keys(common, _FIELDS - {"protocol", "relationships", "entities"})
        records = _canonical_relations(canonical_relations)
        counts = Counter((row["type"], row["resolution"], row["knowledge_type"]) for row in records)
        rows = [dict(zip(_COUNT_COLUMNS, (*key, count), strict=True)) for key, count in sorted(counts.items())]
        digest = canonical_digest if canonical_digest is not None else canonical_relationship_digest(records)
        wire = {**copy.deepcopy(common), "protocol": "project-brain-synthesis/3",
                "relationships": {"view": "resolution_counts", "total": len(records),
                                  "counts": _compact_table(rows, _COUNT_COLUMNS),
                                  "canonical_digest": digest, "detail_retained": True}}
        return validate_resolution_counts_wire(wire)
    except (KeyError, TypeError, ValueError):
        raise ValueError(_ERROR) from None
