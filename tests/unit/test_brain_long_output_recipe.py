"""Long-field and semantic recovery limits invalidate compilation recipes."""

import pytest

from src.config import settings
from src.core.project_brain.lifecycle import recipe_hash


@pytest.mark.parametrize("field", ["PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS", "PROJECT_BRAIN_LLM_RETRY_MAX_TOKENS"])
def test_complete_field_and_output_recovery_limits_are_recipe_inputs(monkeypatch, field: str) -> None:
    original = recipe_hash()
    monkeypatch.setattr(settings, field, getattr(settings, field) + 1000)
    assert recipe_hash() != original


def test_semantic_protocol_override_is_a_recipe_input(monkeypatch) -> None:
    original = recipe_hash()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_EXTRA_BODY", '{"reasoning_effort":"none"}')
    assert recipe_hash() != original


def test_semantic_output_transport_is_a_recipe_input(monkeypatch) -> None:
    original = recipe_hash()
    monkeypatch.setattr(settings, "PROJECT_BRAIN_SEMANTIC_OUTPUT_MODE", "strict_tool")
    assert recipe_hash() != original
