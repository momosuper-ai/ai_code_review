"""A valid paid late finding must reach the sealed-artifact quarantine gate."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.llm_client import ChatResult, TokenUsage
from src.core.project_brain.compiler import CompilationGuard, encoded
from src.core.project_brain.exceptions import BudgetExhausted, PrivacySealConflict
from src.core.project_brain.privacy import PrivacyLedger


@pytest.mark.asyncio
@pytest.mark.parametrize("window", ["response", "account"])
async def test_valid_late_positive_is_accounted_then_reports_seal_conflict(tmp_path, monkeypatch, window):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    ledger = PrivacyLedger(tmp_path / "work", seal_directory=tmp_path / "seal")
    charges, reservations, calls = [], [], []
    def seal():
        ledger.seal(ledger.revision(), "a" * 64)
    async def chat(*args, **kwargs):
        calls.append(1)
        if window == "response":
            seal()
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": [
            {"value": "synthetic-late-credential", "type": "SECRET"}]})), TokenUsage(100, 10), "stop")
    local = SimpleNamespace(chat=chat, aclose=AsyncMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: local)
    def account(usage, *args):
        charges.append(usage.total_tokens)
        if window == "account":
            seal()
            raise BudgetExhausted("settled overdraw")
    def reserve(amount):
        reservations.append(amount)
        if amount == 0:
            if window == "reserve":
                seal()
            raise ValueError("expired lease")
    guard = CompilationGuard(account, reserve, ledger)
    try:
        with pytest.raises(PrivacySealConflict, match="privacy_seal_conflict"):
            await guard._scan([{"field": "source_excerpt", "text": "password=synthetic-late-credential"}],
                              ledger.digest("scope"), 0)
    finally:
        await guard.close()
    assert charges == [110] and len(calls) == 1
    assert len([amount for amount in reservations if amount > 0]) == 1
    assert 0 not in reservations
    assert not ledger.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lease", "account"])
async def test_unsealed_valid_old_result_is_registered_before_lease_rejection(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    ledger = PrivacyLedger(tmp_path / "work", seal_directory=tmp_path / "seal")
    before = ledger.revision()
    charges, reservations = [], []
    async def chat(*args, **kwargs):
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded({"items": [
            {"value": "synthetic-late-credential", "type": "SECRET"}]})), TokenUsage(100, 10), "stop")
    local = SimpleNamespace(chat=chat, aclose=AsyncMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: local)
    def account(usage, *args):
        charges.append(usage.total_tokens)
        if failure == "account":
            raise BudgetExhausted("settled account failure")
    def reserve(amount):
        reservations.append(amount)
        if amount == 0:
            raise ValueError("expired lease")
    guard = CompilationGuard(account, reserve, ledger)
    scope = ledger.digest("scope")
    record = {"field": "source_excerpt", "text": "password=synthetic-late-credential"}
    try:
        with pytest.raises(BudgetExhausted if failure == "account" else ValueError):
            await guard._scan([record], scope, 0)
    finally:
        await guard.close()
    assert charges == [110]
    assert ledger.mask("synthetic-late-credential") != "synthetic-late-credential"
    assert ledger.revision() != before
    assert len(ledger.origins["calls"]) == 1
    packet_key = ledger.digest("packet:v2:" + encoded([record]))
    assert ledger.receipt(packet_key) is None
    assert ledger.receipt(ledger.digest("record:v2:" + encoded(record))) is None
    with pytest.raises(PrivacySealConflict):
        ledger.seal(before, "a" * 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"items": []},
    {"items": [{"value": "not-present-in-input", "type": "SECRET"}]},
    {"items": [{"value": "synthetic-late-credential", "type": "INVALID"}]},
])
async def test_account_failure_never_infers_a_discovery_from_invalid_or_empty_output(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(settings, "PRIVACY_FILTER_ENABLED", True)
    monkeypatch.setattr(settings, "PRIVACY_TOOL_SCAN_MODE", "hybrid")
    monkeypatch.setattr(settings, "PRIVACY_CUSTOM_PATTERNS", "")
    ledger = PrivacyLedger(tmp_path / "work", seal_directory=tmp_path / "seal")
    charges = []
    async def chat(*args, **kwargs):
        ledger.seal(ledger.revision(), "a" * 64)
        return ChatResult(ChatCompletionMessage(role="assistant", content=encoded(payload)), TokenUsage(100, 10), "stop")
    local = SimpleNamespace(chat=chat, aclose=AsyncMock())
    monkeypatch.setattr("src.core.project_brain.compiler.LLMClient", lambda *args, **kwargs: local)
    def account(usage, *args):
        charges.append(usage.total_tokens)
        raise BudgetExhausted("original account failure")
    guard = CompilationGuard(account, lambda amount: None, ledger)
    try:
        with pytest.raises(BudgetExhausted, match="original account failure"):
            await guard._scan([{"field": "source_excerpt", "text": "password=synthetic-late-credential"}],
                              ledger.digest("scope"), 0)
    finally:
        await guard.close()
    assert charges == [110]
