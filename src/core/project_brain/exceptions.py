"""Shared budget outcomes without lifecycle/compiler import cycles."""


class BudgetBusy(Exception):
    """A live sibling request temporarily holds the needed budget."""


class BudgetExhausted(ValueError):
    """The remaining known/unknown budget cannot admit another request."""


class PrivacyIdentityConflict(Exception):
    """Privacy masking conflicts with immutable source identity; stop work."""

    failure_code = "source_identity_privacy_conflict"
    COUNT_FIELDS = frozenset({"files", "modules", "source_refs", "observations", "unique_changed"})

    def __init__(self, counts: dict[str, int] | None = None) -> None:
        # Neither caller-supplied keys nor source values may reach diagnostics.
        values = {} if counts is None else counts
        if (type(values) is not dict or not set(values) <= self.COUNT_FIELDS
                or any(type(value) is not int or value < 0 for value in values.values())):
            raise ValueError("源码隐私身份冲突计数无效")
        self.counts = {key: values.get(key, 0) for key in sorted(self.COUNT_FIELDS)}
        super().__init__(self.failure_code)


class PrivacyCheckpointMissing(Exception):
    """An already extracted unit cannot safely replace its private build key."""

    failure_code = "privacy_checkpoint_missing"

    def __init__(self) -> None:
        super().__init__(self.failure_code)


class PrivacySealConflict(Exception):
    """A sealed build cannot accept another discovery or replace its seal."""

    failure_code = "privacy_seal_conflict"

    def __init__(self) -> None:
        super().__init__(self.failure_code)


class SourceBindingError(ValueError):
    """Closed source authentication failure, never a model format retry."""

    failure_code = "source_binding_invalid"

    def __init__(self) -> None:
        super().__init__(self.failure_code)


class ModelCallsIncomplete(Exception):
    """Known in-flight or unknown calls prevent sealing a public version."""

    failure_code = "model_calls_incomplete"

    def __init__(self) -> None:
        super().__init__(self.failure_code)


class PrivacyResponseInvalid(ValueError):
    """Closed response-validation reason; never retain model values or JSON."""

    failure_code = "privacy_response_invalid"
    REASONS = frozenset({"invalid_json", "invalid_shape", "invalid_item", "empty_value", "value_not_in_input"})

    def __init__(self, reason: str, *, correction_exhausted: bool = False) -> None:
        if type(reason) is not str or reason not in self.REASONS or type(correction_exhausted) is not bool:
            raise ValueError("隐私响应错误类别无效")
        self.reason = reason
        self.correction_exhausted = correction_exhausted
        label = "本地隐私扫描引用无效" if reason in {"empty_value", "value_not_in_input"} else "本地隐私扫描格式无效"
        super().__init__(f"{label}；failure_code={self.failure_code} reason={reason}")


class ModelTransportFailed(Exception):
    """A bounded HTTP recovery cannot be resumed by ordinary task retries."""

    def __init__(self, code: str = "model_http_retry_exhausted") -> None:
        self.failure_code = (code if code in {"model_http_retry_exhausted", "model_http_retry_window_exhausted"}
                             else "model_http_retry_exhausted")
        super().__init__(self.failure_code)


class ModelWorkAdmissionDenied(Exception):
    """Local work admission stopped before SDK I/O, independently of HTTP."""

    failure_code = "model_work_window_exhausted"

    def __init__(self, reason: str, phase: str, *, after_response: bool,
                 remaining_seconds: int | None = None, required_seconds: int | None = None) -> None:
        if (type(reason) is not str or reason not in {"deadline", "call_limit"}
                or type(phase) is not str or phase not in {"before_prepare", "before_reserve", "after_reserve"}
                or type(after_response) is not bool
                or remaining_seconds is not None and (type(remaining_seconds) is not int or remaining_seconds < 0)
                or required_seconds is not None and (type(required_seconds) is not int or required_seconds < 0)):
            raise ValueError("模型工作准入诊断字段无效")
        self.reason = reason
        self.phase = phase
        self.after_response = after_response
        self.reservation_created = phase == "after_reserve"
        self.remaining_seconds = remaining_seconds
        self.required_seconds = required_seconds
        super().__init__(self.failure_code)
