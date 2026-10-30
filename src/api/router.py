from fastapi import APIRouter

from src.api.v1 import brain, webhooks, reviews

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
api_router.include_router(reviews.router, prefix="/reviews", tags=["reviews"])
api_router.include_router(brain.router)
