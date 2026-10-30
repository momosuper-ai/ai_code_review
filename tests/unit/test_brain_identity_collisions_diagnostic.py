"""Offline controls for two bounded, wholly synthetic identity-collision probes."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage

from scripts.brain_privacy_diagnostic import Meter, identity_collision_rows, real_diagnosis
from src.config import settings
from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain.compiler import encoded


@pytest.fixture
def identity_probe(monkeypatch):
    monkeypatch.setenv("BRAIN_LOCAL_REAL_MODEL", "1")
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    monkeypatch.setattr(settings, "PRIVACY_INTERNAL_DOMAINS", "")
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS", 6000)
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET", 24)
    marker = "UNCLASSIFIED_PROVIDER_METADATA_MUST_NOT_EXPORT"
    monkeypatch.setattr(settings, "LLM_EXTRA_BODY", encoded({marker: True}))
    monkeypatch.setattr(settings, "PROJECT_BRAIN_PRIVACY_EXTRA_BODY", "")
    sent, meters = [], []
    attach = Meter.attach

    def install(factory):
        def attach_mock(meter, guard, output, positives, negatives, **kwargs):
            meters.append(meter)
            async def create(**request):
                sent.append({"call": len(sent) + 1, "max_tokens": request["max_tokens"]})
                result = factory(positives, negatives, len(sent))
                if isinstance(result, Exception):
                    raise result
                if result == "malformed_response":
                    return SimpleNamespace(choices=[], usage=None)
                content, finish = result
                return SimpleNamespace(model=marker, usage=SimpleNamespace(
                    prompt_tokens=80, completion_tokens=20, prompt_tokens_details=None,
                    completion_tokens_details=SimpleNamespace(reasoning_tokens=0)),
                    choices=[SimpleNamespace(finish_reason=finish, message=ChatCompletionMessage(
                        role="assistant", content=content, **{marker: marker}))])
            monkeypatch.setattr(guard.local._client.chat.completions, "create", create)
            return attach(meter, guard, output, positives, negatives, **kwargs)
        monkeypatch.setattr(Meter, "attach", attach_mock)
    return SimpleNamespace(install=install, sent=sent, meters=meters, marker=marker)


def test_fixture_is_eight_ascii_letters_and_only_positive_has_credential_value():
    negative, negative_values, negative_names, negative_paths = identity_collision_rows(False)
    positive, positive_values, positive_names, positive_paths = identity_collision_rows(True)
    assert len(negative_names) == len(positive_values) == 1
    value = positive_values[0]
    assert len(value) == 8 and value.isascii() and value.isidentifier()
    assert negative_names == positive_values
    assert negative_values == positive_names == []
    assert negative_paths == positive_paths and len(negative_paths) == 2
    assert positive[:-1] == negative
    assert "实际完整值" in positive[-1]["说明"]
    for rows in (negative, positive):
        assert PrivacyFilter().sanitize_text(encoded(rows)) == encoded(rows)


def test_valid_pipeline_distinguishes_names_from_actual_values_and_path_collision(identity_probe, capsys):
    def correct(positives, negatives, count):
        return encoded({"items": [{"value": value, "type": "SECRET"} for value in positives]}), "stop"
    identity_probe.install(correct)
    report = asyncio.run(real_diagnosis("identity_collisions"))
    negative, positive = report["cases"]
    assert report["max_calls"] == report["attempted_sdk_calls"] == len(identity_probe.sent) == 2
    assert report["max_tokens"] == 10000 and report["actual_tokens"] == 200
    assert all(case["pipeline_ok"] and case["detection_ok"] for case in (negative, positive))
    assert negative["false_positive_findings"] == negative["paths_modified"] == 0
    assert negative["negative_identifiers_flagged"] == 0
    assert negative["path_gate_would_reject"] is False
    assert positive["sanitized_positives"] == 1
    assert positive["exact_positive_findings"] == 1 and positive["overbroad_positive_findings"] == 0
    assert positive["exact_value_detection_ok"] is True
    assert negative["exact_value_detection_ok"] is None
    assert positive["paths_modified"] == positive["path_probe_count"] == 2
    assert positive["path_gate_would_reject"] is True
    assert report["extra_body_key_count"] == 1 and "extra_body_keys" not in report
    assert report["diagnostic_limits"] == {"synthetic_only": True, "planned_cases": 2,
        "existing_finding_evaluated": False, "production_accuracy_established": False,
        "pass_means_limited_sample_behavior_only": True, "identifier_shape_is_not_exemption": True,
        "authorizes_build_retry": False}
    exported = json.dumps(report) + capsys.readouterr().out
    _, values, _, paths = identity_collision_rows(True)
    assert all(value not in exported for value in [*values, *paths, identity_probe.marker])
    assert all("response_model" not in call and "message_field_names" not in call
               for case in report["cases"] for call in case["calls"])


def test_names_false_positive_is_not_hidden_by_empty_positive_denominator(identity_probe):
    identity_probe.install(lambda positives, negatives, count: (
        encoded({"items": [{"value": "maplefox", "type": "SECRET"}]}), "stop"))
    negative, positive = asyncio.run(real_diagnosis("identity_collisions"))["cases"]
    assert negative["pipeline_ok"] is True and negative["detection_ok"] is False
    assert negative["false_positive_findings"] == negative["negative_identifiers_flagged"] == 1
    assert negative["paths_modified"] == 2
    assert positive["pipeline_ok"] is True and positive["detection_ok"] is True


def test_empty_legal_json_is_pipeline_success_but_positive_detection_failure(identity_probe):
    identity_probe.install(lambda *args: ('{"items":[]}', "stop"))
    negative, positive = asyncio.run(real_diagnosis("identity_collisions"))["cases"]
    assert negative["pipeline_ok"] and negative["detection_ok"]
    assert positive["pipeline_ok"] and positive["detection_ok"] is False
    assert positive["sanitized_positives"] == positive["paths_modified"] == 0
    assert positive["exact_value_detection_ok"] is False


def test_overbroad_paragraphs_and_paths_can_protect_packet_but_fail_exact_detection(identity_probe):
    def overbroad(positives, negatives, count):
        fixture, _, _, _ = identity_collision_rows(bool(positives))
        identifier = (positives or negatives)[0]
        returned = [text for row in fixture for text in row.values() if identifier in text]
        return encoded({"items": [{"value": text, "type": "SECRET"} for text in returned]}), "stop"
    identity_probe.install(overbroad)
    negative, positive = asyncio.run(real_diagnosis("identity_collisions"))["cases"]
    assert negative["pipeline_ok"] is True and negative["detection_ok"] is False
    assert positive["pipeline_ok"] is True and positive["detection_ok"] is True
    assert positive["sanitized_positives"] == 1 and positive["paths_modified"] == 2
    assert positive["exact_positive_findings"] == 0 and positive["overbroad_positive_findings"] == 5
    assert positive["exact_value_detection_ok"] is False
    assert positive["calls"][0]["recognized_positives"] == 1
    assert positive["calls"][0]["recognized_exact_positives"] == 0


def test_second_sdk_send_in_same_case_is_rejected_before_io(identity_probe):
    # A length response asks the production guard to recursively split records.
    identity_probe.install(lambda *args: ('{"items":[]}', "length"))
    report = asyncio.run(real_diagnosis("identity_collisions"))
    assert len(identity_probe.sent) == report["attempted_sdk_calls"] == 2
    assert all(not case["pipeline_ok"] and case["error_type"] == "RuntimeError" for case in report["cases"])
    assert all(len(case["calls"]) == 1 for case in report["cases"])


@pytest.mark.parametrize("failure", ["network", "response_parsing"])
def test_unknown_reservations_remain_charged_before_next_case(identity_probe, failure):
    identity_probe.install(lambda *args: TimeoutError("private provider body") if failure == "network" else "malformed_response")
    report = asyncio.run(real_diagnosis("identity_collisions"))
    assert report["actual_tokens"] == 0
    assert report["unknown_usage_reserved_tokens"] > 0
    assert len(identity_probe.sent) == report["attempted_sdk_calls"] <= 2
    meter = identity_probe.meters[0]
    assert report["unknown_usage_reserved_tokens"] == meter.unknown_tokens
    with pytest.raises(RuntimeError, match="DiagnosticBudgetExceeded"):
        meter.reserve(meter.max_tokens - meter.unknown_tokens + 1)
    assert all(not case["pipeline_ok"] for case in report["cases"])
    assert all(case["sdk_calls"] == 1 and case["unknown_reserved_tokens"] > 0 for case in report["cases"])
    assert "private provider body" not in json.dumps(report)


def test_metadata_match_is_not_accepted_as_production_detection(identity_probe):
    identity_probe.install(lambda *args: ('{"items":[{"value":"field_name","type":"SECRET"}]}', "stop"))
    report = asyncio.run(real_diagnosis("identity_collisions"))
    assert all(case["calls"][0]["contract_valid"] for case in report["cases"])
    # The diagnostic intentionally permits only one send per case. Production
    # correction is refused by that existing budget, never an extra probe.
    assert all(not case["pipeline_ok"] and case["error_type"] == "RuntimeError" for case in report["cases"])
    assert all(case["sdk_calls"] == 1 and case["unknown_reserved_tokens"] == 0 for case in report["cases"])
    assert all(case["detection_ok"] is None for case in report["cases"])


def test_exception_labels_are_closed_even_for_custom_exception_names(identity_probe):
    untrusted = type(identity_probe.marker, (Exception,), {})
    identity_probe.install(lambda *args: untrusted("private provider body"))
    report = asyncio.run(real_diagnosis("identity_collisions"))
    assert all(case["error_type"] == "OtherError" for case in report["cases"])
    assert all(case["calls"][0]["error_type"] == "OtherError" for case in report["cases"])
    assert identity_probe.marker not in json.dumps(report)


def test_zero_amount_checkpoint_is_allowed_after_final_sdk_call():
    meter = Meter(max_calls=1, max_tokens=100)
    meter.calls, meter.usage = 1, 20
    meter.reserve(0)
    with pytest.raises(RuntimeError, match="DiagnosticBudgetExceeded"):
        meter.reserve(1)
