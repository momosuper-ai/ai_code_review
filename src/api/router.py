from fastapi import APIRouter

from src.api.v1.projects import router as projects_router
from src.api.v1.reviews import router as reviews_router
from src.api.v1.webhooks import router as webhooks_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(webhooks_router)
api_router.include_router(reviews_router)
api_router.include_router(projects_router)
