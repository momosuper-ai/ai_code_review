import json

from fastapi import APIRouter, Header, HTTPException, Request

from src.schemas.webhook import GitLabMRWebhook, SonarQubeWebhook
from src.tasks.review_task import run_ai_review
from src.utils.logger import logger
from src.utils.security import verify_gitlab_webhook

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])

# 需要触发 AI 审查的 MR 动作
REVIEW_TRIGGER_ACTIONS = {"open", "reopen", "update"}


@router.post("/gitlab")
async def handle_gitlab_webhook(
    request: Request,
    x_gitlab_token: str = Header(None, alias="X-Gitlab-Token"),
):
    """接收 GitLab MR Webhook 事件，触发 AI 审查。"""
    # 验证 Webhook Token
    if x_gitlab_token and not verify_gitlab_webhook(x_gitlab_token):
        raise HTTPException(status_code=401, detail="无效的 Webhook Token")

    body = await request.json()
    event_type = body.get("object_kind")

    if event_type != "merge_request":
        return {"message": f"忽略非 MR 事件: {event_type}"}

    webhook = GitLabMRWebhook(**body)
    action = webhook.object_attributes.action

    if action not in REVIEW_TRIGGER_ACTIONS:
        return {"message": f"忽略 MR 动作: {action}"}

    # 异步触发 AI 审查任务
    project_id = webhook.project.id
    mr_iid = webhook.object_attributes.iid

    task = run_ai_review.delay(project_id, mr_iid)
    logger.info(
        f"已触发 AI 审查: 项目={webhook.project.path_with_namespace}, "
        f"MR=!{mr_iid}, 任务ID={task.id}"
    )

    return {
        "message": "AI 审查任务已提交",
        "task_id": task.id,
        "project": webhook.project.path_with_namespace,
        "mr_iid": mr_iid,
    }


@router.post("/sonarqube")
async def handle_sonarqube_webhook(request: Request):
    """接收 SonarQube Webhook，存储分析结果。"""
    body = await request.json()

    try:
        webhook = SonarQubeWebhook(**body)
    except Exception as e:
        logger.warning(f"SonarQube Webhook 解析失败: {e}")
        raise HTTPException(status_code=400, detail="无效的 Webhook 数据")

    # 存储 SonarQube 分析结果
    from sqlalchemy import select

    from src.models import async_session
    from src.models.project import Project
    from src.models.review import SonarResult

    async with async_session() as db:
        stmt = select(Project).where(Project.sonarqube_project_key == webhook.project.key)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()

        if not project:
            logger.info(f"SonarQube 项目未注册: {webhook.project.key}")
            return {"message": "项目未注册，已忽略"}

        sonar_result = SonarResult(
            project_id=project.id,
            task_id=webhook.taskId,
            status=webhook.qualityGate.status if webhook.qualityGate else webhook.status,
            branch=webhook.branch.get("name") if webhook.branch else None,
            metrics=json.dumps(webhook.qualityGate.conditions if webhook.qualityGate else [], ensure_ascii=False),
            raw_payload=json.dumps(body, ensure_ascii=False),
        )
        db.add(sonar_result)
        await db.commit()

    logger.info(f"SonarQube 结果已存储: 项目={webhook.project.key}, 状态={webhook.status}")
    return {"message": "SonarQube 结果已存储"}
