"""Verify repaired scan behavior and keep diagnostic probes bounded/safe."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage

from scripts.brain_privacy_diagnostic import Meter, mixed_rows, offline_diagnosis, real_diagnosis, synthetic_rows
from src.config import settings
from src.core.llm_client import LLMClient
from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain.compiler import CompilationGuard, encoded


@pytest.fixture(scope="module")
def offline() -> dict:
    """Run actual scanner/task paths with deterministic substitutes once."""
    return asyncio.run(offline_diagnosis())


def test_perfect_model_still_requires_serial_project_scale_calls(offline: dict) -> None:
    assert offline["trial_counts"]["calls"] < 456
    assert offline["trial_counts"]["max_concurrent_calls"] == 1
    # Empty observations need no content scans. Complete source paths are now
    # classified together too, instead of silently skipping path privacy.
    assert offline["empty_observations"]["calls"] <= 5


def test_truncation_fails_closed_and_retry_repeats_completed_work(offline: dict) -> None:
    assert offline["truncated_json"] == {"exception": "PrivacyResponseInvalid", "reason": "invalid_json", "output_returned": False}
    assert offline["retry_checkpoint"]["calls_each"][0] == 4
    assert 0 < offline["retry_checkpoint"]["calls_each"][1] < 4
    assert offline["retry_checkpoint"]["completed_calls_repeated"] is False
    assert offline["retry_checkpoint"]["completed_calls_reused"] == 3


def test_previous_character_boundary_is_now_scanned_complete_with_tail_secret(offline: dict) -> None:
    assert offline["character_boundary"] == {
        "calls": 1, "complete_field_seen": True, "secret_seen_intact": True, "sanitized_secret": True,
        "output_returned": True, "exceeds_normal_packet": True, "within_field_limit": True,
    }


def test_field_above_new_hard_ceiling_is_rejected_before_model_call(offline: dict) -> None:
    assert offline["hard_field_limit"] == {
        "calls": 0, "oversized_scalar_rejected": True, "output_returned": False,
    }


def test_synthetic_model_accuracy_fixture_is_not_already_removed_by_rules() -> None:
    rows, values = synthetic_rows(100)
    text = encoded(rows)
    assert len(text) <= 6000
    assert PrivacyFilter().sanitize_text(text) == text
    assert len(set(values)) == 100
    mixed, mixed_values, negatives = mixed_rows()
    assert PrivacyFilter().sanitize_text(encoded(mixed)) == encoded(mixed)
    assert len(mixed_values) == 6
    assert len(negatives) == 8


def test_real_diagnostic_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BRAIN_LOCAL_REAL_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="BRAIN_LOCAL_REAL_MODEL=1"):
        asyncio.run(real_diagnosis())


def test_diagnostic_budget_limits_are_enforced() -> None:
    meter = Meter(max_calls=1, max_tokens=100)
    meter.usage = 80
    with pytest.raises(RuntimeError, match="DiagnosticBudgetExceeded"):
        meter.reserve(21)
    meter.calls = 1
    with pytest.raises(RuntimeError, match="DiagnosticBudgetExceeded"):
        meter.reserve(1)


def test_sdk_observer_records_truncation_metadata_without_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")

    async def scenario() -> tuple[dict, object]:
        meter = Meter()
        guard = CompilationGuard(meter.account, meter.reserve)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=2048, prompt_tokens_details=None,
                                completion_tokens_details=SimpleNamespace(reasoning_tokens=1000))
        response = SimpleNamespace(usage=usage, choices=[SimpleNamespace(finish_reason="length",
                                  message=ChatCompletionMessage(role="assistant", content='{"items":[{"value":"fixture', reasoning="synthetic thought"))])

        async def create(**kwargs: object) -> object:
            return response

        monkeypatch.setattr(guard.local._client.chat.completions, "create", create)
        records = meter.attach(guard, 2048, [], [])
        with pytest.raises(ValueError, match="输出仍被截断"):
            await guard.clean('["fixture"]')
        # Preserve provider metadata independently of JSON parsing.
        plain = LLMClient()
        monkeypatch.setattr(plain._client.chat.completions, "create", create)
        result = await plain.chat([{"role": "user", "content": "synthetic"}], max_tokens=2048)
        await plain.aclose()
        await guard.close()
        return records[0], result

    record, result = asyncio.run(scenario())
    assert record["finish_reason"] == "length"
    assert record["reasoning_tokens"] == 1000
    assert record["json_valid"] is False
    assert "fixture" not in json.dumps(record)
    assert result.finish_reason == "length"
    assert result.reasoning_tokens == 1000
    assert record["reasoning_chars"] == result.reasoning_chars == len("synthetic thought")
