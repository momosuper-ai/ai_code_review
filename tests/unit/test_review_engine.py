"""Tests for ReviewEngine: _try_parse, _fallback_result, _parse_with_retry, multi-agent."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.review_engine import (
    DimensionResult,
    ReviewEngine,
    ReviewResult,
    _parse_with_retry,
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
        content = '{"overall_assessment": "ok", "score": "A",}'  # trailing comma
        result, err_type, err_detail = ReviewEngine._try_parse(content)
        assert result is None
        assert err_type == "json_syntax_error"
        assert err_detail  # should contain JSONDecodeError message

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
        result = await _parse_with_retry(mock_llm, valid_json, [], tag="[test] ")
        assert result.score == "A"
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
        result = await engine._parse_with_retry(valid_json, [], tag="[test] ")
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

        mock_response = MagicMock()
        mock_response.content = good_json
        mock_llm.chat = AsyncMock(return_value=mock_response)

        result = await _parse_with_retry(mock_llm, bad_content, [], tag="[test] ")
        assert result.score == "B"
        assert result.overall_assessment == "Fixed"
        assert mock_llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_all_retries_fail_uses_fallback(self, mock_llm):
        bad_content = "still no json"

        mock_response = MagicMock()
        mock_response.content = "also no json"
        mock_llm.chat = AsyncMock(return_value=mock_response)

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 2
            result = await _parse_with_retry(mock_llm, bad_content, [], tag="[test] ")

        assert "审查结果格式解析失败" in result.overall_assessment
        assert mock_llm.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_llm_exception_does_not_break_retry(self, mock_llm):
        bad_content = "no json"
        good_json = json.dumps({
            "overall_assessment": "Recovered",
            "score": "A",
            "issues": [],
            "highlights": [],
        })

        mock_good = MagicMock()
        mock_good.content = good_json
        mock_llm.chat = AsyncMock(
            side_effect=[Exception("LLM down"), mock_good],
        )

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 3
            result = await _parse_with_retry(mock_llm, bad_content, [], tag="[test] ")

        assert result.score == "A"
        assert result.overall_assessment == "Recovered"

    @pytest.mark.asyncio
    async def test_temperature_decreases(self, mock_llm):
        bad_content = "no json"

        mock_response = MagicMock()
        mock_response.content = "still bad"
        mock_llm.chat = AsyncMock(return_value=mock_response)

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

        mock_response = MagicMock()
        mock_response.content = json.dumps({
            "overall_assessment": "ok",
            "score": "B",
            "issues": [],
            "highlights": [],
        })
        mock_llm.chat = AsyncMock(return_value=mock_response)

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
        mock_response = MagicMock()
        mock_response.content = good_json
        mock_llm.chat = AsyncMock(return_value=mock_response)

        result = await _parse_with_retry(
            mock_llm, bad_content, [], tag="",
            categories=["bug"],
        )
        assert result.score == "B"
        # The retry message should contain "bug" as category
        call_args = mock_llm.chat.call_args
        retry_msgs = call_args[0][0] if call_args[0] else call_args[1]["messages"]
        user_msg = [m for m in retry_msgs if isinstance(m, dict) and m.get("role") == "user"]
        assert any("bug" in m.get("content", "") for m in user_msg)


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
