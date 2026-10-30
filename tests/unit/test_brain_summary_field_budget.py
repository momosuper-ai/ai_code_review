"""Exercise summary response budgets at the real compiler transport boundary."""
import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.project_brain.compiler import CompilationLimitError, compilation_transport, compile_packet, encoded
from src.core.project_brain.context import compact_packet
from src.core.project_brain.source_bound import SourceBoundPlan
from src.core.project_brain.summary_budget import summary_field_budget, summary_prose_limit, summary_prose_size
from tests.unit.test_brain_compiler_transport import model, transport  # noqa: F401
from tests.unit.test_brain_strict_transport import formatter_result
from tests.unit.test_brain_synthesis_transport import response, synthesis  # noqa: F401


def test_summary_schema_budgets_each_authored_field(synthesis, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    _, kwargs, _ = compilation_transport(synthesis, compact_packet(synthesis))
    schema = kwargs["json_schema_builder"]()["json_schema"]["schema"]
    assert schema["properties"]["title"]["maxLength"] > 0
    slot = schema["$defs"]["SlotText"]["properties"]
    assert 1 <= slot["texts"]["maxItems"] <= 3
    assert slot["texts"]["items"]["maxLength"] > 0
    assert slot["unknowns"]["maxItems"] == 1
    assert schema["properties"]["unknowns"]["maxItems"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "source_bound"])
async def test_retry_reports_numeric_size_and_keeps_tighter_budget_after_invalid_json(synthesis, transport, monkeypatch, mode):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", mode)
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 3)
    oversized = response(text="完整的合成概览仍然过长。" * 250)
    short = response(text="入口负责请求校验并协调两个组件；动态依赖仍需验证。")
    bodies = [encoded(oversized), "{", encoded(short)]
    messages = ([formatter_result(body) for body in bodies] if mode == "source_bound" else
                [ChatCompletionMessage(role="assistant", content=body) for body in bodies])
    mock = model(monkeypatch, messages)
    result = await compile_packet(synthesis, transport.guard)
    assert result.claims[0].text == short["slots"]["S1"]["texts"][0]
    assert len(mock.calls) == 3
    size = summary_prose_size(SourceBoundPlan.from_packet(synthesis, compact_packet(synthesis)).bind(bodies[0]))
    assert f"actual_bytes={size}" in mock.calls[1][-1]["content"]
    assert "limit_bytes=4000" in mock.calls[1][-1]["content"]
    assert "summary_budget_level=1" in mock.calls[1][0]["content"]
    assert "summary_budget_level=1" in mock.calls[2][0]["content"]
    assert oversized["slots"]["S1"]["texts"][0] not in encoded(mock.calls[1:])
    assert all(call[-2]["content"] == "{}" for call in mock.calls[1:])


@pytest.mark.asyncio
async def test_repeated_overview_overflow_has_specific_capacity_code(synthesis, transport, monkeypatch):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "auto")
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    body = ChatCompletionMessage(role="assistant", content=encoded(response(text="摘要过长。" * 1500)))
    mock = model(monkeypatch, [body, body])
    with pytest.raises(CompilationLimitError) as caught:
        await compile_packet(synthesis, transport.guard)
    assert caught.value.failure_code == "summary_output_capacity"
    assert len(mock.calls) == 2


@pytest.mark.parametrize("packet_limit", [1, 1024, 8192, 16000, 32000, 65536, 1000000])
@pytest.mark.parametrize("level", [0, 1, 2])
@pytest.mark.parametrize("character", ["\x00", '"', "\\", "\n", "中", "😀"])
def test_field_limits_bound_complete_serialized_bytes(synthesis, packet_limit, level, character):
    budget = summary_field_budget(packet_limit, level)
    path = "docs/project-brain/" + "a" * (budget.path_chars - len("docs/project-brain/")) if budget.path_chars else None
    value = {"title": "A" + character * (budget.title_chars - 1),
             "slots": {"S1": {"texts": ["A" + character * (budget.text_chars - 1)] * budget.text_count,
                                "unknowns": [character * budget.unknown_chars] * budget.unknown_count}},
             "unknowns": [character * budget.unknown_chars] * budget.unknown_count, "document_path": path}
    budget.require_response(value)
    compilation = SourceBoundPlan.from_packet(synthesis, compact_packet(synthesis)).bind(encoded(value))
    assert summary_prose_size(compilation) <= summary_prose_limit(packet_limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["title_chars", "text_count", "text_chars", "slot_unknown_count", "top_unknown_count", "unknown_chars", "path_chars"])
async def test_provider_ignoring_field_limits_is_corrected_locally(synthesis, transport, monkeypatch, case):
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "source_bound")
    monkeypatch.setattr(settings, "AGENT_FORMAT_RETRY_MAX", 1)
    budget = summary_field_budget(32000)
    value = response(text="短概览。")
    if case == "title_chars":
        value["title"] = "A" * (budget.title_chars + 1)
    elif case == "text_count":
        value["slots"]["S1"]["texts"] *= budget.text_count + 1
    elif case == "text_chars":
        value["slots"]["S1"]["texts"] = ["A" * (budget.text_chars + 1)]
    elif case == "slot_unknown_count":
        value["slots"]["S1"]["unknowns"] = ["重复"] * (budget.unknown_count + 1)
    elif case == "top_unknown_count":
        value["unknowns"] = ["重复"] * (budget.unknown_count + 1)
    elif case == "unknown_chars":
        value["unknowns"] = ["A" * (budget.unknown_chars + 1)]
    else:
        value["document_path"] = "docs/project-brain/" + "a" * budget.path_chars
    assert summary_prose_size(SourceBoundPlan.from_packet(synthesis, compact_packet(synthesis)).bind(encoded(value))) < 4000
    mock = model(monkeypatch, [formatter_result(encoded(value)), formatter_result(encoded(response()))])
    await compile_packet(synthesis, transport.guard)
    assert len(mock.calls) == 2
    assert f"fields={case}" in mock.calls[1][-1]["content"]
    assert "summary_field_budget_exceeded" in mock.calls[1][-1]["content"]
    # The provider-compatible schema drops unsupported maxLength/maxItems,
    # but actionable descriptions and the independently checked prompt remain.
    params = mock.kwargs[0]["tools"][0]["function"]["parameters"]
    assert "maxLength" not in encoded(params)
    assert str(budget.text_chars) in params["$defs"]["SlotText"]["properties"]["texts"]["description"]


def test_public_error_code_distinguishes_summary_output_from_input_capacity():
    from src.api.v1.brain import _error_code
    assert _error_code("概览失败；failure_code=summary_output_capacity", "other") == "summary_output_capacity"
    assert _error_code("输入失败；failure_code=capacity_limit", "other") == "capacity_limit"
