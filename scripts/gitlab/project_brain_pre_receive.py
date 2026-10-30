#!/usr/bin/env python3
"""GitLab administrator-installed directory guard for the publisher identity."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath

CONFIG_PATH = "/etc/gitlab/project-brain-hook.json"


def git(*args: str) -> bytes:
    """Read quarantined Git objects without changing the repository."""
    result = subprocess.run(["git", *args], capture_output=True, check=True, timeout=60)
    return result.stdout


def allowed(path: str) -> bool:
    """Directory authorization without filename or extension restrictions."""
    parts = path.split("/")
    safe = (bool(path) and not path.startswith("/") and "\\" not in path and ":" not in path
            and all(p not in {"", ".", ".."} and not p.endswith((".", " "))
                    and not PureWindowsPath(p).is_reserved() for p in parts)
            and all(ord(c) >= 32 for c in path))
    return safe and (path == "PROJECT_BRAIN.md" or path.startswith("docs/project-brain/"))


def check_diff(old: str, new: str) -> None:
    """Check each changed path and Git mode, with rename detection disabled."""
    fields = git("diff-tree", "--no-commit-id", "--raw", "-r", "-z", "--no-renames", old, new).split(b"\0")
    for i in range(0, len(fields) - 1, 2):
        metadata = fields[i].decode("ascii").split()
        path = fields[i + 1].decode("utf-8", errors="strict")
        if not allowed(path) or metadata[0][1:] not in {"000000", "100644"} or metadata[1] not in {"000000", "100644"}:
            raise ValueError("发布身份只能修改 PROJECT_BRAIN.md 和 docs/project-brain/ 内普通文件")


def guard(config: dict, updates: list[tuple[str, str, str]]) -> None:
    """Reject history rewrites and inspect every introduced commit and final tree."""
    branches = config["target_branches"]
    prefix = config.get("temp_branch_prefix", "ai-brain/")
    for old, new, ref in updates:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", old) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", new):
            raise ValueError("提交标识无效")
        if not ref.startswith("refs/heads/"):
            raise ValueError("发布身份不允许修改标签或其他引用")
        branch = ref.removeprefix("refs/heads/")
        temporary = branch.startswith(prefix) and bool(re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", branch[len(prefix):]))
        if branch not in branches and not temporary:
            raise ValueError("发布分支不在管理员配置范围内")
        if set(new) == {"0"}:
            if not temporary:
                raise ValueError("发布身份不允许删除目标分支")
            continue
        if set(old) == {"0"}:
            if not temporary:
                raise ValueError("发布身份不允许创建目标分支")
            baseline = None
            for target in branches:
                try:
                    tip = git("rev-parse", "--verify", f"refs/heads/{target}").decode().strip()
                    if git("merge-base", tip, new).decode().strip() == tip:
                        baseline = tip
                        break
                except subprocess.CalledProcessError:
                    continue
            if baseline is None:
                raise ValueError("临时分支必须从当前目标分支创建")
        else:
            baseline = old
            if git("merge-base", old, new).decode().strip() != old:
                raise ValueError("发布身份不允许强制推送或改写历史")
        check_diff(baseline, new)
        for commit in git("rev-list", "--reverse", new, f"^{baseline}").decode().splitlines():
            parents = git("rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
            if not parents:
                raise ValueError("发布身份不允许引入独立历史")
            for parent in parents:
                check_diff(parent, commit)


def main(config_path: str = CONFIG_PATH) -> int:
    """Use GitLab GL_ID rather than spoofable Git author names to identify the bot."""
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not os.environ.get("GL_ID"):
            raise ValueError("缺少 GitLab GL_ID，无法验证发布身份")
        if os.environ.get("GL_ID") not in config["publisher_gl_ids"]:
            return 0
        updates = [tuple(line.split()) for line in sys.stdin if line.strip()]
        guard(config, updates)
        return 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        # 不输出 Git 响应、仓库内容或凭据。
        message = str(exc) if type(exc) is ValueError else "发布路径校验无法完成，请联系 GitLab 管理员"
        print(f"GL-HOOK-ERR: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) == 2 else CONFIG_PATH))
