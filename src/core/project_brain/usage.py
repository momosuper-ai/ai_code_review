"""Body-free call accounting and transparent attribution, including unknowns."""
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from src.config import settings
from src.core.project_brain.exceptions import ModelCallsIncomplete
from src.models.brain import BrainBuild, BrainModelCall, BrainUnit, utcnow

ROLES = {"privacy", "semantic"}
PURPOSES = {"initial", "format_retry", "source_excerpt", "retry_reasoning", "semantic_output", "generic", "mixed"}
HTTP_ERROR_UNIT_LIMIT = 6
HTTP_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,254}\Z")


def model_label(value: Any) -> str | None:
    """Bound provider-reported labels; never persist arbitrary response text."""
    return value if isinstance(value, str) and _MODEL.fullmatch(value) else None


@dataclass(frozen=True)
class CallSpec:
    role: str
    requested_model: str
    purpose: str
    attempt: int = 1
    transport_group: str | None = None
    transport_attempt: int | None = None

    def validate(self) -> None:
        if (not isinstance(self.role, str) or self.role not in ROLES
                or not isinstance(self.purpose, str) or self.purpose not in PURPOSES or model_label(self.requested_model) is None
                or type(self.attempt) is not int or self.attempt < 1):
            raise ValueError("知识模型记账元数据无效")
        if (self.transport_group is None) != (self.transport_attempt is None):
            raise ValueError("知识HTTP重试元数据无效")
        if self.transport_group is not None:
            validate_call_id(self.transport_group)
            if (type(self.transport_attempt) is not int
                    or not 1 <= self.transport_attempt <= settings.PROJECT_BRAIN_MODEL_HTTP_RETRIES + 1):
                raise ValueError("知识HTTP重试元数据无效")


def validate_call_id(value: str) -> None:
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("知识模型调用标识无效") from None


class BuildCallMeter:
    """The compiler knows calls; this adapter alone binds them to durable leases."""

    def __init__(self, build_id: str, unit_id: int, lease_token: str):
        self.build_id, self.unit_id, self.lease_token = build_id, unit_id, lease_token

    def reserve(self, call_id: str, bound: int, spec: CallSpec) -> str:
        from src.core.project_brain.lifecycle import reserve_tokens
        reserve_tokens(self.build_id, self.lease_token, self.unit_id, bound, call_id=call_id, call_spec=spec)
        return call_id

    def settle(self, call_id: str, usage, estimated: bool, bound: int, response_model: str | None) -> None:
        from src.core.project_brain.lifecycle import account_tokens
        account_tokens(self.build_id, usage, estimated, bound, unit_id=self.unit_id,
                       lease_token=self.lease_token, call_id=call_id, response_model=response_model)

    def close_http_error(self, call_id: str, bound: int, status: int) -> int:
        """Retain the complete charge for an authenticated terminal HTTP error."""
        from src.core.project_brain.lifecycle import close_http_error
        return close_http_error(self.build_id, self.unit_id, self.lease_token, call_id, bound, status)


def _http_error_valid(call: BrainModelCall) -> bool:
    return (call.state == "http_error" and type(call.http_status) is int
            and call.http_status in HTTP_RETRY_STATUSES and call.closed_at is not None
            and call.settled_at is None and call.response_model is None
            and type(call.usage_estimated) is bool and not call.usage_estimated
            and all(type(value) is int and value == 0
                    for value in (call.prompt_tokens, call.completion_tokens, call.cached_tokens))
            and type(call.reserved_tokens) is int and call.reserved_tokens > 0)


def _call_group_identity(call: BrainModelCall) -> tuple:
    return (call.build_id, call.unit_id, call.lease_token, call.stage,
            call.role, call.purpose, call.requested_model, call.attempt)


def validate_transport_reservation(session, build_id: str, unit: BrainUnit, token: str, spec: CallSpec) -> None:
    """Called under build/unit locks before reserving or sending another UUID."""
    closed = session.scalar(select(func.count()).select_from(BrainModelCall).where(
        BrainModelCall.build_id == build_id, BrainModelCall.unit_id == unit.id,
        BrainModelCall.state == "http_error"))
    if closed >= HTTP_ERROR_UNIT_LIMIT:
        raise ValueError("知识HTTP错误次数已达硬上限")
    if spec.transport_group is None:
        return
    # Scope is global: a group UUID must never be borrowed from another unit,
    # lease or build. Its unique sequence index also fences concurrent reuse.
    previous = session.scalars(select(BrainModelCall).where(
        BrainModelCall.transport_group == spec.transport_group)
        .order_by(BrainModelCall.transport_attempt).with_for_update()).all()
    expected = (build_id, unit.id, token, unit.kind, spec.role,
                spec.purpose, spec.requested_model, spec.attempt)
    if (spec.transport_attempt != len(previous) + 1
            or any(_call_group_identity(call) != expected
                   or call.transport_attempt != index or not _http_error_valid(call)
                   for index, call in enumerate(previous, 1))):
        raise ValueError("知识HTTP重试来源或顺序无效")


def assert_calls_terminal(session, build_id: str) -> None:
    """Require each failed HTTP group to end in its own authenticated success."""
    build = session.get(BrainBuild, build_id)
    if build is None:
        raise ModelCallsIncomplete()
    calls = session.scalars(select(BrainModelCall).where(BrainModelCall.build_id == build_id)).all()
    units = {unit.id: unit for unit in session.scalars(select(BrainUnit).where(BrainUnit.build_id == build_id))}
    groups, closed_by_unit = {}, {}
    prompt = completion = cached = closed_bound = 0
    for call in calls:
        try:
            validate_call_id(call.id)
            spec = CallSpec(call.role, call.requested_model, call.purpose, call.attempt,
                            call.transport_group, call.transport_attempt)
            spec.validate()
        except (TypeError, ValueError):
            raise ModelCallsIncomplete() from None
        if call.state not in {"settled", "http_error"}:
            raise ModelCallsIncomplete()
        unit = units.get(call.unit_id)
        if unit is None or unit.build_id != build_id or unit.kind != call.stage:
            raise ModelCallsIncomplete()
        if call.state == "http_error":
            if not _http_error_valid(call) or call.transport_group is None:
                raise ModelCallsIncomplete()
            closed_bound += call.reserved_tokens
            closed_by_unit[call.unit_id] = closed_by_unit.get(call.unit_id, 0) + 1
            if closed_by_unit[call.unit_id] > HTTP_ERROR_UNIT_LIMIT:
                raise ModelCallsIncomplete()
        else:
            if (call.http_status is not None or call.closed_at is not None
                    or any(type(value) is not int or value < 0 for value in
                           (call.prompt_tokens, call.completion_tokens, call.cached_tokens, call.reserved_tokens))
                    or call.cached_tokens > call.prompt_tokens or type(call.usage_estimated) is not bool):
                raise ModelCallsIncomplete()
            prompt += call.prompt_tokens
            completion += call.completion_tokens
            cached += call.cached_tokens
        if call.transport_group is not None:
            groups.setdefault(call.transport_group, []).append(call)
    for group in groups.values():
        group.sort(key=lambda call: call.transport_attempt)
        identity = _call_group_identity(group[0])
        if (any(call.transport_attempt != index or _call_group_identity(call) != identity
                for index, call in enumerate(group, 1))
                or group[-1].state != "settled"
                or any(call.state != "http_error" for call in group[:-1])):
            raise ModelCallsIncomplete()
    actual = build.total_prompt_tokens + build.total_completion_tokens
    if (any(type(value) is not int or value < 0 for value in
            (build.total_prompt_tokens, build.total_completion_tokens, build.total_cached_tokens, build.reserved_tokens))
            or cached > build.total_cached_tokens or prompt > build.total_prompt_tokens
            or completion > build.total_completion_tokens
            or build.total_cached_tokens > build.total_prompt_tokens
            or build.reserved_tokens != actual + closed_bound
            or (build.usage_tracking_version == 1 and (prompt != build.total_prompt_tokens
                 or completion != build.total_completion_tokens or cached != build.total_cached_tokens))):
        raise ModelCallsIncomplete()


def abandon_calls(session, unit: BrainUnit) -> None:
    """Retain the charge on handoff/cancellation; only its certainty changes."""
    for call in session.scalars(select(BrainModelCall).where(
            BrainModelCall.unit_id == unit.id, BrainModelCall.build_id == unit.build_id,
            BrainModelCall.state == "reserved").with_for_update()):
        call.state = "unknown"


def abandon_reservation(unit: BrainUnit, session=None) -> None:
    """Fence all old I/O before clearing a lease; never release its charge."""
    if session is not None:
        abandon_calls(session, unit)
    payload = unit.payload or {}
    live = max(0, int(payload.get("_reserved_tokens", 0)))
    if live:
        unit.payload = {**payload, "_reserved_tokens": 0,
                        "_unknown_tokens": max(0, int(payload.get("_unknown_tokens", 0))) + live}


def usage_data(build: BrainBuild, rows: list[tuple[BrainModelCall, BrainUnit | None]]) -> dict:
    """Shares use the whole build denominator, never silently discard legacy costs."""
    groups = {}
    attributed_prompt = attributed_completion = attributed_cached = estimated_tokens = 0
    live = unknown = settled = closed = closed_calls = 0
    states_valid = True
    now = utcnow()
    for call, unit in rows:
        key = (call.role, call.requested_model, call.response_model, call.stage, call.purpose)
        group = groups.setdefault(key, {"role": call.role, "requested_model": call.requested_model,
            "response_model": call.response_model, "stage": call.stage, "purpose": call.purpose,
            "settled_calls": 0, "estimated_calls": 0, "reserved_calls": 0, "unknown_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens_subset": 0,
            "total_tokens": 0, "live_reserved_tokens": 0, "unknown_reserved_tokens": 0,
            "closed_http_error_calls": 0, "closed_http_error_reserved_tokens": 0, "ambiguous_reserved_tokens": 0})
        if call.state == "settled":
            count = call.prompt_tokens + call.completion_tokens
            group["settled_calls"] += 1
            group["estimated_calls"] += int(call.usage_estimated)
            group["prompt_tokens"] += call.prompt_tokens
            group["completion_tokens"] += call.completion_tokens
            group["cached_tokens_subset"] += call.cached_tokens
            group["total_tokens"] += count
            attributed_prompt += call.prompt_tokens
            attributed_completion += call.completion_tokens
            attributed_cached += call.cached_tokens
            settled += 1
            if call.usage_estimated:
                estimated_tokens += count
        elif call.state == "http_error":
            # Compatibility unknown includes both closed unknown charges and
            # potentially outstanding responses; expose the split explicitly.
            group["closed_http_error_calls"] += 1
            group["closed_http_error_reserved_tokens"] += call.reserved_tokens
            group["unknown_reserved_tokens"] += call.reserved_tokens
            closed_calls += 1
            closed += call.reserved_tokens
            states_valid = states_valid and _http_error_valid(call) and call.transport_group is not None
        else:
            states_valid = states_valid and call.state in {"reserved", "unknown"}
            active = (call.state == "reserved" and build.status in {"pending", "scanning", "extracting", "compiling", "validating"}
                      and unit is not None and unit.status == "running" and unit.lease_token == call.lease_token
                      and unit.lease_until is not None and unit.lease_until > now)
            group["reserved_calls" if active else "unknown_calls"] += 1
            group["live_reserved_tokens" if active else "unknown_reserved_tokens"] += call.reserved_tokens
            if active:
                live += call.reserved_tokens
            else:
                group["ambiguous_reserved_tokens"] += call.reserved_tokens
                unknown += call.reserved_tokens
    total = build.total_prompt_tokens + build.total_completion_tokens
    attributed = attributed_prompt + attributed_completion
    untracked = total - attributed
    untracked_cached = build.total_cached_tokens - attributed_cached
    untracked_reserved = build.reserved_tokens - attributed - live - unknown - closed
    consistent = (states_valid and untracked >= 0 and untracked_reserved >= 0 and attributed_prompt <= build.total_prompt_tokens
                  and attributed_completion <= build.total_completion_tokens and attributed_cached <= build.total_cached_tokens)
    coverage = ("inconsistent" if not consistent else "legacy_unattributed" if not rows and total > 0
                else "complete" if build.usage_tracking_version == 1 and untracked == 0 and untracked_reserved == 0 and untracked_cached == 0
                else "partial")
    exact = coverage == "complete" and not build.usage_estimated and estimated_tokens == 0 and live == unknown == closed == 0
    model_totals = {}
    for group in groups.values():
        key = (group["role"], group["requested_model"])
        model = model_totals.setdefault(key, {"role": key[0], "requested_model": key[1],
            "settled_calls": 0, "estimated_calls": 0, "reserved_calls": 0, "unknown_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "cached_tokens_subset": 0, "total_tokens": 0, "live_reserved_tokens": 0, "unknown_reserved_tokens": 0,
            "closed_http_error_calls": 0, "closed_http_error_reserved_tokens": 0, "ambiguous_reserved_tokens": 0})
        for name in tuple(model)[2:]:
            model[name] += group[name]
    for model in model_totals.values():
        model["token_share_percent"] = round(model["total_tokens"] * 100 / total, 6) if exact and total else None
        model["settled_call_share_percent"] = round(model["settled_calls"] * 100 / settled, 6) if exact and settled else None
    return {"tracking_version": build.usage_tracking_version, "attribution_status": coverage,
            "build_status": build.status, "share_scope": "截至读取时的已结算用量；执行中构建后续仍会增加",
            "exact_model_shares_available": exact and total > 0,
            "total_prompt_tokens": build.total_prompt_tokens, "total_completion_tokens": build.total_completion_tokens,
            "total_cached_tokens_subset": build.total_cached_tokens, "total_tokens": total,
            "attributed_tokens": attributed, "unattributed_tokens": untracked,
            "unattributed_cached_tokens_subset": untracked_cached,
            "estimated_tokens": estimated_tokens, "live_reserved_tokens": live,
            "unknown_reserved_tokens": unknown + closed, "ambiguous_reserved_tokens": unknown,
            "closed_http_error_calls": closed_calls, "closed_http_error_reserved_tokens": closed,
            "unattributed_reserved_tokens": untracked_reserved,
            "models": [model_totals[key] for key in sorted(model_totals)],
            "breakdown": [groups[key] for key in sorted(groups, key=lambda key: tuple(value or "" for value in key))],
            "currency_cost": None,
            "identity_policy": "requested_model为网关别名；response_model仅保留与配置别名一致的报告值，其他值不落盘，不证明实际后端身份",
            "share_policy": "仅全量归属且无估算/未知/活动预留时给整构建占比；HTTP错误费用仍未知且保留完整预留；cached是prompt子集，不重复扣除"}
