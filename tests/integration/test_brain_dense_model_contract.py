"""Opt-in dense model protocol probe; all source and inputs are synthetic."""

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from src.config import settings
from src.core.llm_client import ChatResult, LLMClient, TokenUsage
from src.core.project_brain.artifacts import owned_path
from src.core.project_brain.compiler import CompilationGuard, compilation_response, compile_packet, encoded, packets, token_bound
from src.core.project_brain.context import compact_packet
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.schema import Compilation
from src.core.project_brain.source_bound import SourceBoundPlan
from src.core.project_brain.snapshot import export_snapshot
from tests.unit.test_project_brain import git

pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_LOCAL_TEST") != "1" or os.environ.get("BRAIN_LOCAL_REAL_MODEL") != "1",
    reason="仅独立本机栈显式授权真实模型时执行；输入全部为合成源码，可能计费",
)

_BUDGET_TOKENS = 50000
_SEMANTIC_CALL_LIMIT = 4


def _require(condition: bool, rule: str) -> None:
    """Keep assertion diagnostics free of packets, model text and credentials."""
    if not condition:
        raise AssertionError(rule)


def _synthetic_packet(tmp_path: Path, declaration_count: int = 20,
                      misleading_names: bool = False) -> tuple[dict, dict[str, int]]:
    """Build a synthetic fixed snapshot with bounded connected declarations."""
    _require(20 <= declaration_count <= 60, "合成声明数量必须在20至60之间")
    repository = tmp_path / "synthetic-dense-repository"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    source = []
    def function_name(index: int) -> str:
        return f"E9{index:03d}" if misleading_names else f"f{index:02d}"

    for index in range(declaration_count):
        source.append(f"def {function_name(index)}(value: int) -> int:")
        if index == 0:
            source.extend(["    base = value * 3", "    adjustment = value + 7"])
        elif index == 1:
            source.extend([f"    base = {function_name(0)}(value)", "    adjustment = value + 11"])
        else:
            source.extend([
                f"    base = {function_name(index - 1)}(value)",
                f"    adjustment = {function_name(index - 2)}(value + {index})",
            ])
        source.extend([f"    return (base + adjustment + {index * 13}) % 1009", ""])
    (repository / "synthetic_pipeline.py").write_text("\n".join(source), encoding="utf-8", newline="\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic dense model fixture")
    snapshot = export_snapshot(repository, tmp_path / "synthetic-dense-snapshot",
                               git(repository, "rev-parse", "HEAD"), 10)
    bundle = FactExtractor(snapshot).extract()
    contexts = packets(bundle, snapshot)
    packet = max(contexts, key=lambda candidate: len(encoded(compact_packet(candidate).wire).encode()))
    wire_bytes = len(encoded(compact_packet(packet).wire).encode())
    declarations = {item["fact"]["id"] for item in packet["items"] if item["fact"]["kind"] == "symbol"}
    evidence_ids = {reference["id"] for item in packet["items"] for reference in item["evidence"]}
    resolved_calls = {relation["id"] for item in packet["items"] for relation in item["relations"]
                      if relation["type"] == "CALLS" and relation["resolution"] == "resolved"}
    _require(15 <= len(declarations) <= declaration_count, "合成包声明数量越界")
    all_declarations = {item["fact"]["id"] for candidate in contexts for item in candidate["items"]
                        if item["fact"]["kind"] == "symbol"}
    _require(len(all_declarations) == declaration_count, "所有合成声明必须完整保留在包集合中")
    _require(len(evidence_ids) >= 30, "合成知识包必须保留至少30条源码证据")
    _require(len(resolved_calls) >= 30, "合成知识包必须保留至少30条静态确认调用")
    _require(wire_bytes >= 8192, "密集测试必须传输至少8KiB紧凑知识包")
    if declaration_count == 60 and settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE != "source_bound":
        _require(wire_bytes >= 25000, "大包协议测试必须传输至少25KiB紧凑知识包")
    return packet, {"declarations": len(declarations), "evidence": len(evidence_ids),
                    "resolved_calls": len(resolved_calls), "wire_bytes": wire_bytes,
                    "fixture_declarations": declaration_count, "planned_packets": len(contexts),
                    "selected_explanation_slots": len(packet["items"])}


@dataclass
class _ProbeBudget:
    """Returned usage plus unresolved conservative reservations share one cap."""

    usage: TokenUsage = field(default_factory=TokenUsage)
    reserved_tokens: int = 0
    accounted_calls: int = 0
    estimated_calls: int = 0
    maximum_request_bound: int = 0
    peak_budget_occupancy: int = 0

    def reserve(self, bound: int) -> None:
        _require(bound >= 0, "探测预留额度不得为负数")
        if self.usage.total_tokens + self.reserved_tokens + bound > _BUDGET_TOKENS:
            raise ValueError(f"合成密集协议探测超过{_BUDGET_TOKENS} Token硬预算，停止调用")
        self.reserved_tokens += bound
        self.maximum_request_bound = max(self.maximum_request_bound, bound)
        self.peak_budget_occupancy = max(self.peak_budget_occupancy, self.usage.total_tokens + self.reserved_tokens)

    def account(self, usage: TokenUsage, estimated: bool, bound: int) -> None:
        _require(self.reserved_tokens >= bound, "探测结算缺少对应预留")
        self.usage += usage
        self.reserved_tokens -= bound
        self.accounted_calls += 1
        self.estimated_calls += int(estimated)
        _require(self.usage.total_tokens + self.reserved_tokens <= _BUDGET_TOKENS,
                 "供应商返回用量超过合成探测硬预算")


@pytest.mark.asyncio
async def test_dense_real_model_compilation_and_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise dense input, privacy, adaptive output and evidence contracts."""
    database = make_url(settings.DATABASE_URL_SYNC)
    _require(database.host == "postgres" and database.database == "obc_brain_local"
             and database.username == "brain_local", "真实探测仅允许独立本机测试栈")
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PACKET_MAX_TOKENS", 32000)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 8192)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS", 16384)
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", _SEMANTIC_CALL_LIMIT - 1)
    packet, input_metadata = _synthetic_packet(tmp_path)
    context = compact_packet(packet)
    acceptable_ids = set(context.evidence_ids) | set(context.evidence_ids.values())
    canonical_ids = set(context.evidence_ids.values())
    original_chat = LLMClient.chat
    budget = _ProbeBudget()
    calls = []
    semantic_calls = 0
    claim_count = 0
    final_evidence_valid = False

    # The client normally logs provider exceptions verbatim. This isolated probe
    # emits its own numeric metadata and never echoes provider/model content.
    quiet_logger = logging.Logger("synthetic_dense_model_contract", level=logging.CRITICAL)
    quiet_logger.disabled = True
    monkeypatch.setattr("src.core.llm_client.logger", quiet_logger)

    async def inspected_chat(self: LLMClient, messages: list[dict], **kwargs: object) -> ChatResult:
        nonlocal semantic_calls
        semantic = messages[0]["content"].startswith("你是项目知识编译器")
        _require(kwargs.get("retry_max") == 0, "探测不得启用模型客户端内部重试")
        if semantic:
            _require(semantic_calls < _SEMANTIC_CALL_LIMIT, "密集合成探测最多4次语义请求")
            semantic_calls += 1
        request = {"messages": messages, "tools": kwargs["tools"], "tool_choice": kwargs["tool_choice"]} if kwargs.get("tools") else messages
        record = {"call": len(calls) + 1, "stage": "semantic" if semantic else "privacy",
                  "output_cap": kwargs.get("max_tokens"), "responded": False,
                  "request_bound": token_bound(encoded(request)) + int(kwargs["max_tokens"])}
        calls.append(record)
        try:
            result = await original_chat(self, messages, **kwargs)
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise RuntimeError(f"密集合成模型调用失败：{type(exc).__name__}") from None
        record.update(responded=True, prompt_tokens=result.usage.prompt_tokens,
                      completion_tokens=result.usage.completion_tokens, cached_tokens=result.usage.cached_tokens,
                      finish_reason=result.finish_reason, reasoning_chars=result.reasoning_chars)
        if semantic:
            try:
                content = compilation_response(result.message,
                    strict_tool=settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE in {"strict_tool", "source_bound"})
                parsed = (SourceBoundPlan.from_packet(packet, context).bind(content)
                          if settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE == "source_bound"
                          else Compilation.model_validate_json(content))
                record.update(structure_valid=True, claim_count=len(parsed.claims),
                              evidence_valid=all(set(claim.evidence_ids) <= acceptable_ids for claim in parsed.claims),
                              document_path_valid=parsed.document_path is None or (
                                  owned_path(parsed.document_path) and parsed.document_path.startswith("docs/project-brain/")))
            except (ValidationError, ValueError) as exc:
                record.update(structure_valid=False, claim_count=0, evidence_valid=False,
                              structure_error_type=type(exc).__name__)
                if isinstance(exc, ValidationError):
                    allowed_types = {"missing", "json_invalid", "extra_forbidden", "list_type", "string_type",
                                     "dict_type", "model_type", "too_short", "string_too_short", "value_error"}
                    record["structure_error_counts"] = {
                        kind: sum(error["type"] == kind for error in exc.errors(include_input=False))
                        for kind in allowed_types
                        if any(error["type"] == kind for error in exc.errors(include_input=False))
                    }
                if settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE == "source_bound":
                    try:
                        raw = json.loads(content)
                        returned_slots = raw.get("slots") if isinstance(raw, dict) else None
                        expected_slots = set(SourceBoundPlan.from_packet(packet, context).scopes)
                        record["expected_slots"] = len(expected_slots)
                        record["returned_slots"] = len(returned_slots) if isinstance(returned_slots, dict) else 0
                        if isinstance(returned_slots, dict):
                            record["missing_slots"] = len(expected_slots - returned_slots.keys())
                            record["unexpected_slots"] = len(returned_slots.keys() - expected_slots)
                    except (TypeError, ValueError):
                        record["output_json_valid"] = False
        return result

    monkeypatch.setattr(LLMClient, "chat", inspected_chat)
    guard = CompilationGuard(budget.account, budget.reserve)
    try:
        compiled = await compile_packet(packet, guard)
        claim_count = len(compiled.claims)
        final_evidence_valid = bool(compiled.claims) and all(
            set(claim.evidence_ids) <= canonical_ids for claim in compiled.claims)
        _require(final_evidence_valid, "正式结论必须引用当前合成包的规范源码证据")
        _require(0 < semantic_calls <= _SEMANTIC_CALL_LIMIT, "密集合成探测语义请求次数无效")
        semantic_records = [record for record in calls if record["stage"] == "semantic"]
        _require(semantic_records[0]["output_cap"] == 8192, "密集合成探测初次语义输出上限必须为8192")
        _require(all(int(record["output_cap"]) <= 16384 for record in semantic_records),
                 "密集合成探测重试输出上限不得超过16384")
        _require(budget.usage.total_tokens > 0, "密集合成探测必须记录非零用量")
        _require(budget.reserved_tokens == 0, "成功探测不得残留未结算调用")
    finally:
        await guard.close()
        print(json.dumps({"protocol": "brain-dense-model-probe/1", "input": input_metadata,
                          "budget_tokens": _BUDGET_TOKENS, "semantic_call_limit": _SEMANTIC_CALL_LIMIT,
                          "semantic_calls": semantic_calls, "accounted_calls": budget.accounted_calls,
                          "prompt_tokens": budget.usage.prompt_tokens,
                          "completion_tokens": budget.usage.completion_tokens,
                          "cached_tokens": budget.usage.cached_tokens,
                          "unknown_reserved_tokens": budget.reserved_tokens,
                          "estimated_calls": budget.estimated_calls,
                          "budget_occupancy_tokens": budget.usage.total_tokens + budget.reserved_tokens,
                          "maximum_request_bound": budget.maximum_request_bound,
                          "peak_budget_occupancy_tokens": budget.peak_budget_occupancy,
                          "claim_count": claim_count, "evidence_valid": final_evidence_valid,
                          "calls": calls}, ensure_ascii=False, sort_keys=True))


@pytest.mark.asyncio
async def test_large_synthetic_nonreasoning_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One opt-in large-packet probe with a separate 100K hard allowance."""
    if os.environ.get("BRAIN_LOCAL_LARGE_MODEL_PROBE") != "1":
        pytest.skip("额外大包合成测试需要显式开启")
    original_builder = _synthetic_packet
    monkeypatch.setattr(__import__(__name__, fromlist=["_synthetic_packet"]), "_synthetic_packet",
                        lambda path: original_builder(path, 60))
    monkeypatch.setattr(__import__(__name__, fromlist=["_BUDGET_TOKENS"]), "_BUDGET_TOKENS", 100000)
    monkeypatch.setattr(__import__(__name__, fromlist=["_SEMANTIC_CALL_LIMIT"]), "_SEMANTIC_CALL_LIMIT", 2)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", '{"reasoning_effort":"none"}')
    original_chat = LLMClient.chat

    async def require_nonreasoning(self: LLMClient, messages: list[dict], **kwargs: object) -> ChatResult:
        result = await original_chat(self, messages, **kwargs)
        if messages[0]["content"].startswith("你是项目知识编译器"):
            _require(result.reasoning_chars == 0, "语义网关未遵守非思考参数：仍返回reasoning")
        return result

    monkeypatch.setattr(LLMClient, "chat", require_nonreasoning)
    await test_dense_real_model_compilation_and_evidence(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_large_source_names_cannot_be_used_as_evidence_aliases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Challenge source/E-alias confusion without using real project data."""
    original = _synthetic_packet
    module = __import__(__name__, fromlist=["_synthetic_packet"])
    monkeypatch.setattr(module, "_synthetic_packet", lambda path, count=20: original(path, 60, True))
    await test_large_synthetic_nonreasoning_contract(tmp_path, monkeypatch)


@pytest.mark.asyncio
async def test_large_source_bound_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe one complete bounded packet from the retained 60-declaration fixture."""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    await test_large_source_names_cannot_be_used_as_evidence_aliases(tmp_path, monkeypatch)
