"""Opt-in real model contract test using only synthetic fixture source."""

import os

import pytest
from pydantic import ValidationError

from src.config import settings
from src.core.llm_client import LLMClient, TokenUsage
from src.core.project_brain.artifacts import owned_path
from src.core.project_brain.compiler import CompilationGuard, compilation_response, compile_packet, encoded, packets
from src.core.project_brain.context import compact_packet
from src.core.project_brain.extractor import FactExtractor
from src.core.project_brain.schema import Compilation
from src.core.project_brain.source_bound import SourceBoundPlan
from tests.unit.test_project_brain import repository, snapshot  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_LOCAL_TEST") != "1" or os.environ.get("BRAIN_LOCAL_REAL_MODEL") != "1",
    reason="真实模型可能计费，必须显式开启 BRAIN_LOCAL_REAL_MODEL，仅发送合成测试源码",
)


@pytest.mark.asyncio
async def test_real_model_compilation_and_evidence(snapshot, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实隐私扫描/语义调用必须输出合规 JSON 和已有证据，不证明业务语义正确率。"""
    monkeypatch.setattr(settings, "PROJECT_BRAIN_LLM_MAX_TOKENS", 4096)
    bundle = FactExtractor(snapshot).extract()
    packet = min(packets(bundle, snapshot), key=lambda item: len(encoded(item)))
    original_chat = LLMClient.chat

    async def inspected_chat(self, messages, **kwargs):
        result = await original_chat(self, messages, **kwargs)
        if messages[0]["content"].startswith("你是项目知识编译器"):
            content = compilation_response(result.message,
                strict_tool=settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE in {"strict_tool", "source_bound"})
            try:
                parsed = (SourceBoundPlan.from_packet(packet, compact_packet(packet)).bind(content)
                          if settings.PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE == "source_bound"
                          else Compilation.model_validate_json(content))
                aliases = compact_packet(packet).evidence_ids
                ids = set(aliases) | set(aliases.values())
                valid = all(set(c.evidence_ids) <= ids for c in parsed.claims)
                try:
                    path_valid = parsed.document_path is None or (owned_path(parsed.document_path) and parsed.document_path.startswith("docs/project-brain/"))
                except ValueError:
                    path_valid = False
                print(f"模型输出结构有效：字符数 {len(content)}，证据匹配 {valid}，路径合规 {path_valid}。")
            except (ValidationError, ValueError) as exc:
                count = len(exc.errors(include_input=False)) if isinstance(exc, ValidationError) else 1
                print(f"模型输出结构诊断（不含正文）：字符数 {len(content)}，错误类型 {type(exc).__name__}，数量 {count}。")
        return result

    monkeypatch.setattr(LLMClient, "chat", inspected_chat)
    total = TokenUsage()
    reserved = 0
    calls = 0

    def reserve(bound: int) -> None:
        nonlocal reserved
        if total.total_tokens + reserved + bound > 50000:
            raise ValueError("本机真实模型探测超过 50000 Token 预算")
        reserved += bound

    def account(usage: TokenUsage, estimated: bool, bound: int) -> None:
        nonlocal total, reserved, calls
        total += usage
        reserved -= bound
        calls += 1

    guard = CompilationGuard(account, reserve)
    try:
        compiled = await compile_packet(packet, guard)
    finally:
        await guard.close()
    valid_ids = {e["id"] for item in packet["items"] for e in item["evidence"]}
    assert compiled.claims and all(set(claim.evidence_ids) <= valid_ids for claim in compiled.claims)
    assert calls >= 1 and total.total_tokens > 0
    print(f"真实模型协议验证通过：调用 {calls} 次，总 Token {total.total_tokens}，结论 {len(compiled.claims)} 条，证据均有效。")
