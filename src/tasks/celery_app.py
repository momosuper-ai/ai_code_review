from celery import Celery
from celery.signals import worker_init

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
        "src.tasks.readme_task.generate_readme": {"queue": "readme"},
    },
)

celery_app.conf.include = [
    "src.tasks.review_task",
    "src.tasks.readme_task",
]


@worker_init.connect
def verify_db_connection(**kwargs):
    """Worker 启动时验证数据库连接，失败则终止进程等待重启。"""
    import sys
    import time as _time

    from sqlalchemy import text

    from src.models import SyncSession, ensure_database_exists
    from src.utils.logger import logger

    max_attempts = 5
    retry_delay = 3
    logger.info("Celery worker 启动，验证数据库连接...")

    for attempt in range(1, max_attempts + 1):
        try:
            ensure_database_exists()
            with SyncSession() as session:
                session.execute(text("SELECT 1"))
            logger.info("数据库连接验证通过")
            return
        except Exception as e:
            if attempt == max_attempts:
                logger.error(
                    "数据库连接验证失败，已重试 %d 次，终止 worker: %s",
                    max_attempts, e,
                )
                sys.exit(1)
            logger.warning(
                "数据库连接验证失败 (第 %d/%d 次)，%d 秒后重试: %s",
                attempt, max_attempts, retry_delay, e,
            )
            _time.sleep(retry_delay)
