"""Ownership-aware GitLab publishing, reconciliation, and source fencing."""

import hashlib
from collections.abc import Callable

from src.config import settings
from src.core.gitlab_client import GitLabClient
from src.core.project_brain.artifacts import ArtifactStore, owned_path
from src.core.project_brain.public_schema import parse_manifest
from src.core.project_brain.snapshot import allowed_source, source_fingerprint
from src.core.project_brain.write_policy import WriteConflict, assert_tree_safe, validate_changes, verify_commit


class PublicationSuperseded(ValueError):
    """当前维护请求已推进，不允许继续外部写入。"""


class PublicationDeferred(ValueError):
    """目标分支待核对，保留同一版本的发布请求。"""


async def branch_fingerprint(client: GitLabClient, project_id: int, sha: str) -> str:
    """分页扫描固定提交的输入 blob，不下载源码和敏感配置值。"""
    files = {}
    page = 1
    while True:
        entries = await client.brain_request("GET", f"/projects/{project_id}/repository/tree",
                                             params={"ref": sha, "recursive": True, "per_page": 100, "page": page})
        for entry in entries:
            if entry["type"] in {"blob", "commit"} and allowed_source(entry["path"]):
                files[entry["path"]] = {"blob_sha": entry["id"], "mode": entry.get("mode", "100644")}
        if len(files) > settings.PROJECT_BRAIN_MAX_SOURCE_FILES:
            raise ValueError("发布前源码扫描超过文件上限")
        if len(entries) < 100:
            break
        page += 1
    return source_fingerprint(files)


async def publication_actions(client: GitLabClient, project_id: int, ref: str, verified: dict[str, str],
                              known_owner: Callable[[str, str], bool]) -> tuple[list[dict], set[str]]:
    """拒绝无所有权或已被人工修改的目标，仅删除旧机器人清单内条目。"""
    manifest_path = "docs/project-brain/manifest.json"
    prior_file = await client.get_repository_file(project_id, manifest_path, ref)
    prior = None
    if prior_file:
        prior = parse_manifest(prior_file["decoded_content"])
        digest = hashlib.sha256(prior_file["decoded_content"].encode()).hexdigest()
        if prior.project_id != project_id or not known_owner(prior.build_id, digest) or any(not owned_path(p) for p in prior.files):
            raise WriteConflict("远端知识清单缺少服务所有权", "unowned_manifest")
    actions, deletions = [], set()
    for path in sorted(set(verified) | (set(prior.files) if prior else set())):
        remote = prior_file if path == manifest_path else await client.get_repository_file(project_id, path, ref)
        desired = verified.get(path)
        if remote:
            if not prior or (path != manifest_path and path not in prior.files):
                raise WriteConflict("目标知识路径存在人工内容，拒绝覆盖", "human_file")
            if path != manifest_path and hashlib.sha256(remote["decoded_content"].encode()).hexdigest() != prior.files[path]:
                raise WriteConflict("知识文件被并发或人工修改，拒绝覆盖", "human_edit")
        if remote and remote["decoded_content"] == desired:
            continue
        if desired is not None:
            action = {"action": "update" if remote else "create", "file_path": path, "content": desired}
            if remote:
                action["last_commit_id"] = remote["last_commit_id"]
            actions.append(action)
        elif remote and prior and path in prior.files:
            deletions.add(path)
            actions.append({"action": "delete", "file_path": path, "last_commit_id": remote["last_commit_id"]})
    return actions, deletions


async def publish(build_id: str, project_id: int, branch: str, checkpoint: dict,
                  save: Callable[[dict], None], known_owner: Callable[[str, str], bool]) -> dict:
    """构建就绪后独立发布；网络丢失响应时先对账远端清单和 MR。"""
    store, client = ArtifactStore(), GitLabClient(purpose="brain_publish")
    manifest, verified = store.read_manifest(build_id), store.repository(build_id)
    digest = hashlib.sha256(verified["docs/project-brain/manifest.json"].encode()).hexdigest()
    # Schema 2's branch is a privacy-safe display value. The operational branch
    # comes exclusively from the DB project identity passed by the task; the
    # owner callback authenticates that exact project's manifest hash. Legacy
    # manifests retain the original equality check.
    legacy_branch_mismatch = manifest.schema_version != "2.0" and manifest.branch != branch
    if manifest.build_id != build_id or manifest.project_id != project_id or legacy_branch_mismatch or not known_owner(build_id, digest):
        raise WriteConflict("产物与数据库登记的发布身份或完整性不符", "invalid_artifact")
    branch_info = await client.get_branch(project_id, branch)
    head = branch_info["commit"]["id"]
    if await branch_fingerprint(client, project_id, head) != manifest.source_fingerprint:
        return {"status": "superseded", "source_changed": True}
    result = dict(checkpoint)
    if result.get("mr_iid"):
        mr = await client.brain_request("GET", f"/projects/{project_id}/merge_requests/{result['mr_iid']}")
        if mr.get("state") == "merged":
            return {**result, "status": "published", "merge_commit_sha": mr.get("merge_commit_sha") or mr.get("squash_commit_sha")}
        if mr.get("state") == "closed":
            return {**result, "status": "failed", "error_message": "知识 MR 已关闭，可手动重试发布"}
    protected = bool(branch_info.get("protected"))
    target = settings.PROJECT_BRAIN_TEMP_BRANCH_PREFIX + build_id if protected else branch
    if protected:
        result["temp_branch"] = target
        save(result)  # 创建前持久化确定分支，响应丢失后可复用。
        await client.create_branch(project_id, target, head)
    target_info = await client.get_branch(project_id, target)
    target_sha = target_info["commit"]["id"]
    if await branch_fingerprint(client, project_id, target_sha) != manifest.source_fingerprint:
        raise ValueError("发布临时分支包含不匹配的业务源码")
    await assert_tree_safe(client, project_id, target_sha, set(verified))
    deletions = set()
    # 远端 manifest 的完整字节相同证明上一提交已成功，不重复创建。
    remote = await client.get_repository_file(project_id, "docs/project-brain/manifest.json", target_sha)
    reconciled = remote and remote["decoded_content"] == verified["docs/project-brain/manifest.json"]
    if reconciled:
        # 仍检查每个文件，防止清单正确但内容被人工修改。
        for path, content in verified.items():
            current = await client.get_repository_file(project_id, path, target_sha)
            if not current or current["decoded_content"] != content:
                raise WriteConflict("远端清单与知识文件内容不一致", "human_edit")
        result["commit_sha"] = remote["last_commit_id"]
        commit = await client.brain_request("GET", f"/projects/{project_id}/repository/commits/{result['commit_sha']}")
        parents = commit.get("parent_ids", [])
        if parents:
            _, deletions = await publication_actions(client, project_id, parents[0], verified, known_owner)
    else:
        actions, deletions = await publication_actions(client, project_id, target_sha, verified, known_owner)
        await assert_tree_safe(client, project_id, target_sha, set(verified) | deletions)
        save(result)  # 在远端写入前再次核对 DB 当前指针和发布开关。
        committed = await client.create_brain_commit(project_id, target, build_id, actions, verified, deletions)
        result["commit_sha"] = committed.get("id") or target_sha
    save(result)
    await verify_commit(client, project_id, result["commit_sha"], verified, deletions)
    if protected:
        latest = await client.get_branch(project_id, branch)
        if await branch_fingerprint(client, project_id, latest["commit"]["id"]) != manifest.source_fingerprint:
            return {**result, "status": "superseded", "source_changed": True}
        save(result)
        if not result.get("mr_iid"):
            candidates = await client.brain_request("GET", f"/projects/{project_id}/merge_requests",
                                                     params={"source_branch": target, "target_branch": branch, "state": "all", "per_page": 100})
            marker = f"<!-- project-brain:{build_id} -->"
            matches = [m for m in candidates if m.get("state") != "closed" and marker in (m.get("description") or "")]
            if len(matches) > 1:
                raise ValueError("同一构建发现多个知识 MR，停止自动写回")
            mr = matches[0] if matches else await client.create_merge_request(project_id, target, branch,
                        f"[AI-BRAIN] 更新项目知识 v{manifest.brain_version}",
                        f"{marker}\n来源提交：{manifest.source_commit}\n已校验证据的项目知识。", squash=False)
            result.update(mr_iid=mr["iid"], mr_url=mr["web_url"])
            save(result)
            if mr.get("state") == "merged":
                return {**result, "status": "published", "merge_commit_sha": mr.get("merge_commit_sha")}
            if mr.get("state") == "closed":
                return {**result, "status": "failed", "error_message": "该构建的知识 MR 已关闭"}
        result["status"] = "mr_open"
        # 自动合并前检验整个 MR，而不只验证本次提交。
        changes = await client.get_mr_changes(project_id, result["mr_iid"])
        if changes.get("overflow") or not isinstance(changes.get("changes"), list):
            raise WriteConflict("知识 MR 差异未完整返回", "invalid_diff")
        validate_changes(changes["changes"], set(verified), deletions)
        current_source = await client.get_branch(project_id, target)
        if current_source["commit"]["id"] != result["commit_sha"]:
            raise WriteConflict("知识 MR 源分支被并发推进", "source_advanced")
        if settings.PROJECT_BRAIN_MR_AUTO_MERGE and current_source.get("protected"):
            save(result)
            merged = await client.merge_brain_mr(project_id, result["mr_iid"], result["commit_sha"])
            if merged.get("state") == "merged":
                result.update(status="published", merge_commit_sha=merged.get("merge_commit_sha"))
            elif merged:
                result["status"] = "auto_merge_pending"
        elif settings.PROJECT_BRAIN_MR_AUTO_MERGE:
            result["error_code"] = "unprotected_source_branch"
            result["error_message"] = "临时分支未保护，已保留 MR 供人工合并；请限制 ai-brain/* 的推送身份"
    else:
        result["status"] = "published"
    latest = await client.get_branch(project_id, branch)
    if await branch_fingerprint(client, project_id, latest["commit"]["id"]) != manifest.source_fingerprint:
        result["source_changed"] = True
    return result
