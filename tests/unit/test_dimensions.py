"""Tests for src/core/dimensions.py — dimension definitions and helpers."""

from unittest.mock import patch

import pytest

from src.core.dimensions import (
    ReviewDimension,
    build_dimension_prompt,
    build_format_retry_prompt,
    get_all_dimensions,
    get_enabled_dimensions,
)


class TestDimensionDefinitions:
    def test_five_dimensions_defined(self):
        dims = get_all_dimensions()
        assert len(dims) == 5

    def test_dimension_names(self):
        names = {d.name for d in get_all_dimensions()}
        assert names == {"bug", "security", "performance", "quality", "architecture"}

    def test_weights_sum_to_one(self):
        total = sum(d.weight for d in get_all_dimensions())
        assert abs(total - 1.0) < 1e-9

    def test_each_dimension_has_categories(self):
        for d in get_all_dimensions():
            assert len(d.categories) > 0, f"{d.name} has no categories"

    def test_dimensions_are_frozen(self):
        d = get_all_dimensions()[0]
        with pytest.raises(AttributeError):
            d.name = "changed"


class TestBuildDimensionPrompt:
    def test_contains_common_suffix(self):
        for d in get_all_dimensions():
            prompt = build_dimension_prompt(d)
            assert "输出格式" in prompt
            assert "评级标准" in prompt

    def test_contains_dimension_specific_content(self):
        dims = get_all_dimensions()
        bug_dim = next(d for d in dims if d.name == "bug")
        prompt = build_dimension_prompt(bug_dim)
        assert "空指针" in prompt or "Off-by-one" in prompt

        sec_dim = next(d for d in dims if d.name == "security")
        prompt = build_dimension_prompt(sec_dim)
        assert "注入漏洞" in prompt or "OWASP" in prompt


class TestBuildFormatRetryPrompt:
    def test_default_all_categories(self):
        prompt = build_format_retry_prompt()
        assert "bug/security/performance/maintainability/style/design" in prompt

    def test_specific_categories(self):
        prompt = build_format_retry_prompt(["bug"])
        assert '"bug"' in prompt or "bug" in prompt
        # Should not contain the full default list
        assert "bug/security/performance" not in prompt

    def test_multiple_categories(self):
        prompt = build_format_retry_prompt(["maintainability", "style"])
        assert "maintainability/style" in prompt

    def test_has_format_placeholders(self):
        prompt = build_format_retry_prompt()
        assert "{error_type}" in prompt
        assert "{error_detail}" in prompt


class TestGetEnabledDimensions:
    def test_default_all_enabled(self):
        dims = get_enabled_dimensions()
        assert len(dims) == 5

    @patch("src.core.dimensions.settings")
    def test_filter_by_config(self, mock_settings):
        mock_settings.enabled_dimensions = ["bug", "security"]
        dims = get_enabled_dimensions()
        assert len(dims) == 2
        names = {d.name for d in dims}
        assert names == {"bug", "security"}

    @patch("src.core.dimensions.settings")
    def test_empty_returns_none(self, mock_settings):
        mock_settings.enabled_dimensions = []
        dims = get_enabled_dimensions()
        assert len(dims) == 0

    @patch("src.core.dimensions.settings")
    def test_unknown_dimension_ignored(self, mock_settings):
        mock_settings.enabled_dimensions = ["bug", "nonexistent"]
        dims = get_enabled_dimensions()
        assert len(dims) == 1
        assert dims[0].name == "bug"
