"""Network-free controls for the synthetic provenance/identity diagnostic."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletionMessage

from scripts import brain_identity_context_diagnostic as probe
from src.config import settings


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.setenv("BRAIN_LOCAL_REAL_MODEL", "1")
    for name, value in {"PRIVACY_FILTER_ENABLED": True, "PRIVACY_TOOL_SCAN_MODE": "hybrid",
        "VLLM_MODEL_NAME": "local_model", "PRIVACY_CUSTOM_PATTERNS": "", "PRIVACY_INTERNAL_DOMAINS": "", "PRIVACY_INTERNAL_CIDRS": "",
        "PROJECT_BRAIN_PRIVACY_EXTRA_BODY": '{"reasoning_effort":"none"}',
        "PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS": 6000, "PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS": 32000,
        "PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET": 24, "LLM_MAX_CONTEXT_TOKENS": 128000,
        "PROJECT_BRAIN_PRIVACY_OUTPUT_MAX_CALLS_PER_UNIT": 64}.items():
        monkeypatch.setattr(settings, name, value)
    original = probe.compiler.LLMClient
    requests = []

    def install(response):
        def factory(*args, **kwargs):
            client = original(*args, **kwargs)

            async def create(**request):
                requests.append(request)
                body = response(len(requests), request)
                if isinstance(body, BaseException):
                    raise body
                if body == "malformed_choices":
                    return SimpleNamespace(choices=[], usage=None)
                content, finish, usage = body
                return SimpleNamespace(model="UNTRUSTED_PROVIDER_NAME", choices=[SimpleNamespace(
                    finish_reason=finish, message=ChatCompletionMessage(role="assistant", content=content,
                        arbitrary_provider_key="UNTRUSTED_PROVIDER_METADATA"))],
                    usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                        prompt_tokens_details=None, completion_tokens_details=None) if usage else None)

            client._client.chat.completions.create = create
            return client

        monkeypatch.setattr(probe.compiler, "LLMClient", factory)

    return SimpleNamespace(install=install, requests=requests)


def run(path):
    return probe.run_probe(path, expected_plan_digest=probe.plan_digest(probe.plan()))


def correct(index, _request):
    return json.dumps({"items": [] if index == 1 else [{"value": probe._NAME, "type": "SECRET"}]}), "stop", True


def test_default_plan_is_complete_bounded_and_contains_no_fixture_text(sdk):
    sdk.install(lambda *_: AssertionError("NO_NETWORK"))
    planned = probe.plan()
    assert len(planned["cases"]) == planned["max_calls"] == 2
    assert planned["planned_reserved_bound"] <= planned["max_tokens"] == 10000
    assert [case["expected_conflict"] for case in planned["cases"]] == [False, True]
    assert not sdk.requests
    serialized = json.dumps(planned)
    assert all(value not in serialized for value in (probe._NAME, *probe._PATHS))


def test_real_gate_refuses_before_sdk_or_journal(sdk, monkeypatch, tmp_path):
    sdk.install(correct)
    monkeypatch.delenv("BRAIN_LOCAL_REAL_MODEL")
    path = tmp_path / "run.json"
    with pytest.raises(RuntimeError, match="real_model_gate_required"):
        asyncio.run(run(path))
    assert not path.exists() and not sdk.requests


def test_v2_provenance_precedes_expected_conflict_and_receipts_remain_unfinished(sdk, tmp_path):
    sdk.install(correct)
    path = tmp_path / "run.json"
    report = asyncio.run(run(path))
    assert report["passed"] and report["sdk_calls"] == 2
    negative, positive = report["cases"]
    assert negative["protocol_ok"] and not negative["actual_conflict"]
    assert negative["candidate_locations"] == negative["provenance_calls"] == 0
    assert negative["index_mac_verified"] is None
    assert positive["protocol_ok"] and positive["actual_conflict"] and positive["reopen_conflict"]
    assert positive["index_mac_verified"] is True and positive["provenance_calls"] == 1
    assert positive["provenance_call_matches_io"] is True
    assert positive["candidate_locations"] > 1
    assert not positive["packet_receipt_complete"] and positive["completed_record_receipts"] == 0
    assert report["settled_tokens"] == 240 and report["unknown_reserved_tokens"] == 0
    raw = json.dumps(report) + path.read_text()
    assert all(value not in raw for value in (probe._NAME, *probe._PATHS, "UNTRUSTED_PROVIDER_NAME", "UNTRUSTED_PROVIDER_METADATA"))
    assert not report["classification_causality_proven"] and not report["trial_finding_evaluated"]


def test_completed_or_partially_used_journal_cannot_send_again(sdk, tmp_path):
    sdk.install(correct)
    path = tmp_path / "run.json"
    asyncio.run(run(path))
    with pytest.raises(FileExistsError):
        asyncio.run(run(path))
    assert len(sdk.requests) == 2


def test_crash_after_journal_claim_does_not_allow_a_new_run(sdk, tmp_path):
    sdk.install(correct)
    path = tmp_path / "run.json"
    journal = probe.Journal(path, probe.plan())
    journal.sending(probe.CASES[0], 3000)
    with pytest.raises(FileExistsError):
        asyncio.run(run(path))
    assert not sdk.requests
    assert json.loads(path.read_text())["unknown_reserved_tokens"] == 3000


@pytest.mark.parametrize("response", [
    lambda *_: TimeoutError("UNTRUSTED_ERROR_TEXT"),
    lambda *_: asyncio.CancelledError("UNTRUSTED_ERROR_TEXT"),
    lambda *_: "malformed_choices",
])
def test_missing_responses_keep_unknown_bound_and_stop_the_pair(sdk, tmp_path, response):
    sdk.install(response)
    path = tmp_path / "run.json"
    report = asyncio.run(run(path))
    assert not report["passed"] and report["sdk_calls"] == len(sdk.requests) == 1
    assert report["settled_tokens"] == 0 and report["unknown_reserved_tokens"] > 0
    assert "UNTRUSTED_ERROR_TEXT" not in json.dumps(report) + path.read_text()


@pytest.mark.parametrize("content,finish", [('{"items":[]}', "length"), ('{', "stop"),
    ('{"items":[],"UNTRUSTED_FIELD":"DO_NOT_EXPORT"}', "stop")])
def test_truncation_or_format_failure_never_sends_a_second_call(sdk, tmp_path, content, finish):
    sdk.install(lambda *_: (content, finish, True))
    report = asyncio.run(run(tmp_path / "run.json"))
    assert not report["passed"] and report["sdk_calls"] == len(sdk.requests) == 1
    assert report["settled_tokens"] == 120 and report["unknown_reserved_tokens"] == 0
    assert "UNTRUSTED_FIELD" not in json.dumps(report) and "DO_NOT_EXPORT" not in json.dumps(report)


def test_negative_false_positive_stops_before_positive_case(sdk, tmp_path):
    sdk.install(lambda *_: (json.dumps({"items": [{"value": probe._NAME, "type": "SECRET"}]}), "stop", True))
    report = asyncio.run(run(tmp_path / "run.json"))
    assert not report["passed"] and report["sdk_calls"] == 1
    assert report["cases"][0]["protocol_ok"] and report["cases"][0]["false_positive_findings"] == 1


def test_empty_valid_positive_is_not_detection_success(sdk, tmp_path):
    sdk.install(lambda *_: ('{"items":[]}', "stop", True))
    report = asyncio.run(run(tmp_path / "run.json"))
    assert not report["passed"] and report["sdk_calls"] == 2
    assert report["cases"][1]["protocol_ok"] and not report["cases"][1]["actual_conflict"]


def test_overbroad_positive_is_not_exact_detection(sdk, tmp_path):
    def broad(index, _request):
        return json.dumps({"items": [] if index == 1 else [{"value": probe._PATHS[0], "type": "SECRET"}]}), "stop", True
    sdk.install(broad)
    report = asyncio.run(run(tmp_path / "run.json"))
    assert not report["passed"] and report["cases"][1]["overbroad_findings"] == 1


def test_plan_budget_and_packet_capacity_refuse_before_journal_or_sdk(sdk, tmp_path, monkeypatch):
    sdk.install(correct)
    monkeypatch.setattr(probe, "MAX_TOKENS", 100)
    with pytest.raises(ValueError, match="synthetic_budget_capacity"):
        asyncio.run(run(tmp_path / "run.json"))
    assert not sdk.requests and not (tmp_path / "run.json").exists()


def test_single_case_and_global_sdk_limits_are_hard(sdk, tmp_path):
    journal = probe.Journal(tmp_path / "run.json", probe.plan())
    meter = probe.ContextMeter(journal)
    meter.calls = 1
    with pytest.raises(RuntimeError, match="diagnostic_case_limit"):
        meter.reserve(1)
    journal.sending(probe.CASES[0], 2000)
    with pytest.raises(RuntimeError, match="diagnostic_limit"):
        journal.sending(probe.CASES[0], 2000)
    journal.settled(100, False)
    journal.sending(probe.CASES[1], 2000)
    with pytest.raises(RuntimeError, match="diagnostic_limit"):
        journal.sending(probe.CASES[1], 2000)


def test_missing_usage_is_accounted_as_complete_response_estimate(sdk, tmp_path):
    sdk.install(lambda index, request: (*correct(index, request)[:2], False))
    path = tmp_path / "run.json"
    report = asyncio.run(run(path))
    assert report["passed"] and report["settled_tokens"] > 0
    assert all(call["estimated"] for call in json.loads(path.read_text())["calls"])


def test_frozen_plan_and_model_alias_are_checked_before_journal(sdk, tmp_path, monkeypatch):
    sdk.install(correct)
    with pytest.raises(RuntimeError, match="frozen_plan_required"):
        asyncio.run(probe.run_probe(tmp_path / "missing.json"))
    before = probe.plan_digest(probe.plan())
    monkeypatch.setattr(settings, "VLLM_API_URL", "http://127.0.0.1:17/v1")
    with pytest.raises(RuntimeError, match="frozen_plan_required"):
        asyncio.run(probe.run_probe(tmp_path / "changed.json", expected_plan_digest=before))
    monkeypatch.setattr(settings, "VLLM_MODEL_NAME", "other_model")
    with pytest.raises(RuntimeError, match="frozen_plan_required"):
        asyncio.run(run(tmp_path / "other.json"))
    assert not sdk.requests and not list(tmp_path.iterdir())


def test_scalar_anchored_rules_reject_during_plan(sdk, monkeypatch):
    sdk.install(correct)
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", json.dumps([
        {"name": "fixture", "pattern": "^birchowl$", "type_tag": "SECRET"}]))
    with pytest.raises(ValueError, match="synthetic_rules_conflict"):
        probe.plan()
    assert not sdk.requests


def test_valid_mac_without_complete_candidate_locations_cannot_pass(sdk, tmp_path, monkeypatch):
    sdk.install(correct)
    verify = probe.exact_provenance

    def omit_one(ledger, records, **kwargs):
        assert verify(ledger, records, **kwargs)
        finding = next(iter(ledger.origins["findings"].values()))
        finding["records"].pop(next(iter(finding["records"])))
        assert not verify(ledger, records, **kwargs)
        return False

    monkeypatch.setattr(probe, "exact_provenance", omit_one)
    report = asyncio.run(run(tmp_path / "run.json"))
    assert not report["passed"] and not report["cases"][1]["provenance_matches_complete_input"]


def test_sending_persistence_failure_never_reaches_sdk(sdk, tmp_path, monkeypatch):
    sdk.install(correct)

    def unavailable(_journal):
        raise OSError("synthetic_disk_failure")

    monkeypatch.setattr(probe.Journal, "write", unavailable)
    with pytest.raises(OSError, match="synthetic_disk_failure"):
        asyncio.run(run(tmp_path / "run.json"))
    assert not sdk.requests
    with pytest.raises(FileExistsError):
        asyncio.run(run(tmp_path / "run.json"))


def test_journal_parent_must_already_be_private(sdk, tmp_path):
    sdk.install(correct)
    with pytest.raises(ValueError, match="diagnostic_private_parent_required"):
        asyncio.run(run(tmp_path / "not_created" / "run.json"))
    assert not sdk.requests

