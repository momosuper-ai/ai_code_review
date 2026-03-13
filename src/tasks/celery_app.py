from celery import Celery

from src.config import settings

celery_app = Celery(
    "obc_review",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # 任务超时设置
    task_soft_time_limit=300,  # 5 分钟软超时
    task_time_limit=600,  # 10 分钟硬超时
)
