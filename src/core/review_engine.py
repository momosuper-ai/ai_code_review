"""Core review engine — orchestrates the Agent loop for code review."""

import json
import re
import time
from dataclasses import dataclass, field

from src.core.agent_tools import AgentToolExecutor
from src.core.llm_client import LLMClient
from src.config import settings
from src.utils.logger import logger

# File patterns to skip during review
_SKIP_PATTERNS = {
    # Lock files
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Pipfile.lock",
    "poetry.lock", "Gemfile.lock", "composer.lock", "Cargo.lock",
    "go.sum",
}

_SKIP_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".bmp", ".webp",
    # Fonts
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    # Binary/compiled
    ".pyc", ".pyo", ".class", ".o", ".so", ".dll", ".exe",
    ".jar", ".war", ".zip", ".tar", ".gz", ".rar",
    # Generated
    ".min.js", ".min.css", ".map",
    # Data
    ".sqlite", ".db",
}

_SKIP_PATH_PREFIXES = [
    "vendor/", "node_modules/", "dist/", "build/", ".git/",
    "__pycache__/", ".venv/", "venv/",
]

SYSTEM_PROMPT = """你是一位资深代码审查专家，负责对代码变更进行全面审查。

## 审查维度
1. **潜在缺陷**: 逻辑错误、边界条件、空指针、资源泄漏等
2. **安全问题**: 注入漏洞、敏感信息泄露、权限问题等
3. **性能问题**: 不必要的计算、N+1 查询、内存泄漏等
4. **代码质量**: 可读性、命名规范、重复代码等
5. **设计模式**: 架构合理性、SOLID 原则、耦合度等
6. **代码规范**: 格式、风格一致性等

## 审查原则
- 只审查 diff 中的变更代码，不要评论未改动的代码
- 只报告有实际价值的问题，避免琐碎的样式问题
- 使用中文输出
- 可以使用工具查看完整文件上下文来理解代码逻辑

## 使用工具
你可以调用工具来查看文件内容、搜索代码、查找定义等，以获取足够的上下文来做出准确的判断。
在分析 diff 时，如果需要理解某个函数或类的完整实现，请使用工具获取。
**重要：你可以在同一轮次中同时调用多个工具。** 例如，如果需要查看多个文件或搜索多个关键词，请在一次回复中同时发起所有工具调用，而不是逐个调用。这样能显著提高审查效率。

## 输出格式
完成审查后，请输出以下 JSON 格式（不要包含 markdown 代码块标记）：
{
  "overall_assessment": "对本次代码变更的整体评价",
  "score": "A/B/C/D",
  "issues": [
    {
      "file_path": "文件路径",
      "line_start": 行号或null,
      "line_end": 行号或null,
      "severity": "critical/major/minor/info",
      "category": "bug/security/performance/maintainability/style/design",
      "content": "问题描述",
      "suggestion": "修改建议"
    }
  ],
  "highlights": [
    "代码亮点1",
    "代码亮点2"
  ]
}

评级标准:
- A: 优秀，无重要问题
- B: 良好，有少量小问题
- C: 需改进，存在较多问题或少量严重问题
- D: 需重写，存在严重设计/安全问题"""

FORMAT_RETRY_PROMPT = """你上一次的输出格式不符合要求，无法被系统正确解析。

错误类型: {error_type}
错误详情: {error_detail}

请严格按照以下 JSON 格式重新输出你的审查结果（不要包含 markdown 代码块标记，不要在 JSON 前后添加任何其他文字）：
{{
  "overall_assessment": "对本次代码变更的整体评价",
  "score": "A/B/C/D",
  "issues": [
    {{
      "file_path": "文件路径",
      "line_start": 行号或null,
      "line_end": 行号或null,
      "severity": "critical/major/minor/info",
      "category": "bug/security/performance/maintainability/style/design",
      "content": "问题描述",
      "suggestion": "修改建议"
    }}
  ],
  "highlights": ["代码亮点1", "代码亮点2"]
}}

请直接输出纯 JSON，不要包含任何解释文字。"""

_ERROR_TYPE_DESCRIPTIONS = {
    "no_json": "你的回复中没有找到 JSON 对象，请确保输出一个完整的 JSON。",
    "json_syntax_error": "找到了 JSON 结构但语法有误: {detail}。请检查括号匹配、逗号和引号。",
    "missing_fields": "JSON 解析成功但缺少必需字段: {detail}。",
}

_RETRY_TEMPERATURES = [0.1, 0.05, 0.0]


@dataclass
class ReviewResult:
    overall_assessment: str = ""
    score: str = "B"
    issues: list[dict] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    tool_calls_count: int = 0


class ReviewEngine:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.llm = LLMClient()
        self.tools = AgentToolExecutor(repo_path)
        self.max_rounds = settings.AGENT_MAX_TOOL_ROUNDS

    @staticmethod
    def filter_diffs(changes: list[dict]) -> list[dict]:
        """Filter out non-code files from the diff list."""
        filtered = []
        for change in changes:
            path = change.get("new_path") or change.get("old_path", "")
            filename = path.split("/")[-1] if "/" in path else path

            if filename in _SKIP_PATTERNS:
                continue
            ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext in _SKIP_EXTENSIONS:
                continue
            if any(path.startswith(p) for p in _SKIP_PATH_PREFIXES):
                continue

            filtered.append(change)
        return filtered

    def _build_diff_prompt(self, changes: list[dict]) -> str:
        """Build user prompt containing the diffs."""
        parts = ["以下是本次代码变更的 diff 内容，请进行审查：\n"]
        for change in changes:
            path = change.get("new_path") or change.get("old_path", "")
            diff = change.get("diff", "")
            if not diff:
                continue
            parts.append(f"### {path}\n```diff\n{diff}\n```\n")
        return "\n".join(parts)

    async def run_review(self, changes: list[dict], review_id: int | None = None) -> ReviewResult:
        """Run the agent review loop on a set of (pre-filtered) file changes."""
        tag = f"[review:{review_id}] " if review_id else ""

        if not changes:
            return ReviewResult(
                overall_assessment="本次变更中没有需要审查的代码文件。",
                score="A",
            )

        user_prompt = self._build_diff_prompt(changes)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        tool_defs = AgentToolExecutor.tool_definitions()
        total_tool_calls = 0

        for round_num in range(self.max_rounds):
            logger.info("%sAgent 第 %d/%d 轮", tag, round_num + 1, self.max_rounds)

            response = await self.llm.chat(messages, tools=tool_defs)

            # Check if there are tool calls
            if not response.tool_calls:
                # No tool calls — LLM has finished analysis
                content = response.content or ""
                result = await self._parse_with_retry(content, messages, tag)
                result.tool_calls_count = total_tool_calls
                logger.info(
                    "%s结果解析成功: 评级=%s 问题数=%d",
                    tag, result.score, len(result.issues),
                )
                return result

            # Process tool calls
            messages.append(response)
            for tc in response.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                t0 = time.time()
                tool_result = self.tools.execute_tool(tc.function.name, args)
                tool_ms = (time.time() - t0) * 1000
                total_tool_calls += 1

                status = "成功" if not tool_result.output.startswith("Error") else "失败"
                logger.info(
                    "%s工具调用: %s(%s) -> %s (%.0f毫秒)",
                    tag, tc.function.name,
                    json.dumps(args, ensure_ascii=False)[:200],
                    status, tool_ms,
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result.output,
                })

        # Max rounds reached — force a final response without tools
        logger.warning("%s达到最大工具轮次 (%d)，请求最终输出", tag, self.max_rounds)

        # 检查最后一条消息是否是 tool 角色
        # 如果是，将终止指令嵌入最后一条 tool 消息，避免在 tool 后插入 user 消息导致 API 报错
        if messages and messages[-1].get("role") == "tool":
            messages[-1]["content"] += (
                "\n\n[系统提示: 已达到最大工具调用轮次限制，请不要再调用工具，"
                "直接输出你的最终审查结果（JSON 格式）。]"
            )
        else:
            messages.append({
                "role": "user",
                "content": "请停止使用工具，直接输出你的最终审查结果（JSON 格式）。",
            })
        response = await self.llm.chat(messages, tools=None)
        content = response.content or ""
        result = await self._parse_with_retry(content, messages, tag)
        result.tool_calls_count = total_tool_calls
        return result

    @staticmethod
    def _try_parse(content: str) -> tuple[ReviewResult | None, str, str]:
        """尝试解析 LLM 输出，返回 (result_or_None, error_type, error_detail)"""
        cleaned = re.sub(r"```json\s*", "", content)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        cleaned = cleaned.strip()

        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if not json_match:
            return None, "no_json", "回复中未找到 JSON 对象"

        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            return None, "json_syntax_error", str(e)

        required = {"overall_assessment", "score"}
        missing = required - set(data.keys())
        if missing:
            return None, "missing_fields", ", ".join(sorted(missing))

        return ReviewResult(
            overall_assessment=data.get("overall_assessment", ""),
            score=data.get("score", "B"),
            issues=data.get("issues", []),
            highlights=data.get("highlights", []),
        ), "", ""

    @staticmethod
    def _fallback_result(content: str) -> ReviewResult:
        """降级解析：尽力提取部分字段，否则使用原始文本。"""
        cleaned = re.sub(r"```json\s*", "", content)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        cleaned = cleaned.strip()

        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return ReviewResult(
                    overall_assessment=data.get("overall_assessment", ""),
                    score=data.get("score", "B"),
                    issues=data.get("issues", []),
                    highlights=data.get("highlights", []),
                )
            except json.JSONDecodeError:
                pass

        # 清理残余 JSON 碎片
        text = re.sub(r"\{[\s\S]*", "", content).strip() or content.strip()
        assessment = (
            "> **注意**: 审查结果格式解析失败，以下为 AI 原始审查意见：\n\n"
            + text
        )
        return ReviewResult(overall_assessment=assessment, score="B")

    async def _parse_with_retry(
        self,
        content: str,
        messages: list[dict],
        tag: str = "",
    ) -> ReviewResult:
        """解析 LLM 输出，失败则重试引导 LLM 重新输出正确格式。"""
        result, error_type, error_detail = self._try_parse(content)
        if result is not None:
            return result

        max_retries = settings.AGENT_FORMAT_RETRY_MAX
        retry_messages = list(messages)  # 浅拷贝
        last_content = content

        for attempt in range(1, max_retries + 1):
            desc = _ERROR_TYPE_DESCRIPTIONS.get(error_type, error_detail)
            if "{detail}" in desc:
                desc = desc.format(detail=error_detail)

            logger.warning(
                "%s输出格式解析失败 (类型=%s)，开始第 %d/%d 次格式重试",
                tag, error_type, attempt, max_retries,
            )

            retry_messages.append({"role": "assistant", "content": last_content})
            retry_messages.append({
                "role": "user",
                "content": FORMAT_RETRY_PROMPT.format(
                    error_type=desc,
                    error_detail=error_detail,
                ),
            })

            temperature = _RETRY_TEMPERATURES[min(attempt - 1, len(_RETRY_TEMPERATURES) - 1)]
            try:
                response = await self.llm.chat(
                    retry_messages, tools=None, temperature=temperature,
                )
                last_content = response.content or ""
            except Exception:
                logger.error(
                    "%s格式重试第 %d 次 LLM 调用异常", tag, attempt, exc_info=True,
                )
                continue

            result, error_type, error_detail = self._try_parse(last_content)
            if result is not None:
                logger.info("%s格式重试成功 (第 %d 次尝试)", tag, attempt)
                return result

        logger.error("%s格式重试全部失败 (%d 次)，使用降级 fallback", tag, max_retries)
        return self._fallback_result(last_content)

    @staticmethod
    def _parse_result(content: str) -> ReviewResult:
        """Parse the LLM output into a ReviewResult (同步，兼容外部调用)."""
        result, _, _ = ReviewEngine._try_parse(content)
        if result is not None:
            return result
        return ReviewEngine._fallback_result(content)

    @staticmethod
    def format_gitlab_comment(result: ReviewResult, files_count: int) -> str:
        """Format the review result as a GitLab Markdown comment."""
        severity_icons = {
            "critical": "🔴",
            "major": "🟠",
            "minor": "🟡",
            "info": "🔵",
        }

        score_icons = {
            "A": "🟢",
            "B": "🔵",
            "C": "🟡",
            "D": "🔴",
        }

        score_icon = score_icons.get(result.score, "⚪")
        lines = [
            f"## AI 代码审查报告 {score_icon} 评级: {result.score}",
            "",
            f"**审查文件数**: {files_count} | **发现问题数**: {len(result.issues)}",
            "",
            "### 整体评价",
            result.overall_assessment,
            "",
        ]

        if result.issues:
            lines.append("### 问题列表")
            lines.append("")
            for i, issue in enumerate(result.issues, 1):
                icon = severity_icons.get(issue.get("severity", "info"), "⚪")
                severity = issue.get("severity", "info").upper()
                category = issue.get("category", "")
                file_path = issue.get("file_path", "")
                line_start = issue.get("line_start")
                content = issue.get("content", "")
                suggestion = issue.get("suggestion", "")

                location = file_path
                if line_start:
                    location += f":{line_start}"
                    line_end = issue.get("line_end")
                    if line_end and line_end != line_start:
                        location += f"-{line_end}"

                lines.append(f"#### {icon} {i}. [{severity}] {category} — `{location}`")
                lines.append(f"{content}")
                if suggestion:
                    lines.append(f"\n> **建议**: {suggestion}")
                lines.append("")

        if result.highlights:
            lines.append("### 亮点")
            lines.append("")
            for h in result.highlights:
                lines.append(f"- ✨ {h}")
            lines.append("")

        lines.append("---")
        lines.append("*由 AI 代码审查服务自动生成*")

        return "\n".join(lines)
