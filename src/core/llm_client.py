from openai import AsyncOpenAI

from src.config import settings
from src.utils.logger import logger


class LLMClient:
    """vLLM API 客户端（OpenAI 兼容格式）。"""

    def __init__(self):
        self.client = AsyncOpenAI(
            base_url=settings.vllm_api_url,
            api_key="not-needed",  # vLLM 通常不需要 API key
        )
        self.model = settings.vllm_model_name

    async def review_code(self, prompt: str, max_tokens: int = 4096) -> str:
        """调用 LLM 进行代码审查。"""
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是一位资深的代码审查专家，请用中文进行代码审查。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.1,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            raise


llm_client = LLMClient()
