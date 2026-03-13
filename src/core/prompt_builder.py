import json
from pathlib import Path

# 语言到文件扩展名的映射
EXTENSION_LANG_MAP = {
    ".java": "java",
    ".py": "python",
    ".go": "go",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".vue": "vue",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".kt": "kotlin",
    ".scala": "scala",
    ".swift": "swift",
}


def detect_language(file_path: str) -> str:
    """根据文件扩展名检测编程语言。"""
    ext = Path(file_path).suffix.lower()
    return EXTENSION_LANG_MAP.get(ext, "unknown")


def build_review_prompt(file_path: str, diff: str, custom_rules: str | None = None) -> str:
    """构建代码审查 Prompt。

    Args:
        file_path: 文件路径
        diff: 文件 diff 内容
        custom_rules: 项目自定义审查规则
    """
    language = detect_language(file_path)

    output_format = json.dumps(
        {
            "comments": [
                {
                    "line": "问题所在行号（整数）",
                    "severity": "critical/major/minor/info",
                    "category": "security/performance/maintainability/style/bug",
                    "content": "问题描述（中文）",
                    "suggestion": "修改建议（中文，可选）",
                }
            ],
            "summary": "整体评价（中文，2-3句话）",
        },
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""请审查以下代码变更，文件路径为 `{file_path}`（语言：{language}）。

## 审查维度
1. **安全性**：是否存在注入、XSS、敏感信息泄露等安全问题
2. **潜在 Bug**：是否存在空指针、越界、资源泄漏、并发问题等
3. **性能**：是否有明显的性能问题（如 N+1 查询、不必要的循环等）
4. **可维护性**：代码结构是否清晰、命名是否合理、是否有过度复杂的逻辑
5. **代码风格**：是否符合该语言的惯用写法

## 审查要求
- 只关注 diff 中新增和修改的代码（以 + 开头的行）
- 不要报告 diff 中删除的代码（以 - 开头的行）
- 只报告真正有价值的问题，避免过于琐碎的建议
- 如果代码质量良好，可以返回空的 comments 列表
- 所有输出必须使用中文"""

    if custom_rules:
        prompt += f"""

## 项目自定义规则
{custom_rules}"""

    prompt += f"""

## 代码变更（Diff）
```diff
{diff}
```

## 输出格式
请严格按照以下 JSON 格式输出，不要包含其他内容：
```json
{output_format}
```"""

    return prompt


def build_summary_prompt(file_summaries: list[dict]) -> str:
    """构建总结评论的 Prompt。

    Args:
        file_summaries: 各文件的审查结果列表
    """
    files_info = "\n".join(
        f"- `{s['file_path']}`: {s.get('summary', '无问题')}" for s in file_summaries
    )

    return f"""请根据以下各文件的代码审查结果，生成一份简洁的总结评论。

## 各文件审查结果
{files_info}

## 要求
- 使用中文
- 先给出整体评价（1-2句话）
- 如果有问题，按严重程度列出关键问题
- 格式使用 Markdown，适合在 GitLab MR 评论中展示
- 开头使用 "## AI 代码审查报告" 作为标题
"""
