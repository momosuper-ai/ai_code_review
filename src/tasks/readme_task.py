"""Celery task that generates/updates AI_README.md files."""

import asyncio
import time
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
    trigger_type: str = "push",
):
    """文档生成主任务。

    trigger_type: "push" | "merge_request"
        - push：主分支能被推送即代表未保护，直接 commit
        - merge_request：探测主分支 `protected` 字段，受保护走临时分支+MR+自动合并
    """
    logger.info(
        "开始文档生成任务: 项目=%s 分支=%s 提交=%s",
        project_name, branch, commit_sha[:8],
    )
    task_start = time.time()

    # 入口幂等：同 (project_id, branch, commit_sha) 已有 completed 记录则跳过
    if _has_completed_readme(project_id, branch, commit_sha):
        logger.info(
            "文档生成任务已跳过: 项目=%s 分支=%s 提交=%s 原因=已有 completed 记录",
            project_name, branch, commit_sha[:8],
        )
        return {"status": "skipped", "reason": "already_completed"}

    try:
        result = asyncio.run(
            _do_generate_async(
                project_id=project_id,
                project_name=project_name,
                branch=branch,
                commit_sha=commit_sha,
                trigger_type=trigger_type,
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
    trigger_type: str = "push",
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

        commit_msg = f"{settings.README_COMMIT_MESSAGE_PREFIX} 更新项目文档"
        result_mr_iid: int | None = None

        # 仅 merge 链路需要探测主分支保护状态；push 到主分支能成功本身意味着未保护
        use_temp_branch_mr = False
        if trigger_type == "merge_request":
            try:
                branch_info = await gitlab.get_branch(project_id, branch)
                use_temp_branch_mr = bool(branch_info.get("protected", False))
                logger.info(
                    "文档生成: 主分支保护状态 project=%d branch=%s protected=%s",
                    project_id, branch, use_temp_branch_mr,
                )
            except Exception:
                logger.warning(
                    "文档生成: 获取分支保护状态失败，保守按未保护直接提交",
                    exc_info=True,
                )
                use_temp_branch_mr = False

        t0 = time.time()
        if use_temp_branch_mr:
            try:
                result_commit_sha, result_mr_iid = await _submit_via_temp_branch_mr(
                    gitlab=gitlab,
                    project_id=project_id,
                    project_name=project_name,
                    main_branch=branch,
                    commit_sha=commit_sha,
                    gitlab_actions=gitlab_actions,
                    commit_msg=commit_msg,
                )
                logger.info(
                    "文档生成: 临时分支 MR 流程完成 mr=%s (%.1f秒)",
                    result_mr_iid, time.time() - t0,
                )
            except Exception:
                logger.error("文档生成: 临时分支 MR 流程失败", exc_info=True)
                return {
                    "status": "failed",
                    "error_message": "临时分支+MR 流程失败",
                    "actions": gitlab_actions,
                    "fingerprint": generator._compute_root_fingerprint(),
                    "root_generated": root_generated,
                    "subdirs_generated": subdirs_generated,
                    "result_commit_sha": None,
                    "total_prompt_tokens": readme_result.total_prompt_tokens,
                    "total_completion_tokens": readme_result.total_completion_tokens,
                }
        else:
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
            "result_mr_iid": result_mr_iid,
            "total_prompt_tokens": readme_result.total_prompt_tokens,
            "total_completion_tokens": readme_result.total_completion_tokens,
        }
    finally:
        await generator.aclose()


async def _submit_via_temp_branch_mr(
    gitlab: GitLabClient,
    project_id: int,
    project_name: str,
    main_branch: str,
    commit_sha: str,
    gitlab_actions: list[dict],
    commit_msg: str,
) -> tuple[str, int | None]:
    """受保护主分支：临时分支 → commit → MR → 可选自动合并。

    返回 (result_commit_sha, mr_iid)。
    临时分支命名含时间戳避免同 sha 重试冲突。
    """
    ts = int(time.time())
    temp_branch = f"{settings.README_TEMP_BRANCH_PREFIX}{ts}-{commit_sha[:8]}"

    await gitlab.create_branch(project_id, temp_branch, ref=main_branch)

    try:
        commit_resp = await gitlab.create_commit(
            project_id=project_id,
            branch=temp_branch,
            commit_message=commit_msg,
            actions=gitlab_actions,
            author_name=settings.README_AUTHOR_NAME,
            author_email=settings.README_AUTHOR_EMAIL,
        )
        result_commit_sha = commit_resp.get("id", "")
    except Exception:
        # commit 失败时清理临时分支（失败再忽略），避免残留
        try:
            await gitlab.delete_branch(project_id, temp_branch)
        except Exception:
            pass
        raise

    mr_resp = await gitlab.create_merge_request(
        project_id=project_id,
        source_branch=temp_branch,
        target_branch=main_branch,
        title=f"{settings.README_MR_TITLE_PREFIX} 更新 AI_README ({commit_sha[:8]})",
        description=settings.README_MR_DESCRIPTION,
        remove_source_branch=True,
        squash=True,
    )
    mr_iid = mr_resp.get("iid")
    logger.info(
        "文档生成: 临时分支 MR 已创建 项目=%s mr=%s temp=%s",
        project_name, mr_iid, temp_branch,
    )

    if settings.README_MR_AUTO_MERGE and mr_iid:
        try:
            await gitlab.accept_merge_request(
                project_id=project_id,
                mr_iid=mr_iid,
                should_remove_source_branch=settings.README_MR_AUTO_MERGE_REMOVE_BRANCH,
            )
            logger.info(
                "文档生成: MR 已自动合并 项目=%s mr=%s", project_name, mr_iid,
            )
        except Exception:
            logger.warning(
                "文档生成: MR 自动合并失败，等待人工合并 项目=%s mr=%s",
                project_name, mr_iid, exc_info=True,
            )

    return result_commit_sha, mr_iid


def _has_completed_readme(project_id: int, branch: str, commit_sha: str) -> bool:
    """检查是否已有 (project_id, branch, commit_sha) 的 completed 文档记录。

    故障时返回 False（fail-open）——保留原行为，由 DB 唯一约束兜底。
    """
    try:
        with SyncSession() as session:
            stmt = select(ReadmeRecord.id).where(
                ReadmeRecord.gitlab_project_id == project_id,
                ReadmeRecord.branch == branch,
                ReadmeRecord.commit_sha == commit_sha,
                ReadmeRecord.status == "completed",
            ).limit(1)
            return session.execute(stmt).scalar_one_or_none() is not None
    except Exception:
        logger.warning("文档生成: 幂等查询失败，放行任务（fail-open）", exc_info=True)
        return False


def _save_readme_record(
    project_id: int,
    project_name: str,
    branch: str,
    commit_sha: str,
    result: dict,
) -> None:
    """持久化文档生成记录到数据库。

    遇到 partial unique index (status='completed') 冲突时降级为 info 日志，
    不抛异常，也不触发 Celery 重试——代表已有并发任务成功写入。
    """
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
    except IntegrityError:
        logger.info(
            "文档生成: 记录已存在（唯一约束命中），跳过写入: 项目=%s commit=%s",
            project_name, commit_sha[:8],
        )
    except Exception:
        logger.error("文档生成: 记录持久化失败", exc_info=True)
