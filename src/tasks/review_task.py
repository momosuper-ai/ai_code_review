import asyncio

from src.tasks.celery_app import celery_app
from src.utils.logger import logger


@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def run_ai_review(self, project_id: int, mr_iid: int):
    """异步执行 AI 代码审查任务。

    Args:
        project_id: GitLab 项目 ID
        mr_iid: MR IID
    """
    logger.info(f"开始 AI 审查任务: 项目={project_id}, MR={mr_iid}")

    try:
        from src.core.review_engine import run_review

        review_id = asyncio.run(run_review(project_id, mr_iid))
        logger.info(f"AI 审查任务完成: 审查ID={review_id}")
        return {"review_id": review_id, "status": "completed"}
    except Exception as exc:
        logger.error(f"AI 审查任务失败: 项目={project_id}, MR={mr_iid}, 错误={exc}")
        raise self.retry(exc=exc)
