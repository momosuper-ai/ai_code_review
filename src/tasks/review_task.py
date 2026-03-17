"""Celery task that orchestrates the full review pipeline."""

import asyncio
import time
from datetime import datetime

from src.core.gitlab_client import GitLabClient
from src.core.repo_manager import RepoManager
from src.core.review_engine import ReviewEngine
from src.models import SyncSession
from src.models.review import Review, ReviewComment, ReviewStatus, Severity, TriggerType
from src.tasks.celery_app import celery_app
from src.utils.logger import logger


@celery_app.task(
    bind=True,
    name="src.tasks.review_task.run_review",
    soft_time_limit=600,
    max_retries=2,
    default_retry_delay=60,
)
def run_review(
    self,
    trigger_type: str,
    project_id: int,
    project_name: str,
    branch: str,
    commit_sha: str,
    author: str,
    mr_iid: int | None = None,
    mr_title: str | None = None,
    before_sha: str | None = None,
):
    """Main review task entry point."""
    logger.info(
        "开始审查任务: 项目=%s 触发=%s 提交=%s",
        project_name, trigger_type, commit_sha[:8],
    )
    task_start = time.time()

    # 1. 同步创建 review 记录
    with SyncSession() as session:
        review = Review(
            gitlab_project_id=project_id,
            project_name=project_name,
            trigger_type=TriggerType(trigger_type),
            merge_request_iid=mr_iid,
            merge_request_title=mr_title,
            commit_sha=commit_sha,
            branch=branch,
            author=author,
            status=ReviewStatus.in_progress,
        )
        session.add(review)
        session.commit()
        session.refresh(review)
        review_id = review.id

    logger.info("[review:%d] 开始审查: 项目=%s 提交=%s", review_id, project_name, commit_sha[:8])

    try:
        # 2. async 部分：clone repo、获取 diff、LLM 审查、发 GitLab 评论
        result = asyncio.run(
            _do_review_async(
                trigger_type=trigger_type,
                project_id=project_id,
                project_name=project_name,
                branch=branch,
                commit_sha=commit_sha,
                mr_iid=mr_iid,
                before_sha=before_sha,
                review_id=review_id,
            )
        )

        # 3. 同步写入审查结果
        t0 = time.time()
        with SyncSession() as session:
            review_obj = session.get(Review, review_id)
            review_obj.status = ReviewStatus.completed
            review_obj.summary = result["overall_assessment"]
            review_obj.files_reviewed = result["files_count"]
            review_obj.issues_found = len(result["issues"])
            review_obj.completed_at = datetime.utcnow()

            for issue in result["issues"]:
                comment = ReviewComment(
                    review_id=review_id,
                    file_path=issue.get("file_path", ""),
                    line_start=issue.get("line_start"),
                    line_end=issue.get("line_end"),
                    severity=Severity(issue.get("severity", "info")),
                    category=issue.get("category", ""),
                    content=issue.get("content", ""),
                    suggestion=issue.get("suggestion"),
                    code_snippet=issue.get("code_snippet"),
                )
                session.add(comment)

            session.commit()
        logger.info("[review:%d] 结果已持久化到数据库 (%.1f秒)", review_id, time.time() - t0)

        total_time = time.time() - task_start
        logger.info(
            "[review:%d] 审查流程结束: 文件数=%d 问题数=%d 评级=%s 总耗时=%.1f秒",
            review_id, result["files_count"], len(result["issues"]),
            result["score"], total_time,
        )

    except Exception as exc:
        # 4. 同步标记失败
        with SyncSession() as session:
            review_obj = session.get(Review, review_id)
            if review_obj:
                review_obj.status = ReviewStatus.failed
                review_obj.error_message = str(exc)[:2000]
                review_obj.completed_at = datetime.utcnow()
                session.commit()
        logger.error(
            "审查任务失败: 项目=%s 提交=%s 重试次数=%d/%d 错误=%s",
            project_name, commit_sha[:8], self.request.retries, self.max_retries, exc,
        )
        raise self.retry(exc=exc)


async def _do_review_async(
    trigger_type: str,
    project_id: int,
    project_name: str,
    branch: str,
    commit_sha: str,
    mr_iid: int | None,
    before_sha: str | None,
    review_id: int,
) -> dict:
    """执行非 DB 的 async 操作：clone repo、获取 diff、LLM 审查、发 GitLab 评论。"""
    gitlab = GitLabClient()
    repo_mgr = RepoManager()

    # 1. Ensure local repo
    t0 = time.time()
    repo_dir = await repo_mgr.ensure_repo(project_name, branch)
    logger.info("[review:%d] 仓库准备就绪 (%.1f秒)", review_id, time.time() - t0)

    # 2. Get changes
    t0 = time.time()
    if trigger_type == "merge_request" and mr_iid:
        data = await gitlab.get_mr_changes(project_id, mr_iid)
        changes = data.get("changes", [])
    elif before_sha:
        data = await gitlab.get_compare(project_id, before_sha, commit_sha)
        changes = data.get("diffs", [])
    else:
        logger.warning("[review:%d] 无 diff 来源，跳过审查", review_id)
        return {"overall_assessment": "", "files_count": 0, "issues": [], "score": "N/A"}
    logger.info("[review:%d] 获取变更完成: %d 个文件 (%.1f秒)", review_id, len(changes), time.time() - t0)

    # 3. Run review engine
    engine = ReviewEngine(repo_dir)
    filtered = engine.filter_diffs(changes)
    files_count = len(filtered)
    logger.info(
        "[review:%d] Diff 过滤: %d -> %d 个文件 (跳过 %d 个)",
        review_id, len(changes), files_count, len(changes) - files_count,
    )

    t0 = time.time()
    review_result = await engine.run_review(filtered, review_id=review_id)
    logger.info(
        "[review:%d] 审查完成: 评级=%s 问题数=%d 工具调用=%d (%.1f秒)",
        review_id, review_result.score, len(review_result.issues),
        review_result.tool_calls_count, time.time() - t0,
    )

    # 4. Format and post comment
    comment_body = ReviewEngine.format_gitlab_comment(review_result, files_count)

    t0 = time.time()
    if trigger_type == "merge_request" and mr_iid:
        await gitlab.post_mr_note(project_id, mr_iid, comment_body)
        logger.info("[review:%d] 评论已发布到 GitLab MR#%s (%.1f秒)", review_id, mr_iid, time.time() - t0)
    else:
        await gitlab.post_commit_comment(project_id, commit_sha, comment_body)
        logger.info("[review:%d] 评论已发布到 GitLab 提交 %s (%.1f秒)", review_id, commit_sha[:8], time.time() - t0)

    # 5. Update local repo to latest
    await repo_mgr.update_repo(project_name, branch)

    return {
        "overall_assessment": review_result.overall_assessment,
        "files_count": files_count,
        "issues": review_result.issues,
        "score": review_result.score,
    }
