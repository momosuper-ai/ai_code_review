"""Deterministic, checkpoint-authenticated module and project synthesis trees.

Overview prose is deliberately lossy. Complete leaf conclusions, inherited
limitations and immutable dependency identities remain available for assembly.
This module plans data-only tasks; it never calls a model or a database.
"""

from collections import defaultdict
from dataclasses import dataclass
import re
from typing import Any

from src.config import settings
from src.core.project_brain.compiler import CompilationLimitError, encoded, packet_wire, token_bound
from src.core.project_brain.schema import Compilation, FactBundle, stable_id
from src.core.project_brain.synthesis_wire import synthesis_relationship_view
from src.core.project_brain.trusted_packet import checkpoint_digest

MAX_SUMMARY_CHILDREN = 24
MAX_SUMMARY_DEPTH = 8
SUMMARY_KINDS = frozenset({"module_summary", "project_summary"})
_ERROR = "分层汇总检查点或来源契约无效"


@dataclass(frozen=True)
class SummaryTask:
    """A canonical input and stable database key, independent of finish order."""

    key: str
    kind: str
    payload: dict
    input_hash: str


@dataclass(frozen=True)
class _Child:
    key: str
    compilation: Compilation
    result: dict
    module_ids: tuple[str, ...]
    leaf_ids: tuple[str, ...]
    all_unknowns: tuple[str, ...]
    module_name: str


class _EvaluationIndex:
    """Indexes and memoized canonical values owned by one tree evaluation.

    Relation positions deliberately retain duplicate IDs. An ID dictionary
    alone would hide conflicting records from the downstream contract gate.
    """

    def __init__(self, bundle: FactBundle):
        self.refs = {ref.id: ref for ref in bundle.source_refs}
        self.ref_ids = frozenset(self.refs)
        self.facts = {fact.id: fact for fact in bundle.facts}
        self.relations = {relation.id: relation for relation in bundle.relations}
        self.relation_ids = frozenset(self.relations)
        self.module_paths: dict[str, set[str]] = defaultdict(set)
        for fact in bundle.facts:
            self.module_paths[fact.module_id].update(self.refs[rid].path for rid in fact.source_ref_ids
                                                     if rid in self.refs)
        self.relation_refs = tuple(frozenset(relation.source_ref_ids) for relation in bundle.relations)
        self.by_relation_id: dict[str, list[int]] = defaultdict(list)
        self.by_anchor: dict[str, list[int]] = defaultdict(list)
        self.empty_relations: list[int] = []
        for position, (relation, refs) in enumerate(zip(bundle.relations, self.relation_refs, strict=True)):
            self.by_relation_id[relation.id].append(position)
            if refs:
                self.by_anchor[min(refs)].append(position)
            else:
                self.empty_relations.append(position)
        self.child_hashes: dict[int, str] = {}
        self.items: dict[tuple[str, str, str, str], dict] = {}
        self.fits: dict[tuple, tuple[bool, str | None]] = {}

    def child_hash(self, child: _Child) -> str:
        # Every _Child is constructed from a freshly validated complete result
        # and remains immutable throughout this evaluation. No cache escapes.
        identity = id(child)
        if identity not in self.child_hashes:
            self.child_hashes[identity] = checkpoint_digest(child.result)
        return self.child_hashes[identity]

    def relation_positions(self, evidence: set[str], scoped: set[str]) -> list[int]:
        candidates = set(self.empty_relations)
        for rid in scoped:
            candidates.update(self.by_relation_id.get(rid, ()))
        for rid in evidence:
            candidates.update(self.by_anchor.get(rid, ()))
        # The anchor only narrows candidates. The original OR predicate still
        # decides inclusion, including empty-source relations and duplicate IDs.
        return sorted(candidates)


def _fail() -> None:
    # Checkpoint and input values may contain private/untrusted model text.
    raise ValueError(_ERROR)


def _order(key: str) -> tuple:
    return tuple(int(value) if value.isdigit() else value
                 for value in re.split(r"(\d+)", key))


def _strings(values: list[str]) -> list[str]:
    return sorted(set(values))


def _required_modules(bundle: FactBundle) -> list[str]:
    modules = sorted({fact.module_id for fact in bundle.facts})
    if any(module not in bundle.modules for module in modules):
        _fail()
    return modules


def _unit_map(units: list[Any]) -> dict[str, Any]:
    selected = [unit for unit in units if unit.kind in SUMMARY_KINDS | {"compile"}]
    keys = [unit.key for unit in selected]
    if any(type(key) is not str or not key for key in keys) or len(keys) != len(set(keys)):
        _fail()
    return {unit.key: unit for unit in selected}


def _compile_children(bundle: FactBundle, units: dict[str, Any],
                      index: _EvaluationIndex | None = None) -> dict[str, list[_Child]] | None:
    leaves = [unit for unit in units.values() if unit.kind == "compile"]
    if not leaves or any(unit.status != "completed" for unit in leaves):
        return None
    required = _required_modules(bundle)
    grouped: dict[str, list[_Child]] = defaultdict(list)
    index = index or _EvaluationIndex(bundle)
    refs, facts, relations = index.refs, index.facts, index.relations
    module_paths = index.module_paths
    for unit in sorted(leaves, key=lambda unit: _order(unit.key)):
        try:
            module = unit.payload["module_id"]
            compilation = Compilation.model_validate(unit.result)
        except (KeyError, TypeError, ValueError):
            _fail()
        if module not in required or not re.fullmatch(r"compile:\d+", unit.key):
            _fail()
        for claim in compilation.claims:
            if (not set(claim.evidence_ids) <= index.ref_ids
                    or not any(refs[rid].path in module_paths[module] for rid in claim.evidence_ids)):
                _fail()
            if claim.source_scope:
                scope = claim.source_scope
                if (scope.kind != "declaration_fragment" or scope.fact_id not in facts
                        or facts[scope.fact_id].module_id != module
                        or not set(scope.source_ref_ids) <= index.ref_ids
                        or not set(scope.relation_ids) <= index.relation_ids
                        or any(relations[rid].from_id != scope.fact_id for rid in scope.relation_ids)):
                    _fail()
        grouped[module].append(_Child(unit.key, compilation, unit.result, (module,), (unit.key,),
                                      tuple(_strings(compilation.unknowns)), bundle.modules[module]))
    if sorted(grouped) != required:
        _fail()
    return grouped


def _selection(child: _Child, cs: int, ce: int, us: int, ue: int) -> dict:
    return {"source_key": child.key, "claim_start": cs, "claim_end": ce,
            "unknown_start": us, "unknown_end": ue}


def _item(bundle: FactBundle, child: _Child, selection: dict,
          index: _EvaluationIndex | None = None) -> dict:
    cache_key = None
    if index is not None:
        cache_key = (index.child_hash(child), child.key, child.module_name, encoded(selection))
        if cache_key in index.items:
            return index.items[cache_key]
    claims = child.compilation.claims[selection["claim_start"]:selection["claim_end"]]
    unknowns = child.compilation.unknowns[selection["unknown_start"]:selection["unknown_end"]]
    if not claims and not unknowns:
        _fail()
    refs = index.refs if index is not None else {ref.id: ref for ref in bundle.source_refs}
    ref_ids = index.ref_ids if index is not None else frozenset(refs)
    evidence = {rid for claim in claims for rid in claim.evidence_ids}
    evidence.update(rid for claim in claims if claim.source_scope for rid in claim.source_scope.source_ref_ids)
    if not claims:
        # A standalone unknown fragment has no independent claim evidence;
        # retain its entire authenticated parent input range.
        evidence.update(rid for claim in child.compilation.claims for rid in claim.evidence_ids)
        evidence.update(rid for claim in child.compilation.claims if claim.source_scope
                        for rid in claim.source_scope.source_ref_ids)
    if not evidence or not evidence <= ref_ids:
        _fail()
    scoped = {rid for claim in claims if claim.source_scope for rid in claim.source_scope.relation_ids}
    # Retain current canonical relationships backed by this complete input
    # pool. Transport explicitly selects a symbol graph or count view; source
    # membership alone does not prove a module boundary or resolved edge.
    if index is None:
        relationships = [relation for relation in bundle.relations
                         if relation.id in scoped or set(relation.source_ref_ids) <= evidence]
        if any(not set(relation.source_ref_ids) <= ref_ids for relation in relationships):
            _fail()
        evidence.update(rid for relation in relationships for rid in relation.source_ref_ids)
    else:
        selected = [position for position in index.relation_positions(evidence, scoped)
                    if bundle.relations[position].id in scoped or index.relation_refs[position] <= evidence]
        if any(not index.relation_refs[position] <= ref_ids for position in selected):
            _fail()
        relationships = [bundle.relations[position] for position in selected]
        evidence.update(rid for position in selected for rid in index.relation_refs[position])
    child_id = stable_id("summary-input", child.key,
                         *(str(selection[key]) for key in ("claim_start", "claim_end", "unknown_start", "unknown_end")))
    result = {"fact": {"module": child.module_name, "title": child.compilation.title,
                     "claims": [claim.model_dump() for claim in claims], "unknowns": list(unknowns),
                     "child_id": child_id},
            "relations": [relation.model_dump() for relation in sorted(relationships, key=lambda relation: relation.id)],
            "evidence": [refs[rid].model_dump() for rid in sorted(evidence)]}
    if cache_key is not None:
        index.items[cache_key] = result
    return result


def _packet(bundle: FactBundle, purpose: str, module: str, level: int, index: int,
            fragments: list[tuple[_Child, dict]], evaluation: _EvaluationIndex | None = None) -> dict:
    items = [_item(bundle, child, selection, evaluation) for child, selection in fragments]
    child_ids = [item["fact"]["child_id"] for item in items]
    return {"module_id": module, "module": bundle.modules[module] if purpose == "module" else "项目概览",
            "items": items, "coverage": list(bundle.issues),
            "synthesis": {"purpose": purpose, "level": level, "index": index, "child_ids": child_ids,
                          "module_ids": _strings([value for child, _ in fragments for value in child.module_ids]),
                          "leaf_ids": sorted({value for child, _ in fragments for value in child.leaf_ids}, key=_order),
                          "child_hashes": {child_id: evaluation.child_hash(child) if evaluation is not None
                                           else checkpoint_digest(child.result)
                                           for child_id, (child, _) in zip(child_ids, fragments, strict=True)},
                          "all_unknowns": _strings([value for child, _ in fragments for value in child.all_unknowns]),
                          "selections": {child_id: selection for child_id, (_, selection)
                                         in zip(child_ids, fragments, strict=True)}}}


def _fits(packet: dict, evaluation: _EvaluationIndex | None = None) -> bool:
    if evaluation is None:
        return (len(packet["items"]) <= MAX_SUMMARY_CHILDREN
                and token_bound(encoded(packet_wire(packet))) <= settings.PROJECT_BRAIN_PACKET_MAX_TOKENS)
    if len(packet["items"]) > MAX_SUMMARY_CHILDREN:
        # Preserve the original short circuit: this case never serializes or
        # interprets a wire and therefore has nothing useful to memoize.
        return False
    # Hash the whole normative packet, not additive item sizes or fact IDs.
    # The first decision always measures the actual configured complete wire.
    key = (checkpoint_digest(packet), MAX_SUMMARY_CHILDREN, settings.PROJECT_BRAIN_PACKET_MAX_TOKENS,
           settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE, settings.PROJECT_BRAIN_MAX_EXPLANATION_SLOTS)
    if key not in evaluation.fits:
        wire = encoded(packet_wire(packet))
        evaluation.fits[key] = (token_bound(wire) <= settings.PROJECT_BRAIN_PACKET_MAX_TOKENS,
                                checkpoint_digest(wire))
    return evaluation.fits[key][0]


def _fragments(bundle: FactBundle, child: _Child, purpose: str, module: str, level: int,
               evaluation: _EvaluationIndex | None = None) -> list[tuple[_Child, dict]]:
    count_claims, count_unknowns = len(child.compilation.claims), len(child.compilation.unknowns)
    full = (child, _selection(child, 0, count_claims, 0, count_unknowns))
    if _fits(_packet(bundle, purpose, module, level, 0, [full], evaluation), evaluation):
        return [full]
    fragments = []
    start_claim = end_claim = start_unknown = end_unknown = 0
    for kind in ["claim"] * count_claims + ["unknown"] * count_unknowns:
        next_claim = end_claim + (kind == "claim")
        next_unknown = end_unknown + (kind == "unknown")
        candidate = (child, _selection(child, start_claim, next_claim, start_unknown, next_unknown))
        if not _fits(_packet(bundle, purpose, module, level, 0, [candidate], evaluation), evaluation):
            if start_claim == end_claim and start_unknown == end_unknown:
                raise CompilationLimitError("分层汇总单条结论或未知项超过知识包硬上限")
            fragments.append((child, _selection(child, start_claim, end_claim, start_unknown, end_unknown)))
            start_claim, start_unknown = end_claim, end_unknown
            candidate = (child, _selection(child, start_claim, next_claim, start_unknown, next_unknown))
            if not _fits(_packet(bundle, purpose, module, level, 0, [candidate], evaluation), evaluation):
                raise CompilationLimitError("分层汇总单条结论或未知项超过知识包硬上限")
        end_claim, end_unknown = next_claim, next_unknown
    fragments.append((child, _selection(child, start_claim, end_claim, start_unknown, end_unknown)))
    return fragments


def _level_tasks(bundle: FactBundle, children: list[_Child], purpose: str, module: str, level: int,
                 evaluation: _EvaluationIndex | None = None) -> list[SummaryTask]:
    if level >= MAX_SUMMARY_DEPTH:
        raise CompilationLimitError("分层汇总达到深度硬上限且未收敛")
    fragments = [fragment for child in children
                 for fragment in _fragments(bundle, child, purpose, module, level, evaluation)]
    groups: list[list[tuple[_Child, dict]]] = []
    current = []
    for fragment in fragments:
        if current and not _fits(_packet(bundle, purpose, module, level, len(groups), current + [fragment], evaluation),
                                 evaluation):
            groups.append(current)
            current = []
        current.append(fragment)
    if current:
        groups.append(current)
    if level > 0 and len(children) > 1 and len(groups) >= len(children) and all(
            len({child.key for child, _ in group}) == 1 for group in groups):
        raise CompilationLimitError("分层汇总完整概述后仍无法归并")
    tasks = []
    prefix = (f"module_summary:{stable_id('module', module).split(':')[1]}" if purpose == "module"
              else "project_summary")
    for index, group in enumerate(groups):
        packet = _packet(bundle, purpose, module, level, index, group, evaluation)
        if not _fits(packet, evaluation):
            raise CompilationLimitError("分层汇总超过知识包硬上限")
        tasks.append(SummaryTask(f"{prefix}:{level}:{index}", f"{purpose}_summary", packet, checkpoint_digest(packet)))
    return tasks


def wrap_summary_result(packet: dict, compilation: Compilation) -> dict:
    """Bind output to the complete service-owned input and inherited limits."""
    try:
        meta = packet["synthesis"]
        digest = checkpoint_digest(packet)
        refs = sorted({ref["id"] for item in packet["items"] for ref in item["evidence"]})
        for claim in compilation.claims:
            scope = claim.source_scope
            if (scope is None or scope.kind != "summary_synthesis" or scope.packet_digest != digest
                    or scope.id != stable_id("scope", digest, "S1")
                    or scope.fragment_index != 0 or scope.fragment_total != 1
                    or scope.child_ids != meta["child_ids"] or scope.module_ids != meta["module_ids"]
                    or scope.relationship_view != synthesis_relationship_view(meta["purpose"], meta["level"])
                    or scope.source_ref_ids != refs or sorted(claim.evidence_ids) != refs):
                _fail()
        from src.core.project_brain.summary_budget import SummaryProseTooLarge, require_summary_prose
        try:
            require_summary_prose(compilation,settings.PROJECT_BRAIN_PACKET_MAX_TOKENS)
        except SummaryProseTooLarge:
            raise CompilationLimitError("综合概览检查点超过输出容量", failure_code="summary_output_capacity") from None
        return {"compilation": compilation.model_dump(), "child_ids": list(meta["child_ids"]),
                "child_hashes": dict(meta["child_hashes"]), "module_ids": list(meta["module_ids"]),
                "leaf_ids": list(meta["leaf_ids"]),
                "packet_digest": digest}
    except CompilationLimitError:
        raise
    except (KeyError, TypeError, ValueError):
        _fail()


def _checked_unit(task: SummaryTask, unit: Any) -> _Child | None:
    # Budget accounting maintains these counters on the same persisted row.
    # They are not model input and must not invalidate its immutable digest.
    payload = dict(unit.payload)
    for field in ("_reserved_tokens", "_unknown_tokens"):
        value = payload.pop(field, 0)
        if type(value) is not int or value < 0:
            _fail()
    if (unit.kind != task.kind or checkpoint_digest(payload) != task.input_hash
            or unit.input_hash != task.input_hash):
        _fail()
    if unit.status != "completed":
        return None
    try:
        compilation = Compilation.model_validate(unit.result["compilation"])
        if checkpoint_digest(unit.result) != checkpoint_digest(wrap_summary_result(task.payload, compilation)):
            _fail()
    except CompilationLimitError:
        raise
    except (KeyError, TypeError, ValueError):
        _fail()
    return _Child(unit.key, compilation, unit.result, tuple(unit.result["module_ids"]),
                  tuple(unit.result["leaf_ids"]),
                  tuple(_strings([*task.payload["synthesis"]["all_unknowns"], *compilation.unknowns])), task.payload["module"])


def _evaluate(bundle: FactBundle, all_units: list[Any]) -> tuple[list[SummaryTask], dict, dict, str | None]:
    units = _unit_map(all_units)
    evaluation = _EvaluationIndex(bundle)
    grouped = _compile_children(bundle, units, evaluation)
    if grouped is None:
        return [], {}, {}, None
    expected: list[SummaryTask] = []
    intermediates: dict[str, list[_Child]] = defaultdict(list)
    roots: dict[str, _Child] = {}
    inspected = set()

    def tree(children: list[_Child], purpose: str, module: str) -> _Child | None:
        for level in range(MAX_SUMMARY_DEPTH):
            tasks = _level_tasks(bundle, children, purpose, module, level, evaluation)
            expected.extend(tasks)
            completed = []
            for task in tasks:
                if task.key in units:
                    inspected.add(task.key)
                    result = _checked_unit(task, units[task.key])
                    if result:
                        completed.append(result)
            intermediates[module].extend(completed)
            if len(completed) != len(tasks):
                return None
            if len(completed) == 1:
                return completed[0]
            children = completed
        raise CompilationLimitError("分层汇总达到深度硬上限且未收敛")

    for module in sorted(grouped):
        root = tree(grouped[module], "module", module)
        if root:
            roots[module] = root
    project_root = None
    if len(roots) == len(grouped):
        project = tree([roots[module] for module in sorted(roots)], "project", "project")
        if project:
            project_root = project.key
            roots["project"] = project
    if any(unit.kind in SUMMARY_KINDS and unit.key not in inspected for unit in units.values()):
        _fail()
    return expected, roots, intermediates, project_root


def next_summary_tasks(bundle: FactBundle, units: list[Any]) -> list[SummaryTask]:
    """Return only missing tasks whose complete dependency level is ready."""
    expected, _, _, _ = _evaluate(bundle, units)
    existing = _unit_map(units)
    return [task for task in expected if task.key not in existing]


def check_summary_task(bundle: FactBundle, unit: Any, all_units: list[Any]) -> dict:
    """Recompute the entire dependency tree; stored selections are untrusted."""
    try:
        expected, _, _, _ = _evaluate(bundle, all_units)
        matches = [task for task in expected if task.key == unit.key]
        if len(matches) != 1:
            _fail()
        task = matches[0]
        _checked_unit(task, unit)
        return task.payload
    except CompilationLimitError:
        raise
    except (KeyError, TypeError, ValueError):
        _fail()


def hierarchy_for_assembly(bundle: FactBundle, units: list[Any]) -> dict:
    """Expose complete roots plus retained intermediate detail, never fallback."""
    _, roots, intermediates, project_key = _evaluate(bundle, units)
    required = _required_modules(bundle)
    if project_key is None or set(roots) != set(required) | {"project"}:
        _fail()
    leaves = sorted([unit.key for unit in units if unit.kind == "compile"], key=_order)
    project = roots["project"]
    if list(project.module_ids) != required or list(project.leaf_ids) != leaves:
        _fail()
    return {"modules": {module: roots[module].compilation for module in required},
            "project": project.compilation,
            "intermediates": {module: [child.compilation for child in intermediates[module]
                                        if child.key != roots[module].key] for module in [*required, "project"]},
            "all_unknowns": {module: list(roots[module].all_unknowns) for module in [*required, "project"]},
            "coverage": {"module_ids": required, "leaf_ids": leaves, "root": project_key}}
