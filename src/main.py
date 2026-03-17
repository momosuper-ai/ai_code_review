import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from src.api.router import api_router
from src.models import Base, engine
from src.models.review import Review, ReviewComment  # noqa: F401
from src.utils.logger import logger

_DB_RETRY_MAX = 5
_DB_RETRY_DELAY = 3  # 秒


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("AI 代码审查服务启动中...")
    for attempt in range(1, _DB_RETRY_MAX + 1):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            logger.info("数据库表初始化完成")
            break
        except Exception as exc:
            if attempt == _DB_RETRY_MAX:
                logger.error("数据库连接失败，已重试 %d 次，放弃启动: %s", _DB_RETRY_MAX, exc)
                raise
            logger.warning(
                "数据库连接失败 (第 %d/%d 次)，%d 秒后重试: %s",
                attempt, _DB_RETRY_MAX, _DB_RETRY_DELAY, exc,
            )
            await asyncio.sleep(_DB_RETRY_DELAY)
    yield
    logger.info("AI 代码审查服务关闭")


app = FastAPI(title="AI Code Review Service", version="0.1.0", lifespan=lifespan)

app.include_router(api_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration_ms = (time.time() - start) * 1000
    logger.info(
        "HTTP 请求: %s %s -> 状态码 %d (%.1f毫秒)",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/health")
async def health():
    return {"status": "ok"}
