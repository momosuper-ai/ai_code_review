"""Tests for src/core/dimensions.py — dimension definitions and helpers."""

from unittest.mock import patch

import pytest

from src.core.dimensions import (
    ReviewDimension,
    build_dimension_json_schema,
    build_dimension_prompt,
    build_format_retry_prompt,
    build_response_format,
    build_single_agent_json_schema,
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


class TestJsonSchemaBuilders:
    def test_dimension_schema_strict_true(self):
        s = build_dimension_json_schema(get_all_dimensions()[0])
        assert s["type"] == "json_schema"
        assert s["json_schema"]["strict"] is True

    def test_dimension_schema_required_complete(self):
        # OpenAI strict 要求 required 必须等于 properties 全集
        s = build_dimension_json_schema(get_all_dimensions()[0])["json_schema"]["schema"]
        assert set(s["required"]) == set(s["properties"].keys())
        item = s["properties"]["issues"]["items"]
        assert set(item["required"]) == set(item["properties"].keys())

    def test_dimension_schema_additional_properties_false(self):
        s = build_dimension_json_schema(get_all_dimensions()[0])["json_schema"]["schema"]
        assert s["additionalProperties"] is False
        assert s["properties"]["issues"]["items"]["additionalProperties"] is False

    def test_bug_dim_category_enum_isolated(self):
        bug = next(d for d in get_all_dimensions() if d.name == "bug")
        s = build_dimension_json_schema(bug)["json_schema"]["schema"]
        assert s["properties"]["issues"]["items"]["properties"]["category"]["enum"] == ["bug"]

    def test_quality_dim_category_enum(self):
        q = next(d for d in get_all_dimensions() if d.name == "quality")
        s = build_dimension_json_schema(q)["json_schema"]["schema"]
        cats = s["properties"]["issues"]["items"]["properties"]["category"]["enum"]
        assert cats == ["maintainability", "style"]

    def test_line_start_nullable(self):
        s = build_dimension_json_schema(get_all_dimensions()[0])["json_schema"]["schema"]
        ls = s["properties"]["issues"]["items"]["properties"]["line_start"]["type"]
        assert ls == ["integer", "null"]

    def test_severity_enum(self):
        s = build_dimension_json_schema(get_all_dimensions()[0])["json_schema"]["schema"]
        sev = s["properties"]["issues"]["items"]["properties"]["severity"]["enum"]
        assert sev == ["critical", "major", "minor", "info"]

    def test_score_enum(self):
        s = build_dimension_json_schema(get_all_dimensions()[0])["json_schema"]["schema"]
        assert s["properties"]["score"]["enum"] == ["A", "B", "C", "D"]

    def test_single_agent_schema_all_categories(self):
        s = build_single_agent_json_schema()["json_schema"]["schema"]
        cats = s["properties"]["issues"]["items"]["properties"]["category"]["enum"]
        assert set(cats) == {"bug", "security", "performance", "maintainability", "style", "design"}

    def test_schema_name_unique_per_dimension(self):
        names = {build_dimension_json_schema(d)["json_schema"]["name"] for d in get_all_dimensions()}
        assert len(names) == 5
        assert all(n.startswith("review_") for n in names)

    def test_build_response_format_off(self):
        assert build_response_format("off") is None
        assert build_response_format("off", build_single_agent_json_schema) is None

    def test_build_response_format_json_object(self):
        assert build_response_format("json_object") == {"type": "json_object"}

    def test_build_response_format_json_schema_with_builder(self):
        rf = build_response_format("json_schema", build_single_agent_json_schema)
        assert rf["type"] == "json_schema"
        assert rf["json_schema"]["name"] == "review_full"

    def test_build_response_format_json_schema_without_builder_falls_back(self):
        # 缺少 builder 时降级到 json_object，保证不会传一个无效结构
        assert build_response_format("json_schema", None) == {"type": "json_object"}

    def test_build_response_format_unknown_mode_returns_none(self):
        assert build_response_format("invalid_mode") is None
