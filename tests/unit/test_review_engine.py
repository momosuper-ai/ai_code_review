"""Tests for ReviewEngine: _try_parse, _fallback_result, _parse_with_retry."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.review_engine import ReviewEngine, ReviewResult


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
# _parse_with_retry
# ---------------------------------------------------------------------------
class TestParseWithRetry:
    @pytest.fixture
    def engine(self):
        with patch("src.core.review_engine.LLMClient"):
            with patch("src.core.review_engine.AgentToolExecutor"):
                engine = ReviewEngine("/fake/repo")
                engine.llm = MagicMock()
                return engine

    @pytest.mark.asyncio
    async def test_success_on_first_try(self, engine):
        valid_json = json.dumps({
            "overall_assessment": "Good",
            "score": "A",
            "issues": [],
            "highlights": [],
        })
        result = await engine._parse_with_retry(valid_json, [], tag="[test] ")
        assert result.score == "A"
        # LLM should not be called at all
        engine.llm.chat.assert_not_called()

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(self, engine):
        bad_content = "no json here"
        good_json = json.dumps({
            "overall_assessment": "Fixed",
            "score": "B",
            "issues": [],
            "highlights": [],
        })

        mock_response = MagicMock()
        mock_response.content = good_json
        engine.llm.chat = AsyncMock(return_value=mock_response)

        result = await engine._parse_with_retry(bad_content, [], tag="[test] ")
        assert result.score == "B"
        assert result.overall_assessment == "Fixed"
        assert engine.llm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_all_retries_fail_uses_fallback(self, engine):
        bad_content = "still no json"

        mock_response = MagicMock()
        mock_response.content = "also no json"
        engine.llm.chat = AsyncMock(return_value=mock_response)

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 2
            result = await engine._parse_with_retry(bad_content, [], tag="[test] ")

        assert "审查结果格式解析失败" in result.overall_assessment
        assert engine.llm.chat.call_count == 2

    @pytest.mark.asyncio
    async def test_llm_exception_does_not_break_retry(self, engine):
        bad_content = "no json"
        good_json = json.dumps({
            "overall_assessment": "Recovered",
            "score": "A",
            "issues": [],
            "highlights": [],
        })

        mock_good = MagicMock()
        mock_good.content = good_json
        engine.llm.chat = AsyncMock(
            side_effect=[Exception("LLM down"), mock_good],
        )

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 3
            result = await engine._parse_with_retry(bad_content, [], tag="[test] ")

        assert result.score == "A"
        assert result.overall_assessment == "Recovered"

    @pytest.mark.asyncio
    async def test_temperature_decreases(self, engine):
        bad_content = "no json"

        mock_response = MagicMock()
        mock_response.content = "still bad"
        engine.llm.chat = AsyncMock(return_value=mock_response)

        with patch("src.core.review_engine.settings") as mock_settings:
            mock_settings.AGENT_FORMAT_RETRY_MAX = 3
            await engine._parse_with_retry(bad_content, [], tag="")

        temps = [call.kwargs["temperature"] for call in engine.llm.chat.call_args_list]
        assert temps == [0.1, 0.05, 0.0]

    @pytest.mark.asyncio
    async def test_messages_not_mutated(self, engine):
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
        engine.llm.chat = AsyncMock(return_value=mock_response)

        await engine._parse_with_retry(bad_content, original_messages, tag="")
        assert len(original_messages) == original_len


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
