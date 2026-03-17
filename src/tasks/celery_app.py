from celery import Celery

from src.config import settings

celery_app = Celery(
    "obc_review",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_routes={
        "src.tasks.review_task.run_review": {"queue": "review"},
    },
)

celery_app.autodiscover_tasks(["src.tasks"], related_name="review_task")
