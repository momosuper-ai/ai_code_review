"""vLLM client using OpenAI-compatible API with tool calling support."""

import asyncio
import json
import random
import time
from dataclasses import dataclass

from openai import AsyncOpenAI, APIConnectionError, APIStatusError, BadRequestError, RateLimitError
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.core.http_transport import TIMEOUT_PHASES
from src.utils.logger import logger


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __iadd__(self, other: "TokenUsage") -> "TokenUsage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        return self


@dataclass
class ChatResult:
    message: ChatCompletionMessage
    usage: TokenUsage
    finish_reason: str | None = None
    reasoning_tokens: int | None = None
    reasoning_chars: int = 0
    response_model: str | None = None


# Sentinel：未传 response_format 时走自动决策；显式传 None 表示禁用；显式传 dict 表示透传
_RESPONSE_FORMAT_AUTO = object()
_EXTRA_BODY_AUTO = object()


def _transport_phase(exc: BaseException) -> str:
    """Classify a bounded exception chain without inspecting provider text."""
    pending, seen = [exc], set()
    while pending and len(seen) < 16:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        for kind, phase in TIMEOUT_PHASES:
            if isinstance(current, kind):
                return phase
        # The SDK wraps httpx failures. Only inspect exception identity/types;
        # messages and request/response objects may contain credentials.
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException) and id(linked) not in seen:
                pending.append(linked)
    return "unknown"


class LLMClient:
    def __init__(self, model_name: str | None = None, sdk_retries: int | None = None,
                 metadata_errors: bool = False):
        client_options = {"base_url": settings.VLLM_API_URL, "api_key": settings.VLLM_API_KEY}
        if sdk_retries is not None:
            client_options["max_retries"] = sdk_retries
        self._client = AsyncOpenAI(**client_options)
        self._model = model_name or settings.VLLM_MODEL_NAME
        self._privacy_filter = None  # PrivacyFilter | None
        self._metadata_errors = metadata_errors
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.total_cached_tokens = 0

    def set_privacy_filter(self, pf) -> None:
        """注入隐私过滤器，chat() 调用时自动脱敏。"""
        self._privacy_filter = pf

    def _is_retryable(self, exc: Exception) -> bool:
        """判断异常是否为可重试的瞬态错误。"""
        if isinstance(exc, RateLimitError):
            return True
        if isinstance(exc, APIConnectionError):
            return True
        if isinstance(exc, APIStatusError) and exc.status_code in {500, 502, 503, 529}:
            return True
        if isinstance(exc, BadRequestError) and "rate" in str(exc).lower():
            return True
        return False

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        response_format=_RESPONSE_FORMAT_AUTO,
        json_schema_builder=None,
        extra_body=_EXTRA_BODY_AUTO,
        retry_max: int | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatResult:
        """Send a chat completion request, optionally with tools.

        max_tokens 为 None 时使用 settings.LLM_MAX_TOKENS。

        response_format 三态:
          - 未传(默认 _RESPONSE_FORMAT_AUTO): 自动决策——有 tools 不挂；否则按
            settings.response_format_mode_for(model) 决定挂 json_object/json_schema 或不挂
          - None: 显式禁用（README 等 Markdown 输出场景）
          - dict: 显式透传（测试/特殊调用）

        json_schema_builder: callable 返回完整 {'type':'json_schema',...} dict，
        仅在 mode=json_schema 自动决策时使用；缺失会降级为 json_object。
        extra_body: 默认沿用全局配置，dict 替换本次配置，None 禁用。
        retry_max: 默认沿用全局重试次数；Brain 由持久任务控制重试并逐次预留预算。
        tool_choice: 可显式选择结构化输出函数；未传时原工具循环保持 auto。
        """
        from src.core.context_manager import trim_messages
        from src.core.dimensions import build_response_format

        if max_tokens is None:
            max_tokens = settings.LLM_MAX_TOKENS

        messages = trim_messages(messages, reserve_for_completion=max_tokens)

        # 隐私过滤：脱敏后再发送
        if self._privacy_filter:
            messages = self._privacy_filter.sanitize_messages(messages)

        kwargs = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        elif tool_choice is not None:
            raise ValueError("指定输出函数时必须提供工具定义")

        # 决定本次调用的 response_format
        if response_format is _RESPONSE_FORMAT_AUTO:
            if tools:
                final_rf = None  # 工具循环阶段绝不挂 response_format
            else:
                mode = settings.response_format_mode_for(self._model)
                final_rf = build_response_format(mode, json_schema_builder)
        elif response_format is None:
            final_rf = None  # 显式禁用
        else:
            final_rf = response_format  # 显式指定

        if final_rf is not None:
            kwargs["response_format"] = final_rf

        # 模型特性透传（thinking / reasoning_effort 等，由 .env 配置）
        if extra_body is _EXTRA_BODY_AUTO and settings.LLM_EXTRA_BODY:
            try:
                configured_body = json.loads(settings.LLM_EXTRA_BODY)
                if isinstance(configured_body, dict):
                    kwargs["extra_body"] = configured_body
            except json.JSONDecodeError:
                logger.warning("LLM_EXTRA_BODY 解析失败，忽略该配置")
        elif extra_body is not _EXTRA_BODY_AUTO and extra_body is not None:
            if not isinstance(extra_body, dict):
                raise ValueError("LLM 扩展参数必须是对象或 None")
            kwargs["extra_body"] = extra_body

        max_retries = settings.LLM_RETRY_MAX if retry_max is None else retry_max
        if max_retries < 0:
            raise ValueError("LLM 重试次数不能小于零")

        for attempt in range(max_retries + 1):
            try:
                t0 = time.time()
                response = await self._client.chat.completions.create(**kwargs)
                duration_ms = (time.time() - t0) * 1000
                usage = response.usage
                cached = 0
                if usage:
                    self.total_prompt_tokens += usage.prompt_tokens
                    self.total_completion_tokens += usage.completion_tokens
                    if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
                        cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
                    self.total_cached_tokens += cached
                call_usage = TokenUsage(
                    prompt_tokens=usage.prompt_tokens if usage else 0,
                    completion_tokens=usage.completion_tokens if usage else 0,
                    cached_tokens=cached,
                )
                logger.info(
                    "LLM 调用: 模型=%s 消息数=%d 工具=%s rf=%s token用量=%s (%.0f毫秒)",
                    self._model,
                    len(messages),
                    "是" if tools else "否",
                    (final_rf.get("type") if final_rf else "none"),
                    f"{usage.prompt_tokens}+{usage.completion_tokens}(cached:{cached})" if usage else "无",
                    duration_ms,
                )
                # 截断预警：completion 达到 max_tokens 95% 说明输出可能未写完（常见 JSON 语法错诱因）
                if usage and usage.completion_tokens >= int(max_tokens * 0.95):
                    finish = getattr(response.choices[0], "finish_reason", None)
                    logger.warning(
                        "LLM 输出可能被截断: completion=%d ≥ 95%% of max_tokens=%d, finish_reason=%s",
                        usage.completion_tokens, max_tokens, finish,
                    )
                details = getattr(usage, "completion_tokens_details", None) if usage else None
                return ChatResult(message=response.choices[0].message, usage=call_usage,
                                  response_model=getattr(response, "model", None),
                                  finish_reason=getattr(response.choices[0], "finish_reason", None),
                                  reasoning_tokens=getattr(details, "reasoning_tokens", None),
                                  reasoning_chars=sum(len(getattr(response.choices[0].message, key, "") or "")
                                                      for key in ("reasoning", "reasoning_content")
                                                      if isinstance(getattr(response.choices[0].message, key, ""), str)))
            except Exception as exc:
                if attempt < max_retries and self._is_retryable(exc):
                    delay = min(
                        settings.LLM_RETRY_BASE_DELAY * (2 ** attempt) + random.random(),
                        settings.LLM_RETRY_MAX_DELAY,
                    )
                    if self._metadata_errors:
                        logger.warning("LLM API 调用失败 (可重试): 第 %d/%d 次重试 延迟=%.1f 错误类型=%s HTTP状态=%s transport_phase=%s",
                                       attempt + 1, max_retries, delay, type(exc).__name__, getattr(exc, "status_code", None),
                                       _transport_phase(exc))
                    else:
                        logger.warning(
                            "LLM API 调用失败 (可重试), 第 %d/%d 次重试, %.1f秒后重试: %s: %s",
                            attempt + 1, max_retries, delay, type(exc).__name__, exc,
                        )
                    await asyncio.sleep(delay)
                    continue
                if self._metadata_errors:
                    logger.error("LLM API 调用失败: 模型=%s 错误类型=%s HTTP状态=%s transport_phase=%s",
                                 self._model, type(exc).__name__, getattr(exc, "status_code", None), _transport_phase(exc))
                else:
                    logger.error("LLM API 调用失败: 模型=%s 错误类型=%s 错误=%s", self._model, type(exc).__name__, exc)
                raise

    async def aclose(self):
        """关闭底层 HTTP 客户端连接。"""
        await self._client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
