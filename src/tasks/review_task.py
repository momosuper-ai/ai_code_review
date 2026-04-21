"""Celery task that orchestrates the full review pipeline."""

import asyncio
import time
from datetime import datetime, timedelta

from sqlalchemy import select

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.repo_manager import RepoManager
from src.core.review_engine import ReviewEngine
from src.models import SyncSession
from src.models.review import Review, ReviewComment, ReviewStatus, Severity, TriggerType
from src.tasks.celery_app import celery_app
from src.utils.logger import logger

# 幂等窗口：同一 (project_id, commit_sha) 在该窗口内已存在 in_progress/completed review 则跳过
_REVIEW_DEDUP_WINDOW = timedelta(minutes=30)


def _should_dispatch_readme(
    trigger_type: str,
    mr_action: str | None,
    branch: str,
) -> bool:
    """README 触发矩阵：push 到主分支，或 MR merge action 到主分支。

    Task 层防御性校验，与 webhook 层过滤组合形成双保险：
    - push 到特性分支 → False
    - MR open/update/reopen（即使透传到 task）→ False
    - push 到主分支 或 merge_request+merge 到主分支 → True
    """
    if not settings.README_GENERATION_ENABLED:
        return False
    if branch not in settings.main_branch_list:
        return False
    if trigger_type == "push":
        return True
    if trigger_type == "merge_request" and mr_action == "merge":
        return True
    return False


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
    mr_action: str | None = None,
    before_sha: str | None = None,
):
    """Main review task entry point."""
    logger.info(
        "开始审查任务: 项目=%s 触发=%s 提交=%s",
        project_name, trigger_type, commit_sha[:8],
    )
    task_start = time.time()

    # 1. 入口去重：同一 (project_id, commit_sha) 近 30 分钟内已有 in_progress/completed 审查则跳过
    existing = _find_recent_review(project_id, commit_sha)
    if existing is not None:
        logger.info(
            "审查任务已去重跳过: 项目=%s 提交=%s 触发=%s 现有=review_id=%d status=%s",
            project_name, commit_sha[:8], trigger_type, existing.id, existing.status.value,
        )
        return {
            "status": "skipped",
            "reason": "duplicate",
            "existing_review_id": existing.id,
        }

    # 2. 同步创建 review 记录
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
        # 2. async 部分：clone repo、获取 diff、LLM 审查（不发评论）
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

        # 3. 先持久化到 DB
        _save_review_result(review_id, result)

        # 维度统计日志
        for stat in result.get("dimension_stats", []):
            dim_tokens = stat.get("prompt_tokens", 0) + stat.get("completion_tokens", 0)
            token_detail = (
                f" token={dim_tokens}(输入:{stat.get('prompt_tokens', 0)}"
                f"/输出:{stat.get('completion_tokens', 0)}"
                f"/缓存:{stat.get('cached_tokens', 0)})"
            ) if dim_tokens > 0 else ""
            logger.info(
                "[review:%d] 维度 %s: 评级=%s 问题数=%d 工具调用=%d 耗时=%.1fs%s %s",
                review_id, stat["display_name"], stat["score"],
                stat["issues_count"], stat["tool_calls"], stat["duration"],
                token_detail,
                f"(失败: {stat['error']})" if not stat["success"] else "",
            )

        # 4. DB 成功后再发评论（失败不触发任务重试）
        try:
            asyncio.run(
                _post_comment_async(
                    trigger_type=trigger_type,
                    project_id=project_id,
                    mr_iid=mr_iid,
                    commit_sha=commit_sha,
                    comment_body=result["comment_body"],
                    review_id=review_id,
                )
            )
        except Exception:
            logger.error("[review:%d] GitLab 评论发送失败", review_id, exc_info=True)

        # 5. 触发文档生成（push 到主分支 或 MR merge 到主分支；失败不影响审查流程）
        if _should_dispatch_readme(trigger_type, mr_action, branch):
            try:
                from src.tasks.readme_task import generate_readme
                generate_readme.delay(
                    trigger_type=trigger_type,
                    project_id=project_id,
                    project_name=project_name,
                    branch=branch,
                    commit_sha=commit_sha,
                )
                logger.info(
                    "[review:%d] 已分发文档生成任务 trigger=%s branch=%s",
                    review_id, trigger_type, branch,
                )
            except Exception:
                logger.warning("[review:%d] 文档生成任务分发失败", review_id, exc_info=True)

        total_time = time.time() - task_start
        total_tokens = result.get("total_prompt_tokens", 0) + result.get("total_completion_tokens", 0)
        logger.info(
            "[review:%d] 审查流程结束: 文件数=%d 问题数=%d 评级=%s 总耗时=%.1f秒 token用量=%d(输入:%d/输出:%d/缓存:%d)",
            review_id, result["files_count"], len(result["issues"]),
            result["score"], total_time,
            total_tokens,
            result.get("total_prompt_tokens", 0),
            result.get("total_completion_tokens", 0),
            result.get("total_cached_tokens", 0),
        )

    except Exception as exc:
        _mark_review_failed(review_id, exc)
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
    """执行 async 操作：clone repo、获取 diff、LLM 审查。不发送评论。"""
    gitlab = GitLabClient()
    repo_mgr = RepoManager()

    # 1. Ensure local repo
    t0 = time.time()
    repo_dir = await repo_mgr.ensure_repo(project_name, branch)
    logger.info("[review:%d] 仓库准备就绪 (%.1f秒)", review_id, time.time() - t0)

    # 2. Update code graph (non-blocking)
    from src.core.code_graph.incremental import update_graph_incremental

    t0 = time.time()
    graph_summary = await asyncio.to_thread(
        update_graph_incremental, repo_dir, before_sha, commit_sha,
    )
    logger.info("[review:%d] 代码图更新: %s (%.1f秒)", review_id, graph_summary, time.time() - t0)

    # 3. Get changes
    t0 = time.time()
    if trigger_type == "merge_request" and mr_iid:
        data = await gitlab.get_mr_changes(project_id, mr_iid)
        changes = data.get("changes", [])
    elif before_sha:
        data = await gitlab.get_compare(project_id, before_sha, commit_sha)
        changes = data.get("diffs", [])
    else:
        logger.warning("[review:%d] 无 diff 来源，跳过审查", review_id)
        return {
            "overall_assessment": "", "files_count": 0, "issues": [],
            "score": "N/A", "dimension_stats": [], "comment_body": "",
        }
    logger.info("[review:%d] 获取变更完成: %d 个文件 (%.1f秒)", review_id, len(changes), time.time() - t0)

    # 4. Run review engine
    from src.core.code_graph.query import CodeGraph

    graph = CodeGraph(repo_dir)
    engine = ReviewEngine(repo_dir, code_graph=graph if graph.available() else None)
    try:
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

        comment_body = ReviewEngine.format_gitlab_comment(review_result, files_count)

        # 更新本地仓库，失败不影响审查结果
        try:
            await repo_mgr.update_repo(project_name, branch)
        except Exception:
            logger.warning("[review:%d] 仓库更新失败（不影响审查结果）", review_id, exc_info=True)
    finally:
        await engine.aclose()
        graph.close()

    return {
        "overall_assessment": review_result.overall_assessment,
        "files_count": files_count,
        "issues": review_result.issues,
        "score": review_result.score,
        "dimension_stats": review_result.dimension_stats,
        "comment_body": comment_body,
        "total_prompt_tokens": review_result.total_prompt_tokens,
        "total_completion_tokens": review_result.total_completion_tokens,
        "total_cached_tokens": review_result.total_cached_tokens,
    }


async def _post_comment_async(
    trigger_type: str,
    project_id: int,
    mr_iid: int | None,
    commit_sha: str,
    comment_body: str,
    review_id: int,
) -> None:
    """发送评论到 GitLab。"""
    gitlab = GitLabClient()
    t0 = time.time()
    if trigger_type == "merge_request" and mr_iid:
        await gitlab.post_mr_note(project_id, mr_iid, comment_body)
        logger.info("[review:%d] 评论已发布到 GitLab MR#%s (%.1f秒)",
                     review_id, mr_iid, time.time() - t0)
    else:
        await gitlab.post_commit_comment(project_id, commit_sha, comment_body)
        logger.info("[review:%d] 评论已发布到 GitLab 提交 %s (%.1f秒)",
                     review_id, commit_sha[:8], time.time() - t0)


def _save_review_result(review_id: int, result: dict) -> None:
    """持久化审查结果到数据库。"""
    t0 = time.time()
    with SyncSession() as session:
        review_obj = session.get(Review, review_id)
        review_obj.status = ReviewStatus.completed
        review_obj.summary = result["overall_assessment"]
        review_obj.files_reviewed = result["files_count"]
        review_obj.issues_found = len(result["issues"])
        review_obj.total_prompt_tokens = result.get("total_prompt_tokens", 0)
        review_obj.total_completion_tokens = result.get("total_completion_tokens", 0)
        review_obj.total_cached_tokens = result.get("total_cached_tokens", 0)
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
                dimension=issue.get("dimension"),
            )
            session.add(comment)
        session.commit()
    logger.info("[review:%d] 结果已持久化到数据库 (%.1f秒)", review_id, time.time() - t0)


def _find_recent_review(project_id: int, commit_sha: str) -> Review | None:
    """查询近 30 分钟内是否已有同 (project_id, commit_sha) 的 in_progress/completed 审查。

    故障时返回 None（fail-open）——保留原有行为，由下游 DB 约束和 Redis 抑制兜底。
    """
    try:
        cutoff = datetime.utcnow() - _REVIEW_DEDUP_WINDOW
        with SyncSession() as session:
            stmt = (
                select(Review)
                .where(
                    Review.gitlab_project_id == project_id,
                    Review.commit_sha == commit_sha,
                    Review.status.in_([ReviewStatus.in_progress, ReviewStatus.completed]),
                    Review.created_at >= cutoff,
                )
                .order_by(Review.created_at.desc())
                .limit(1)
            )
            return session.execute(stmt).scalar_one_or_none()
    except Exception:
        logger.warning("审查去重查询失败，放行任务（fail-open）", exc_info=True)
        return None


def _mark_review_failed(review_id: int, exc: Exception) -> None:
    """标记审查任务失败。"""
    try:
        with SyncSession() as session:
            review_obj = session.get(Review, review_id)
            if review_obj:
                review_obj.status = ReviewStatus.failed
                review_obj.error_message = str(exc)[:2000]
                review_obj.completed_at = datetime.utcnow()
                session.commit()
    except Exception:
        logger.error("[review:%d] 标记失败状态时数据库写入也失败", review_id)
