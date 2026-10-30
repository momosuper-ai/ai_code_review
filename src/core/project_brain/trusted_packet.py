"""Bind privacy reuse to completed, immutable, fixed-recipe checkpoints."""

import hashlib
import json
from dataclasses import dataclass


def checkpoint_digest(value: object) -> str:
    """Hash the complete canonical structure, including unknown adapter fields."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _canonical_metadata(packet: dict, bundle: dict) -> None:
    # A plan hash alone does not prove a future generator's extra field was
    # classified. Accept only complete canonical identities and excerpt fields.
    if set(packet) != {"module_id", "module", "items", "coverage"}:
        raise ValueError("隐私检查点知识包包含未分类字段")
    if (bundle["modules"].get(packet["module_id"]) != packet["module"]
            or packet["coverage"] != bundle.get("issues", [])):
        raise ValueError("隐私检查点知识包归属或覆盖不匹配")
    facts = {fact["id"]: fact for fact in bundle["facts"]}
    relations = {relation["id"]: relation for relation in bundle["relations"]}
    refs = {ref["id"]: ref for ref in bundle["source_refs"]}
    for item in packet["items"]:
        if set(item) - {"fact", "relations", "evidence", "fragment"}:
            raise ValueError("隐私检查点知识声明包含未分类字段")
        fact = item["fact"]
        if facts.get(fact.get("id")) != fact or fact["module_id"] != packet["module_id"]:
            raise ValueError("隐私检查点知识声明与规范事实不匹配")
        for relation in item["relations"]:
            if relations.get(relation.get("id")) != relation or relation["from_id"] != fact["id"]:
                raise ValueError("隐私检查点知识关系与规范事实不匹配")
        if "fragment" in item:
            fragment = item["fragment"]
            if (set(fragment) != {"index", "total"} or not isinstance(fragment["index"], int)
                    or not isinstance(fragment["total"], int) or not 0 <= fragment["index"] < fragment["total"]):
                raise ValueError("隐私检查点知识分片无效")
        for evidence in item["evidence"]:
            canonical = refs.get(evidence.get("id"))
            if canonical is not None and bundle.get("source_identity") is not None:
                path = canonical.get("path")
                if path not in bundle.get("display_paths", {}):
                    raise ValueError("隐私检查点来源展示绑定不存在")
                canonical = {**canonical, "display_path": bundle["display_paths"][path]}
            if canonical is not None and canonical.get("display_path") is None and "display_path" not in evidence:
                # Legacy packets predate the nullable display-only schema
                # coordinate. Absence and explicit None carry no extra trust.
                canonical = {key: value for key, value in canonical.items() if key != "display_path"}
            if (canonical is None or set(evidence) != set(canonical) | {"excerpt", "excerpt_end"}
                    or any(evidence.get(key) != value for key, value in canonical.items())):
                raise ValueError("隐私检查点源码证据与规范事实不匹配")
            end = evidence["excerpt_end"]
            if (not isinstance(evidence["excerpt"], str) or not isinstance(end, int)
                    or not canonical["line_start"] <= end <= canonical["line_end"]):
                raise ValueError("隐私检查点源码片段范围无效")


@dataclass(frozen=True)
class VerifiedPacket:
    """An internal checkpoint proof; never accepted from REST request bodies.

    Only the worker can supply the persisted finalize result and privacy-unit
    states. The packet hash binds all metadata and new excerpts to the planned
    fixed-SHA input. Excerpts still require classification before cloud use.
    """

    digest: str

    @classmethod
    def from_checkpoint(cls, packet: dict, bundle: dict, *, source_commit: str,
                        recipe_hash: str, finalize: dict, privacy_states: list[str],
                        finalize_state: str, planned_hash: str) -> "VerifiedPacket":
        """Require the completed scan group, source, recipe and complete hashes."""
        if not privacy_states or any(state != "completed" for state in privacy_states) or finalize_state != "completed":
            raise ValueError("知识隐私检查点尚未完成")
        if (finalize.get("source_commit") != source_commit or bundle.get("source_commit") != source_commit
                or finalize.get("recipe_hash") != recipe_hash
                or finalize.get("bundle_hash") != checkpoint_digest(bundle)):
            raise ValueError("知识隐私检查点的源码、配方或内容不匹配")
        _canonical_metadata(packet, bundle)
        planned = finalize.get("packets", [])
        if not any(item.get("hash") == planned_hash and item.get("packet_digest") == checkpoint_digest(packet)
                   for item in planned):
            raise ValueError("知识包不属于已完成的隐私检查点")
        return cls(checkpoint_digest(packet))

    def validate(self, packet: dict) -> None:
        """Reject any mutation or extra field after checkpoint verification."""
        if checkpoint_digest(packet) != self.digest:
            raise ValueError("知识包在隐私检查点验证后被修改")


@dataclass(frozen=True)
class VerifiedSummaryPacket:
    """Worker-only proof for an exactly reconstructed summary dependency tree.

    This is distinct from a declaration proof: a summary contains newly
    generated prose, which always needs classification. Only its authenticated
    machine coordinates may be excluded from repeated model classification.
    The worker must first authenticate the fixed-SHA bundle's private MAC.
    """

    digest: str

    @classmethod
    def from_checkpoints(cls, bundle, unit, all_units: list, *, source_commit: str,
                         recipe_hash: str) -> tuple[dict, "VerifiedSummaryPacket"]:
        # Local import avoids summary -> compiler -> trusted_packet cycles.
        from src.core.project_brain.summary import check_summary_task

        finalized = [u for u in all_units if u.kind == "finalize"]
        privacy = [u for u in all_units if u.kind == "privacy"]
        source_privacy = [u for u in all_units if u.kind == "source_privacy"]
        if (len(finalized) != 1 or finalized[0].status != "completed" or not privacy
                or any(u.status != "completed" for u in privacy)
                or bundle.source_commit != source_commit
                or (finalized[0].result or {}).get("bundle_hash") != checkpoint_digest(bundle.model_dump())
                or (finalized[0].result or {}).get("source_commit") != source_commit
                or (finalized[0].result or {}).get("recipe_hash") != recipe_hash
                or (bundle.source_identity is not None
                    and (not source_privacy or any(u.status != "completed" for u in source_privacy)))):
            raise ValueError("综合汇总缺少完整且匹配的事实隐私检查点")
        packet = check_summary_task(bundle, unit, all_units)
        return packet, cls(checkpoint_digest(packet))

    def validate(self, packet: dict) -> None:
        """Never reuse classification on a changed source, schema or child."""
        if checkpoint_digest(packet) != self.digest:
            raise ValueError("综合知识包在隐私检查点验证后被修改")
