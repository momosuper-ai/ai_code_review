"""Checkpoint-bound privacy reuse cannot exempt changed or new source data."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain.compiler import compile_packet, encoded
from src.core.project_brain.schema import stable_id
from src.core.project_brain.trusted_packet import VerifiedPacket, checkpoint_digest


@pytest.fixture
def checkpoint() -> tuple[dict, dict, dict]:
    """Represent a complete privacy checkpoint without executing project code."""
    sha, path = "a" * 40, "app/service.py"
    fid, mid, eid = stable_id("symbol", "lookup"), stable_id("module", "app"), stable_id("evidence", "lookup")
    fact = {"id": fid, "kind": "symbol", "name": "lookup", "module_id": mid,
            "source_ref_ids": [eid], "extractor": "brain.python/1",
            "attributes": {"path": path, "signature": "lookup()", "language": "python"}}
    ref = {"id": eid, "path": path, "line_start": 1, "line_end": 2,
           "commit_sha": sha, "blob_sha": "b" * 40, "symbol_id": fid}
    bundle = {"source_commit": sha, "facts": [copy.deepcopy(fact)], "relations": [],
              "source_refs": [copy.deepcopy(ref)], "modules": {mid: "app"}, "issues": []}
    packet = {"module_id": mid, "module": "app", "coverage": [], "items": [{
        "fact": fact, "relations": [], "evidence": [{**ref, "excerpt_end": 2,
            "excerpt": "def lookup():\n    return 'synthetic full source literal'"}],
    }]}
    finalize = {"source_commit": sha, "recipe_hash": "c" * 64,
                "bundle_hash": checkpoint_digest(bundle), "packets": [{
                    "hash": "d" * 64, "packet_digest": checkpoint_digest(packet),
                }]}
    return packet, bundle, finalize


def proof(checkpoint: tuple[dict, dict, dict], **kwargs) -> VerifiedPacket:
    """Use the same factory contract as the durable worker."""
    packet, bundle, finalize = checkpoint
    values = {"source_commit": "a" * 40, "recipe_hash": "c" * 64,
              "finalize": finalize, "privacy_states": ["completed"],
              "finalize_state": "completed", "planned_hash": "d" * 64, **kwargs}
    return VerifiedPacket.from_checkpoint(packet, bundle, **values)


@pytest.mark.parametrize("states", [[], ["pending"], ["running"], ["failed"], ["completed", "pending"]])
def test_incomplete_privacy_group_cannot_issue_proof(checkpoint, states) -> None:
    with pytest.raises(ValueError, match="尚未完成"):
        proof(checkpoint, privacy_states=states)


@pytest.mark.parametrize("state", ["pending", "running", "failed"])
def test_incomplete_finalize_cannot_issue_proof(checkpoint, state) -> None:
    with pytest.raises(ValueError, match="尚未完成"):
        proof(checkpoint, finalize_state=state)


@pytest.mark.parametrize("change", ["source_commit", "recipe_hash", "planned_hash"])
def test_foreign_source_recipe_or_unit_plan_cannot_issue_proof(checkpoint, change) -> None:
    with pytest.raises(ValueError):
        proof(checkpoint, **{change: "e" * (40 if change == "source_commit" else 64)})


def test_changed_canonical_free_field_fails_complete_checkpoint_digest(checkpoint) -> None:
    checkpoint[1]["facts"][0]["attributes"]["credential"] = "synthetic unscanned credential"
    with pytest.raises(ValueError, match="内容不匹配"):
        proof(checkpoint)


@pytest.mark.parametrize("location", ["envelope", "fact", "evidence"])
def test_new_free_fields_after_proof_are_not_privacy_exempt(checkpoint, location) -> None:
    approved = proof(checkpoint)
    packet = checkpoint[0]
    target = {"envelope": packet, "fact": packet["items"][0]["fact"]["attributes"],
              "evidence": packet["items"][0]["evidence"][0]}[location]
    target["credential"] = "synthetic new credential"
    with pytest.raises(ValueError, match="被修改"):
        approved.validate(packet)


def test_excerpt_mutation_invalidates_plan_even_when_reference_stays_valid(checkpoint) -> None:
    approved = proof(checkpoint)
    checkpoint[0]["items"][0]["evidence"][0]["excerpt"] = "synthetic changed source"
    with pytest.raises(ValueError, match="被修改"):
        approved.validate(checkpoint[0])


@pytest.mark.parametrize("change", ["module", "coverage", "envelope_extra", "item_extra", "fact", "evidence", "excerpt_range", "fragment"])
def test_recomputed_digest_cannot_certify_noncanonical_metadata(checkpoint, change) -> None:
    """A matching plan digest does not prove foreign metadata was classified."""
    packet, bundle, finalize = checkpoint
    item = packet["items"][0]
    if change == "module":
        packet["module"] = "synthetic foreign module"
    elif change == "coverage":
        packet["coverage"] = [{"detail": "synthetic foreign coverage"}]
    elif change == "envelope_extra":
        packet["unclassified"] = "synthetic extra value"
    elif change == "item_extra":
        item["unclassified"] = "synthetic extra value"
    elif change == "fact":
        item["fact"]["name"] = "foreign_name"
    elif change == "evidence":
        item["evidence"][0]["blob_sha"] = "f" * 40
    elif change == "excerpt_range":
        item["evidence"][0]["excerpt_end"] = 3
    elif change == "fragment":
        item["fragment"] = {"index": 2, "total": 2}
    finalize["packets"][0]["packet_digest"] = checkpoint_digest(packet)
    finalize["bundle_hash"] = checkpoint_digest(bundle)
    with pytest.raises(ValueError, match="不匹配|未分类|无效"):
        proof(checkpoint)


def test_foreign_relationship_cannot_issue_proof_even_with_matching_digest(checkpoint) -> None:
    packet, _, finalize = checkpoint
    item = packet["items"][0]
    item["relations"] = [{"id": "synthetic-foreign-relation", "from_id": item["fact"]["id"]}]
    finalize["packets"][0]["packet_digest"] = checkpoint_digest(packet)
    with pytest.raises(ValueError, match="关系与规范事实不匹配"):
        proof(checkpoint)


@pytest.fixture
def guard() -> SimpleNamespace:
    async def clean(text: str) -> str:
        return text

    return SimpleNamespace(clean=AsyncMock(side_effect=clean), filter=PrivacyFilter(local_llm=None),
                           ledger=None, calls=0, reserve=MagicMock(), _account=MagicMock(),
                           reserve_model_call=AsyncMock(return_value=None), check_discoveries=MagicMock(),
                           remaining_seconds=lambda: 480)


@pytest.fixture
def client(monkeypatch) -> SimpleNamespace:
    content = encoded({"title": "职责", "claims": [{"text": "依据源码解释职责", "evidence_ids": ["E1"]}],
                       "unknowns": [], "document_path": None})
    client = SimpleNamespace(chat=AsyncMock(return_value=ChatResult(
        ChatCompletionMessage(role="assistant", content=content), TokenUsage(20, 10), "stop")),
        aclose=AsyncMock(), set_privacy_filter=MagicMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", MagicMock(return_value=client))
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    return client


@pytest.mark.asyncio
async def test_proof_reuses_only_metadata_and_scans_complete_new_excerpt(checkpoint, guard, client) -> None:
    packet = checkpoint[0]
    result = await compile_packet(packet, guard, proof=proof(checkpoint))
    scanned = [json.loads(call.args[0]) for call in guard.clean.await_args_list]
    assert scanned[0] == {"source_excerpt": [packet["items"][0]["evidence"][0]["excerpt"]]}
    assert result.claims[0].evidence_ids == [packet["items"][0]["evidence"][0]["id"]]
    assert any("claims.text" in value for value in scanned[1:])
    assert not any("items" in value for value in scanned)


@pytest.mark.asyncio
async def test_no_proof_still_scans_complete_packet(checkpoint, guard, client) -> None:
    await compile_packet(checkpoint[0], guard)
    assert json.loads(guard.clean.await_args_list[0].args[0]) == checkpoint[0]


@pytest.mark.asyncio
async def test_excerpt_privacy_failure_prevents_every_cloud_call(checkpoint, guard, client) -> None:
    guard.clean.side_effect = ValueError("隐私扫描失败")
    with pytest.raises(ValueError, match="隐私扫描失败"):
        await compile_packet(checkpoint[0], guard, proof=proof(checkpoint))
    assert client.chat.await_count == 0


@pytest.mark.asyncio
async def test_invalid_output_reasoning_is_classified_before_retry(checkpoint, guard, client) -> None:
    invalid = ChatCompletionMessage(role="assistant", content="invalid JSON",
                                    reasoning_content="synthetic full reasoning credential")
    valid = client.chat.return_value
    client.chat.side_effect = [ChatResult(invalid, TokenUsage(20, 10), "stop"), valid]
    await compile_packet(checkpoint[0], guard, proof=proof(checkpoint))
    scanned = [json.loads(call.args[0]) for call in guard.clean.await_args_list]
    assert {"model_retry_reasoning": "synthetic full reasoning credential"} in scanned
    retry = client.chat.await_args_list[1].args[0]
    assert retry[2]["reasoning_content"] == "synthetic full reasoning credential"
