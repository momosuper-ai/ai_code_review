"""Independent safety checks for compact compilation and incremental reuse."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationLimitError, CompilationGuard, compile_packet, encoded, packet_hash, packets, token_bound
from src.core.project_brain.context import compact_packet, read_rows
from src.core.project_brain.planning import compilation_plan
from src.core.project_brain.schema import Fact, FactBundle, Relation, SourceRef, stable_id
from src.core.project_brain.snapshot import RepoSnapshot, source_fingerprint
from src.core.project_brain.trusted_packet import VerifiedPacket, checkpoint_digest


@pytest.fixture
def transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Use two static files; no repository execution or real model requests."""
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", False)
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 0)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    sha = "a" * 40
    module = stable_id("module", "pkg")
    files, facts, refs = {}, [], []
    for name, body in (("caller", "def caller():\n    return callee()\n"),
                       ("callee", "def callee():\n    return 1\n")):
        path = f"pkg/{name}.py"
        full = tmp_path / path
        full.parent.mkdir(exist_ok=True)
        full.write_text(body, encoding="utf-8")
        data = body.encode()
        blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
        files[path] = {"blob_sha": blob, "lines": 2, "size": len(data), "mode": "100644"}
        fid = stable_id("symbol", path, name)
        rid = stable_id("evidence", path, "1", "2", fid)
        refs.append(SourceRef(id=rid, commit_sha=sha, path=path, blob_sha=blob,
                              line_start=1, line_end=2, symbol_id=fid))
        facts.append(Fact(id=fid, kind="symbol", name=name, module_id=module,
                          source_ref_ids=[rid], extractor="brain.python/1",
                          attributes={"path": path, "signature": f"{name}()", "language": "python"}))
    relation = Relation(id=stable_id("relation", "call"), from_id=facts[0].id, type="CALLS",
                        to_id=facts[1].id, target_name="callee", resolution="resolved",
                        source_ref_ids=[refs[0].id])
    bundle = FactBundle(source_commit=sha, files=files, modules={module: "pkg"}, facts=facts,
                        relations=[relation], source_refs=refs, source_fingerprint=source_fingerprint(files))
    snapshot = RepoSnapshot(str(tmp_path), sha, files, [], bundle.source_fingerprint)
    packet = next(packet for packet in packets(bundle, snapshot)
                  if any(item["fact"]["id"] == facts[0].id for item in packet["items"]))
    guard = CompilationGuard(lambda *args: None, lambda *args: None)
    return SimpleNamespace(bundle=bundle, snapshot=snapshot, packet=packet, guard=guard)


def output(evidence: str, *, text: str = "该模块负责处理调用", claims: list | None = None) -> str:
    """Return a minimal formal model result, with deliberately controlled IDs."""
    return encoded({"title": "模块说明", "claims": [{"text": text, "evidence_ids": [evidence]}]
                    if claims is None else claims, "unknowns": [], "document_path": None})


def model(monkeypatch: pytest.MonkeyPatch, responses: list[ChatCompletionMessage | ChatResult]) -> SimpleNamespace:
    """Collect exactly what the model boundary would receive after sanitizing."""
    mock = SimpleNamespace(calls=[], kwargs=[], aclose=AsyncMock(), privacy=None)

    async def chat(messages: list[dict], **kwargs: object) -> ChatResult:
        safe = mock.privacy.sanitize_messages(messages) if mock.privacy else messages
        mock.calls.append(copy.deepcopy(safe))
        mock.kwargs.append(kwargs)
        response = responses[len(mock.calls) - 1]
        return response if isinstance(response, ChatResult) else ChatResult(response, TokenUsage(20, 10), finish_reason="stop")

    mock.chat = chat
    mock.set_privacy_filter = lambda privacy: setattr(mock, "privacy", privacy)
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: mock)
    return mock


def proof_for(fixture: SimpleNamespace) -> VerifiedPacket:
    """Produce the same complete checkpoint binding as the worker does."""
    recipe = "fixture-recipe"
    digest = packet_hash(fixture.bundle, recipe, fixture.packet)
    finalized = {"source_commit": fixture.bundle.source_commit, "recipe_hash": recipe,
                 "bundle_hash": checkpoint_digest(fixture.bundle.model_dump()),
                 "packets": [{"hash": digest, "packet_digest": checkpoint_digest(fixture.packet)}]}
    return VerifiedPacket.from_checkpoint(fixture.packet, fixture.bundle.model_dump(),
        source_commit=fixture.bundle.source_commit, recipe_hash=recipe, finalize=finalized,
        privacy_states=["completed"], finalize_state="completed", planned_hash=digest)


@pytest.mark.asyncio
async def test_evidence_aliases_restore_to_fixed_source_ids(transport, monkeypatch) -> None:
    context = compact_packet(transport.packet)
    alias, canonical = next(iter(context.evidence_ids.items()))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    result = await compile_packet(transport.packet, transport.guard)
    assert result.claims[0].evidence_ids == [canonical]
    assert json.loads(mock.calls[0][-1]["content"])["protocol"] == "project-brain-context/1"
    assert mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_response_schema_and_prompt_share_exact_packet_evidence_enum(transport, monkeypatch) -> None:
    from src.core.project_brain.schema import Compilation

    context = compact_packet(transport.packet)
    alias = next(iter(context.evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard)
    schema = mock.kwargs[0]["json_schema_builder"]()["json_schema"]["schema"]
    enum = schema["$defs"]["InferredClaim"]["properties"]["evidence_ids"]["items"]["enum"]
    assert enum == list(context.evidence_ids)
    assert encoded(enum) in mock.calls[0][0]["content"]
    assert not set(enum) & {"F1", "R1", "E999999"}
    original = Compilation.model_json_schema()["$defs"]["InferredClaim"]["properties"]["evidence_ids"]["items"]
    assert "enum" not in original


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["E999999", "F1", "R1", "evidence:" + "f" * 24])
async def test_non_evidence_or_foreign_alias_never_becomes_formal(transport, monkeypatch, reference) -> None:
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(reference))])
    with pytest.raises(ValueError, match="unknown_evidence"):
        await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("claims,text", [([], "说明"), (None, ""), (None, "   ")])
async def test_empty_or_whitespace_claims_never_become_formal(transport, monkeypatch, claims, text) -> None:
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias, claims=claims, text=text))])
    with pytest.raises(ValueError, match="schema_mismatch"):
        await compile_packet(transport.packet, transport.guard)


@pytest.mark.asyncio
async def test_whitespace_title_never_becomes_formal(transport, monkeypatch) -> None:
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    result = json.loads(output(alias))
    result["title"] = "\t\n  "
    model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded(result))])
    with pytest.raises(ValueError, match="schema_mismatch"):
        await compile_packet(transport.packet, transport.guard)


@pytest.mark.asyncio
async def test_invalid_source_range_stops_before_semantic_call(transport, monkeypatch) -> None:
    transport.packet["items"][0]["evidence"][0]["line_end"] = 0
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError, match="行范围"):
        await compile_packet(transport.packet, transport.guard)
    assert not mock.calls


@pytest.mark.asyncio
async def test_complete_new_excerpt_is_scanned_before_trusted_metadata_use(transport, monkeypatch) -> None:
    secret = "synthetic-source-credential-Z"
    transport.packet["items"][0]["evidence"][0]["excerpt"] = f"def caller():\n    password = '{secret}'"
    proof = proof_for(transport)
    calls = []
    original_clean = transport.guard.clean

    async def scan(text: str) -> str:
        calls.append(json.loads(text))
        if secret in text:
            transport.guard.filter._dynamic_values[secret] = "SECRET"
        return await original_clean(text)

    monkeypatch.setattr(transport.guard, "clean", scan)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard, proof=proof)
    assert calls[0]["source_excerpt"]
    assert any(secret in text for text in calls[0]["source_excerpt"])
    assert secret not in mock.calls[0][-1]["content"]
    assert "<<SECRET_" in mock.calls[0][-1]["content"]


@pytest.mark.asyncio
async def test_checkpoint_mutation_cannot_skip_source_classification(transport, monkeypatch) -> None:
    proof = proof_for(transport)
    transport.packet["items"][0]["fact"]["attributes"]["new_field"] = "unscanned"
    mock = model(monkeypatch, [])
    with pytest.raises(ValueError, match="被修改"):
        await compile_packet(transport.packet, transport.guard, proof=proof)
    assert not mock.calls


@pytest.mark.asyncio
async def test_format_retry_drops_unrequested_tool_calls(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    invalid = ChatCompletionMessage(role="assistant", content="{}", tool_calls=[
        {"id": "unexpected", "type": "function", "function": {"name": "unrequested",
         "arguments": '{"credential":"synthetic-unscanned-retry-value"}'}}])
    mock = model(monkeypatch, [invalid, ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 2
    assert "tool_calls" not in mock.calls[1][2]
    assert "synthetic-unscanned-retry-value" not in encoded(mock.calls[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["reasoning", "reasoning_content", "thinking"])
async def test_full_retry_reasoning_fields_are_scanned_and_sanitized(transport, monkeypatch, field) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    secret = "synthetic-retry-credential-Q"
    reasoning = "synthetic safe reasoning " * 200 + secret
    value = {"text": reasoning, "format": "synthetic"} if field == "thinking" else reasoning
    invalid = ChatCompletionMessage(role="assistant", content="{}", **{field: value})
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [invalid, ChatCompletionMessage(role="assistant", content=output(alias))])
    scanned = []
    original_clean = transport.guard.clean

    async def scan(text: str) -> str:
        parsed = json.loads(text)
        if "model_retry_reasoning" in parsed:
            scanned.append(parsed["model_retry_reasoning"])
            transport.guard.filter._dynamic_values[secret] = "SECRET"
        return await original_clean(text)

    monkeypatch.setattr(transport.guard, "clean", scan)
    await compile_packet(transport.packet, transport.guard)
    assert scanned == [value]
    retry = mock.calls[1][2]
    assert retry["role"] == "assistant" and retry["content"] == "{}"
    assert secret not in encoded(retry)
    assert "<<SECRET_" in encoded(retry[field])
    assert len(encoded(retry[field])) > 4000


@pytest.mark.asyncio
async def test_retry_reasoning_privacy_failure_stops_before_second_semantic_call(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 3)
    invalid = ChatCompletionMessage(role="assistant", content="{}", reasoning="synthetic reasoning")
    mock = model(monkeypatch, [invalid])
    original_clean = transport.guard.clean

    async def scan(text: str) -> str:
        if "model_retry_reasoning" in json.loads(text):
            raise ValueError("合成 reasoning 扫描失败")
        return await original_clean(text)

    monkeypatch.setattr(transport.guard, "clean", scan)
    with pytest.raises(ValueError, match="扫描失败"):
        await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 1 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_valid_output_privacy_failure_never_starts_format_retry(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 3)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    original_clean = transport.guard.clean

    async def scan(text: str) -> str:
        if "claims.text" in json.loads(text):
            raise ValueError("合成输出扫描失败")
        return await original_clean(text)

    monkeypatch.setattr(transport.guard, "clean", scan)
    with pytest.raises(ValueError, match="输出扫描失败"):
        await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 1 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_length_recovery_doubles_only_output_limit_and_charges_each_attempt(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 3)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 16384)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    incomplete = ChatCompletionMessage(role="assistant", content="{}", reasoning="完整合成推理")
    responses = [ChatResult(incomplete, TokenUsage(20, 4096), "length"),
                 ChatResult(incomplete, TokenUsage(30, 8192), "length"),
                 ChatCompletionMessage(role="assistant", content=output(alias))]
    mock = model(monkeypatch, responses)
    reservations, charged = [], []
    transport.guard.reserve = reservations.append
    transport.guard.account = lambda usage, estimated, bound: charged.append((usage.total_tokens, estimated, bound))
    result = await compile_packet(transport.packet, transport.guard)
    assert result.claims
    assert [kwargs["max_tokens"] for kwargs in mock.kwargs] == [4096, 8192, 16384]
    assert reservations == [token_bound(encoded(messages)) + limit for messages, limit in
                            zip(mock.calls, [4096, 8192, 16384], strict=True)]
    assert [charge[0] for charge in charged] == [4116, 8222, 30]
    assert [charge[2] for charge in charged] == reservations
    assert all(kwargs["retry_max"] == 0 for kwargs in mock.kwargs)


@pytest.mark.asyncio
async def test_length_retry_scans_complete_large_reasoning_and_masks_its_tail(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    secret = "合成推理尾端口令松鼠蓝帆甲乙丙丁零零壹"
    reasoning = "完整合成推理。" * 600 + secret
    scans = []

    async def local_scan(messages, **kwargs):
        scans.append(json.loads(messages[-1]["content"]))
        found = secret in messages[-1]["content"]
        items = [{"value": secret, "type": "SECRET"}] if found else []
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": items})),
                          TokenUsage(100, 10), "stop")

    transport.guard.local = SimpleNamespace(chat=local_scan, aclose=AsyncMock())
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatResult(ChatCompletionMessage(role="assistant", content="{}",
        reasoning_content=reasoning), TokenUsage(20, 4096), "length"),
        ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard)
    assert {"model_retry_reasoning": [reasoning]} in scans
    forwarded = mock.calls[1][2]["reasoning_content"]
    assert "完整合成推理。" * 600 in forwarded
    assert secret not in forwarded and "<<SECRET_" in forwarded
    assert mock.kwargs[1]["max_tokens"] == 8192
    await transport.guard.close()


@pytest.mark.asyncio
async def test_semantic_diagnostics_are_metadata_only(transport, monkeypatch) -> None:
    from src.utils.logger import logger

    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    diagnostics = []
    monkeypatch.setattr(logger, "info", lambda template, *args: diagnostics.append(template % args))
    monkeypatch.setattr(logger, "warning", lambda template, *args: diagnostics.append(template % args))
    private_reasoning = "synthetic-private-reasoning-body"
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    private_claim = "synthetic-private-claim-body"
    model(monkeypatch, [ChatResult(ChatCompletionMessage(role="assistant", content="{}",
        reasoning_content=private_reasoning), TokenUsage(20, 4096), "length"),
        ChatCompletionMessage(role="assistant", content=output(alias, text=private_claim))])
    await compile_packet(transport.packet, transport.guard)
    assert any("finish_reason=length" in message and "output_cap=4096" in message for message in diagnostics)
    assert any("failure_code=output_truncated" in message for message in diagnostics)
    assert any("finish_reason=stop" in message and "output_cap=8192" in message for message in diagnostics)
    assert all(private_reasoning not in message and private_claim not in message for message in diagnostics)


@pytest.mark.asyncio
async def test_schema_or_evidence_retries_do_not_increase_output_limit(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content="{}"),
        ChatCompletionMessage(role="assistant", content=output("E999999")),
        ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard)
    assert [kwargs["max_tokens"] for kwargs in mock.kwargs] == [4096, 4096, 4096]


@pytest.mark.asyncio
async def test_length_at_recovery_cap_fails_without_repeating_or_accepting_json(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 8)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 8192)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    complete_but_truncated = ChatCompletionMessage(role="assistant", content=output(alias))
    mock = model(monkeypatch, [ChatResult(complete_but_truncated, TokenUsage(20, 4096), "length"),
                              ChatResult(complete_but_truncated, TokenUsage(20, 8192), "length")])
    with pytest.raises(CompilationLimitError, match="output_truncated"):
        await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == 2 and mock.aclose.await_count == 1


@pytest.mark.asyncio
async def test_length_recovery_checks_context_before_higher_budget_reservation(transport, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 16384)
    monkeypatch.setattr(settings, "LLM_MAX_CONTEXT_TOKENS", 13000)
    monkeypatch.setattr(settings, "LLM_CONTEXT_THRESHOLD", 0.95)
    mock = model(monkeypatch, [ChatResult(ChatCompletionMessage(role="assistant", content="{}"),
                                        TokenUsage(20, 4096), "length")])
    reservations = []
    transport.guard.reserve = reservations.append
    with pytest.raises(CompilationLimitError, match="模型上下文"):
        await compile_packet(transport.packet, transport.guard)
    assert len(mock.calls) == len(reservations) == 1


def test_semantic_recovery_cap_must_cover_initial_limit() -> None:
    from pydantic import ValidationError
    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, PROJECT_BRAIN_LLM_MAX_TOKENS=8192,
                 PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS=4096)


@pytest.mark.asyncio
async def test_semantic_extra_body_is_local_to_brain_not_privacy_or_global(transport, monkeypatch) -> None:
    global_body = '{"thinking":{"type":"disabled"}}'
    privacy_body = '{"chat_template_kwargs":{"enable_thinking":false}}'
    override = {"reasoning_effort": "none"}
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", global_body)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", privacy_body)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", encoded(override))
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    local_guard = CompilationGuard(lambda *args: None, lambda *args: None)
    assert local_guard.privacy_kwargs == {"extra_body": json.loads(privacy_body)}
    await compile_packet(transport.packet, transport.guard)
    assert mock.kwargs[0]["extra_body"] == override
    assert settings.LLM_EXTRA_BODY == global_body
    assert settings.PROJECT_BRAIN_PRIVACY_EXTRA_BODY == privacy_body
    assert transport.guard.privacy_kwargs == {"extra_body": {}}
    await local_guard.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "   "])
async def test_empty_semantic_extra_body_keeps_global_inheritance(transport, monkeypatch, value) -> None:
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", '{"thinking":{"type":"disabled"}}')
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", value)
    alias = next(iter(compact_packet(transport.packet).evidence_ids))
    mock = model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(alias))])
    await compile_packet(transport.packet, transport.guard)
    # The Brain layer resolves and validates inheritance before I/O; the
    # shared SDK receives the same global settings as an explicit object.
    assert mock.kwargs[0]["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.parametrize("invalid", ["{invalid", "[]", "null", '"not-object"'])
def test_invalid_semantic_extra_body_is_rejected_at_configuration(invalid) -> None:
    from pydantic import ValidationError
    from src.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None, PROJECT_BRAIN_SEMANTIC_EXTRA_BODY=invalid)


@pytest.mark.asyncio
async def test_schema_diagnostics_allow_only_fixed_types_and_known_locations(transport, monkeypatch) -> None:
    from src.utils.logger import logger

    diagnostics = []
    monkeypatch.setattr(logger, "warning", lambda template, *args: diagnostics.append(template % args))
    private_key = "synthetic-private-field-name"
    private_value = "synthetic-private-value"
    response = {"title": private_value, "claims": [{"text": private_value, "evidence_ids": [17]}],
                "unknowns": [], "document_path": None, private_key: private_value}
    model(monkeypatch, [ChatCompletionMessage(role="assistant", content=encoded(response))])
    with pytest.raises(ValueError, match="schema_mismatch"):
        await compile_packet(transport.packet, transport.guard)
    assert any("schema_error_type=string_type" in message and "schema_loc=claims.[].evidence_ids.[]" in message
               for message in diagnostics)
    assert any("schema_error_type=extra_forbidden" in message and "schema_loc=unknown_field" in message
               for message in diagnostics)
    assert all(private_key not in message and private_value not in message for message in diagnostics)


@pytest.mark.asyncio
async def test_unknown_evidence_diagnostic_counts_all_refs_without_values(transport, monkeypatch) -> None:
    from src.utils.logger import logger

    diagnostics = []
    monkeypatch.setattr(logger, "warning", lambda template, *args: diagnostics.append(template % args))
    aliases = list(compact_packet(transport.packet).evidence_ids)
    invalids = ["synthetic-private-reference", "E999999"]
    claims = [{"text": "第一条", "evidence_ids": [aliases[0], invalids[0]]},
              {"text": "第二条", "evidence_ids": [aliases[-1], invalids[1]]}]
    model(monkeypatch, [ChatCompletionMessage(role="assistant", content=output(aliases[0], claims=claims))])
    with pytest.raises(ValueError, match="unknown_evidence"):
        await compile_packet(transport.packet, transport.guard)
    assert any("invalid_count=2" in message and "total_count=4" in message
               and f"known_e_alias_max={len(aliases)}" in message for message in diagnostics)
    assert all(all(value not in message for value in invalids) for message in diagnostics)


def test_incremental_hash_ignores_only_source_commit(transport) -> None:
    original = packet_hash(transport.bundle, "recipe", transport.packet)
    changed = copy.deepcopy(transport.packet)
    for item in changed["items"]:
        for ref in item["evidence"]:
            ref["commit_sha"] = "b" * 40
    assert packet_hash(transport.bundle, "recipe", changed) == original
    changed["items"][0]["fact"]["attributes"]["commit_sha"] = "b" * 40
    assert packet_hash(transport.bundle, "recipe", changed) != original
    assert packet_hash(transport.bundle, "new-recipe", transport.packet) != original


def test_callee_blob_change_invalidates_caller_packet_even_outside_context(transport) -> None:
    caller = transport.bundle.facts[0]
    packet = {**transport.packet, "items": [item for item in transport.packet["items"] if item["fact"]["id"] == caller.id]}
    original = packet_hash(transport.bundle, "recipe", packet)
    changed = transport.bundle.model_copy(deep=True)
    changed.source_refs[1].blob_sha = "c" * 40
    changed.files[changed.source_refs[1].path]["blob_sha"] = "c" * 40
    assert packet_hash(changed, "recipe", packet) != original


def test_public_configuration_change_invalidates_context(transport) -> None:
    original = packet_hash(transport.bundle, "recipe", transport.packet)
    changed = transport.bundle.model_copy(deep=True)
    changed.files["config.yaml"] = {"blob_sha": "d" * 40}
    assert packet_hash(changed, "recipe", transport.packet) != original


def test_fragmented_context_keeps_all_relationships_and_source_identities(transport, monkeypatch) -> None:
    caller = transport.bundle.facts[0]
    ref = transport.bundle.source_refs[0]
    transport.bundle.facts = [caller]
    transport.bundle.relations = [Relation(id=stable_id("relation", str(index)), from_id=caller.id,
        type="CALLS", to_id=None, target_name=f"target_{index}", resolution="candidate",
        source_ref_ids=[ref.id], candidates=[f"symbol:{index:024x}"]) for index in range(300)]
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 4096)
    contexts = packets(transport.bundle, transport.snapshot)
    assert len(contexts) > 1
    relation_ids = [relation["id"] for packet in contexts for item in packet["items"] for relation in item["relations"]]
    assert len(relation_ids) == len(set(relation_ids)) == 300
    assert set(relation_ids) == {relation.id for relation in transport.bundle.relations}
    for packet in contexts:
        compact = compact_packet(packet)
        assert token_bound(encoded(compact.wire)) <= 4096
        for row in read_rows(compact.wire, "relations"):
            assert row["resolution"] == "candidate"
            assert row["to_id"] is None
            assert len(row["candidates"]) == 1
        for row in read_rows(compact.wire, "evidence"):
            assert compact.restore_evidence_ids([row["id"]]) == [ref.id]
            assert row["line_start"] == 1 and row["line_end"] == row["excerpt_end"] == 2


def test_cost_plan_does_not_claim_provider_tokenization_or_whole_build_cost(transport) -> None:
    plan = compilation_plan([transport.packet])
    assert plan["semantic_packets"] == 1
    assert plan["wire_bytes"] < plan["canonical_bytes"]
    assert plan["estimated_semantic_input_tokens"] < plan["reservation_envelope"]
    assert "not provider tokenizer" in plan["estimate_method"]
    assert {"privacy", "semantic_output", "retries", "project_summary", "assembly"} <= set(plan["excluded_costs"])


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085", "\v", "\f"])
def test_source_excerpt_uses_physical_lines_not_unicode_string_separators(transport, separator) -> None:
    """Unicode string contents do not create new physical source lines."""
    path = next(iter(transport.snapshot.files))
    full = Path(transport.snapshot.path) / path
    full.write_text(f"def caller():\n    marker = 'one{separator}two'\n    return callee()\n", encoding="utf-8")
    physical_lines = full.read_text().split("\n")[:-1]
    data = full.read_bytes()
    blob = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()
    for ref in transport.bundle.source_refs:
        if ref.path == path:
            ref.line_end = 3
            ref.blob_sha = blob
    relation_ref = SourceRef(id=stable_id("evidence", path, "3", "3", "call"),
        commit_sha=transport.bundle.source_commit, path=path, blob_sha=transport.bundle.source_refs[0].blob_sha,
        line_start=3, line_end=3, symbol_id=transport.bundle.facts[0].id)
    transport.bundle.source_refs.append(relation_ref)
    transport.bundle.relations[0].source_ref_ids = [relation_ref.id]
    for metadata in (transport.snapshot.files[path], transport.bundle.files[path]):
        metadata.update(lines=3, size=len(data), blob_sha=blob)
    contexts = packets(transport.bundle, transport.snapshot)
    evidence = next(ref for packet in contexts for item in packet["items"] for ref in item["evidence"]
                    if ref["id"] == relation_ref.id)
    assert evidence["excerpt"] == physical_lines[2]
    assert evidence["line_start"] == evidence["excerpt_end"] == 3
