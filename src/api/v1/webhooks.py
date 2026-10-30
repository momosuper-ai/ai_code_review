from fastapi import APIRouter, Depends, Header, HTTPException, Request

from src.config import settings
from src.utils.logger import logger
from src.utils.rate_limit import rate_limit_dependency
from src.utils.redis_client import get_redis
from src.utils.security import verify_gitlab_token

router = APIRouter()

# Null SHA used by GitLab for branch creation
_NULL_SHA = "0" * 40

# MR merge 抑制标记：mr_merge:{project_id}:{commit_sha}，TTL 5 分钟
# 用于跳过 MR merge 附带产生的 push 事件，避免重复触发审查
_MR_MERGE_SUPPRESS_TTL = 300


def _mr_merge_mark_key(project_id: int, commit_sha: str) -> str:
    return f"mr_merge:{project_id}:{commit_sha}"


def _mark_mr_merge(project_id: int, commit_sha: str) -> None:
    """标记某个 commit_sha 由 MR merge 事件分发过，供 push 事件去重。故障 fail-open。"""
    if not project_id or not commit_sha:
        return
    try:
        get_redis().setex(_mr_merge_mark_key(project_id, commit_sha), _MR_MERGE_SUPPRESS_TTL, "1")
        logger.info(
            "已写入 MR merge 抑制标记: project_id=%s commit=%s TTL=%ss (将抑制同 commit 的 push 事件)",
            project_id, commit_sha[:8], _MR_MERGE_SUPPRESS_TTL,
        )
    except Exception:
        logger.warning("Redis 写入 MR merge 标记失败，降级由 task 层去重")


def _is_mr_merge_dispatched(project_id: int, commit_sha: str) -> bool:
    """检查 commit_sha 是否已被 MR merge 事件分发过（push 事件抑制）。故障 fail-open。"""
    if not project_id or not commit_sha:
        return False
    try:
        return bool(get_redis().get(_mr_merge_mark_key(project_id, commit_sha)))
    except Exception:
        logger.warning("Redis 读取 MR merge 标记失败，放行 push 事件（fail-open）")
        return False


@router.post("/gitlab", dependencies=[Depends(rate_limit_dependency("30/minute"))])
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(None),
):
    if not verify_gitlab_token(x_gitlab_token):
        raise HTTPException(status_code=401, detail="Invalid token")

    payload = await request.json()
    object_kind = payload.get("object_kind")

    if settings.PROJECT_BRAIN_ENABLED:
        if not settings.GITLAB_WEBHOOK_SECRET:
            raise HTTPException(status_code=503, detail="自动知识维护要求配置 GITLAB_WEBHOOK_SECRET")
        from src.core.project_brain.webhook import handle_brain_event

        # DB 先登记，审查去重/失败不会丢失知识更新；失败返回 503 让 GitLab 重送。
        try:
            if await handle_brain_event(payload):
                return {"status": "ignored", "reason": "verified_project_brain_publication"}
        except Exception:
            logger.warning("知识 Webhook 登记失败，请 GitLab 重试")
            raise HTTPException(status_code=503, detail="知识更新请求暂时无法持久化") from None

    if object_kind == "merge_request":
        return _handle_mr(payload)
    elif object_kind == "push":
        return _handle_push(payload)
    else:
        logger.info("Webhook 事件已忽略: 事件类型=%s 原因=不支持的事件类型", object_kind)
        return {"status": "ignored", "reason": f"unsupported event: {object_kind}"}


_MR_REVIEW_ACTIONS = {"merge"}


def _handle_mr(payload: dict) -> dict:
    attrs = payload.get("object_attributes", {})
    action = attrs.get("action")
    target_branch = attrs.get("target_branch", "")
    source_branch = attrs.get("source_branch", "") or ""
    mr_title = attrs.get("title", "") or ""
    project = payload.get("project", {})
    project_name = project.get("path_with_namespace", "")

    mr_iid = attrs.get("iid")

    # 防递归：AI-README 自动 MR（由临时分支合并至主分支）不触发任何任务
    if not settings.PROJECT_BRAIN_ENABLED and (
        source_branch.startswith(settings.README_TEMP_BRANCH_PREFIX)
        or mr_title.startswith(settings.README_MR_TITLE_PREFIX)
    ):
        logger.info(
            "Webhook 事件已忽略: 事件类型=merge_request 项目=%s MR=%s 原因=AI-README 自动 MR (source=%s title=%s)",
            project_name, mr_iid, source_branch, mr_title,
        )
        return {"status": "ignored", "reason": "ai_readme_mr"}

    if action not in _MR_REVIEW_ACTIONS:
        logger.info(
            "Webhook 事件已忽略: 事件类型=merge_request 项目=%s MR=%s 原因=action=%s 不在审查触发列表中 (仅 merge 触发)",
            project_name, mr_iid, action,
        )
        return {"status": "ignored", "reason": f"action={action}, not reviewable"}

    if target_branch not in settings.main_branch_list:
        logger.info(
            "Webhook 事件已忽略: 事件类型=merge_request 项目=%s MR=%s 原因=目标分支 %s 不在主分支列表 %s 中",
            project_name, mr_iid, target_branch, settings.main_branch_list,
        )
        return {
            "status": "ignored",
            "reason": f"target_branch={target_branch} not in main branches",
        }

    project_id = project.get("id")
    commit_sha = (attrs.get("last_commit") or {}).get("id", "")
    author = (payload.get("user") or {}).get("username", "unknown")
    # 合并事件使用目标分支，其他事件使用源分支（代码在源分支上）
    branch = target_branch if action == "merge" else source_branch

    logger.info(
        "收到合并请求事件: 项目=%s MR=%s action=%s 分支=%s",
        project_name, mr_iid, action, branch,
    )

    from src.tasks.review_task import run_review

    result = run_review.delay(
        trigger_type="merge_request",
        project_id=project_id,
        project_name=project_name,
        branch=branch,
        commit_sha=commit_sha,
        author=author,
        mr_iid=mr_iid,
        mr_title=mr_title,
        mr_action=action,
    )

    logger.info("已分发审查任务: task_id=%s 项目=%s", result.id, project_name)

    # MR merge 时 GitLab 会同步发送 push 事件到目标分支，写入抑制标记以避免重复审查
    if action == "merge":
        _mark_mr_merge(project_id, commit_sha)

    return {"status": "accepted", "trigger": "merge_request"}


def _handle_push(payload: dict) -> dict:
    ref = payload.get("ref", "")
    before = payload.get("before", "")
    total_commits = payload.get("total_commits_count", 0)

    # Extract branch name from ref (refs/heads/main -> main)
    branch = ref.split("/", 2)[-1] if ref.startswith("refs/heads/") else ref

    project = payload.get("project", {})
    project_name = project.get("path_with_namespace", "")

    # 防递归：AI-README 临时分支的 push 事件（由 _submit_via_temp_branch_mr 触发）不触发任何任务
    if not settings.PROJECT_BRAIN_ENABLED and branch.startswith(settings.README_TEMP_BRANCH_PREFIX):
        logger.info(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 分支=%s 原因=AI-README 临时分支 (前缀=%s)",
            project_name, branch, settings.README_TEMP_BRANCH_PREFIX,
        )
        return {"status": "ignored", "reason": "ai_readme_temp_branch"}

    if before == _NULL_SHA:
        logger.info(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 分支=%s 原因=分支创建事件 (before=null SHA)",
            project_name, branch,
        )
        return {"status": "ignored", "reason": "branch creation event"}

    if total_commits == 0:
        logger.info(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 分支=%s 原因=无提交",
            project_name, branch,
        )
        return {"status": "ignored", "reason": "no commits"}

    # 防递归：AI_README 自动提交不触发任何后续任务
    commits = payload.get("commits", [])
    if not settings.PROJECT_BRAIN_ENABLED and commits and all(
        c.get("message", "").startswith("[AI-README]") for c in commits
    ):
        logger.info(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 分支=%s 原因=AI_README 自动提交 (commits=%d 均以 [AI-README] 开头)",
            project_name, branch, len(commits),
        )
        return {"status": "ignored", "reason": "AI_README auto-commit"}

    project_id = project.get("id")
    after = payload.get("after", "")
    author = payload.get("user_name", "unknown")
    # GitLab 用户名（用于 commit comment 中的 @mention，触发 GitLab 邮件通知给提交人）
    author_username = payload.get("user_username") or ""

    # 抑制 MR merge 附带产生的 push 事件（短窗口 Redis 标记）
    if _is_mr_merge_dispatched(project_id, after):
        logger.info(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 分支=%s commit=%s 原因=由 MR merge 事件抑制",
            project_name, branch, after[:8],
        )
        return {"status": "ignored", "reason": "suppressed by MR merge event"}

    # 推送触发者诊断日志：对比 webhook 顶层 user_* 字段与 commits[0].author，便于排查 @mention 不准
    head_author = (commits[0].get("author") or {}) if commits else {}
    logger.info(
        "收到推送事件: 项目=%s 分支=%s before=%s after=%s "
        "推送触发者[user_id=%s user_username=%s user_name=%s user_email=%s] "
        "首个commit作者[name=%s email=%s] @mention将使用=%s",
        project_name, branch, before[:8], after[:8],
        payload.get("user_id"), author_username or "<empty>",
        payload.get("user_name"), payload.get("user_email"),
        head_author.get("name"), head_author.get("email"),
        f"@{author_username}" if author_username else "<跳过 @mention>",
    )

    from src.tasks.review_task import run_review

    result = run_review.delay(
        trigger_type="push",
        project_id=project_id,
        project_name=project_name,
        branch=branch,
        commit_sha=after,
        author=author,
        mr_iid=None,
        mr_title=None,
        before_sha=before,
        author_username=author_username,
    )

    logger.info("已分发审查任务: task_id=%s 项目=%s", result.id, project_name)

    return {"status": "accepted", "trigger": "push"}
