"""Brain event registration before independent review deduplication."""

import hashlib

from sqlalchemy import select

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.project_brain.artifacts import owned_path
from src.core.project_brain.lifecycle import mark_dirty
from src.core.project_brain.schema import BrainManifest
from src.core.project_brain.write_policy import assert_tree_safe
from src.models import async_session
from src.models.brain import BrainBuild, BrainProject, BrainPublication


async def _verified_manifest(client: GitLabClient, project_id: int, publication: BrainPublication, sha: str) -> BrainManifest | None:
    remote = await client.get_repository_file(project_id, "docs/project-brain/manifest.json", sha)
    if not remote:
        return None
    try:
        manifest = BrainManifest.model_validate_json(remote["decoded_content"])
        digest = hashlib.sha256(remote["decoded_content"].encode()).hexdigest()
        async with async_session() as session:
            build = await session.get(BrainBuild, publication.build_id)
            if (not build or manifest.build_id != build.id or manifest.project_id != project_id
                    or manifest.source_commit != build.source_commit or build.coverage.get("manifest_sha256") != digest):
                return None
        if not manifest.files or any(not owned_path(p) for p in manifest.files):
            return None
        await assert_tree_safe(client, project_id, sha, set(manifest.files))
        for path, expected in manifest.files.items():
            file = await client.get_repository_file(project_id, path, sha)
            if not file or hashlib.sha256(file["decoded_content"].encode()).hexdigest() != expected:
                return None
        return manifest
    except ValueError:
        return None


async def handle_brain_event(payload: dict) -> bool:
    """返回已确认机器人事件标记，同时独立登记主分支知识更新请求。"""
    if not settings.PROJECT_BRAIN_ENABLED:
        return False
    project_id = (payload.get("project") or {}).get("id")
    if not isinstance(project_id, int) or project_id <= 0:
        return False
    kind = payload.get("object_kind")
    attrs = payload.get("object_attributes") or {}
    branch = attrs.get("target_branch") if kind == "merge_request" else payload.get("ref", "").removeprefix("refs/heads/")
    if kind not in {"push", "merge_request"}:
        return False
    async with async_session() as session:
        publications = (await session.scalars(select(BrainPublication).join(BrainBuild).join(BrainProject).where(BrainProject.gitlab_project_id == project_id))).all()
        session.expunge_all()
    client = GitLabClient(purpose="read")
    known = None
    if kind == "merge_request":
        known = next((p for p in publications if p.mr_iid == attrs.get("iid") and p.temp_branch == attrs.get("source_branch")), None)
        if known:
            changes = await client.get_mr_changes(project_id, known.mr_iid)
            diffs = changes.get("changes", [])
            sha = changes.get("sha") or (changes.get("diff_refs") or {}).get("head_sha")
            manifest = await _verified_manifest(client, project_id, known, sha) if sha else None
            safe = bool(manifest and diffs) and not changes.get("overflow") and all(owned_path(d["old_path"]) and owned_path(d["new_path"]) for d in diffs)
            if safe:
                if attrs.get("action") == "merge":
                    async with async_session() as session:
                        record = await session.get(BrainPublication, known.id)
                        record.status = "published"
                        record.merge_commit_sha = attrs.get("merge_commit_sha") or attrs.get("squash_commit_sha")
                        await session.commit()
                return True
        if attrs.get("action") != "merge":
            return False
    else:
        after, before = payload.get("after"), payload.get("before")
        known = next((p for p in publications if after and after in {p.commit_sha, p.merge_commit_sha}), None)
        # GitLab 在提交响应返回之前就可能发送 Webhook。先用已登记构建的
        # 精确提交标记和远端清单对账，而不是仅凭 [AI-BRAIN] 标题。
        candidates = [p for p in publications if p.target_branch == branch or p.temp_branch == branch]
        if not known and candidates and after and set(after) != {"0"}:
            commit = await client.brain_request("GET", f"/projects/{project_id}/repository/commits/{after}")
            matched = next((p for p in candidates if (commit.get("message") or "").splitlines()[0] == f"[AI-BRAIN] 更新项目知识 ({p.build_id})"), None) if commit.get("message") else None
            if matched:
                remote = await client.get_repository_file(project_id, "docs/project-brain/manifest.json", after)
                if remote and await _verified_manifest(client, project_id, matched, after):
                    known = matched
                    async with async_session() as session:
                        record = await session.get(BrainPublication, matched.id)
                        record.commit_sha = after
                        await session.commit()
        if known and before and after and set(before) != {"0"} and set(after) != {"0"}:
            compare = await client.get_compare(project_id, before, after)
            diffs = compare.get("diffs", [])
            if (diffs and not compare.get("compare_timeout") and not compare.get("overflow")
                    and all(owned_path(d["old_path"]) and owned_path(d["new_path"]) for d in diffs)
                    and await _verified_manifest(client, project_id, known, after)):
                return True
    if branch not in settings.main_branch_list:
        return False
    if kind == "push" and not payload.get("ref", "").startswith("refs/heads/"):
        return False
    if kind == "push" and not payload.get("after"):
        return False
    # 不依赖审查的 Redis merge 标记、审查任务结果或 README 前缀判断。
    async with async_session() as session:
        await session.run_sync(lambda sync: mark_dirty(sync, project_id, branch,
            name=(payload.get("project") or {}).get("path_with_namespace") or str(project_id),
            deleted=kind == "push" and set(payload["after"]) == {"0"}))
        await session.commit()
    return False
