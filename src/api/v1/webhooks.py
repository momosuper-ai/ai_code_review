from fastapi import APIRouter, Depends, Header, HTTPException, Request

from src.config import settings
from src.utils.logger import logger
from src.utils.rate_limit import rate_limit_dependency
from src.utils.security import verify_gitlab_token

router = APIRouter()

# Null SHA used by GitLab for branch creation
_NULL_SHA = "0" * 40


@router.post("/gitlab", dependencies=[Depends(rate_limit_dependency("30/minute"))])
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(None),
):
    if not verify_gitlab_token(x_gitlab_token):
        raise HTTPException(status_code=401, detail="Invalid token")

    payload = await request.json()
    object_kind = payload.get("object_kind")

    if object_kind == "merge_request":
        return _handle_mr(payload)
    elif object_kind == "push":
        return _handle_push(payload)
    else:
        logger.debug("Webhook 事件已忽略: 事件类型=%s 原因=不支持的事件类型", object_kind)
        return {"status": "ignored", "reason": f"unsupported event: {object_kind}"}


_MR_REVIEW_ACTIONS = {"open", "update", "reopen", "merge"}


def _handle_mr(payload: dict) -> dict:
    attrs = payload.get("object_attributes", {})
    action = attrs.get("action")
    target_branch = attrs.get("target_branch", "")
    source_branch = attrs.get("source_branch", "")
    project = payload.get("project", {})
    project_name = project.get("path_with_namespace", "")

    if action not in _MR_REVIEW_ACTIONS:
        logger.debug(
            "Webhook 事件已忽略: 事件类型=merge_request 项目=%s 原因=action=%s 不在审查触发列表中",
            project_name, action,
        )
        return {"status": "ignored", "reason": f"action={action}, not reviewable"}

    if target_branch not in settings.main_branch_list:
        logger.debug(
            "Webhook 事件已忽略: 事件类型=merge_request 项目=%s 原因=目标分支 %s 不在主分支列表中",
            project_name, target_branch,
        )
        return {
            "status": "ignored",
            "reason": f"target_branch={target_branch} not in main branches",
        }

    project_id = project.get("id")
    mr_iid = attrs.get("iid")
    mr_title = attrs.get("title", "")
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
    )

    logger.info("已分发审查任务: task_id=%s 项目=%s", result.id, project_name)

    return {"status": "accepted", "trigger": "merge_request"}


def _handle_push(payload: dict) -> dict:
    ref = payload.get("ref", "")
    before = payload.get("before", "")
    total_commits = payload.get("total_commits_count", 0)

    # Extract branch name from ref (refs/heads/main -> main)
    branch = ref.split("/", 2)[-1] if ref.startswith("refs/heads/") else ref

    project = payload.get("project", {})
    project_name = project.get("path_with_namespace", "")

    if before == _NULL_SHA:
        logger.debug(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 原因=分支创建事件",
            project_name,
        )
        return {"status": "ignored", "reason": "branch creation event"}

    if total_commits == 0:
        logger.debug(
            "Webhook 事件已忽略: 事件类型=push 项目=%s 原因=无提交",
            project_name,
        )
        return {"status": "ignored", "reason": "no commits"}

    project_id = project.get("id")
    after = payload.get("after", "")
    author = payload.get("user_name", "unknown")

    logger.info(
        "收到推送事件: 项目=%s 分支=%s before=%s after=%s",
        project_name, branch, before[:8], after[:8],
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
    )

    logger.info("已分发审查任务: task_id=%s 项目=%s", result.id, project_name)

    return {"status": "accepted", "trigger": "push"}
