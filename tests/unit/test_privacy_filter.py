"""Unit tests for privacy filter module."""

import pytest

from src.core.privacy_filter import PrivacyFilter, SanitizationMapping
from src.core.privacy_rules import get_rules_for_level


# ---------------------------------------------------------------------------
# SanitizationMapping tests
# ---------------------------------------------------------------------------

class TestSanitizationMapping:
    def test_get_or_create_new(self):
        m = SanitizationMapping()
        p = m.get_or_create("my-secret-value", "SECRET")
        assert p == "<<SECRET_1>>"
        assert m.count == 1

    def test_get_or_create_same_value(self):
        m = SanitizationMapping()
        p1 = m.get_or_create("my-secret", "SECRET")
        p2 = m.get_or_create("my-secret", "SECRET")
        assert p1 == p2
        assert m.count == 1

    def test_get_or_create_different_values(self):
        m = SanitizationMapping()
        p1 = m.get_or_create("secret1", "SECRET")
        p2 = m.get_or_create("secret2", "SECRET")
        assert p1 == "<<SECRET_1>>"
        assert p2 == "<<SECRET_2>>"
        assert m.count == 2

    def test_get_or_create_different_types(self):
        m = SanitizationMapping()
        p1 = m.get_or_create("10.0.0.1", "IP")
        p2 = m.get_or_create("my-key-value", "KEY")
        assert p1 == "<<IP_1>>"
        assert p2 == "<<KEY_1>>"

    def test_reverse_lookup(self):
        m = SanitizationMapping()
        m.get_or_create("original_value", "SECRET")
        assert m.reverse_lookup("<<SECRET_1>>") == "original_value"
        assert m.reverse_lookup("<<SECRET_99>>") is None


# ---------------------------------------------------------------------------
# Rule level tests
# ---------------------------------------------------------------------------

class TestRuleLevels:
    def test_minimal_rules_exist(self):
        rules = get_rules_for_level("minimal")
        assert len(rules) > 0

    def test_standard_rules_superset_of_minimal(self):
        minimal = get_rules_for_level("minimal")
        standard = get_rules_for_level("standard")
        assert len(standard) >= len(minimal)

    def test_strict_rules_superset_of_standard(self):
        standard = get_rules_for_level("standard")
        strict = get_rules_for_level("strict")
        assert len(strict) >= len(standard)

    def test_unknown_level_falls_back_to_standard(self):
        rules = get_rules_for_level("unknown_level")
        standard = get_rules_for_level("standard")
        assert len(rules) == len(standard)


# ---------------------------------------------------------------------------
# PrivacyFilter sanitize/desanitize tests
# ---------------------------------------------------------------------------

class TestPrivacyFilterSanitize:
    @pytest.fixture
    def pf(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_FILTER_ENABLED", "true")
        monkeypatch.setenv("PRIVACY_FILTER_LEVEL", "standard")
        monkeypatch.setenv("PRIVACY_INTERNAL_DOMAINS", "")
        monkeypatch.setenv("PRIVACY_CUSTOM_PATTERNS", "")
        # Reload settings is not necessary since PrivacyFilter reads settings at init
        return PrivacyFilter(local_llm=None)

    def test_sanitize_aws_key(self, pf):
        text = 'aws_key = "AKIAIOSFODNN7EXAMPLE"'
        result = pf.sanitize_text(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "<<KEY_" in result

    def test_sanitize_openai_key(self, pf):
        text = 'api_key = "sk-aaaaaaaaaaaaaaaaaaaaaaaa"'
        result = pf.sanitize_text(text)
        assert "sk-aaaaaaaaaaaaaaaaaaaaaaaa" not in result

    def test_sanitize_github_pat(self, pf):
        text = 'token = "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"'
        result = pf.sanitize_text(text)
        assert "ghp_" not in result

    def test_sanitize_password_assignment(self, pf):
        text = 'password = "SuperSecret123!"'
        result = pf.sanitize_text(text)
        assert "SuperSecret123!" not in result

    def test_sanitize_private_ip(self, pf):
        text = "connect to 10.0.1.52 on port 8080"
        result = pf.sanitize_text(text)
        assert "10.0.1.52" not in result
        assert "<<IP_" in result

    def test_sanitize_private_ip_172(self, pf):
        text = "server at 172.16.0.100"
        result = pf.sanitize_text(text)
        assert "172.16.0.100" not in result

    def test_sanitize_private_ip_192(self, pf):
        text = "host = 192.168.1.1"
        result = pf.sanitize_text(text)
        assert "192.168.1.1" not in result

    def test_sanitize_pem_key(self, pf):
        text = """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF8PbnGcE
-----END RSA PRIVATE KEY-----"""
        result = pf.sanitize_text(text)
        assert "MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn" not in result
        assert "<<CERT_" in result

    def test_sanitize_email(self, pf):
        text = "contact: admin@example.com"
        result = pf.sanitize_text(text)
        assert "admin@example.com" not in result
        assert "<<EMAIL_" in result

    def test_sanitize_connection_string(self, pf):
        text = 'DATABASE_URL=postgresql://user:mypassword@db.internal:5432/mydb'
        result = pf.sanitize_text(text)
        assert "mypassword" not in result

    def test_no_sanitize_normal_code(self, pf):
        text = """def calculate_sum(a, b):
    return a + b

class MyService:
    def __init__(self):
        self.name = "test"
"""
        result = pf.sanitize_text(text)
        assert "calculate_sum" in result
        assert "MyService" in result
        assert "<<" not in result

    def test_sanitize_preserves_code_structure(self, pf):
        text = 'config = {"host": "10.0.0.1", "port": 5432}'
        result = pf.sanitize_text(text)
        assert "config" in result
        assert '"port": 5432' in result
        assert "10.0.0.1" not in result

    def test_desanitize_roundtrip(self, pf):
        original = 'password = "MySecret123" and host = "10.0.0.5"'
        sanitized = pf.sanitize_text(original)
        assert "MySecret123" not in sanitized
        assert "10.0.0.5" not in sanitized
        desanitized = pf.desanitize_text(sanitized)
        assert "MySecret123" in desanitized
        assert "10.0.0.5" in desanitized

    def test_consistency_same_value_same_placeholder(self, pf):
        text1 = 'password = "SharedSecret"'
        text2 = 'secret = "SharedSecret"'
        r1 = pf.sanitize_text(text1)
        r2 = pf.sanitize_text(text2)
        # Same value should get same placeholder in both texts
        import re
        placeholders_1 = re.findall(r"<<\w+_\d+>>", r1)
        placeholders_2 = re.findall(r"<<\w+_\d+>>", r2)
        assert len(set(placeholders_1) & set(placeholders_2)) > 0

    def test_desanitize_no_placeholders(self, pf):
        text = "normal text without placeholders"
        assert pf.desanitize_text(text) == text

    def test_sanitize_empty_text(self, pf):
        assert pf.sanitize_text("") == ""
        assert pf.sanitize_text(None) is None

    def test_desanitize_empty_text(self, pf):
        assert pf.desanitize_text("") == ""
        assert pf.desanitize_text(None) is None


# ---------------------------------------------------------------------------
# PrivacyFilter sanitize_messages tests
# ---------------------------------------------------------------------------

class TestPrivacyFilterMessages:
    @pytest.fixture
    def pf(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_FILTER_ENABLED", "true")
        monkeypatch.setenv("PRIVACY_FILTER_LEVEL", "standard")
        monkeypatch.setenv("PRIVACY_INTERNAL_DOMAINS", "")
        monkeypatch.setenv("PRIVACY_CUSTOM_PATTERNS", "")
        return PrivacyFilter(local_llm=None)

    def test_sanitize_messages_content(self, pf):
        messages = [
            {"role": "system", "content": "You are a reviewer"},
            {"role": "user", "content": 'Review: password = "secret123abc"'},
        ]
        sanitized = pf.sanitize_messages(messages)
        # Original unchanged
        assert "secret123abc" in messages[1]["content"]
        # Sanitized copy
        assert "secret123abc" not in sanitized[1]["content"]

    def test_sanitize_messages_preserves_role(self, pf):
        messages = [
            {"role": "user", "content": "host: 10.0.0.1"},
        ]
        sanitized = pf.sanitize_messages(messages)
        assert sanitized[0]["role"] == "user"

    def test_sanitize_messages_is_deep_copy(self, pf):
        messages = [
            {"role": "user", "content": "host: 10.0.0.1"},
        ]
        sanitized = pf.sanitize_messages(messages)
        assert sanitized is not messages
        assert sanitized[0] is not messages[0]

    def test_sanitize_messages_with_pydantic_model(self, pf):
        """ChatCompletionMessage（pydantic 模型）混在 messages 中不应报错。"""
        from openai.types.chat import ChatCompletionMessage

        pydantic_msg = ChatCompletionMessage(
            role="assistant",
            content='Found password = "secret_from_llm_123"',
        )
        messages = [
            {"role": "user", "content": "host: 10.0.0.1"},
            pydantic_msg,
        ]
        sanitized = pf.sanitize_messages(messages)
        assert len(sanitized) == 2
        # All entries should be dicts now
        assert isinstance(sanitized[0], dict)
        assert isinstance(sanitized[1], dict)
        # Sensitive data should be sanitized
        assert "10.0.0.1" not in sanitized[0]["content"]
        assert "secret_from_llm_123" not in sanitized[1]["content"]


# ---------------------------------------------------------------------------
# PrivacyFilter LLM scan result parsing
# ---------------------------------------------------------------------------

class TestLLMScanParsing:
    @pytest.fixture
    def pf(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_FILTER_ENABLED", "true")
        monkeypatch.setenv("PRIVACY_FILTER_LEVEL", "standard")
        monkeypatch.setenv("PRIVACY_INTERNAL_DOMAINS", "")
        monkeypatch.setenv("PRIVACY_CUSTOM_PATTERNS", "")
        return PrivacyFilter(local_llm=None)

    def test_parse_valid_json_array(self, pf):
        content = '[{"value": "my_internal_api_key_123456", "type": "KEY"}]'
        count = pf._parse_llm_scan_result(content)
        assert count == 1
        assert "my_internal_api_key_123456" in pf._dynamic_values

    def test_parse_empty_array(self, pf):
        content = "[]"
        count = pf._parse_llm_scan_result(content)
        assert count == 0

    def test_parse_invalid_json(self, pf):
        content = "No sensitive data found"
        count = pf._parse_llm_scan_result(content)
        assert count == 0

    def test_parse_mixed_response(self, pf):
        content = """Based on my analysis:
[{"value": "internal_token_xyz", "type": "SECRET"}, {"value": "ab", "type": "KEY"}]
"""
        count = pf._parse_llm_scan_result(content)
        # "ab" is < 4 chars, should be skipped
        assert count == 1
        assert "internal_token_xyz" in pf._dynamic_values

    def test_parse_invalid_type_defaults_to_custom(self, pf):
        content = '[{"value": "some_secret_value_here", "type": "UNKNOWN"}]'
        count = pf._parse_llm_scan_result(content)
        assert count == 1
        assert pf._dynamic_values["some_secret_value_here"] == "CUSTOM"


# ---------------------------------------------------------------------------
# Suspicious content detection
# ---------------------------------------------------------------------------

class TestSuspiciousContentDetection:
    @pytest.fixture
    def pf(self, monkeypatch):
        monkeypatch.setenv("PRIVACY_FILTER_ENABLED", "true")
        monkeypatch.setenv("PRIVACY_FILTER_LEVEL", "standard")
        monkeypatch.setenv("PRIVACY_INTERNAL_DOMAINS", "")
        monkeypatch.setenv("PRIVACY_CUSTOM_PATTERNS", "")
        return PrivacyFilter(local_llm=None)

    def test_no_suspicious_in_normal_code(self, pf):
        text = """def hello():
    print("Hello, world!")
    return 42
"""
        assert not pf.has_unmatched_suspicious_content(text)

    def test_suspicious_long_hex(self, pf):
        text = 'custom_key = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6"'
        # This is a long hex string that might be suspicious
        assert pf.has_unmatched_suspicious_content(text)
