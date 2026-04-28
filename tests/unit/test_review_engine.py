"""Tests for ReviewEngine: _try_parse, _fallback_result, _parse_with_retry, multi-agent."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.llm_client import ChatResult, TokenUsage
from src.core.review_engine import (
    DimensionResult,
    ReviewEngine,
    ReviewResult,
    _parse_changed_lines,
    _parse_with_retry,
    _ranges_overlap,
)


def _make_chat_result(content: str = "", tool_calls=None,
                       prompt_tokens: int = 100, completion_tokens: int = 50,
                       cached_tokens: int = 0) -> ChatResult:
    """Helper to create a ChatResult with a mock message."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    return ChatResult(
        message=msg,
        usage=TokenUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        ),
    )


# ---------------------------------------------------------------------------
# _try_parse
# ---------------------------------------------------------------------------
class TestTryParse:
    def test_valid_json(self):
        content = json.dumps({
            "overall_assessment": "Good code",
            "score": "A",
            "issues": [],
            "highlights": ["Nice naming"],
        })
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.score == "A"
        assert result.overall_assessment == "Good code"
        assert err_type == ""

    def test_valid_json_in_markdown_block(self):
        content = '```json\n{"overall_assessment": "ok", "score": "B"}\n```'
        result, _, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.score == "B"

    def test_no_json(self):
        content = "This is just plain text with no JSON at all."
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is None
        assert err_type == "no_json"

    def test_json_syntax_error(self):
        # 配平 `{...}` 但内部彻底不像 JSON，json_repair 也无法修复成 dict
        content = '{this is not json at all !!! @#$%}'
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is None
        assert err_type in ("json_syntax_error", "missing_fields")
        assert err_detail

    def test_missing_fields_overall_assessment(self):
        content = json.dumps({"issues": [], "score": "B"})
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is None
        assert err_type == "missing_fields"
        assert "overall_assessment" in err_detail

    def test_missing_fields_score(self):
        content = json.dumps({"overall_assessment": "ok", "issues": []})
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is None
        assert err_type == "missing_fields"
        assert "score" in err_detail

    def test_missing_both_required_fields(self):
        content = json.dumps({"issues": [], "highlights": []})
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is None
        assert err_type == "missing_fields"
        assert "overall_assessment" in err_detail
        assert "score" in err_detail

    def test_json_with_surrounding_text(self):
        content = 'Here is my review:\n{"overall_assessment": "ok", "score": "A"}\nDone.'
        result, _, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.score == "A"

    def test_thinking_wrapper_with_brace_inside(self):
        """thinking 标签内含 `{` 不应被误抓为 JSON 起点。"""
        content = (
            "<think>let me analyze this { example } carefully</think>\n"
            '{"overall_assessment": "ok", "score": "A"}'
        )
        result, err_type, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.score == "A"

    def test_multiple_braces_in_preamble(self):
        """preamble 中的孤立 `{` 后才出现真 JSON。"""
        content = (
            "Looking at code: {b == 0} is the check.\n"
            "Now my analysis result:\n"
            '{"overall_assessment": "Found a bug", "score": "C"}'
        )
        result, _, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.score == "C"

    def test_brace_inside_string_literal(self):
        """JSON 字符串字面量中的 `}` 不应导致提前结束。"""
        content = (
            '{"overall_assessment": "代码有问题: } 漏掉了", "score": "B"}'
        )
        result, _, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert "漏掉了" in result.overall_assessment

    def test_thinking_plus_markdown_wrapped_json(self):
        """thinking + markdown 包装的 JSON 都能识别。"""
        content = (
            "<thinking>The diff shows a fix.</thinking>\n"
            "```json\n"
            '{"overall_assessment": "Good fix", "score": "B"}\n'
            "```"
        )
        result, _, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.score == "B"

    def test_two_json_objects_takes_first(self):
        """两个 JSON 对象只取第一个完整闭合的。"""
        content = (
            '{"overall_assessment": "first", "score": "A"}\n'
            'And another: {"overall_assessment": "second", "score": "D"}'
        )
        result, _, _ = ReviewEngine._try_parse(content)
        assert result is not None
        assert result.overall_assessment == "first"


# ---------------------------------------------------------------------------
# _fallback_result
# ---------------------------------------------------------------------------
class TestFallbackResult:
    def test_partial_json_with_missing_required(self):
        """JSON is valid but was missing required fields — fallback can still extract."""
        content = json.dumps({
            "overall_assessment": "partial review",
            "score": "C",
            "issues": [{"content": "issue1"}],
        })
        result = ReviewEngine._fallback_result(content)
        assert result.overall_assessment == "partial review"
        assert result.score == "C"

    def test_no_json_uses_raw_text(self):
        content = "The code looks fine overall."
        result = ReviewEngine._fallback_result(content)
        assert "审查结果格式解析失败" in result.overall_assessment
        assert "The code looks fine overall." in result.overall_assessment
        assert result.score == "B"

    def test_broken_json_falls_back_to_text(self):
        content = "Review: {broken json here"
        result = ReviewEngine._fallback_result(content)
        assert "审查结果格式解析失败" in result.overall_assessment
        assert result.score == "B"


# ---------------------------------------------------------------------------
# _parse_with_retry (module-level function)
# ---------------------------------------------------------------------------
class TestParseWithRetry:
    @pytest.fixture
    def mock_llm(self):
        return MagicMock()

    @pytest.fixture
    def engine(self):
        with patch("src.core.review_engine.LLMClient"):
            with patch("src.core.review_engine.AgentToolExecutor"):
                engine = ReviewEngine("/fake/repo")
                engine.llm = MagicMock()
                return engine

    @pytest.mark.asyncio
    async def test_success_on_first_try(self, mock_llm):
        valid_json = json.dumps({
            "overall_assessment": "Good",
            "score": "A",
            "issues": [],
            "highlights": [],
        })
        result, usage = await _parse_with_retry(mock_llm, valid_json, [], tag="[test] ")
        assert result.score == "A"
        assert usage.total_tokens == 0
        mock_llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_instance_method_delegates(self, engine):
        """Instance method _parse_with_retry delegates to module-level function."""
        valid_json = json.dumps({
            "overall_assessment": "Good",
            "score": "A",
            "issues": [],
            "highlights": [],
        })
        result, usage = await engine._parse_with_retry(valid_json, [], tag="[test] ")
        assert result.score == "A"
        engine.llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self, mock_llm):
        bad_content = "no json here"
        good_json = json.dumps({
            "overall_assessment": "Fixed",
            "score": "B",
            "issues": [],
            "highlights": [],
        })

        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content=good_json))

        result, usage = await _parse_with_retry(mock_llm, bad_content, [], tag="[test] ")
        assert result.score == "B"
        assert result.overall_assessment == "Fixed"
        assert mock_llm.chat.call_count == 1
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50

    @pytest.mark.asyncio
    async def test_all_retries_fail_uses_fallback(self, mock_llm):
        bad_content = "still no json"

        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content="also no json"))

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 2
            result, usage = await _parse_with_retry(mock_llm, bad_content, [], tag="[test] ")

        assert "审查结果格式解析失败" in result.overall_assessment
        assert mock_llm.chat.call_count == 2
        assert usage.prompt_tokens == 200  # 2 retries × 100

    @pytest.mark.asyncio
    async def test_llm_exception_does_not_break_retry(self, mock_llm):
        bad_content = "no json"
        good_json = json.dumps({
            "overall_assessment": "Recovered",
            "score": "A",
            "issues": [],
            "highlights": [],
        })

        mock_llm.chat = AsyncMock(
            side_effect=[Exception("LLM down"), _make_chat_result(content=good_json)],
        )

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 3
            result, usage = await _parse_with_retry(mock_llm, bad_content, [], tag="[test] ")

        assert result.score == "A"
        assert result.overall_assessment == "Recovered"

    @pytest.mark.asyncio
    async def test_temperature_decreases(self, mock_llm):
        bad_content = "no json"

        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content="still bad"))

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 3
            await _parse_with_retry(mock_llm, bad_content, [], tag="")

        temps = [call.kwargs["temperature"] for call in mock_llm.chat.call_args_list]
        assert temps == [0.1, 0.05, 0.0]

    @pytest.mark.asyncio
    async def test_messages_not_mutated(self, mock_llm):
        bad_content = "no json"
        original_messages = [{"role": "user", "content": "test"}]
        original_len = len(original_messages)

        good_json = json.dumps({
            "overall_assessment": "ok",
            "score": "B",
            "issues": [],
            "highlights": [],
        })
        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content=good_json))

        await _parse_with_retry(mock_llm, bad_content, original_messages, tag="")
        assert len(original_messages) == original_len

    @pytest.mark.asyncio
    async def test_categories_param_passed(self, mock_llm):
        """When categories are provided, they constrain the retry prompt."""
        bad_content = "no json"
        good_json = json.dumps({
            "overall_assessment": "ok",
            "score": "B",
            "issues": [],
            "highlights": [],
        })
        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content=good_json))

        result, _ = await _parse_with_retry(
            mock_llm, bad_content, [], tag="",
            categories=["bug"],
        )
        assert result.score == "B"
        # The retry message should contain "bug" as category
        call_args = mock_llm.chat.call_args
        retry_msgs = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        user_msg = [m for m in retry_msgs if isinstance(m, dict) and m.get("role") == "user"]
        assert any("bug" in m.get("content", "") for m in user_msg)

    @pytest.mark.asyncio
    async def test_original_response_reasoning_content_passed_back(self, mock_llm):
        """传入 original_response 时，重试消息中 assistant 条目应带 reasoning_content。

        场景：DeepSeek thinking 模式要求后续轮次必须回传 reasoning_content。
        """
        bad_content = "no json here"
        good_json = json.dumps({
            "overall_assessment": "ok", "score": "B",
            "issues": [], "highlights": [],
        })
        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content=good_json))

        class FakeResponse:
            def model_dump(self, exclude_none=True):
                return {
                    "role": "assistant",
                    "content": bad_content,
                    "reasoning_content": "<think>let me analyze</think>",
                }

        await _parse_with_retry(
            mock_llm, bad_content, [], tag="",
            original_response=FakeResponse(),
        )

        call_args = mock_llm.chat.call_args
        retry_msgs = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        assistant_msgs = [
            m for m in retry_msgs if isinstance(m, dict) and m.get("role") == "assistant"
        ]
        assert assistant_msgs, "retry_messages 应包含 assistant 消息"
        assert assistant_msgs[-1].get("reasoning_content") == "<think>let me analyze</think>"

    @pytest.mark.asyncio
    async def test_failed_content_is_dumped_to_file(self, mock_llm, tmp_path, monkeypatch):
        """解析失败时调用 _dump_failed_content 落盘。"""
        from src.core import review_engine as re_mod
        monkeypatch.setattr(re_mod, "_FAILED_DUMP_DIR", str(tmp_path))

        bad_content = "no json at all"
        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content="still bad"))
        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 1
            await _parse_with_retry(
                mock_llm, bad_content, [], tag="[review:42][security] ",
            )
        files = list(tmp_path.iterdir())
        assert len(files) >= 1
        assert any("42" in f.name and "security" in f.name for f in files)

    @pytest.mark.asyncio
    async def test_without_original_response_falls_back_to_content_only(self, mock_llm):
        """向后兼容：不传 original_response 时仍用 content-only dict。"""
        bad_content = "no json"
        good_json = json.dumps({
            "overall_assessment": "ok", "score": "B",
            "issues": [], "highlights": [],
        })
        mock_llm.chat = AsyncMock(return_value=_make_chat_result(content=good_json))

        await _parse_with_retry(mock_llm, bad_content, [], tag="")

        call_args = mock_llm.chat.call_args
        retry_msgs = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        assistant_msgs = [
            m for m in retry_msgs if isinstance(m, dict) and m.get("role") == "assistant"
        ]
        assert assistant_msgs
        assert set(assistant_msgs[-1].keys()) == {"role", "content"}
        assert assistant_msgs[-1]["content"] == bad_content


# ---------------------------------------------------------------------------
# _parse_result (backward compat)
# ---------------------------------------------------------------------------
class TestParseResult:
    def test_valid_json(self):
        content = json.dumps({
            "overall_assessment": "Good",
            "score": "A",
            "issues": [],
            "highlights": [],
        })
        result = ReviewEngine._parse_result(content)
        assert result.score == "A"

    def test_invalid_falls_back(self):
        result = ReviewEngine._parse_result("no json here")
        assert "审查结果格式解析失败" in result.overall_assessment
        assert result.score == "B"


# ---------------------------------------------------------------------------
# _deduplicate_issues
# ---------------------------------------------------------------------------
class TestDeduplicateIssues:
    def test_no_duplicates(self):
        """Issues at different locations are all kept."""
        r1 = DimensionResult(
            dimension_name="bug",
            issues=[
                {"file_path": "a.py", "line_start": 1, "line_end": 1, "category": "bug",
                 "severity": "major", "content": "issue1"},
            ],
        )
        r2 = DimensionResult(
            dimension_name="security",
            issues=[
                {"file_path": "a.py", "line_start": 10, "line_end": 10, "category": "security",
                 "severity": "major", "content": "issue2"},
            ],
        )
        result = ReviewEngine._deduplicate_issues([r1, r2])
        assert len(result) == 2

    def test_same_location_different_category_kept(self):
        """Same line but different categories are NOT duplicates."""
        r1 = DimensionResult(
            dimension_name="bug",
            issues=[
                {"file_path": "a.py", "line_start": 5, "line_end": 5, "category": "bug",
                 "severity": "major", "content": "bug issue"},
            ],
        )
        r2 = DimensionResult(
            dimension_name="security",
            issues=[
                {"file_path": "a.py", "line_start": 5, "line_end": 5, "category": "security",
                 "severity": "major", "content": "security issue"},
            ],
        )
        result = ReviewEngine._deduplicate_issues([r1, r2])
        assert len(result) == 2

    def test_exact_duplicate_keeps_higher_severity(self):
        """Same location + same category: keep the more severe one."""
        r1 = DimensionResult(
            dimension_name="bug",
            issues=[
                {"file_path": "a.py", "line_start": 5, "line_end": 5, "category": "bug",
                 "severity": "minor", "content": "minor issue"},
            ],
        )
        r2 = DimensionResult(
            dimension_name="bug",
            issues=[
                {"file_path": "a.py", "line_start": 5, "line_end": 5, "category": "bug",
                 "severity": "critical", "content": "critical issue"},
            ],
        )
        result = ReviewEngine._deduplicate_issues([r1, r2])
        assert len(result) == 1
        assert result[0]["severity"] == "critical"

    def test_same_severity_keeps_longer_content(self):
        """Same severity: keep the more detailed (longer content) one."""
        r1 = DimensionResult(
            dimension_name="bug",
            issues=[
                {"file_path": "a.py", "line_start": 5, "line_end": 5, "category": "bug",
                 "severity": "major", "content": "short"},
            ],
        )
        r2 = DimensionResult(
            dimension_name="bug",
            issues=[
                {"file_path": "a.py", "line_start": 5, "line_end": 5, "category": "bug",
                 "severity": "major", "content": "a much longer and more detailed description"},
            ],
        )
        result = ReviewEngine._deduplicate_issues([r1, r2])
        assert len(result) == 1
        assert "longer" in result[0]["content"]


# ---------------------------------------------------------------------------
# _compute_weighted_score
# ---------------------------------------------------------------------------
class TestComputeWeightedScore:
    def _make_result(self, name: str, score: str) -> DimensionResult:
        return DimensionResult(dimension_name=name, score=score)

    def test_all_a_gives_a(self):
        results = [
            self._make_result("bug", "A"),
            self._make_result("security", "A"),
            self._make_result("performance", "A"),
            self._make_result("quality", "A"),
            self._make_result("architecture", "A"),
        ]
        score = ReviewEngine._compute_weighted_score(results, [])
        assert score == "A"

    def test_all_d_gives_d(self):
        results = [
            self._make_result("bug", "D"),
            self._make_result("security", "D"),
        ]
        score = ReviewEngine._compute_weighted_score(results, [])
        assert score == "D"

    def test_d_override_caps_at_c(self):
        """Any dimension with D score caps the final score at C."""
        results = [
            self._make_result("bug", "A"),
            self._make_result("security", "A"),
            self._make_result("performance", "A"),
            self._make_result("quality", "A"),
            self._make_result("architecture", "D"),
        ]
        score = ReviewEngine._compute_weighted_score(results, [])
        assert score == "C"

    def test_critical_issue_caps_at_c(self):
        """Any critical issue caps the final score at C."""
        results = [
            self._make_result("bug", "A"),
            self._make_result("security", "A"),
        ]
        issues = [{"severity": "critical", "content": "bad"}]
        score = ReviewEngine._compute_weighted_score(results, issues)
        assert score == "C"

    def test_mixed_scores(self):
        """Mixed scores produce weighted average."""
        results = [
            self._make_result("bug", "B"),        # weight 0.30
            self._make_result("security", "C"),    # weight 0.25
            self._make_result("performance", "A"), # weight 0.15
        ]
        score = ReviewEngine._compute_weighted_score(results, [])
        # weighted = 3*0.30 + 2*0.25 + 4*0.15 = 0.90 + 0.50 + 0.60 = 2.0 / 0.70 = 2.857
        assert score == "B"


# ---------------------------------------------------------------------------
# _merge_results
# ---------------------------------------------------------------------------
class TestMergeResults:
    @pytest.fixture
    def engine(self):
        with patch("src.core.review_engine.LLMClient"):
            with patch("src.core.review_engine.AgentToolExecutor"):
                return ReviewEngine("/fake/repo")

    def test_all_successful(self, engine):
        dim_results = [
            DimensionResult(
                dimension_name="bug", display_name="缺陷分析", icon="🔍",
                score="A", issues=[], highlights=["good"],
                success=True, tool_calls_count=3, duration_seconds=5.0,
            ),
            DimensionResult(
                dimension_name="security", display_name="安全审查", icon="🛡️",
                score="B", issues=[{"file_path": "x.py", "severity": "major",
                                     "category": "security", "content": "问题",
                                     "dimension": "security"}],
                highlights=[], success=True, tool_calls_count=2, duration_seconds=4.0,
            ),
        ]
        result = engine._merge_results(dim_results)
        assert result.score in ("A", "B")
        assert len(result.issues) == 1
        assert len(result.dimension_stats) == 2
        assert result.tool_calls_count == 5

    def test_partial_failure(self, engine):
        dim_results = [
            DimensionResult(
                dimension_name="bug", display_name="缺陷分析", icon="🔍",
                score="A", issues=[], highlights=[],
                success=True, tool_calls_count=1, duration_seconds=3.0,
            ),
            DimensionResult(
                dimension_name="security", display_name="安全审查", icon="🛡️",
                success=False, error="审查超时",
                tool_calls_count=0, duration_seconds=180.0,
            ),
        ]
        result = engine._merge_results(dim_results)
        assert result.score == "A"
        assert "审查未完成" in result.overall_assessment
        failed_stats = [s for s in result.dimension_stats if not s["success"]]
        assert len(failed_stats) == 1

    def test_all_failed_raises(self, engine):
        dim_results = [
            DimensionResult(
                dimension_name="bug", display_name="缺陷分析", icon="🔍",
                success=False, error="timeout",
            ),
            DimensionResult(
                dimension_name="security", display_name="安全审查", icon="🛡️",
                success=False, error="error",
            ),
        ]
        with pytest.raises(RuntimeError, match="所有审查维度均失败"):
            engine._merge_results(dim_results)


# ---------------------------------------------------------------------------
# _build_overall_assessment
# ---------------------------------------------------------------------------
class TestBuildOverallAssessment:
    def test_successful_dimensions(self):
        successful = [
            DimensionResult(
                dimension_name="bug", display_name="缺陷分析", icon="🔍",
                overall_assessment="代码逻辑清晰",
            ),
            DimensionResult(
                dimension_name="security", display_name="安全审查", icon="🛡️",
                overall_assessment="无安全问题",
            ),
        ]
        result = ReviewEngine._build_overall_assessment(successful, [])
        assert "缺陷分析" in result
        assert "安全审查" in result
        assert "代码逻辑清晰" in result

    def test_failed_dimensions_warning(self):
        successful = [
            DimensionResult(
                dimension_name="bug", display_name="缺陷分析", icon="🔍",
                overall_assessment="ok",
            ),
        ]
        failed = [
            DimensionResult(
                dimension_name="security", display_name="安全审查", icon="🛡️",
                success=False, error="timeout",
            ),
        ]
        result = ReviewEngine._build_overall_assessment(successful, failed)
        assert "审查未完成" in result
        assert "安全审查" in result


# ---------------------------------------------------------------------------
# format_gitlab_comment (multi-dimension)
# ---------------------------------------------------------------------------
class TestFormatGitlabCommentMultiDimension:
    def test_legacy_format_when_no_dimension_stats(self):
        """When dimension_stats is empty, falls back to legacy format."""
        result = ReviewResult(
            overall_assessment="Good",
            score="A",
            issues=[],
            highlights=[],
        )
        comment = ReviewEngine.format_gitlab_comment(result, 5)
        assert "AI 代码审查报告" in comment
        assert "由 AI 代码审查服务自动生成" in comment
        assert "多维度" not in comment

    def test_multi_dimension_format(self):
        """Multi-dimension format includes per-dimension sections and summary table."""
        result = ReviewResult(
            overall_assessment="**🔍 缺陷分析**：ok\n\n**🛡️ 安全审查**：有问题",
            score="B",
            issues=[
                {"file_path": "x.py", "line_start": 10, "severity": "major",
                 "category": "security", "content": "SQL注入", "suggestion": "参数化",
                 "dimension": "security"},
            ],
            highlights=["🔍 好代码"],
            dimension_stats=[
                {"name": "bug", "display_name": "缺陷分析", "icon": "🔍",
                 "score": "A", "issues_count": 0, "tool_calls": 3,
                 "duration": 5.0, "success": True, "error": None, "assessment": "ok"},
                {"name": "security", "display_name": "安全审查", "icon": "🛡️",
                 "score": "C", "issues_count": 1, "tool_calls": 4,
                 "duration": 8.0, "success": True, "error": None, "assessment": "有问题"},
            ],
        )
        comment = ReviewEngine.format_gitlab_comment(result, 10)
        assert "审查维度" in comment
        assert "安全审查" in comment
        assert "SQL注入" in comment
        assert "审查详情" in comment
        assert "多维度并行审查" in comment

    def test_no_issues_dimension_not_expanded(self):
        """Dimensions with no issues should not get expanded issue sections."""
        result = ReviewResult(
            overall_assessment="all good",
            score="A",
            issues=[],
            highlights=[],
            dimension_stats=[
                {"name": "bug", "display_name": "缺陷分析", "icon": "🔍",
                 "score": "A", "issues_count": 0, "tool_calls": 2,
                 "duration": 3.0, "success": True, "error": None, "assessment": "ok"},
            ],
        )
        comment = ReviewEngine.format_gitlab_comment(result, 5)
        # Should not have a "### 🔍 缺陷分析 (评级:" section since no issues
        assert "(评级:" not in comment


# ---------------------------------------------------------------------------
# _parse_changed_lines
# ---------------------------------------------------------------------------
class TestParseChangedLines:
    def test_parse_single_hunk(self):
        diff = "@@ -1,3 +1,4 @@\n+new line\n context\n"
        assert _parse_changed_lines(diff) == [(1, 4)]

    def test_parse_multiple_hunks(self):
        diff = "@@ -1,3 +1,4 @@\n+line\n@@ -20,5 +21,3 @@\n-removed\n"
        assert _parse_changed_lines(diff) == [(1, 4), (21, 23)]

    def test_parse_count_omitted(self):
        diff = "@@ -1 +1 @@\n+single\n"
        assert _parse_changed_lines(diff) == [(1, 1)]

    def test_parse_pure_delete(self):
        diff = "@@ -5,3 +5,0 @@\n-deleted\n"
        assert _parse_changed_lines(diff) == []

    def test_parse_empty_diff(self):
        assert _parse_changed_lines("") == []

    def test_parse_no_hunks(self):
        assert _parse_changed_lines("just some text without hunk headers") == []

    def test_parse_real_world_diff(self):
        diff = (
            "--- a/src/core/engine.py\n"
            "+++ b/src/core/engine.py\n"
            "@@ -10,6 +10,8 @@ class Engine:\n"
            "     def __init__(self):\n"
            "+        self.x = 1\n"
            "+        self.y = 2\n"
            "         pass\n"
            "@@ -50,4 +52,5 @@ class Engine:\n"
            "     def run(self):\n"
            "+        return True\n"
            "         pass\n"
        )
        assert _parse_changed_lines(diff) == [(10, 17), (52, 56)]


# ---------------------------------------------------------------------------
# _ranges_overlap
# ---------------------------------------------------------------------------
class TestRangesOverlap:
    def test_overlap_exact(self):
        assert _ranges_overlap(5, 10, [(5, 10)]) is True

    def test_overlap_partial(self):
        assert _ranges_overlap(5, 10, [(8, 15)]) is True

    def test_no_overlap(self):
        assert _ranges_overlap(5, 10, [(11, 20)]) is False

    def test_node_contains_range(self):
        assert _ranges_overlap(1, 100, [(50, 60)]) is True

    def test_range_contains_node(self):
        assert _ranges_overlap(50, 60, [(1, 100)]) is True

    def test_adjacent_no_overlap(self):
        assert _ranges_overlap(5, 10, [(11, 15)]) is False

    def test_multiple_ranges_one_overlaps(self):
        assert _ranges_overlap(50, 60, [(1, 10), (55, 65)]) is True

    def test_empty_ranges(self):
        assert _ranges_overlap(5, 10, []) is False


# ---------------------------------------------------------------------------
# _build_blast_radius_context
# ---------------------------------------------------------------------------
class TestBuildBlastRadiusContext:
    @pytest.fixture
    def engine(self):
        with patch("src.core.review_engine.LLMClient"):
            with patch("src.core.review_engine.AgentToolExecutor"):
                return ReviewEngine("/fake/repo")

    @pytest.fixture
    def changes(self):
        return [
            {"new_path": "src/core/payment.py", "diff": "+code"},
            {"new_path": "src/api/orders.py", "diff": "+more"},
        ]

    def _make_blast_radius(self, target, location, callers=None, callees=None):
        from src.core.code_graph.query import BlastRadius
        return BlastRadius(
            target=target,
            target_location=location,
            upstream_callers=callers or [],
            downstream_callees=callees or [],
        )

    def test_returns_empty_when_disabled(self, engine, changes):
        """配置关闭时返回空字符串。"""
        engine.code_graph = MagicMock()
        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.BLAST_RADIUS_PRE_INJECT = False
            result = engine._build_blast_radius_context(changes)
        assert result == ""

    def test_returns_empty_when_no_graph(self, engine, changes):
        """code_graph=None 时返回空。"""
        engine.code_graph = None
        result = engine._build_blast_radius_context(changes)
        assert result == ""

    def test_returns_empty_when_graph_unavailable(self, engine, changes):
        """代码图为空时返回空。"""
        mock_graph = MagicMock()
        mock_graph.available.return_value = False
        engine.code_graph = mock_graph
        result = engine._build_blast_radius_context(changes)
        assert result == ""

    def test_formats_blast_radius(self, engine, changes):
        """正常格式化，包含上下游信息。"""
        br = self._make_blast_radius(
            "function process_payment",
            "src/core/payment.py:42",
            callers=[{"name": "handle_order", "location": "src/api/orders.py:88"}],
            callees=[{"name": "validate_card"}, {"name": "send_to_gateway"}],
        )
        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.get_blast_radius.return_value = [br]
        engine.code_graph = mock_graph

        result = engine._build_blast_radius_context(changes)
        assert "变更影响面分析" in result
        assert "process_payment" in result
        assert "handle_order" in result
        assert "validate_card" in result

    def test_truncates_at_max_chars(self, engine, changes):
        """超长文本被截断。"""
        br = self._make_blast_radius(
            "function long_name_" + "x" * 200,
            "src/core/payment.py:1",
            callers=[{"name": f"caller_{i}", "location": f"file.py:{i}"} for i in range(10)],
        )
        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.get_blast_radius.return_value = [br]
        engine.code_graph = mock_graph

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.BLAST_RADIUS_PRE_INJECT = True
            mock_settings.BLAST_RADIUS_MAX_ITEMS = 30
            mock_settings.BLAST_RADIUS_MAX_CHARS = 100  # 极小值强制截断
            result = engine._build_blast_radius_context(changes)

        assert "影响面信息已截断" in result
        # 截断前文本 + 截断后缀不超过 MAX_CHARS + 后缀长度
        assert len(result) < 200

    def test_max_items_limit(self, engine):
        """符号数量上限生效。"""
        changes = [{"new_path": f"file_{i}.py", "diff": "+x"} for i in range(10)]
        brs = [
            self._make_blast_radius(f"function func_{i}", f"file_{i}.py:{i}")
            for i in range(5)
        ]
        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.get_blast_radius.return_value = brs
        engine.code_graph = mock_graph

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.BLAST_RADIUS_PRE_INJECT = True
            mock_settings.BLAST_RADIUS_MAX_ITEMS = 3
            mock_settings.BLAST_RADIUS_MAX_CHARS = 10000
            result = engine._build_blast_radius_context(changes)

        # 最多 3 个符号，第一个文件返回 5 个但只取前 3 个
        assert result.count("**function func_") == 3

    def test_exception_per_file_non_blocking(self, engine, changes):
        """单文件异常不阻塞其他。"""
        br = self._make_blast_radius("function ok_func", "src/api/orders.py:1")
        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.get_blast_radius.side_effect = [
            RuntimeError("parse error"),  # 第一个文件失败
            [br],                         # 第二个文件成功
        ]
        engine.code_graph = mock_graph

        result = engine._build_blast_radius_context(changes)
        assert "ok_func" in result

    def test_blast_radius_before_diffs(self, engine, changes):
        """影响面文本出现在 diff 之前。"""
        br = self._make_blast_radius(
            "function process_payment", "src/core/payment.py:42",
        )
        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.get_blast_radius.return_value = [br]
        engine.code_graph = mock_graph

        blast_ctx = engine._build_blast_radius_context(changes)
        prompt = engine._build_diff_prompt(changes, blast_ctx)

        blast_pos = prompt.index("变更影响面分析")
        diff_pos = prompt.index("```diff")
        assert blast_pos < diff_pos

    def test_only_affected_symbols(self, engine):
        """验证仅分析与 diff 行重叠的符号。"""
        from src.core.code_graph.store import NodeRow
        diff_with_hunks = "@@ -10,3 +10,5 @@\n+new line\n context\n"
        changes = [{"new_path": "src/app.py", "diff": diff_with_hunks}]

        # 3 个节点：只有 func_b (行 8-15) 与变更范围 (10-14) 重叠
        node_a = NodeRow(id=1, path="src/app.py", kind="function", name="func_a",
                         line_start=1, line_end=5, signature="def func_a()")
        node_b = NodeRow(id=2, path="src/app.py", kind="function", name="func_b",
                         line_start=8, line_end=15, signature="def func_b()")
        node_c = NodeRow(id=3, path="src/app.py", kind="function", name="func_c",
                         line_start=20, line_end=30, signature="def func_c()")

        br_b = self._make_blast_radius("function func_b", "src/app.py:8")

        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.store.find_nodes_by_path.return_value = [node_a, node_b, node_c]
        mock_graph.get_blast_radius_for_nodes.return_value = [br_b]
        engine.code_graph = mock_graph

        result = engine._build_blast_radius_context(changes)

        # 应该调用 get_blast_radius_for_nodes 而非 get_blast_radius
        mock_graph.get_blast_radius_for_nodes.assert_called_once()
        mock_graph.get_blast_radius.assert_not_called()

        # 传入的 affected_nodes 应该只有 node_b
        call_args = mock_graph.get_blast_radius_for_nodes.call_args
        affected_nodes = call_args[0][1] if len(call_args[0]) > 1 else call_args[1]["nodes"]
        assert len(affected_nodes) == 1
        assert affected_nodes[0].name == "func_b"

        assert "func_b" in result

    def test_fallback_when_no_hunks(self, engine):
        """diff 无 hunk header 时 fallback 到全文件分析。"""
        changes = [{"new_path": "src/app.py", "diff": "+just added text"}]

        br = self._make_blast_radius("function fallback_func", "src/app.py:1")
        mock_graph = MagicMock()
        mock_graph.available.return_value = True
        mock_graph.get_blast_radius.return_value = [br]
        engine.code_graph = mock_graph

        result = engine._build_blast_radius_context(changes)

        # 应该调用 get_blast_radius（fallback）
        mock_graph.get_blast_radius.assert_called_once()
        mock_graph.get_blast_radius_for_nodes.assert_not_called()
        assert "fallback_func" in result


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------
class TestTokenUsage:
    def test_token_usage_iadd(self):
        """TokenUsage += operator works correctly."""
        a = TokenUsage(prompt_tokens=100, completion_tokens=50, cached_tokens=20)
        b = TokenUsage(prompt_tokens=200, completion_tokens=100, cached_tokens=40)
        a += b
        assert a.prompt_tokens == 300
        assert a.completion_tokens == 150
        assert a.cached_tokens == 60
        assert a.total_tokens == 450

    def test_token_usage_total(self):
        u = TokenUsage(prompt_tokens=100, completion_tokens=50, cached_tokens=30)
        assert u.total_tokens == 150

    def test_cached_tokens_extraction(self):
        """ChatResult correctly extracts cached_tokens from API response."""
        cr = _make_chat_result(
            content="test", prompt_tokens=500, completion_tokens=200, cached_tokens=150,
        )
        assert cr.usage.cached_tokens == 150
        assert cr.usage.prompt_tokens == 500
        assert cr.usage.total_tokens == 700

    @pytest.mark.asyncio
    async def test_parse_with_retry_accumulates_tokens(self):
        """_parse_with_retry accumulates tokens across retries."""
        mock_llm = MagicMock()
        bad_content = "no json"
        good_json = json.dumps({
            "overall_assessment": "ok", "score": "B", "issues": [], "highlights": [],
        })

        mock_llm.chat = AsyncMock(side_effect=[
            _make_chat_result(content="still bad", prompt_tokens=100, completion_tokens=50, cached_tokens=10),
            _make_chat_result(content=good_json, prompt_tokens=120, completion_tokens=60, cached_tokens=15),
        ])

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 3
            result, usage = await _parse_with_retry(mock_llm, bad_content, [], tag="")

        assert result.score == "B"
        assert usage.prompt_tokens == 220
        assert usage.completion_tokens == 110
        assert usage.cached_tokens == 25

    def test_dimension_result_has_token_fields(self):
        """DimensionResult includes token tracking fields."""
        dr = DimensionResult(
            dimension_name="bug",
            prompt_tokens=500,
            completion_tokens=200,
            cached_tokens=100,
        )
        assert dr.prompt_tokens == 500
        assert dr.completion_tokens == 200
        assert dr.cached_tokens == 100

    def test_merge_results_aggregates_tokens(self):
        """_merge_results sums token usage across dimensions."""
        with patch("src.core.review_engine.LLMClient"):
            with patch("src.core.review_engine.AgentToolExecutor"):
                engine = ReviewEngine("/fake/repo")

        dim_results = [
            DimensionResult(
                dimension_name="bug", display_name="缺陷分析", icon="🔍",
                score="A", issues=[], highlights=["good"],
                success=True, tool_calls_count=3, duration_seconds=5.0,
                prompt_tokens=1000, completion_tokens=500, cached_tokens=200,
            ),
            DimensionResult(
                dimension_name="security", display_name="安全审查", icon="🛡️",
                score="B", issues=[{
                    "file_path": "x.py", "severity": "major",
                    "category": "security", "content": "问题",
                    "dimension": "security",
                }],
                highlights=[], success=True, tool_calls_count=2, duration_seconds=4.0,
                prompt_tokens=800, completion_tokens=400, cached_tokens=150,
            ),
        ]
        result = engine._merge_results(dim_results)
        assert result.total_prompt_tokens == 1800
        assert result.total_completion_tokens == 900
        assert result.total_cached_tokens == 350
        # Check dimension stats contain token info
        for stat in result.dimension_stats:
            assert "prompt_tokens" in stat
            assert "completion_tokens" in stat
            assert "cached_tokens" in stat

    def test_review_result_has_model_token_fields(self):
        """ReviewResult includes cloud/local model token fields."""
        result = ReviewResult(
            total_prompt_tokens=2000,
            total_completion_tokens=800,
            total_cached_tokens=400,
            cloud_prompt_tokens=1500,
            cloud_completion_tokens=600,
            cloud_cached_tokens=300,
            local_prompt_tokens=500,
            local_completion_tokens=200,
        )
        assert result.cloud_prompt_tokens == 1500
        assert result.local_prompt_tokens == 500
