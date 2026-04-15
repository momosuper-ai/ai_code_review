"""Privacy filter — sanitizes sensitive data before sending to cloud LLM."""

import copy
import json
import re
import time

from src.config import settings
from src.core.privacy_rules import SensitivePattern, get_rules_for_level
from src.utils.logger import logger


# ---------------------------------------------------------------------------
# SanitizationMapping — bidirectional mapping: original <-> placeholder
# ---------------------------------------------------------------------------

class SanitizationMapping:
    """双向映射表：原始值 <-> 占位符，会话级生命周期。"""

    def __init__(self):
        self._forward: dict[str, str] = {}   # original -> placeholder
        self._reverse: dict[str, str] = {}   # placeholder -> original
        self._counters: dict[str, int] = {}   # type_tag -> counter

    def get_or_create(self, value: str, type_tag: str) -> str:
        """查找或创建占位符，保证同一值始终映射到同一占位符。"""
        if value in self._forward:
            return self._forward[value]
        count = self._counters.get(type_tag, 0) + 1
        self._counters[type_tag] = count
        placeholder = f"<<{type_tag}_{count}>>"
        self._forward[value] = placeholder
        self._reverse[placeholder] = value
        return placeholder

    def reverse_lookup(self, placeholder: str) -> str | None:
        """反向查找：占位符 -> 原始值。"""
        return self._reverse.get(placeholder)

    @property
    def count(self) -> int:
        return len(self._forward)


# ---------------------------------------------------------------------------
# PrivacyFilter
# ---------------------------------------------------------------------------

# Placeholder pattern for desanitize
_PLACEHOLDER_RE = re.compile(r"<<[A-Z]+_\d+>>")

# Suspicious content heuristics
_SUSPICIOUS_PATTERNS = [
    # Long hex strings that might be keys
    re.compile(r"[a-fA-F0-9]{32,}"),
    # Long base64-like strings
    re.compile(r"[a-zA-Z0-9+/=_\-]{40,}"),
]

_LLM_SCAN_PROMPT = """请检查以下代码内容，识别其中可能包含的敏感信息（如密码、密钥、Token、内部地址等）。
如果发现敏感信息，请以 JSON 格式列出每个敏感项的：
- value: 敏感值原文
- type: 类型（SECRET/KEY/IP/DOMAIN/EMAIL/CERT/CUSTOM）
如果没有发现敏感信息，请返回空数组 []。

重要：只返回 JSON 数组，不要包含其他文字。

代码内容：
{content}"""


class PrivacyFilter:
    """隐私过滤器：规则引擎 + 可选本地模型扫描。"""

    def __init__(self, local_llm=None):
        from src.core.llm_client import TokenUsage

        self._mapping = SanitizationMapping()
        self._local_llm = local_llm
        self.token_usage = TokenUsage()
        self._rules: list[SensitivePattern] = get_rules_for_level(
            settings.PRIVACY_FILTER_LEVEL
        )
        self._domain_patterns: list[re.Pattern] = self._build_domain_patterns()
        self._cidr_patterns: list[re.Pattern] = self._build_cidr_patterns()
        self._custom_patterns: list[SensitivePattern] = self._load_custom_patterns()
        # Dynamic patterns discovered by local LLM during scanning
        self._dynamic_values: dict[str, str] = {}  # value -> type_tag

    def _build_domain_patterns(self) -> list[re.Pattern]:
        """从配置构建内部域名匹配模式。"""
        domains_str = settings.PRIVACY_INTERNAL_DOMAINS
        if not domains_str.strip():
            return []
        patterns = []
        for domain in domains_str.split(","):
            domain = domain.strip()
            if domain:
                escaped = re.escape(domain)
                patterns.append(re.compile(
                    rf"\b[\w.-]*{escaped}\b", re.IGNORECASE,
                ))
        return patterns

    def _build_cidr_patterns(self) -> list[re.Pattern]:
        """CIDR patterns are already covered by the built-in IP rules."""
        return []

    def _load_custom_patterns(self) -> list[SensitivePattern]:
        """从配置加载自定义正则模式。"""
        raw = settings.PRIVACY_CUSTOM_PATTERNS.strip()
        if not raw:
            return []
        try:
            items = json.loads(raw)
            patterns = []
            for item in items:
                patterns.append(SensitivePattern(
                    name=item.get("name", "custom"),
                    type_tag=item.get("type", "CUSTOM"),
                    pattern=re.compile(item["pattern"]),
                    description=item.get("description", ""),
                ))
            return patterns
        except (json.JSONDecodeError, KeyError, re.error) as exc:
            logger.warning("自定义隐私规则解析失败: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Phase 1: Pre-scan diff
    # ------------------------------------------------------------------

    async def pre_scan(self, diff_text: str) -> None:
        """Phase 1: 规则引擎 + 本地模型预扫描 diff，构建初始敏感模式集。"""
        t0 = time.time()

        # Step 1: Rule engine scan
        rule_count = self._rule_scan(diff_text)

        # Step 2: Local LLM scan (if enabled)
        llm_count = 0
        if (
            self._local_llm
            and settings.PRIVACY_PRE_SCAN_ENABLED
            and self.has_unmatched_suspicious_content(diff_text)
        ):
            llm_count = await self._llm_scan(diff_text)
            # LLM 发现的新值已写入 _dynamic_values，需要再跑一次 sanitize
            # 将它们注册到映射表中，以便后续一致替换
            if llm_count > 0:
                self.sanitize_text(diff_text)

        duration_ms = (time.time() - t0) * 1000
        total = self._mapping.count
        logger.info(
            "隐私预扫描完成: 规则发现=%d 本地模型发现=%d 总计=%d (%.0f毫秒)",
            rule_count, llm_count, total, duration_ms,
        )

    # ------------------------------------------------------------------
    # Phase 2: Scan tool results
    # ------------------------------------------------------------------

    async def scan_tool_results(self, tool_results: list[str]) -> None:
        """Phase 2 辅助: 用本地模型扫描本轮工具结果，发现新敏感模式。"""
        if not tool_results:
            return

        combined = "\n---\n".join(tool_results)

        # Rule engine scan first
        self._rule_scan(combined)

        # If hybrid mode and there's suspicious unmatched content, call local LLM
        if (
            self._local_llm
            and settings.PRIVACY_TOOL_SCAN_MODE == "hybrid"
            and self.has_unmatched_suspicious_content(combined)
        ):
            new_count = await self._llm_scan(combined)
            if new_count > 0:
                self.sanitize_text(combined)

    # ------------------------------------------------------------------
    # Sanitize / Desanitize
    # ------------------------------------------------------------------

    def sanitize_text(self, text: str) -> str:
        """对文本应用规则替换，返回脱敏文本。"""
        if not text:
            return text

        result = text

        # Apply dynamic values (discovered by LLM)
        for value, type_tag in self._dynamic_values.items():
            if value in result:
                placeholder = self._mapping.get_or_create(value, type_tag)
                result = result.replace(value, placeholder)

        # Apply built-in rules
        for rule in self._rules:
            for match in rule.pattern.finditer(result):
                # Use the full match or the first group if available
                matched_value = match.group(1) if match.lastindex else match.group(0)
                if matched_value and len(matched_value) >= 4:
                    placeholder = self._mapping.get_or_create(matched_value, rule.type_tag)
                    result = result.replace(matched_value, placeholder)

        # Apply custom patterns
        for rule in self._custom_patterns:
            for match in rule.pattern.finditer(result):
                matched_value = match.group(1) if match.lastindex else match.group(0)
                if matched_value and len(matched_value) >= 4:
                    placeholder = self._mapping.get_or_create(matched_value, rule.type_tag)
                    result = result.replace(matched_value, placeholder)

        # Apply domain patterns
        for pat in self._domain_patterns:
            for match in pat.finditer(result):
                matched_value = match.group(0)
                if matched_value:
                    placeholder = self._mapping.get_or_create(matched_value, "DOMAIN")
                    result = result.replace(matched_value, placeholder)

        return result

    def sanitize_messages(self, messages: list) -> list[dict]:
        """深拷贝并脱敏所有消息的 content 字段（含 tool_calls 参数）。

        messages 中可能混合 dict 和 ChatCompletionMessage（pydantic 模型），
        统一转为 dict 再进行脱敏。
        """
        sanitized: list[dict] = []
        for msg in messages:
            # 将 pydantic 模型转为 dict
            if isinstance(msg, dict):
                msg_dict = copy.deepcopy(msg)
            elif hasattr(msg, "model_dump"):
                # OpenAI SDK pydantic v2 model
                msg_dict = msg.model_dump(exclude_none=True)
            elif hasattr(msg, "dict"):
                # pydantic v1 fallback
                msg_dict = msg.dict(exclude_none=True)
            else:
                sanitized.append(msg)
                continue

            # Sanitize content
            if isinstance(msg_dict.get("content"), str):
                msg_dict["content"] = self.sanitize_text(msg_dict["content"])

            # Sanitize tool call arguments
            tool_calls = msg_dict.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function") if isinstance(tc, dict) else None
                    if func and isinstance(func.get("arguments"), str):
                        func["arguments"] = self.sanitize_text(func["arguments"])

            sanitized.append(msg_dict)

        return sanitized

    def desanitize_text(self, text: str) -> str:
        """反向替换：占位符 -> 原始值。"""
        if not text:
            return text

        def _replace(match: re.Match) -> str:
            original = self._mapping.reverse_lookup(match.group(0))
            return original if original is not None else match.group(0)

        return _PLACEHOLDER_RE.sub(_replace, text)

    # ------------------------------------------------------------------
    # Suspicious content detection
    # ------------------------------------------------------------------

    def has_unmatched_suspicious_content(self, text: str) -> bool:
        """启发式检查：文本中是否存在规则引擎未匹配但看起来可疑的内容。"""
        # First, sanitize a copy to remove known matches
        sanitized = self.sanitize_text(text)

        # Check if there are still suspicious patterns in the sanitized text
        for pat in _SUSPICIOUS_PATTERNS:
            match = pat.search(sanitized)
            if match:
                # Skip if it looks like a placeholder we created
                matched_str = match.group(0)
                if _PLACEHOLDER_RE.search(matched_str):
                    continue
                return True

        return False

    # ------------------------------------------------------------------
    # Internal: rule engine scan
    # ------------------------------------------------------------------

    def _rule_scan(self, text: str) -> int:
        """用规则引擎扫描文本，将发现的敏感值注册到映射表。返回新发现数量。"""
        initial_count = self._mapping.count
        self.sanitize_text(text)
        return self._mapping.count - initial_count

    # ------------------------------------------------------------------
    # Internal: local LLM scan
    # ------------------------------------------------------------------

    async def _llm_scan(self, text: str) -> int:
        """用本地模型扫描文本，发现新的敏感模式并追加到动态值集。"""
        if not self._local_llm:
            return 0

        # Truncate very long text to avoid overwhelming the local model
        max_chars = 15000
        scan_text = text[:max_chars] if len(text) > max_chars else text

        prompt = _LLM_SCAN_PROMPT.format(content=scan_text)
        messages = [{"role": "user", "content": prompt}]

        try:
            t0 = time.time()
            chat_result = await self._local_llm.chat(
                messages, tools=None, temperature=0.0, max_tokens=2048,
            )
            duration_ms = (time.time() - t0) * 1000
            content = chat_result.message.content or ""
            self.token_usage += chat_result.usage
            logger.debug("本地模型隐私扫描完成 (%.0f毫秒)", duration_ms)
        except Exception as exc:
            logger.warning("本地模型隐私扫描调用失败: %s", exc)
            return 0

        return self._parse_llm_scan_result(content)

    def _parse_llm_scan_result(self, content: str) -> int:
        """解析本地模型扫描结果，将新的敏感值加入动态值集。"""
        # Extract JSON array from response
        content = content.strip()
        # Try to find JSON array
        array_match = re.search(r"\[[\s\S]*\]", content)
        if not array_match:
            return 0

        try:
            items = json.loads(array_match.group())
        except json.JSONDecodeError:
            return 0

        if not isinstance(items, list):
            return 0

        new_count = 0
        valid_types = {"SECRET", "KEY", "IP", "DOMAIN", "EMAIL", "CERT", "CUSTOM"}
        for item in items:
            if not isinstance(item, dict):
                continue
            value = item.get("value", "").strip()
            type_tag = item.get("type", "CUSTOM").upper().strip()
            if type_tag not in valid_types:
                type_tag = "CUSTOM"
            if value and len(value) >= 4 and value not in self._dynamic_values:
                self._dynamic_values[value] = type_tag
                new_count += 1

        if new_count > 0:
            logger.info("本地模型发现 %d 个新敏感模式", new_count)

        return new_count
