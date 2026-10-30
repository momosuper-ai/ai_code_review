"""Stop paid work when privacy masking changes immutable source identities."""

from collections import Counter
import re

from src.core.project_brain.exceptions import PrivacyIdentityConflict
from src.core.project_brain import privacy as privacy_module
from src.core.project_brain.privacy import LedgerPrivacyFilter, PrivacyLedger
from src.core.project_brain.schema import FactBundle
from src.core.privacy_filter import PrivacyFilter, SanitizationMapping
from src.core.privacy_rules import SensitivePattern

_PLAIN_SANITIZE = PrivacyFilter.sanitize_text
_LEDGER_SANITIZE = LedgerPrivacyFilter.sanitize_text
_LEDGER_MASK = PrivacyLedger.mask
_LEDGER_DIGEST = PrivacyLedger.digest
_LEDGER_REFRESH = PrivacyLedger.refresh
_MAPPING_CREATE = SanitizationMapping.get_or_create
_IMPLEMENTATIONS = tuple((function, function.__code__, function.__defaults__, function.__kwdefaults__)
                        for function in (_PLAIN_SANITIZE, _LEDGER_SANITIZE, _LEDGER_MASK,
                                         _LEDGER_DIGEST, _LEDGER_REFRESH, _MAPPING_CREATE))
_FILTER_FIELDS = frozenset({"_mapping", "_local_llm", "token_usage", "_rules", "_domain_patterns",
                            "_cidr_patterns", "_custom_patterns", "_dynamic_values"})


def _patterns(values, *, rules: bool) -> tuple:
    if type(values) is not list:
        raise TypeError()
    result = []
    for value in values:
        if rules:
            if type(value) is not SensitivePattern:
                raise TypeError()
            if any(type(entry) is not str for entry in (value.name, value.type_tag, value.description)):
                raise TypeError()
            pattern = value.pattern
        else:
            pattern = value
        if not isinstance(pattern, re.Pattern) or type(pattern.pattern) is not str:
            raise TypeError()
        result.append((value.name, value.type_tag, pattern.pattern, pattern.flags, value.description)
                      if rules else (pattern.pattern, pattern.flags))
    return tuple(result)


def _string_pairs(value, *, counters: bool = False) -> tuple:
    if (type(value) is not dict or any(type(key) is not str or
            (type(item) is not int or item < 0 if counters else type(item) is not str)
            for key, item in value.items())):
        raise TypeError()
    # Dynamic replacement order is semantic; do not sort this mapping.
    return tuple(value.items())


def _filter_signature(privacy_filter) -> tuple | None:
    """Recognize the complete state of the two pure supported sanitizers.

    Future fields, subclasses, replaced methods and unknown mapping objects
    disable reuse. This signature and any source strings remain unit-local
    memory, never diagnostics, model input, receipts or persisted artifacts.
    """
    try:
        kind = type(privacy_filter)
        if kind not in {PrivacyFilter, LedgerPrivacyFilter}:
            return None
        if any(function.__code__ is not code or function.__defaults__ != defaults
               or function.__kwdefaults__ != keywords for function, code, defaults, keywords in _IMPLEMENTATIONS):
            return None
        expected_method = _PLAIN_SANITIZE if kind is PrivacyFilter else _LEDGER_SANITIZE
        if (getattr(privacy_filter.sanitize_text, "__func__", None) is not expected_method
                or PrivacyFilter.sanitize_text is not _PLAIN_SANITIZE
                or SanitizationMapping.get_or_create is not _MAPPING_CREATE):
            return None
        fields = _FILTER_FIELDS | ({"ledger"} if kind is LedgerPrivacyFilter else set())
        if set(vars(privacy_filter)) != fields or privacy_filter._local_llm is not None:
            return None
        mapping = privacy_filter._mapping
        if (type(mapping) is not SanitizationMapping
                or set(vars(mapping)) != {"_forward", "_reverse", "_counters"}):
            return None
        usage = vars(privacy_filter.token_usage)
        if (set(usage) != {"prompt_tokens", "completion_tokens", "cached_tokens"}
                or any(type(value) is not int or value < 0 for value in usage.values())):
            return None
        ledger_state = None
        if kind is LedgerPrivacyFilter:
            ledger = privacy_filter.ledger
            if (type(ledger) is not PrivacyLedger
                    or getattr(ledger.mask, "__func__", None) is not _LEDGER_MASK
                    or getattr(ledger.digest, "__func__", None) is not _LEDGER_DIGEST
                    or getattr(ledger.refresh, "__func__", None) is not _LEDGER_REFRESH
                    or type(ledger.key) is not bytes or len(ledger.key) != 32
                    or type(ledger.entries) is not dict):
                return None
            entries = tuple(sorted((fingerprint, *record) for fingerprint, record in ledger.entries.items()))
            ledger_state = (ledger.key, entries, _LEDGER_MASK, _LEDGER_DIGEST, _LEDGER_REFRESH,
                            privacy_module._PLACEHOLDER.pattern, privacy_module._PLACEHOLDER.flags)
        return (kind, _IMPLEMENTATIONS, expected_method, _PLAIN_SANITIZE, _MAPPING_CREATE, ledger_state,
                _patterns(privacy_filter._rules, rules=True),
                _patterns(privacy_filter._custom_patterns, rules=True),
                _patterns(privacy_filter._domain_patterns, rules=False),
                _patterns(privacy_filter._cidr_patterns, rules=False),
                _string_pairs(privacy_filter._dynamic_values),
                _string_pairs(mapping._forward), _string_pairs(mapping._reverse),
                _string_pairs(mapping._counters, counters=True), tuple(usage.items()))
    except (AttributeError, TypeError, ValueError):
        return None


class SourceIdentityGuard:
    """Check the complete build identity against every current discovery.

    One guard belongs to one worker unit. Successful reuse requires complete
    current identity and recognized filter state, after refreshing fingerprints.
    Equal index sizes are insufficient. Unknown filters always run full checks.
    """

    def __init__(self, bundle: FactBundle, privacy_filter) -> None:
        self.bundle = bundle
        self.privacy_filter = privacy_filter
        self._last_success: tuple | None = None

    def _changed(self, unique: set[str]) -> set[str]:
        return {value for value in unique if self.privacy_filter.sanitize_text(value) != value}

    def check(self) -> None:
        """Refresh fingerprints, inspect every pool, and report closed counts."""
        previous, self._last_success = self._last_success, None
        ledger = getattr(self.privacy_filter, "ledger", None)
        refresh = getattr(ledger, "refresh", None)
        if callable(refresh):
            refresh()
        pools = {
            "files": Counter(list(self.bundle.files)),
            "modules": Counter(self.bundle.modules.values()),
            "source_refs": Counter(ref.path for ref in self.bundle.source_refs),
            "observations": Counter(list(self.bundle.observations)),
        }
        unique = set().union(*(pool.keys() for pool in pools.values()))
        identity = tuple((name, tuple(sorted(pool.items()))) for name, pool in sorted(pools.items()))
        state = _filter_signature(self.privacy_filter)
        signature = (identity, state) if state is not None else None
        if signature is not None and signature == previous:
            self._last_success = signature
            return
        changed = self._changed(unique)
        if changed:
            counts = {name: sum(count for value, count in pool.items() if value in changed)
                      for name, pool in pools.items()}
            raise PrivacyIdentityConflict({**counts, "unique_changed": len(changed)})
        # Rule masking may extend its deterministic placeholder mapping. Bind
        # the successful post-check state too; never reuse a failed traversal.
        state = _filter_signature(self.privacy_filter)
        self._last_success = (identity, state) if state is not None else None
