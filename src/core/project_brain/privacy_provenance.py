"""Strict, read-only contract for irreversible privacy discovery provenance.

Locations are *candidate occurrences in the records actually sent*, not proof of
which field caused a scanner to classify a value. No original text or field name
belongs in this format. The pure decoder checks structure, not keyed authenticity;
the worker ledger additionally verifies its keyed call and location bindings.
"""

import json
import re
from uuid import UUID

TYPES = frozenset({"SECRET", "KEY", "IP", "DOMAIN", "EMAIL", "CERT", "CUSTOM"})
STAGES = frozenset({"scan", "extract", "privacy", "finalize", "source_privacy", "compile", "summary",
                    "module_summary", "project_summary", "planning", "assemble"})
FIELD_ROLES = frozenset({"code_identifier", "source_path", "source_excerpt",
                         "semantic_output", "retry_reasoning", "field_name", "generic"})
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 10_000
MAX_LENGTHS = 128
MAX_CALLS = 100_000
MAX_LOCATIONS = 50_000
MAX_SCAN_RECORDS = 1024
MAX_SCAN_FINDINGS = 256
MAX_RECORD_CHARS = 131_072
MAX_FIELD_CHARS = 1024
MAX_SCAN_BYTES = 1024 * 1024
MAX_RECORD_INDEX = 2**31 - 1
_HEX = re.compile(r"[a-f0-9]{64}")
_ERROR = "隐私指纹或来源检查点无效"
_CALL_FIELDS = {"context", "after_response", "scope", "packet", "records_hmac",
                "findings_hmac", "start", "record_count", "binding_hmac"}
_LOCATION_FIELDS = {"call_id", "record_hmac", "field_hmac", "field_role", "location_kind",
                    "record_index", "record_length", "first_offset", "occurrence_count",
                    "length", "type"}
_POLICY_FIELDS = {"kind", "context", "scope", "record_hmac", "field_hmac", "field_role",
                  "record_length", "first_offset", "occurrence_count", "length", "type",
                  "source_build_id", "source_commit", "source_snapshot", "source_finding", "detector"}


def _fail() -> None:
    raise ValueError(_ERROR)


def _hex(value: object) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def canonical_json(value: object) -> str:
    """One deterministic encoding for keyed bindings; never log its result."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def validate_context(context: object) -> dict | None:
    """Copy only closed machine identity; reject rather than echo bad metadata."""
    if context is None:
        return None
    if type(context) is not dict or set(context) != {"build_id", "unit_id", "stage"}:
        _fail()
    _uuid(context["build_id"])
    if not _integer(context["unit_id"], 1, 2**63 - 1) or type(context["stage"]) is not str or context["stage"] not in STAGES:
        _fail()
    return dict(context)


def _uuid(value: object) -> None:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            _fail()
    except (ValueError, AttributeError, TypeError):
        _fail()


def validate_scan_metadata(call_id: object, scope: object, packet: object,
                           start: object, context: object, after_response: object) -> dict | None:
    _uuid(call_id)
    if not _hex(scope) or not _hex(packet) or not _integer(start, 0, MAX_RECORD_INDEX) or type(after_response) is not bool:
        _fail()
    return validate_context(context)


def field_role(field: str) -> str:
    """A closed diagnostic hint, never the original semantic field path."""
    if field in FIELD_ROLES:
        return field
    leaf = field.rsplit(".", 1)[-1]
    if leaf in {"model_retry_reasoning", "reasoning", "reasoning_content"}:
        return "retry_reasoning"
    if leaf in {"excerpt", "source_excerpt", "source_signature"}:
        return "source_excerpt"
    if leaf in {"text", "texts", "unknowns", "all_unknowns", "title", "overview"}:
        return "semantic_output"
    return "generic"


def validate_items(items: object) -> list[dict]:
    """Validate ephemeral discoveries before computing any persistent metadata."""
    if type(items) is not list or len(items) > MAX_SCAN_FINDINGS:
        _fail()
    values: dict[str, str] = {}
    for item in items:
        if type(item) is not dict or set(item) != {"value", "type"}:
            _fail()
        value, kind = item["value"], item["type"]
        if type(value) is not str or not 1 <= len(value) <= MAX_RECORD_CHARS or type(kind) is not str or kind not in TYPES:
            _fail()
        try:
            value.encode("utf-8")
        except UnicodeError:
            _fail()
        if value in values and values[value] != kind:
            _fail()
        values[value] = kind
    return [{"value": value, "type": kind} for value, kind in sorted(values.items())]


def validate_records(records: object, start: int) -> list[dict]:
    if type(records) is not list or not 1 <= len(records) <= MAX_SCAN_RECORDS or start + len(records) - 1 > MAX_RECORD_INDEX:
        _fail()
    copied = []
    for record in records:
        if type(record) is not dict or set(record) != {"field", "text"}:
            _fail()
        field, text = record["field"], record["text"]
        if (type(field) is not str or not 1 <= len(field) <= MAX_FIELD_CHARS
                or type(text) is not str or not 1 <= len(text) <= MAX_RECORD_CHARS):
            _fail()
        copied.append({"field": field, "text": text})
    try:
        if len(canonical_json(copied).encode("utf-8")) > MAX_SCAN_BYTES:
            _fail()
    except (UnicodeError, ValueError):
        _fail()
    return copied


def _entries(data: object) -> dict[str, tuple[int, str]]:
    if type(data) is not dict or len(data) > MAX_ENTRIES:
        _fail()
    result = {}
    for digest, item in data.items():
        if (not _hex(digest) or type(item) is not list or len(item) != 2
                or not _integer(item[0], 1, MAX_RECORD_CHARS) or type(item[1]) is not str or item[1] not in TYPES):
            _fail()
        result[digest] = (item[0], item[1])
    if len({length for length, _ in result.values()}) > MAX_LENGTHS:
        _fail()
    return result


def decode_fingerprint_index(data: object) -> tuple[dict[str, tuple[int, str]], dict]:
    """Decode v2/v3 or legacy v1 without disk/key access or invented old origins.

    Only the worker can verify keyed bindings. Audits using this pure decoder
    must not describe structural validity as authenticity of discovery context.
    """
    try:
        if len(canonical_json(data).encode("utf-8")) > MAX_INDEX_BYTES:
            _fail()
        return _decode(data)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


def _decode(data: object) -> tuple[dict[str, tuple[int, str]], dict]:
    if type(data) is not dict:
        _fail()
    if "version" not in data:
        entries = _entries(data)
        return entries, {"calls": {}, "findings": {key: {"legacy_unknown": True, "records": {}} for key in entries}}
    if (set(data) != {"version", "entries", "origins", "index_hmac"}
            or type(data["version"]) is not int or data["version"] not in {2, 3} or not _hex(data["index_hmac"])):
        _fail()
    entries = _entries(data["entries"])
    origins = data["origins"]
    if type(origins) is not dict or set(origins) != {"calls", "findings"}:
        _fail()
    calls, findings = origins["calls"], origins["findings"]
    if type(calls) is not dict or len(calls) > MAX_CALLS or type(findings) is not dict or set(findings) != set(entries):
        _fail()
    clean_calls, build_ids = {}, set()
    for call_id, call in calls.items():
        if type(call) is not dict or set(call) != _CALL_FIELDS:
            _fail()
        context = validate_scan_metadata(call_id, call["scope"], call["packet"], call["start"], call["context"], call["after_response"])
        if (not all(_hex(call[key]) for key in ("records_hmac", "findings_hmac", "binding_hmac"))
                or not _integer(call["record_count"], 1, MAX_SCAN_RECORDS)
                or call["start"] + call["record_count"] - 1 > MAX_RECORD_INDEX):
            _fail()
        clean_calls[call_id] = {**call, "context": context}
        if context is not None:
            build_ids.add(context["build_id"])
    if len(build_ids) > 1:
        _fail()
    clean_findings, used_calls, seen_records, count = {}, set(), {}, 0
    for digest, finding in findings.items():
        finding_fields = {"legacy_unknown", "records"} | ({"policy_records"} if data["version"] == 3 else set())
        if type(finding) is not dict or set(finding) != finding_fields or type(finding["legacy_unknown"]) is not bool or type(finding["records"]) is not dict:
            _fail()
        records = finding["records"]
        policy_records = finding.get("policy_records", {})
        if type(policy_records) is not dict or (not records and not policy_records and not finding["legacy_unknown"]):
            _fail()
        clean_records, positions = {}, set()
        for origin_id, record in records.items():
            count += 1
            if count > MAX_LOCATIONS or not _hex(origin_id) or type(record) is not dict or set(record) != _LOCATION_FIELDS:
                _fail()
            call_id = record["call_id"]
            if type(call_id) is not str or call_id not in clean_calls:
                _fail()
            call = clean_calls[call_id]
            if (not _hex(record["record_hmac"]) or not _hex(record["field_hmac"])
                    or type(record["field_role"]) is not str or record["field_role"] not in FIELD_ROLES
                    or record["location_kind"] != "candidate_occurrence"
                    or not _integer(record["record_index"], call["start"], call["start"] + call["record_count"] - 1)
                    or not _integer(record["record_length"], 1, MAX_RECORD_CHARS)
                    or type(record["length"]) is not int or record["length"] != entries[digest][0]
                    or type(record["type"]) is not str or record["type"] not in TYPES
                    or not _integer(record["first_offset"], 0, record["record_length"] - record["length"])
                    or not _integer(record["occurrence_count"], 1, record["record_length"] - record["length"] - record["first_offset"] + 1)):
                _fail()
            position = (call_id, record["record_index"])
            if position in positions:
                _fail()
            positions.add(position)
            identity = tuple(record[key] for key in ("record_hmac", "field_hmac", "field_role", "record_length"))
            if position in seen_records and seen_records[position] != identity:
                _fail()
            seen_records[position] = identity
            used_calls.add(call_id)
            clean_records[origin_id] = dict(record)
        clean_policy = {}
        for policy_id, record in policy_records.items():
            count += 1
            if count > MAX_LOCATIONS or not _hex(policy_id):
                _fail()
            clean_policy[policy_id] = validate_policy_record(record, entries[digest])
            if record["context"] is not None:
                build_ids.add(record["context"]["build_id"])
        clean_findings[digest] = {"legacy_unknown": finding["legacy_unknown"], "records": clean_records,
                                  **({"policy_records": clean_policy} if data["version"] == 3 else {})}
    if len(build_ids) > 1:
        _fail()
    if used_calls != set(clean_calls):
        _fail()
    return entries, {"calls": clean_calls, "findings": clean_findings}


def validate_policy_record(record: object, entry: tuple[int, str]) -> dict:
    """A non-model occurrence has its own closed origin, never a fake call ID."""
    if type(record) is not dict or set(record) != _POLICY_FIELDS:
        _fail()
    context = validate_context(record["context"])
    if (type(record["kind"]) is not str or record["kind"] not in {"inherited_hmac_match", "deterministic_rule"}
            or not all(_hex(record[key]) for key in ("scope", "record_hmac", "field_hmac"))
            or type(record["field_role"]) is not str or record["field_role"] not in FIELD_ROLES
            or not _integer(record["record_length"], 1, MAX_RECORD_CHARS)
            or type(record["length"]) is not int or record["length"] != entry[0]
            or type(record["type"]) is not str or record["type"] not in TYPES
            or not _integer(record["first_offset"], 0, record["record_length"] - record["length"])
            or not _integer(record["occurrence_count"], 1, record["record_length"] - record["length"] - record["first_offset"] + 1)):
        _fail()
    if record["kind"] == "deterministic_rule":
        if (record["detector"] != "privacy_rules/1"
                or any(record[key] is not None for key in ("source_build_id", "source_commit", "source_snapshot", "source_finding"))):
            _fail()
    else:
        _uuid(record["source_build_id"])
        if (type(record["source_commit"]) is not str
                or re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", record["source_commit"]) is None
                or not _hex(record["source_snapshot"]) or not _hex(record["source_finding"])
                or record["detector"] != "privacy_history/1"):
            _fail()
    return {**record, "context": context}


def parse_fingerprint_document(raw: bytes | str) -> dict:
    """Parse bounded unique-key JSON, without accessing a key or any file."""
    def unique(pairs: list[tuple]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        if type(raw) not in {bytes, str} or len(raw if type(raw) is bytes else raw.encode("utf-8")) > MAX_INDEX_BYTES:
            _fail()
        return json.loads(raw, object_pairs_hook=unique)
    except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
        raise ValueError(_ERROR) from None


def parse_fingerprint_index(raw: bytes | str) -> tuple[dict[str, tuple[int, str]], dict]:
    """Bounded JSON parsing also rejects duplicate keys before decoding."""
    return decode_fingerprint_index(parse_fingerprint_document(raw))
