"""Plan two synthetic context probes; --real requires a new, one-use journal.

Never reads trial source/checkpoints, accepts source text, restores a masked
value, resumes a build, or writes GitLab. Human authorization is separate from
the explicit real-model environment/CLI gates.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if __file__ != "<stdin>":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.brain_privacy_diagnostic import Meter, _identity_metadata
from src.config import settings
from src.core.privacy_filter import PrivacyFilter
from src.core.project_brain import compiler
from src.core.project_brain.exceptions import PrivacyIdentityConflict
from src.core.project_brain.lifecycle import recipe_hash
from src.core.project_brain.privacy import PrivacyLedger, scan_records
from src.core.project_brain.privacy_identity import SourceIdentityGuard
from src.core.project_brain.privacy_provenance import canonical_json, field_role
from src.core.project_brain.privacy_projection import project_records
from src.core.project_brain.schema import FactBundle, SourceRef

PROTOCOL = "brain-synthetic-identity-context/1"
OUTPUT_LIMIT = 2048
MAX_CALLS = 2
MAX_TOKENS = 10000
CASES = ("names_and_limitations", "explicit_credential_value")
_NAME = "birchowl"
_PATHS = ("src/birchowl.py", "src/birchowl/config.py")


def fixture(positive: bool) -> tuple[dict, FactBundle]:
    """Return fixed, wholly synthetic prose; never transform trial prose."""
    rows = [
        {"field": "code_identifier", "text": _NAME},
        {"field": "source_path", "text": _PATHS[0]},
        {"field": "claims.text", "text": f"{_NAME} 是虚构组件名称，负责装配配置与任务入口。"},
        {"field": "claims.text", "text": f"该组件只声明 {_NAME} 配置项名称，没有给出认证口令的实际值。"},
        {"field": "compilation.unknowns", "text": f"无法由 {_NAME} 的名称确认认证方式或凭证存储位置，需要核对运行时设置。"},
        {"field": "model_retry_reasoning", "text": f"这里只解释 {_NAME} 的职责与限制；没有提供任何密钥或口令值，不能补造。"},
        {"field": "source_path", "text": _PATHS[1]},
    ]
    if positive:
        rows.append({"field": "claims.text", "text": f"本合成例另外明确设定认证口令的实际完整值为 {_NAME}；这是虚构凭证值。"})
    bundle = FactBundle(source_commit="0" * 40, source_fingerprint="0" * 64,
        files={path: {"blob_sha": "0" * 40} for path in _PATHS}, modules={"fixture_module": "src"},
        facts=[], relations=[], observations={}, source_refs=[SourceRef(
            id=f"fixture_ref_{index}", commit_sha="0" * 40, path=path,
            blob_sha="0" * 40, line_start=1, line_end=1) for index, path in enumerate(_PATHS)])
    return project_records(rows), bundle


def plan() -> dict:
    """Bound complete input/output, not provider token prices or real accuracy."""
    compiler.brain_extra_body("privacy")
    cases = []
    for label, positive in zip(CASES, (False, True), strict=True):
        payload, bundle = fixture(positive)
        encoded = compiler.encoded(payload)
        base_filter = PrivacyFilter(local_llm=None)
        if compiler.sanitize_structure(payload, base_filter) != payload:
            raise ValueError("synthetic_rules_conflict")
        SourceIdentityGuard(bundle, base_filter).check()
        records = scan_records(payload)
        if compiler.sanitize_structure(records, base_filter) != records:
            raise ValueError("synthetic_rules_conflict")
        output_bound = 16 + sum(len(compiler.encoded({"value": record["text"], "type": "CUSTOM"}).encode()) + 16
                                for record in records)
        if (len(records) > settings.PROJECT_BRAIN_PRIVACY_MAX_RECORDS_PER_PACKET
                or output_bound > OUTPUT_LIMIT * 0.75):
            raise ValueError("synthetic_packet_capacity")
        messages = compiler.CompilationGuard._messages(None, records)
        input_bound = compiler.token_bound(compiler.encoded(messages))
        if (input_bound > settings.PROJECT_BRAIN_PRIVACY_PACKET_MAX_TOKENS
                or input_bound > settings.PROJECT_BRAIN_PRIVACY_MAX_FIELD_TOKENS
                or input_bound + OUTPUT_LIMIT >= int(settings.LLM_MAX_CONTEXT_TOKENS * settings.LLM_CONTEXT_THRESHOLD)):
            raise ValueError("synthetic_packet_capacity")
        cases.append({"case": label, "records": len(records), "input_bound_units": input_bound,
                      "output_limit": OUTPUT_LIMIT, "reserved_bound": input_bound + OUTPUT_LIMIT,
                      "expected_conflict": positive, "maximum_sdk_calls": 1})
    total_bound = sum(case["reserved_bound"] for case in cases)
    if total_bound > MAX_TOKENS:
        raise ValueError("synthetic_budget_capacity")
    return {"protocol": PROTOCOL, "synthetic_only": True, "model_role": "privacy",
            "model_alias": "local_model" if settings.VLLM_MODEL_NAME == "local_model" else "other",
            "recipe_hash": recipe_hash(), "diagnostic_hash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "semantic_requests": 0, "max_calls": MAX_CALLS, "max_tokens": MAX_TOKENS,
            "planned_reserved_bound": total_bound, "bound_is_provider_usage": False,
            "cases": cases, "historical_discovery_context_reconstructed": False,
            "authorizes_build": False}


def plan_digest(planned: dict) -> str:
    return hashlib.sha256(compiler.encoded(planned).encode()).hexdigest()


class Journal:
    """Exclusive one-shot claim; a crash never makes a used path reusable."""

    def __init__(self, path: Path, planned: dict) -> None:
        self.path = path
        self.data = {"protocol": PROTOCOL, "run_id": str(uuid4()), "state": "running",
                     "plan_digest": hashlib.sha256(compiler.encoded(planned).encode()).hexdigest(),
                     "calls": [], "settled_tokens": 0, "unknown_reserved_tokens": 0}
        if (not path.parent.is_dir() or path.parent.is_symlink()
                or os.name == "posix" and path.parent.stat().st_mode & 0o077):
            raise ValueError("diagnostic_private_parent_required")
        # O_EXCL is the cross-process/restart fence. Never recover and resend.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        PrivacyLedger._sync_directory(path.parent)

    def write(self) -> None:
        staging = self.path.with_name(self.path.name + ".pending")
        descriptor = os.open(staging, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self.data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, self.path)
        PrivacyLedger._sync_directory(self.path.parent)

    def sending(self, case: str, bound: int, call_id: str | None = None) -> None:
        if (case not in CASES or any(call["case"] == case for call in self.data["calls"])
                or len(self.data["calls"]) >= MAX_CALLS
                or type(bound) is not int or bound <= 0
                or self.data["settled_tokens"] + self.data["unknown_reserved_tokens"] + bound > MAX_TOKENS):
            raise RuntimeError("diagnostic_limit")
        self.data["calls"].append({"id": call_id or str(uuid4()), "case": case, "state": "sending", "reserved": bound})
        self.data["unknown_reserved_tokens"] += bound
        self.write()  # Before the underlying SDK I/O; no body is written.

    def settled(self, usage: int, estimated: bool) -> None:
        call = self.data["calls"][-1]
        if call["state"] != "sending" or type(usage) is not int or usage < 0:
            raise RuntimeError("diagnostic_settlement")
        call.update(state="settled", actual_or_estimated_tokens=usage, estimated=estimated)
        self.data["unknown_reserved_tokens"] -= call["reserved"]
        self.data["settled_tokens"] += usage
        self.write()
        if self.data["settled_tokens"] + self.data["unknown_reserved_tokens"] > MAX_TOKENS:
            raise RuntimeError("diagnostic_limit")


class ContextMeter(Meter):
    def __init__(self, journal: Journal) -> None:
        super().__init__(max_calls=MAX_CALLS, max_tokens=MAX_TOKENS)
        self.journal = journal
        self.case_started_at = 0

    def reserve(self, amount: int) -> None:
        if amount > 0 and self.calls > self.case_started_at:
            raise RuntimeError("diagnostic_case_limit")
        super().reserve(amount)

    def account(self, usage, estimated: bool, bound: int) -> None:
        # Persist even a supplier overrun before Meter rejects another call.
        self.journal.settled(usage.total_tokens, estimated)
        super().account(usage, estimated, bound)


class CallBinding:
    """Use the guard's real call UUID for both the I/O journal and v2 origin."""

    def __init__(self, meter: ContextMeter) -> None:
        self.meter = meter
        self.call_id = None
        self.bound = None

    def reserve(self, call_id, bound, _spec):
        self.meter.reserve(bound)
        if self.call_id is not None:
            raise RuntimeError("diagnostic_case_limit")
        self.call_id, self.bound = call_id, bound
        return call_id

    def settle(self, call_id, usage, estimated, bound, _response_model):
        if (call_id != self.call_id or bound != self.bound
                or not self.meter.journal.data["calls"]
                or self.meter.journal.data["calls"][-1]["id"] != call_id):
            raise RuntimeError("diagnostic_settlement")
        self.meter.account(usage, estimated, bound)


def exact_provenance(ledger: PrivacyLedger, records: list[dict], *, call_id: str,
                     context: dict, scope: str, packet: str) -> bool:
    """Authenticate every fixture candidate, not just a nonempty origin list."""
    digest = ledger.digest("value:" + _NAME)
    if set(ledger.entries) != {digest} or set(ledger.origins["findings"]) != {digest} or set(ledger.origins["calls"]) != {call_id}:
        return False
    call = ledger.origins["calls"][call_id]
    expected_call = {"context": context, "after_response": True, "scope": scope, "packet": packet,
                     "records_hmac": ledger.digest("provenance:records:v1:" + canonical_json(records)),
                     "start": 0, "record_count": len(records)}
    if any(call[key] != value for key, value in expected_call.items()):
        return False
    finding = ledger.origins["findings"][digest]
    if finding["legacy_unknown"]:
        return False
    expected = []
    for index, record in enumerate(records):
        first = record["text"].find(_NAME)
        if first < 0:
            continue
        count, offset = 0, first
        while offset >= 0:
            count += 1
            offset = record["text"].find(_NAME, offset + 1)
        expected.append({"call_id": call_id, "record_hmac": ledger.digest("provenance:record:v1:" + canonical_json(record)),
            "field_hmac": ledger.digest("provenance:field:v1:" + record["field"]), "field_role": field_role(record["field"]),
            "location_kind": "candidate_occurrence", "record_index": index, "record_length": len(record["text"]),
            "first_offset": first, "occurrence_count": count, "length": len(_NAME), "type": ledger.entries[digest][1]})
    return sorted(finding["records"].values(), key=lambda item: item["record_index"]) == expected


async def run_probe(journal_path: Path, *, expected_plan_digest: str | None = None) -> dict:
    """A newly authorized run only; errors stop the pair without retries."""
    if os.getenv("BRAIN_LOCAL_REAL_MODEL") != "1":
        raise RuntimeError("real_model_gate_required")
    if not settings.PRIVACY_FILTER_ENABLED or settings.PRIVACY_TOOL_SCAN_MODE != "hybrid":
        raise RuntimeError("hybrid_privacy_required")
    planned = plan()
    if planned["model_alias"] != "local_model" or expected_plan_digest != plan_digest(planned):
        raise RuntimeError("frozen_plan_required")
    journal = Journal(journal_path, planned)
    meter = ContextMeter(journal)
    results = []
    try:
        with tempfile.TemporaryDirectory(prefix="brain-synthetic-context-") as temporary:
            for case_index, (label, positive) in enumerate(zip(CASES, (False, True), strict=True)):
                payload, bundle = fixture(positive)
                ledger = PrivacyLedger(Path(temporary) / str(case_index))
                binding = CallBinding(meter)
                guard = compiler.CompilationGuard(meter.account, meter.reserve, ledger=ledger,
                    call_meter=binding,
                    discovery_context={"build_id": str(uuid4()), "unit_id": case_index + 1, "stage": "compile"})
                gate = SourceIdentityGuard(bundle, guard.filter)
                gate.check()  # A rules conflict is not evidence of a new finding.
                guard.discovery_check = gate.check
                guard.after_response = True  # Exercise the semantic-output window.
                meter.case_started_at = meter.calls
                raw_create = guard.local._client.chat.completions.create

                async def send(*, _case=label, **kwargs):
                    bound = compiler.token_bound(compiler.encoded(kwargs["messages"])) + OUTPUT_LIMIT
                    if bound != binding.bound:
                        raise RuntimeError("diagnostic_reservation")
                    journal.sending(_case, bound, binding.call_id)
                    return await raw_create(**kwargs)

                guard.local._client.chat.completions.create = send
                records = meter.attach(guard, OUTPUT_LIMIT, [_NAME] if positive else [],
                                       [] if positive else [_NAME], max_case_calls=1)
                conflict = False
                failure = None
                text = compiler.encoded(payload)
                scan_input = scan_records(payload)
                packet = ledger.digest("packet:v2:" + compiler.encoded(scan_input))
                try:
                    with patch.object(settings, "PROJECT_BRAIN_PRIVACY_LLM_MAX_TOKENS", OUTPUT_LIMIT):
                        await guard.clean(text)
                except PrivacyIdentityConflict:
                    conflict = True
                except (Exception, asyncio.CancelledError) as error:
                    # No exception text, arbitrary keys or provider bodies.
                    failure = type(error).__name__ if type(error) in {ValueError, RuntimeError, TimeoutError, json.JSONDecodeError} else "OtherError"
                finally:
                    meter.abandon_pending()
                    await guard.close()
                reopened = PrivacyLedger(Path(temporary) / str(case_index), create=False)
                reopen_conflict = False
                try:
                    SourceIdentityGuard(bundle, compiler.LedgerPrivacyFilter(reopened)).check()
                except PrivacyIdentityConflict:
                    reopen_conflict = True
                calls = [_identity_metadata(item) for item in records]
                protocol_ok = (len(calls) == 1 and calls[0].get("contract_valid") is True
                               and calls[0].get("finish_reason") == "stop" and failure is None)
                exact = sum(item.get("recognized_exact_positives", 0) for item in calls)
                false_positives = sum(item.get("fp", 0) for item in calls)
                overbroad = sum(item.get("overbroad_positive_findings", 0) for item in calls)
                locations = sum(len(item["records"]) for item in reopened.origins["findings"].values())
                receipt_complete = reopened.receipt(packet) == "completed"
                record_receipts = sum(reopened.receipt(reopened.digest("record:v2:" + compiler.encoded(record))) == "completed"
                                      for record in scan_input)
                scope = ledger.digest("input:" + text)
                source_check = (exact_provenance(reopened, scan_input, call_id=binding.call_id,
                    context=guard.discovery_context, scope=scope, packet=packet) if positive else
                    not reopened.entries and not reopened.origins["calls"] and not reopened.origins["findings"])
                passed = (protocol_ok and conflict == positive and reopen_conflict == positive
                          and false_positives == 0 and overbroad == 0 and exact == int(positive)
                          and source_check and ((locations > 0 and not receipt_complete and record_receipts == 0
                                                and reopened.receipt(scope) is None) if positive else
                                               receipt_complete and record_receipts == len(scan_input)
                                               and reopened.receipt(scope) == "completed"))
                results.append({"case": label, "passed": passed, "protocol_ok": protocol_ok,
                    "expected_conflict": positive, "actual_conflict": conflict, "reopen_conflict": reopen_conflict,
                    "recognized_exact_positives": exact, "false_positive_findings": false_positives,
                    "overbroad_findings": overbroad, "candidate_locations": locations,
                    "provenance_calls": len(reopened.origins["calls"]),
                    "provenance_call_matches_io": (set(reopened.origins["calls"]) == {binding.call_id}) if positive else None,
                    "provenance_matches_complete_input": source_check,
                    "index_mac_applicable": (reopened.root / "fingerprints.json").is_file(),
                    "index_mac_verified": True if (reopened.root / "fingerprints.json").is_file() else None,
                    "packet_receipt_complete": receipt_complete, "completed_record_receipts": record_receipts,
                    "error_code": failure, "calls": calls})
                if not passed:
                    break
    finally:
        journal.data["state"] = "completed" if len(results) == 2 and all(item["passed"] for item in results) else "stopped"
        journal.write()
    return {"plan": planned, "passed": journal.data["state"] == "completed", "cases": results,
            "sdk_calls": meter.calls, "settled_tokens": journal.data["settled_tokens"],
            "unknown_reserved_tokens": journal.data["unknown_reserved_tokens"],
            "classification_causality_proven": False, "trial_finding_evaluated": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--plan-digest")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.real and (args.journal is None or args.plan_digest is None):
        parser.error("--real requires --journal, --plan-digest and separate human authorization")
    try:
        if args.real:
            result = asyncio.run(run_probe(args.journal, expected_plan_digest=args.plan_digest))
        else:
            planned = plan()
            result = {"plan": planned, "plan_digest": plan_digest(planned), "model_calls": 0}
    except Exception:
        print(json.dumps({"diagnostic_stopped": True, "error_code": "diagnostic_precondition_or_runtime_failure"}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
