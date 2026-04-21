"""Celery task that generates/updates AI_README.md files."""

import asyncio
import time
from datetime import datetime

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.repo_manager import RepoManager
from src.models import SyncSession
from src.models.readme_record import ReadmeRecord
from src.tasks.celery_app import celery_app
from src.utils.logger import logger


@celery_app.task(
    bind=True,
    name="src.tasks.readme_task.generate_readme",
    soft_time_limit=1800,
    max_retries=2,
    default_retry_delay=30,
)
def generate_readme(
    self,
    project_id: int,
    project_name: str,
    branch: str,
    commit_sha: str,
):
    """文档生成主任务。"""
    logger.info(
        "开始文档生成任务: 项目=%s 分支=%s 提交=%s",
        project_name, branch, commit_sha[:8],
    )
    task_start = time.time()

    try:
        result = asyncio.run(
            _do_generate_async(
                project_id=project_id,
                project_name=project_name,
                branch=branch,
                commit_sha=commit_sha,
            )
        )

        _save_readme_record(
            project_id=project_id,
            project_name=project_name,
            branch=branch,
            commit_sha=commit_sha,
            result=result,
        )

        total_time = time.time() - task_start
        logger.info(
            "文档生成任务完成: 项目=%s 文件数=%d 状态=%s 总耗时=%.1f秒 token=%d(输入:%d/输出:%d)",
            project_name, len(result.get("actions", [])),
            result["status"], total_time,
            result.get("total_prompt_tokens", 0) + result.get("total_completion_tokens", 0),
            result.get("total_prompt_tokens", 0),
            result.get("total_completion_tokens", 0),
        )

    except Exception as exc:
        _save_readme_record(
            project_id=project_id,
            project_name=project_name,
            branch=branch,
            commit_sha=commit_sha,
            result={
                "status": "failed",
                "error_message": str(exc)[:2000],
                "fingerprint": "",
                "root_generated": False,
                "subdirs_generated": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
            },
        )
        logger.error(
            "文档生成任务失败: 项目=%s 提交=%s 重试次数=%d/%d 错误=%s",
            project_name, commit_sha[:8], self.request.retries, self.max_retries, exc,
        )
        raise self.retry(exc=exc)


async def _do_generate_async(
    project_id: int,
    project_name: str,
    branch: str,
    commit_sha: str,
) -> dict:
    """执行 async 文档生成流程。"""
    from src.core.readme_generator import ReadmeGenerator

    repo_mgr = RepoManager()
    gitlab = GitLabClient()

    # 1. 确保本地仓库
    t0 = time.time()
    repo_dir = await repo_mgr.ensure_repo(project_name, branch)
    logger.info("文档生成: 仓库准备就绪 (%.1f秒)", time.time() - t0)

    # 2. 生成文档
    generator = ReadmeGenerator(repo_path=repo_dir)
    try:
        t0 = time.time()
        readme_result = await generator.run()
        logger.info(
            "文档生成: 完成 actions=%d skipped=%d (%.1f秒)",
            len(readme_result.actions), len(readme_result.skipped_dirs), time.time() - t0,
        )

        if not readme_result.actions:
            return {
                "status": "skipped",
                "actions": [],
                "fingerprint": generator._compute_root_fingerprint(),
                "root_generated": False,
                "subdirs_generated": 0,
                "total_prompt_tokens": readme_result.total_prompt_tokens,
                "total_completion_tokens": readme_result.total_completion_tokens,
            }

        # 3. 通过 GitLab API 批量提交
        gitlab_actions = []
        root_generated = False
        subdirs_generated = 0
        for act in readme_result.actions:
            gitlab_actions.append({
                "action": act.action,
                "file_path": act.file_path,
                "content": act.content,
            })
            if act.file_path == "AI_README.md":
                root_generated = True
            else:
                subdirs_generated += 1

        # 3.5 依据 GitLab 端真实状态校准 action 类型
        # 本地克隆可能早于远端已有的 bot 提交，避免发送 action=create 给已存在的文件
        for act in gitlab_actions:
            try:
                remote_exists = await gitlab.file_exists(
                    project_id=project_id,
                    file_path=act["file_path"],
                    ref=branch,
                )
            except Exception:
                logger.warning(
                    "文档生成: 预查文件存在性失败，保留原 action=%s path=%s",
                    act["action"], act["file_path"],
                )
                continue
            expected = "update" if remote_exists else "create"
            if act["action"] != expected:
                logger.info(
                    "文档生成: 校准 action %s→%s (path=%s, 远端存在=%s)",
                    act["action"], expected, act["file_path"], remote_exists,
                )
                act["action"] = expected

        t0 = time.time()
        commit_msg = f"{settings.README_COMMIT_MESSAGE_PREFIX} 更新项目文档"
        try:
            resp = await gitlab.create_commit(
                project_id=project_id,
                branch=branch,
                commit_message=commit_msg,
                actions=gitlab_actions,
                author_name=settings.README_AUTHOR_NAME,
                author_email=settings.README_AUTHOR_EMAIL,
            )
            result_commit_sha = resp.get("id", "")
            logger.info(
                "文档生成: GitLab 提交成功 commit=%s (%.1f秒)",
                result_commit_sha[:8] if result_commit_sha else "unknown",
                time.time() - t0,
            )
        except Exception:
            logger.error("文档生成: GitLab 提交失败", exc_info=True)
            return {
                "status": "failed",
                "error_message": "GitLab API 提交失败",
                "actions": gitlab_actions,
                "fingerprint": generator._compute_root_fingerprint(),
                "root_generated": root_generated,
                "subdirs_generated": subdirs_generated,
                "result_commit_sha": None,
                "total_prompt_tokens": readme_result.total_prompt_tokens,
                "total_completion_tokens": readme_result.total_completion_tokens,
            }

        return {
            "status": "completed",
            "actions": gitlab_actions,
            "fingerprint": generator._compute_root_fingerprint(),
            "root_generated": root_generated,
            "subdirs_generated": subdirs_generated,
            "result_commit_sha": result_commit_sha,
            "total_prompt_tokens": readme_result.total_prompt_tokens,
            "total_completion_tokens": readme_result.total_completion_tokens,
        }
    finally:
        await generator.aclose()


def _save_readme_record(
    project_id: int,
    project_name: str,
    branch: str,
    commit_sha: str,
    result: dict,
) -> None:
    """持久化文档生成记录到数据库。"""
    try:
        with SyncSession() as session:
            record = ReadmeRecord(
                gitlab_project_id=project_id,
                project_name=project_name,
                branch=branch,
                commit_sha=commit_sha,
                result_commit_sha=result.get("result_commit_sha"),
                root_generated=result.get("root_generated", False),
                subdirs_generated=result.get("subdirs_generated", 0),
                structure_fingerprint=result.get("fingerprint", ""),
                total_prompt_tokens=result.get("total_prompt_tokens", 0),
                total_completion_tokens=result.get("total_completion_tokens", 0),
                status=result.get("status", "failed"),
                error_message=result.get("error_message"),
            )
            session.add(record)
            session.commit()
        logger.info("文档生成: 记录已持久化到数据库")
    except Exception:
        logger.error("文档生成: 记录持久化失败", exc_info=True)
