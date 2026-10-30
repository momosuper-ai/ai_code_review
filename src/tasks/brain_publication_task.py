"""Independent GitLab publication leases and reconciliation."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.project_brain.lifecycle import mark_dirty
from src.core.project_brain.publisher import PublicationDeferred, PublicationSuperseded, publish
from src.core.project_brain.write_policy import POLICY_VERSION, WriteConflict
from src.models import SyncSession
from src.models.brain import BrainBuild, BrainProject, BrainPublication, utcnow
from src.tasks.celery_app import celery_app
from src.utils.logger import logger


def dispatch_publications() -> None:
    """补发未发布版本，并低频核对未合并 MR。"""
    with SyncSession() as session:
        now = utcnow()
        for publication in session.scalars(select(BrainPublication).where(BrainPublication.status.in_(["pending", "publishing", "mr_open", "auto_merge_pending"]))):
            if publication.lease_until and publication.lease_until > now:
                continue
            if publication.status != "pending" and publication.updated_at > now - timedelta(seconds=60):
                continue
            try:
                publish_brain.apply_async(args=[publication.id], queue="brain-publication", retry=False)
            except Exception:
                logger.warning("知识发布投递失败，等待恢复：publication_id=%s", publication.id)
                break


@celery_app.task(bind=True, name="src.tasks.brain_publication_task.publish_brain", soft_time_limit=600,
                 time_limit=660, max_retries=2, default_retry_delay=60, acks_late=True, reject_on_worker_lost=True)
def publish_brain(self, publication_id: int) -> dict:
    """发布故障不改变 ready 状态，也不会重新调用语义模型。"""
    if not settings.PROJECT_BRAIN_ENABLED:
        return {"status": "disabled"}
    with SyncSession() as session:
        original = session.get(BrainPublication, publication_id)
        if not original:
            return {"status": "skipped"}
        build = session.get(BrainBuild, original.build_id)
        project = session.scalar(select(BrainProject).where(BrainProject.id == build.project_id).with_for_update())
        publication = session.scalar(select(BrainPublication).where(BrainPublication.id == publication_id).with_for_update().execution_options(populate_existing=True))
        if not publication or publication.status in {"published", "superseded", "failed"}:
            return {"status": "skipped"}
        if publication.lease_until and publication.lease_until > utcnow():
            return {"status": "leased"}
        if (project.active_build_id != build.id or not project.gitlab_sync
                or project.maintenance_state != "active" or project.branch not in settings.main_branch_list or project.source_status == "missing"):
            publication.status = "superseded"
            session.commit()
            return {"status": "superseded"}
        if project.dirty_at:
            return {"status": "deferred"}
        token = str(uuid4())
        publication.lease_token, publication.lease_until = token, utcnow() + timedelta(seconds=690)
        publication.status = "publishing"
        publication.policy_version = POLICY_VERSION
        publication.error_code = publication.error_message = None
        publication.attempts += 1
        checkpoint = {k: getattr(publication, k) for k in ("temp_branch", "commit_sha", "merge_commit_sha", "mr_iid", "mr_url")}
        session.commit()
        session.expunge_all()

    def save(values: dict) -> None:
        with SyncSession() as session:
            current_project = session.scalar(select(BrainProject).where(BrainProject.id == project.id).with_for_update())
            if (current_project.active_build_id != build.id or not current_project.gitlab_sync
                    or current_project.maintenance_state != "active" or current_project.branch not in settings.main_branch_list or current_project.source_status == "missing"):
                raise PublicationSuperseded("知识发布对应的维护请求已推进")
            if current_project.dirty_at:
                raise PublicationDeferred("知识发布等待目标分支事件核对")
            current = session.scalar(select(BrainPublication).where(BrainPublication.id == publication_id).with_for_update())
            if current.lease_token != token:
                raise ValueError("知识发布租约已失效")
            for key in ("temp_branch", "commit_sha", "merge_commit_sha", "mr_iid", "mr_url", "status", "error_message", "error_code"):
                if key in values:
                    if key == "status" and current.status == "published" and values[key] in {"pending", "publishing", "mr_open", "auto_merge_pending"}:
                        continue
                    setattr(current, key, values[key])
            session.commit()

    def known_owner(build_id: str, manifest_hash: str) -> bool:
        with SyncSession() as session:
            owner = session.get(BrainBuild, build_id)
            owner_project = session.get(BrainProject, owner.project_id) if owner else None
            return bool(owner_project and owner_project.gitlab_project_id == project.gitlab_project_id
                        and owner_project.branch == project.branch
                        and owner.coverage.get("manifest_sha256") == manifest_hash)

    try:
        result = asyncio.run(publish(build.id, project.gitlab_project_id, project.branch, checkpoint, save, known_owner))
        save(result)
        with SyncSession() as session:
            current = session.get(BrainPublication, publication_id)
            if current.lease_token == token:
                current.lease_until = None
            if result.get("source_changed"):
                mark_dirty(session, project.gitlab_project_id, project.branch)
            session.commit()
        if result["status"] in {"published", "mr_open", "auto_merge_pending"}:
            asyncio.run(_close_obsolete(project.id, build.id))
        logger.info("知识发布完成：project_id=%s build_id=%s publication_id=%s status=%s", project.gitlab_project_id, build.id, publication_id, result["status"])
        return result
    except PublicationDeferred:
        with SyncSession() as session:
            current = session.scalar(select(BrainPublication).where(BrainPublication.id == publication_id).with_for_update())
            if current.lease_token == token and current.status != "published":
                current.status, current.lease_until = "pending", None
            session.commit()
        return {"status": "deferred"}
    except PublicationSuperseded:
        with SyncSession() as session:
            current_project = session.scalar(select(BrainProject).where(BrainProject.id == project.id).with_for_update())
            current = session.scalar(select(BrainPublication).where(BrainPublication.id == publication_id).with_for_update())
            if current.lease_token == token:
                current.status, current.lease_until = "superseded", None
                # 外部调用可能已完成，安排最新源码再核对，避免旧文档留在远端。
                if current_project.gitlab_sync and not current_project.dirty_at:
                    mark_dirty(session, project.gitlab_project_id, project.branch)
            session.commit()
        return {"status": "superseded"}
    except Exception as exc:
        with SyncSession() as session:
            current = session.scalar(select(BrainPublication).where(BrainPublication.id == publication_id).with_for_update())
            if current.lease_token != token:
                return {"status": "lease_lost"}
            terminal = isinstance(exc, WriteConflict) or self.request.retries >= 2
            current.status, current.lease_until = "failed" if terminal else "pending", None
            current.error_message = f"{type(exc).__name__}：知识发布失败，请检查 GitLab 权限、人工冲突或网络"
            current.error_code = exc.code if isinstance(exc, WriteConflict) else "publication_failed"
            session.commit()
        if not terminal:
            raise self.retry(exc=RuntimeError("知识发布暂时失败"))
        return {"status": "failed"}


async def _close_obsolete(project_row_id: int, latest_build_id: str) -> None:
    client = GitLabClient(purpose="brain_publish")
    with SyncSession() as session:
        project = session.get(BrainProject, project_row_id)
        records = session.scalars(select(BrainPublication).join(BrainBuild).where(BrainBuild.project_id == project_row_id,
                    BrainBuild.id != latest_build_id, BrainPublication.mr_iid.is_not(None),
                    BrainPublication.status.in_(["mr_open", "auto_merge_pending", "superseded"]))).all()
        session.expunge_all()
    for record in records:
        try:
            mr = await client.brain_request("GET", f"/projects/{project.gitlab_project_id}/merge_requests/{record.mr_iid}")
            marker = f"<!-- project-brain:{record.build_id} -->"
            if mr.get("state") != "opened" or marker not in (mr.get("description") or "") or mr.get("source_branch") != record.temp_branch:
                continue
            await client.close_brain_mr(project.gitlab_project_id, record.mr_iid, record.build_id, record.temp_branch)
            with SyncSession() as session:
                session.get(BrainPublication, record.id).status = "superseded"
                session.commit()
        except Exception as exc:
            logger.warning("过期知识 MR 清理失败：publication_id=%s error_type=%s", record.id, type(exc).__name__)
