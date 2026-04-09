"""Privacy filter rule definitions — regex patterns for sensitive data detection."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SensitivePattern:
    """A regex pattern that matches a specific type of sensitive data."""
    name: str
    type_tag: str  # SECRET, KEY, IP, DOMAIN, EMAIL, CERT, CUSTOM
    pattern: re.Pattern
    description: str = ""


# ---------------------------------------------------------------------------
# API Key / Token patterns
# ---------------------------------------------------------------------------

_KEY_PATTERNS = [
    SensitivePattern(
        name="aws_access_key",
        type_tag="KEY",
        pattern=re.compile(r"AKIA[0-9A-Z]{16}"),
        description="AWS Access Key ID",
    ),
    SensitivePattern(
        name="openai_api_key",
        type_tag="KEY",
        pattern=re.compile(r"sk-[a-zA-Z0-9_-]{20,}"),
        description="OpenAI / generic sk- API key",
    ),
    SensitivePattern(
        name="github_pat",
        type_tag="KEY",
        pattern=re.compile(r"ghp_[a-zA-Z0-9]{36}"),
        description="GitHub Personal Access Token",
    ),
    SensitivePattern(
        name="github_oauth",
        type_tag="KEY",
        pattern=re.compile(r"gho_[a-zA-Z0-9]{36}"),
        description="GitHub OAuth Access Token",
    ),
    SensitivePattern(
        name="gitlab_pat",
        type_tag="KEY",
        pattern=re.compile(r"glpat-[a-zA-Z0-9_-]{20,}"),
        description="GitLab Personal Access Token",
    ),
    SensitivePattern(
        name="bearer_token",
        type_tag="KEY",
        pattern=re.compile(r"Bearer\s+[a-zA-Z0-9_.\-/+=]{20,}"),
        description="Bearer token in Authorization header",
    ),
    SensitivePattern(
        name="generic_api_key_hex",
        type_tag="KEY",
        pattern=re.compile(r"(?:api[_-]?key|apikey|access[_-]?token)\s*[:=]\s*[\"']([a-fA-F0-9]{32,})[\"']", re.IGNORECASE),
        description="Generic API key (hex, in assignment context)",
    ),
]

# ---------------------------------------------------------------------------
# Password / Secret assignment patterns
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = [
    SensitivePattern(
        name="password_assignment",
        type_tag="SECRET",
        pattern=re.compile(
            r"(?:password|passwd|pwd|secret|token|credential|auth_key|private_key)"
            r"\s*[:=]\s*[\"']([^\"']{4,})[\"']",
            re.IGNORECASE,
        ),
        description="Password or secret in assignment context",
    ),
    SensitivePattern(
        name="password_env_var",
        type_tag="SECRET",
        pattern=re.compile(
            r"(?:PASSWORD|PASSWD|SECRET|TOKEN|CREDENTIAL|AUTH_KEY|PRIVATE_KEY)"
            r"\s*=\s*([^\s\"'#]{4,})",
        ),
        description="Password or secret in env variable (unquoted)",
    ),
]

# ---------------------------------------------------------------------------
# Connection string patterns
# ---------------------------------------------------------------------------

_CONN_STRING_PATTERNS = [
    SensitivePattern(
        name="connection_string_with_creds",
        type_tag="SECRET",
        pattern=re.compile(
            r"(\w+://[^:]+):([^@]+)@([\w.\-]+:\d+)",
        ),
        description="Connection string with embedded credentials (user:pass@host:port)",
    ),
    SensitivePattern(
        name="jdbc_connection",
        type_tag="SECRET",
        pattern=re.compile(
            r"jdbc:\w+://[\w.\-]+:\d+/\w+\?.*(?:password|user)=[^\s&]+",
            re.IGNORECASE,
        ),
        description="JDBC connection string with credentials",
    ),
]

# ---------------------------------------------------------------------------
# Internal IP patterns (RFC 1918)
# ---------------------------------------------------------------------------

_IP_PATTERNS = [
    SensitivePattern(
        name="private_ip_10",
        type_tag="IP",
        pattern=re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
        description="Private IP (10.x.x.x)",
    ),
    SensitivePattern(
        name="private_ip_172",
        type_tag="IP",
        pattern=re.compile(r"\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"),
        description="Private IP (172.16-31.x.x)",
    ),
    SensitivePattern(
        name="private_ip_192",
        type_tag="IP",
        pattern=re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
        description="Private IP (192.168.x.x)",
    ),
]

# ---------------------------------------------------------------------------
# PEM certificate / private key patterns
# ---------------------------------------------------------------------------

_CERT_PATTERNS = [
    SensitivePattern(
        name="pem_private_key",
        type_tag="CERT",
        pattern=re.compile(
            r"-----BEGIN\s+[\w\s]*PRIVATE\s+KEY-----[\s\S]+?-----END\s+[\w\s]*PRIVATE\s+KEY-----",
        ),
        description="PEM-encoded private key block",
    ),
    SensitivePattern(
        name="pem_certificate",
        type_tag="CERT",
        pattern=re.compile(
            r"-----BEGIN\s+CERTIFICATE-----[\s\S]+?-----END\s+CERTIFICATE-----",
        ),
        description="PEM-encoded certificate block",
    ),
]

# ---------------------------------------------------------------------------
# Email patterns
# ---------------------------------------------------------------------------

_EMAIL_PATTERNS = [
    SensitivePattern(
        name="email_address",
        type_tag="EMAIL",
        pattern=re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),
        description="Email address",
    ),
]

# ---------------------------------------------------------------------------
# High-entropy string patterns (in assignment context)
# ---------------------------------------------------------------------------

_HIGH_ENTROPY_PATTERNS = [
    SensitivePattern(
        name="high_entropy_assignment",
        type_tag="KEY",
        pattern=re.compile(
            r"(?:key|token|secret|password|credential|auth)\s*[:=]\s*[\"']"
            r"([a-zA-Z0-9+/=_\-]{24,})[\"']",
            re.IGNORECASE,
        ),
        description="High-entropy string in security-sensitive assignment context",
    ),
]

# ---------------------------------------------------------------------------
# Rule levels
# ---------------------------------------------------------------------------

RULE_LEVELS: dict[str, list[SensitivePattern]] = {
    "minimal": [
        *_KEY_PATTERNS,
        *_SECRET_PATTERNS,
        *_CERT_PATTERNS,
    ],
    "standard": [
        *_KEY_PATTERNS,
        *_SECRET_PATTERNS,
        *_CERT_PATTERNS,
        *_IP_PATTERNS,
        *_CONN_STRING_PATTERNS,
        *_EMAIL_PATTERNS,
    ],
    "strict": [
        *_KEY_PATTERNS,
        *_SECRET_PATTERNS,
        *_CERT_PATTERNS,
        *_IP_PATTERNS,
        *_CONN_STRING_PATTERNS,
        *_EMAIL_PATTERNS,
        *_HIGH_ENTROPY_PATTERNS,
    ],
}


def get_rules_for_level(level: str) -> list[SensitivePattern]:
    """Return the list of rules for the given level (minimal/standard/strict)."""
    return RULE_LEVELS.get(level, RULE_LEVELS["standard"])
