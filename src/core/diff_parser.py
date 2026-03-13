import re

# 不需要审查的文件模式
SKIP_PATTERNS = [
    r".*\.lock$",
    r".*\.min\.(js|css)$",
    r".*\.(png|jpg|jpeg|gif|svg|ico|woff|woff2|ttf|eot)$",
    r".*package-lock\.json$",
    r".*yarn\.lock$",
    r".*pnpm-lock\.yaml$",
    r".*go\.sum$",
    r".*\.generated\.\w+$",
    r".*/migrations/.*",
    r".*/node_modules/.*",
    r".*/vendor/.*",
    r".*/dist/.*",
    r".*/build/.*",
]


def should_skip_file(file_path: str) -> bool:
    """判断文件是否应跳过审查。"""
    return any(re.match(pattern, file_path) for pattern in SKIP_PATTERNS)


def parse_diff_changes(changes: list[dict]) -> list[dict]:
    """解析 GitLab MR changes，提取需要审查的文件和变更内容。

    Args:
        changes: GitLab API 返回的 changes 列表

    Returns:
        过滤后的变更文件列表，每项包含 file_path, diff, new_file, deleted_file
    """
    result = []
    for change in changes:
        file_path = change.get("new_path", change.get("old_path", ""))

        # 跳过删除的文件和不需要审查的文件
        if change.get("deleted_file", False):
            continue
        if should_skip_file(file_path):
            continue

        diff = change.get("diff", "")
        if not diff:
            continue

        result.append({
            "file_path": file_path,
            "diff": diff,
            "new_file": change.get("new_file", False),
        })

    return result


def extract_added_lines(diff: str) -> list[tuple[int, str]]:
    """从 diff 中提取新增的代码行及其行号。

    Returns:
        (行号, 代码内容) 的列表
    """
    added_lines = []
    current_line = 0

    for line in diff.split("\n"):
        # 解析 @@ -a,b +c,d @@ 行号信息
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk_match:
            current_line = int(hunk_match.group(1))
            continue

        if line.startswith("+") and not line.startswith("+++"):
            added_lines.append((current_line, line[1:]))
            current_line += 1
        elif line.startswith("-"):
            continue  # 删除行不增加行号
        else:
            current_line += 1

    return added_lines
