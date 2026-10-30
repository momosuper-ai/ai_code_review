"""Compact semantic transport; canonical facts and source identities stay intact."""

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from src.core.project_brain.schema import InferenceScope


_FACT_FIELDS = {"id", "kind", "name", "module_id", "source_ref_ids", "extractor", "attributes"}
_RELATION_FIELDS = {"id", "from_id", "to_id", "type", "target_name", "resolution", "knowledge_type",
                    "source_ref_ids", "candidates"}
_EVIDENCE_FIELDS = {"id", "path", "display_path", "line_start", "line_end", "excerpt_end", "excerpt", "symbol_id",
                    "commit_sha", "blob_sha"}
_INTERNAL_ATTRIBUTES = {"assigned_names", "shadowed_names", "closure_bindings", "scope_shadowed_calls"}
# Protocol columns are explicit: a future contract extension must update the
# encoder, rather than silently adding a column without its corresponding cell.
_SCOPE_FIELDS = ("id", "kind", "packet_digest", "fact_id", "relation_ids", "source_ref_ids",
                 "fragment_index", "fragment_total", "child_ids", "module_ids", "relationship_view")
_SCOPE_FIELD_SETS = tuple(set(_SCOPE_FIELDS) - missing for missing in (
    set(), {"relationship_view"}, {"child_ids", "module_ids"}, {"child_ids", "module_ids", "relationship_view"}))


@dataclass(frozen=True)
class CompactContext:
    """In-memory wire representation and its strictly scoped evidence aliases."""

    wire: dict
    evidence_ids: dict[str, str]
    scope_ids: dict[str, str] = field(default_factory=dict)
    origin_ids: dict[str, str] = field(default_factory=dict)
    packet_digests: dict[str, str] = field(default_factory=dict)

    def restore_evidence_ids(self, ids: list[str]) -> list[str]:
        """Accept only this packet's evidence aliases or existing canonical IDs."""
        if not isinstance(ids, list):
            raise ValueError("模型证据引用必须是数组")
        canonical = set(self.evidence_ids.values())
        result = []
        for reference in ids:
            if not isinstance(reference, str):
                raise ValueError("模型证据引用必须是字符串")
            if reference in self.evidence_ids:
                result.append(self.evidence_ids[reference])
            elif reference in canonical:
                result.append(reference)
            else:
                raise ValueError("模型引用了本知识包不存在的证据")
        return result

    def restore_source_scope(self, reference: str) -> dict:
        """Reconstruct exact summary provenance, independently of this packet's F/R IDs.

        The inverse maps exist only in service memory. An origin alias is an
        opaque identity from an earlier compilation, not a fact or resolved
        relationship offered by the current packet.
        """
        if not isinstance(reference, str) or reference not in self.scope_ids:
            raise ValueError("汇总引用了不存在的来源范围")
        rows = [row for row in read_rows(self.wire, "source_scopes") if row["id"] == reference]
        if len(rows) != 1 or set(rows[0]) != set(_SCOPE_FIELDS):
            raise ValueError("汇总来源范围表结构无效")
        scope = rows[0]

        def origin(alias: str) -> str:
            if not isinstance(alias, str) or alias not in self.origin_ids:
                raise ValueError("汇总引用了不存在的来源身份")
            return self.origin_ids[alias]

        digest = scope["packet_digest"]
        if not isinstance(digest, str) or digest not in self.packet_digests:
            raise ValueError("汇总引用了不存在的来源包摘要")
        scope["id"] = self.scope_ids[reference]
        scope["packet_digest"] = self.packet_digests[digest]
        if scope["fact_id"] is not None:
            scope["fact_id"] = origin(scope["fact_id"])
        scope["relation_ids"] = [origin(alias) for alias in scope["relation_ids"]]
        scope["child_ids"] = [origin(alias) for alias in scope["child_ids"]]
        scope["module_ids"] = [origin(alias) for alias in scope["module_ids"]]
        scope["source_ref_ids"] = self.restore_evidence_ids(scope["source_ref_ids"])
        return _validated_scope(scope)

    def restore_summary_claim(self, claim: dict) -> dict:
        """Decode a wire summary claim without changing any text or future fields."""
        if not isinstance(claim, dict):
            raise ValueError("汇总结论必须是对象")

        def restore(value: Any) -> Any:
            if isinstance(value, dict):
                return {key: self.restore_evidence_ids(entry)
                        if key in {"evidence_ids", "source_ref_ids"} else restore(entry)
                        for key, entry in value.items()}
            if isinstance(value, list):
                return [restore(entry) for entry in value]
            return copy.deepcopy(value)

        result = restore(claim)
        if result.get("source_scope") is not None:
            result["source_scope"] = self.restore_source_scope(result["source_scope"])
        return result


def _validated_scope(raw: Any) -> dict:
    """Accept only the complete service contract; never drop unknown metadata."""
    if not isinstance(raw, dict) or set(raw) not in _SCOPE_FIELD_SETS:
        raise ValueError("汇总来源范围字段无效")
    normalized = {"child_ids": [], "module_ids": [], "relationship_view": None, **raw}
    try:
        scope = InferenceScope.model_validate(normalized, strict=True).model_dump()
    except ValidationError:
        # Pydantic errors can contain arbitrary input keys and values. Do not
        # expose those through task errors or logs.
        raise ValueError("汇总来源范围结构无效") from None
    if scope != normalized:
        raise ValueError("汇总来源范围不能隐式转换")
    return scope


def read_rows(wire: dict, table: str) -> list[dict]:
    """Expand columnar rows and packet-wide constants for integrity auditing."""
    columns = wire["columns"][table]
    defaults = wire.get("defaults", {}).get(table, {})
    if len(set(columns)) != len(columns) or set(columns) & set(defaults):
        raise ValueError("知识传输表列定义冲突")
    result = []
    for row in wire[table]:
        if len(row) != len(columns):
            raise ValueError("知识传输表行与列数不一致")
        result.append({**copy.deepcopy(defaults), **dict(zip(columns, copy.deepcopy(row)))})
    return result


def _hoist_constants(wire: dict) -> None:
    defaults = copy.deepcopy(wire.get("defaults", {}))
    for table in ("facts", "relations", "evidence", "source_scopes"):
        if table not in wire:
            continue
        rows = wire[table]
        if len(rows) < 2:
            continue
        columns = wire["columns"][table]
        selected = []
        for index, column in enumerate(columns):
            if column == "id":
                continue
            value = rows[0][index]
            if not all(row[index] == value for row in rows[1:]):
                continue
            size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
            # Hoist only when transmission is smaller even after naming the
            # constant. Never select a majority value or discard exceptions.
            if (len(rows) - 1) * (size + 1) > len(column.encode()) + 5:
                selected.append(index)
        if selected:
            defaults.setdefault(table, {}).update(
                {columns[index]: copy.deepcopy(rows[0][index]) for index in selected})
            wire["columns"][table] = [column for index, column in enumerate(columns) if index not in selected]
            wire[table] = [[value for index, value in enumerate(row) if index not in selected] for row in rows]
    if defaults:
        wire["defaults"] = defaults


def _collect(records: list[dict], category: str) -> dict[str, dict]:
    values = {}
    for record in records:
        identifier = record.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"{category}必须有有效 ID")
        previous = values.get(identifier)
        if previous is not None and previous != record:
            raise ValueError(f"{category} ID 对应的内容冲突")
        values[identifier] = copy.deepcopy(record)
    return values


def _evidence_records(items: list[dict]) -> dict[str, dict]:
    values: dict[str, dict] = {}
    for item in items:
        for raw in item.get("evidence", []):
            identifier = raw.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("证据必须有有效 ID")
            value = copy.deepcopy(raw)
            previous = values.get(identifier)
            if previous is not None:
                identity = lambda entry: {key: value for key, value in entry.items()
                                          if key not in {"excerpt", "excerpt_end"}}
                if identity(previous) != identity(value):
                    raise ValueError("同一证据 ID 的源码身份冲突")
                old, new = previous.get("excerpt", ""), value.get("excerpt", "")
                if old != new and not (old.startswith(new) or new.startswith(old)):
                    raise ValueError("同一证据 ID 的片段冲突")
                if len(old) >= len(new):
                    value = previous
            values[identifier] = value
    return values


def compact_packet(packet: dict) -> CompactContext:
    """Deduplicate identities and exact source lines without omitting knowledge.

    The model receives short local aliases, columnar facts and relationships,
    and an exact source-chunk pool. Every fact/relationship and resolution state
    is retained. Only the extractor's assigned/shadowed-name sets are excluded
    from transmission, including closure and scope-shadow audit sets; they
    remain in the original service-side facts. The redefined flag is retained
    because it describes ambiguous declarations in the observed source.
    Aliases and excerpts are transient and must never be persisted separately.
    """
    items = packet.get("items", [])
    if not isinstance(items, list):
        raise ValueError("知识包 items 必须是数组")
    facts_input = []
    fact_keys = []
    summary_keys = set()
    scopes_input = []
    for index, item in enumerate(items):
        fact = copy.deepcopy(item["fact"])
        key = fact.get("id") or f"@summary:{index}"
        if not fact.get("id"):
            summary_keys.add(key)
            # Only direct claims of a summary input have service-owned
            # provenance. A similarly named future field elsewhere is data,
            # not metadata, and must keep its complete contents.
            for claim in fact.get("claims", []):
                if isinstance(claim, dict) and claim.get("source_scope") is not None:
                    scopes_input.append(_validated_scope(claim["source_scope"]))
        fact_keys.append(key)
        fact["id"] = key
        facts_input.append(fact)
    facts = _collect(facts_input, "事实")
    scopes = _collect(scopes_input, "汇总来源范围")
    relations = _collect([relation for item in items for relation in item.get("relations", [])], "关系")
    evidence = _evidence_records(items)
    evidence_aliases = {identifier: f"E{index}" for index, identifier in enumerate(sorted(evidence), 1)}
    entity_ids = set(facts)
    for relation in relations.values():
        entity_ids.update(identifier for identifier in [relation.get("from_id"), relation.get("to_id")]
                          if identifier is not None)
        entity_ids.update(relation.get("candidates", []))
        if relation.get("resolution") not in {"resolved", "candidate", "unresolved"}:
            raise ValueError("关系解析状态无效")
    entity_ids.update(ref["symbol_id"] for ref in evidence.values() if ref.get("symbol_id"))
    entity_aliases = {identifier: f"F{index}" for index, identifier in enumerate(sorted(entity_ids), 1)}
    relation_aliases = {identifier: f"R{index}" for index, identifier in enumerate(sorted(relations), 1)}
    scope_aliases = {identifier: f"I{index}" for index, identifier in enumerate(sorted(scopes), 1)}
    origins = {identifier for scope in scopes.values() for identifier in scope["relation_ids"]}
    origins.update(scope["fact_id"] for scope in scopes.values() if scope["fact_id"] is not None)
    origins.update(identifier for scope in scopes.values() for identifier in scope["child_ids"] + scope["module_ids"])
    origin_aliases = {identifier: f"O{index}" for index, identifier in enumerate(sorted(origins), 1)}
    digests = {scope["packet_digest"] for scope in scopes.values()}
    digest_aliases = {digest: f"D{index}" for index, digest in enumerate(sorted(digests), 1)}
    paths = {ref["path"] for ref in evidence.values()}
    paths.update(fact.get("attributes", {})["path"] for fact in facts.values()
                 if "path" in fact.get("attributes", {}))
    path_aliases = {path: f"P{index}" for index, path in enumerate(sorted(paths), 1)}
    modules = {fact.get("module_id", packet.get("module_id")) for fact in facts.values()}
    modules.discard(None)
    module_aliases = {identifier: f"M{index}" for index, identifier in enumerate(sorted(modules), 1)}
    extractors = sorted({fact["extractor"] for fact in facts.values() if fact.get("extractor")})
    extractor_aliases = {name: f"X{index}" for index, name in enumerate(extractors, 1)}

    def refs(ids: list[str]) -> list[str]:
        if any(identifier not in evidence_aliases for identifier in ids):
            raise ValueError("事实或关系引用了缺失证据")
        return [evidence_aliases[identifier] for identifier in ids]

    def extra(value: Any) -> Any:
        # Summary claims carry the same evidence IDs. Preserve future fields,
        # translating only schema-defined identifier lists, never arbitrary text.
        if isinstance(value, dict):
            return {key: refs(entry) if key in {"evidence_ids", "source_ref_ids"}
                    else extra(entry) for key, entry in value.items()}
        if isinstance(value, list):
            return [extra(entry) for entry in value]
        return copy.deepcopy(value)

    def fact_extra(identifier: str, fact: dict) -> dict:
        value = {key: copy.deepcopy(entry) for key, entry in fact.items() if key not in _FACT_FIELDS}
        if identifier in summary_keys:
            for claim in value.get("claims", []):
                if isinstance(claim, dict) and claim.get("source_scope") is not None:
                    claim["source_scope"] = scope_aliases[claim["source_scope"]["id"]]
        return extra(value)

    source_lines: dict[str, dict[int, str]] = {path: {} for path in paths}
    sources = {alias: {"path": path} for path, alias in path_aliases.items()}
    snippets: dict[str, str] = {}
    evidence_rows = []
    for identifier, ref in sorted(evidence.items()):
        path = ref["path"]
        source = sources[path_aliases[path]]
        if ref.get("display_path") is not None:
            display = ref["display_path"]
            if not isinstance(display, str):
                raise ValueError("知识来源展示路径无效")
            # Group by the opaque locator, never by the potentially identical
            # redacted display name of two distinct source files.
            if source.get("_display_bound") and source["path"] != display:
                raise ValueError("同一来源的展示路径冲突")
            source["path"], source["_display_bound"] = display, True
        for key in ("commit_sha", "blob_sha"):
            if ref.get(key) is not None:
                if key in source and source[key] != ref[key]:
                    raise ValueError("证据指向不同源码版本")
                source[key] = ref[key]
        start = ref["line_start"]
        end = ref.get("excerpt_end")
        range_end = ref.get("line_end")
        if start < 1 or (range_end is not None and range_end < start):
            raise ValueError("证据源码行范围无效")
        if end is not None and (end < start or (range_end is not None and end > range_end)):
            raise ValueError("证据片段超出源码行范围")
        snippet = None
        if "excerpt" in ref:
            lines = ref["excerpt"].split("\n")
            if end is not None and len(lines) == end - start + 1:
                for line, text in enumerate(lines, start):
                    existing = source_lines[path].get(line)
                    if existing is not None and existing != text:
                        raise ValueError("同一源码行的证据片段冲突")
                    source_lines[path][line] = text
            else:
                # Existing explicit long-line notices are not invented source
                # lines. Keep their exact text and supplied bounds separately.
                if ref["excerpt"] not in snippets:
                    snippets[ref["excerpt"]] = f"S{len(snippets) + 1}"
                snippet = snippets[ref["excerpt"]]
        evidence_rows.append([evidence_aliases[identifier], path_aliases[path], start, range_end, end,
                              snippet, entity_aliases.get(ref.get("symbol_id")),
                              extra({key: value for key, value in ref.items() if key not in _EVIDENCE_FIELDS})])
    for path, lines in source_lines.items():
        if lines:
            chunks = []
            start = end = None
            text_lines = []
            for number, text in sorted(lines.items()):
                if end is not None and number != end + 1:
                    chunks.append([start, "\n".join(text_lines)])
                    start, text_lines = None, []
                if start is None:
                    start = number
                end = number
                text_lines.append(text)
            chunks.append([start, "\n".join(text_lines)])
            sources[path_aliases[path]]["chunks"] = chunks
    for source in sources.values():
        source.pop("_display_bound", None)

    fact_rows = []
    for identifier, fact in sorted(facts.items()):
        attributes = {key: copy.deepcopy(value) for key, value in fact.get("attributes", {}).items()
                      if key not in _INTERNAL_ATTRIBUTES}
        if "path" in attributes:
            attributes["path"] = path_aliases[attributes["path"]]
        fact_rows.append([entity_aliases[identifier], fact.get("kind"), fact.get("name"),
                          module_aliases.get(fact.get("module_id", packet.get("module_id"))),
                          refs(fact.get("source_ref_ids", [])), extractor_aliases.get(fact.get("extractor")),
                          attributes, fact_extra(identifier, fact)])
    relation_rows = []
    for identifier, relation in sorted(relations.items()):
        relation_rows.append([relation_aliases[identifier], entity_aliases[relation["from_id"]],
                              relation["type"], entity_aliases.get(relation.get("to_id")),
                              relation.get("target_name", ""), relation["resolution"],
                              relation.get("knowledge_type", "FACT"), refs(relation.get("source_ref_ids", [])),
                              [entity_aliases[value] for value in relation.get("candidates", [])],
                              extra({key: value for key, value in relation.items() if key not in _RELATION_FIELDS})])
    compact_items = []
    for key, item in zip(fact_keys, items):
        compact_items.append({"fact": entity_aliases[key],
                              "relations": [relation_aliases[relation["id"]] for relation in item.get("relations", [])],
                              "evidence": refs([ref["id"] for ref in item.get("evidence", [])]),
                              **extra({name: value for name, value in item.items()
                                       if name not in {"fact", "relations", "evidence"}})})
    wire = {"protocol": "project-brain-context/1", "module": packet.get("module", ""),
            "columns": {
                "facts": ["id", "kind", "name", "module_id", "source_ref_ids", "extractor", "attributes", "extra"],
                "relations": ["id", "from_id", "type", "to_id", "target_name", "resolution", "knowledge_type",
                              "source_ref_ids", "candidates", "extra"],
                "evidence": ["id", "source", "line_start", "line_end", "excerpt_end", "snippet", "symbol_id", "extra"],
            }, "sources": sources, "extractors": {alias: name for name, alias in extractor_aliases.items()},
            "facts": fact_rows, "relations": relation_rows, "evidence": evidence_rows, "items": compact_items,
            "coverage": copy.deepcopy(packet.get("coverage", [])),
            "metadata_policy": "解析器内部名称绑定集合仅在服务侧保留；所有语义属性、关系及解析状态完整传输。"}
    if scopes:
        wire["columns"]["source_scopes"] = list(_SCOPE_FIELDS)
        wire["source_scopes"] = [
            [scope_aliases[identifier], scope["kind"], digest_aliases[scope["packet_digest"]],
             origin_aliases.get(scope["fact_id"]),
             [origin_aliases[origin] for origin in scope["relation_ids"]], refs(scope["source_ref_ids"]),
             scope["fragment_index"], scope["fragment_total"],
             [origin_aliases[origin] for origin in scope["child_ids"]],
             [origin_aliases[origin] for origin in scope["module_ids"]], scope["relationship_view"]]
            for identifier, scope in sorted(scopes.items())
        ]
        wire["metadata_policy"] += (
            "汇总结论的I引用source_scopes共享表；O为历史来源身份，D为来源包摘要，均不是本包F/R实体。"
            "来源范围仅表示解释输入范围，不证明语义正确。")
    if any("chunks" in source for source in sources.values()):
        wire["chunk_columns"] = ["line_start", "text"]
    if snippets:
        wire["snippets"] = {alias: text for text, alias in snippets.items()}
    if len(module_aliases) > 1:
        wire["modules"] = {alias: packet.get("module", "") if key == packet.get("module_id") else key
                           for key, alias in module_aliases.items()}
    # Unknown envelope fields are retained rather than implicitly discounted.
    envelope_extra = extra({key: value for key, value in packet.items()
                            if key not in {"module_id", "module", "coverage", "items"}})
    if envelope_extra:
        wire["extra"] = envelope_extra
    _hoist_constants(wire)
    return CompactContext(
        wire, {alias: identifier for identifier, alias in evidence_aliases.items()},
        {alias: identifier for identifier, alias in scope_aliases.items()},
        {alias: identifier for identifier, alias in origin_aliases.items()},
        {alias: digest for digest, alias in digest_aliases.items()},
    )
