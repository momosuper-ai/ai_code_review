from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.router import api_router
from src.config import settings
from src.models import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="OBC AI Code Review",
    description="三层代码质量保障平台：SonarQube + AI Review + Human Review",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {"status": "ok", "env": settings.app_env}
