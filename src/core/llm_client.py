"""vLLM client using OpenAI-compatible API with tool calling support."""

import time

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessage

from src.config import settings
from src.utils.logger import logger


class LLMClient:
    def __init__(self):
        self._client = AsyncOpenAI(
            base_url=settings.VLLM_API_URL,
            api_key=settings.VLLM_API_KEY,
        )
        self._model = settings.VLLM_MODEL_NAME

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
    ) -> ChatCompletionMessage:
        """Send a chat completion request, optionally with tools."""
        from src.core.context_manager import trim_messages

        messages = trim_messages(messages, reserve_for_completion=max_tokens)

        kwargs = {
            "model": self._model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            t0 = time.time()
            response = await self._client.chat.completions.create(**kwargs)
            duration_ms = (time.time() - t0) * 1000
            usage = response.usage
            logger.info(
                "LLM 调用: 模型=%s 消息数=%d 工具=%s token用量=%s (%.0f毫秒)",
                self._model,
                len(messages),
                "是" if tools else "否",
                f"{usage.prompt_tokens}+{usage.completion_tokens}" if usage else "无",
                duration_ms,
            )
            return response.choices[0].message
        except Exception as exc:
            logger.error("LLM API 调用失败: 模型=%s 错误类型=%s 错误=%s", self._model, type(exc).__name__, exc)
            raise

    async def aclose(self):
        """关闭底层 HTTP 客户端连接。"""
        await self._client.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
