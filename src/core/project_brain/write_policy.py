"""Directory-bound publication checks independent of semantic compilation."""

from src.core.gitlab_client import GitLabClient
from src.core.project_brain.artifacts import owned_path

POLICY_VERSION = "2"


class WriteConflict(ValueError):
    """人工修改、结构冲突或不完整差异使发布停止。"""

    def __init__(self, message: str, code: str = "write_conflict") -> None:
        super().__init__(message)
        self.code = code


def validate_changes(changes: list[dict], updates: set[str], deletions: set[str]) -> None:
    """核对完整差异；Git 识别出的删除/新增重命名也必须两端属于清单。"""
    for change in changes:
        old, new = change.get("old_path", ""), change.get("new_path", "")
        if (not owned_path(old) or not owned_path(new)
                or change.get("too_large") or change.get("collapsed")):
            raise WriteConflict("发布差异包含越界路径、重命名或未完整返回的文件", "invalid_diff")
        if (old != new or change.get("renamed_file")) and (old not in deletions or new not in updates):
            raise WriteConflict("文档路径变更缺少旧文件所有权或新文件产物校验", "invalid_diff")
        mode_a, mode_b = change.get("a_mode"), change.get("b_mode")
        if mode_a not in {None, "0", "000000", "100644"} or mode_b not in {None, "0", "000000", "100644"}:
            raise WriteConflict("发布差异包含非普通文件或权限修改", "invalid_mode")
        if new not in (deletions if change.get("deleted_file") else updates):
            raise WriteConflict("发布差异不属于已验证产物或已登记的失效文件", "invalid_diff")


async def assert_tree_safe(client: GitLabClient, project_id: int, sha: str, paths: set[str]) -> None:
    """逐层检查目标，拒绝链接、子模块、可执行文件与文件/目录冲突。"""
    prefixes = {"/".join(path.split("/")[:i]) for path in paths for i in range(1, len(path.split("/")))}
    page = 1
    while True:
        entries = await client.brain_request("GET", f"/projects/{project_id}/repository/tree",
                                             params={"ref": sha, "recursive": True, "per_page": 100, "page": page})
        for entry in entries:
            path = entry["path"]
            if path in paths and (entry.get("type") != "blob" or entry.get("mode") != "100644"):
                raise WriteConflict("知识目标不是普通非执行文件", "invalid_mode")
            if path in prefixes and entry.get("type") != "tree":
                raise WriteConflict("知识目录祖先包含符号链接、子模块或普通文件", "unsafe_ancestor")
        if len(entries) < 100:
            break
        page += 1


async def verify_commit(client: GitLabClient, project_id: int, sha: str, verified: dict[str, str], deletions: set[str]) -> None:
    """提交后核对实际差异与全部产物，响应丢失后的重试也使用同一校验。"""
    changes, page = [], 1
    while True:
        part = await client.brain_request("GET", f"/projects/{project_id}/repository/commits/{sha}/diff",
                                          params={"per_page": 100, "page": page})
        changes.extend(part)
        if len(part) < 100:
            break
        page += 1
    validate_changes(changes, set(verified), deletions)
    await assert_tree_safe(client, project_id, sha, set(verified) | deletions)
    for path, content in verified.items():
        remote = await client.get_repository_file(project_id, path, sha)
        if not remote or remote["decoded_content"] != content:
            raise WriteConflict("提交内容与已验证知识产物不一致", "content_mismatch")
    for path in deletions:
        if await client.get_repository_file(project_id, path, sha) is not None:
            raise WriteConflict("失效文件未从知识提交删除", "content_mismatch")
