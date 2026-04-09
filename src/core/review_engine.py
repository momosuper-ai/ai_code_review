"""Core review engine — orchestrates the Agent loop for code review."""

import asyncio
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
    dimension_stats: list[dict] = field(default_factory=list)


@dataclass
class DimensionResult:
    dimension_name: str
    display_name: str = ""
    icon: str = ""
    overall_assessment: str = ""
    score: str = "B"
    issues: list[dict] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    tool_calls_count: int = 0
    success: bool = True
    error: str | None = None
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Module-level _parse_with_retry (shared by DimensionAgent and ReviewEngine)
# ---------------------------------------------------------------------------

async def _parse_with_retry(
    llm: LLMClient,
    content: str,
    messages: list[dict],
    tag: str = "",
    categories: list[str] | None = None,
) -> ReviewResult:
    """解析 LLM 输出，失败则重试引导 LLM 重新输出正确格式。"""
    result, error_type, error_detail = ReviewEngine._try_parse(content)
    if result is not None:
        return result

    from src.core.dimensions import build_format_retry_prompt

    max_retries = settings.AGENT_FORMAT_RETRY_MAX
    retry_messages = list(messages)  # shallow copy
    last_content = content
    retry_prompt_template = build_format_retry_prompt(categories)

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
            "content": retry_prompt_template.format(
                error_type=desc,
                error_detail=error_detail,
            ),
        })

        temperature = _RETRY_TEMPERATURES[min(attempt - 1, len(_RETRY_TEMPERATURES) - 1)]
        try:
            response = await llm.chat(
                retry_messages, tools=None, temperature=temperature,
            )
            last_content = response.content or ""
        except Exception:
            logger.error(
                "%s格式重试第 %d 次 LLM 调用异常", tag, attempt, exc_info=True,
            )
            continue

        result, error_type, error_detail = ReviewEngine._try_parse(last_content)
        if result is not None:
            logger.info("%s格式重试成功 (第 %d 次尝试)", tag, attempt)
            return result

    logger.error("%s格式重试全部失败 (%d 次)，使用降级 fallback", tag, max_retries)
    return ReviewEngine._fallback_result(last_content)


# ---------------------------------------------------------------------------
# DimensionAgent — runs one review dimension
# ---------------------------------------------------------------------------

class DimensionAgent:
    def __init__(
        self,
        dimension,  # ReviewDimension
        llm: LLMClient,
        tools: AgentToolExecutor,
        max_rounds: int,
        review_id: int | None = None,
        privacy_filter=None,
    ):
        self.dimension = dimension
        self.llm = llm
        self.tools = tools
        self.max_rounds = max_rounds
        self.review_id = review_id
        self.privacy_filter = privacy_filter

    async def run(self, diff_prompt: str) -> DimensionResult:
        """执行该维度的完整 agent tool-calling 循环。"""
        from src.core.dimensions import build_dimension_prompt

        start_time = time.time()
        tag = f"[review:{self.review_id}][{self.dimension.name}] " if self.review_id else f"[{self.dimension.name}] "

        system_prompt = build_dimension_prompt(self.dimension)
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": diff_prompt},
        ]
        tool_defs = AgentToolExecutor.tool_definitions()
        tool_calls_count = 0

        for round_num in range(1, self.max_rounds + 1):
            logger.info("%sAgent 第 %d/%d 轮", tag, round_num, self.max_rounds)

            response = await self.llm.chat(messages, tools=tool_defs)

            if not response.tool_calls:
                content = response.content or ""
                result = await _parse_with_retry(
                    self.llm, content, messages, tag,
                    categories=self.dimension.categories,
                )
                return self._to_dimension_result(result, tool_calls_count, start_time)

            # Process tool calls — 先解析参数，修复无效 JSON 后再追加到消息历史
            parsed_args_map: dict[str, dict] = {}
            for tc in response.tool_calls:
                try:
                    parsed_args_map[tc.id] = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    logger.warning(
                        "%s工具 %s 参数 JSON 解析失败，原始内容: %s",
                        tag, tc.function.name,
                        tc.function.arguments[:200] if tc.function.arguments else "(空)",
                    )
                    parsed_args_map[tc.id] = {}
                    tc.function.arguments = "{}"

            messages.append(response)
            tool_outputs = []
            for tc in response.tool_calls:
                args = parsed_args_map[tc.id]

                t0 = time.time()
                tool_result = self.tools.execute_tool(tc.function.name, args)
                tool_ms = (time.time() - t0) * 1000
                tool_calls_count += 1

                status = "成功" if tool_result.success else "失败"
                logger.info(
                    "%s工具调用: %s(%s) -> %s (%.0f毫秒)",
                    tag, tc.function.name,
                    json.dumps(args, ensure_ascii=False)[:200],
                    status, tool_ms,
                )

                tool_outputs.append(tool_result.output)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result.output,
                })

            # 本轮工具结果批量扫描（发现新敏感模式）
            if self.privacy_filter and tool_outputs:
                await self.privacy_filter.scan_tool_results(tool_outputs)

        # Max rounds reached
        logger.warning("%s达到最大工具轮次 (%d)，请求最终输出", tag, self.max_rounds)

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
        result = await _parse_with_retry(
            self.llm, content, messages, tag,
            categories=self.dimension.categories,
        )
        return self._to_dimension_result(result, tool_calls_count, start_time)

    def _to_dimension_result(
        self, review_result: ReviewResult, tool_calls_count: int, start_time: float,
    ) -> DimensionResult:
        """将 ReviewResult 转为 DimensionResult，注入维度元数据。"""
        for issue in review_result.issues:
            issue["dimension"] = self.dimension.name

        return DimensionResult(
            dimension_name=self.dimension.name,
            display_name=self.dimension.display_name,
            icon=self.dimension.icon,
            overall_assessment=review_result.overall_assessment,
            score=review_result.score,
            issues=review_result.issues,
            highlights=review_result.highlights,
            tool_calls_count=tool_calls_count,
            duration_seconds=time.time() - start_time,
        )


# ---------------------------------------------------------------------------
# ReviewEngine
# ---------------------------------------------------------------------------

class ReviewEngine:
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        self.tools = AgentToolExecutor(repo_path)
        self.max_rounds = settings.AGENT_MAX_TOOL_ROUNDS

        # 双模型 + 隐私过滤
        if settings.PRIVACY_FILTER_ENABLED and settings.CLOUD_LLM_MODEL_NAME:
            from src.core.privacy_filter import PrivacyFilter

            self.local_llm = LLMClient()  # 默认本地模型
            self.llm = LLMClient(model_name=settings.CLOUD_LLM_MODEL_NAME)
            self.privacy_filter = PrivacyFilter(local_llm=self.local_llm)
            self.llm.set_privacy_filter(self.privacy_filter)
            logger.info(
                "双模型模式: 云端=%s 本地=%s 隐私过滤=%s",
                settings.CLOUD_LLM_MODEL_NAME,
                settings.VLLM_MODEL_NAME,
                settings.PRIVACY_FILTER_LEVEL,
            )
        else:
            self.llm = LLMClient()
            self.local_llm = None
            self.privacy_filter = None

    async def aclose(self):
        """关闭底层 LLM 客户端。"""
        await self.llm.aclose()
        if self.local_llm:
            await self.local_llm.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()

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

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run_review(self, changes: list[dict], review_id: int | None = None) -> ReviewResult:
        """Run the agent review loop on a set of (pre-filtered) file changes."""
        if not changes:
            return ReviewResult(
                overall_assessment="本次变更中没有需要审查的代码文件。",
                score="A",
            )

        # Phase 1: Diff 隐私预扫描
        if self.privacy_filter:
            diff_text = self._build_diff_prompt(changes)
            await self.privacy_filter.pre_scan(diff_text)

        if settings.REVIEW_SINGLE_AGENT_MODE:
            result = await self._run_single_agent(changes, review_id)
        else:
            result = await self._run_multi_agent(changes, review_id)

        # Phase 3: 结果反脱敏
        if self.privacy_filter:
            result = self._desanitize_result(result)

        return result

    # ------------------------------------------------------------------
    # Privacy desanitization
    # ------------------------------------------------------------------

    def _desanitize_result(self, result: ReviewResult) -> ReviewResult:
        """还原审查结果中的占位符为原始值。"""
        pf = self.privacy_filter
        if not pf:
            return result

        result.overall_assessment = pf.desanitize_text(result.overall_assessment)
        for issue in result.issues:
            for key in ("content", "suggestion", "file_path", "code_snippet"):
                if issue.get(key):
                    issue[key] = pf.desanitize_text(issue[key])
        result.highlights = [pf.desanitize_text(h) for h in result.highlights]

        # Desanitize dimension stats assessments
        for stat in result.dimension_stats:
            if stat.get("assessment"):
                stat["assessment"] = pf.desanitize_text(stat["assessment"])

        logger.info("结果反脱敏完成: 映射表大小=%d", pf._mapping.count)
        return result

    # ------------------------------------------------------------------
    # Single-agent mode (original logic)
    # ------------------------------------------------------------------

    async def _run_single_agent(self, changes: list[dict], review_id: int | None = None) -> ReviewResult:
        """原有单Agent逻辑，完整保留现有 run_review 行为。"""
        tag = f"[review:{review_id}] " if review_id else ""

        user_prompt = self._build_diff_prompt(changes)
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        tool_defs = AgentToolExecutor.tool_definitions()
        total_tool_calls = 0

        for round_num in range(self.max_rounds):
            logger.info("%sAgent 第 %d/%d 轮", tag, round_num + 1, self.max_rounds)

            response = await self.llm.chat(messages, tools=tool_defs)

            if not response.tool_calls:
                content = response.content or ""
                result = await self._parse_with_retry(content, messages, tag)
                result.tool_calls_count = total_tool_calls
                logger.info(
                    "%s结果解析成功: 评级=%s 问题数=%d",
                    tag, result.score, len(result.issues),
                )
                return result

            # Process tool calls — 先解析参数，修复无效 JSON 后再追加到消息历史
            parsed_args_map: dict[str, dict] = {}
            for tc in response.tool_calls:
                try:
                    parsed_args_map[tc.id] = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    logger.warning(
                        "%s工具 %s 参数 JSON 解析失败，原始内容: %s",
                        tag, tc.function.name,
                        tc.function.arguments[:200] if tc.function.arguments else "(空)",
                    )
                    parsed_args_map[tc.id] = {}
                    tc.function.arguments = "{}"

            messages.append(response)
            tool_outputs = []
            for tc in response.tool_calls:
                args = parsed_args_map[tc.id]

                t0 = time.time()
                tool_result = self.tools.execute_tool(tc.function.name, args)
                tool_ms = (time.time() - t0) * 1000
                total_tool_calls += 1

                status = "成功" if tool_result.success else "失败"
                logger.info(
                    "%s工具调用: %s(%s) -> %s (%.0f毫秒)",
                    tag, tc.function.name,
                    json.dumps(args, ensure_ascii=False)[:200],
                    status, tool_ms,
                )

                tool_outputs.append(tool_result.output)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result.output,
                })

            # 本轮工具结果批量扫描（发现新敏感模式）
            if self.privacy_filter and tool_outputs:
                await self.privacy_filter.scan_tool_results(tool_outputs)

        # Max rounds reached
        logger.warning("%s达到最大工具轮次 (%d)，请求最终输出", tag, self.max_rounds)

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

    # ------------------------------------------------------------------
    # Multi-agent mode
    # ------------------------------------------------------------------

    async def _run_multi_agent(self, changes: list[dict], review_id: int | None = None) -> ReviewResult:
        """多Agent并行审查。"""
        from src.core.dimensions import get_enabled_dimensions

        dimensions = get_enabled_dimensions()
        diff_prompt = self._build_diff_prompt(changes)
        tag = f"[review:{review_id}] " if review_id else ""

        logger.info("%s启动多维度并行审查: %d 个维度", tag, len(dimensions))

        agents = [
            DimensionAgent(dim, self.llm, self.tools,
                           settings.REVIEW_DIMENSION_MAX_ROUNDS, review_id,
                           privacy_filter=self.privacy_filter)
            for dim in dimensions
        ]
        semaphore = asyncio.Semaphore(settings.REVIEW_MAX_CONCURRENCY)

        async def _run_one(agent: DimensionAgent) -> DimensionResult:
            async with semaphore:
                try:
                    return await asyncio.wait_for(
                        agent.run(diff_prompt),
                        timeout=settings.REVIEW_DIMENSION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "%s维度 %s 审查超时 (%ds)",
                        tag, agent.dimension.display_name, settings.REVIEW_DIMENSION_TIMEOUT,
                    )
                    return DimensionResult(
                        dimension_name=agent.dimension.name,
                        display_name=agent.dimension.display_name,
                        icon=agent.dimension.icon,
                        success=False,
                        error="审查超时",
                    )
                except Exception as exc:
                    logger.error(
                        "%s维度 %s 审查失败: %s",
                        tag, agent.dimension.display_name, exc, exc_info=True,
                    )
                    return DimensionResult(
                        dimension_name=agent.dimension.name,
                        display_name=agent.dimension.display_name,
                        icon=agent.dimension.icon,
                        success=False,
                        error=str(exc),
                    )

        dim_results = await asyncio.gather(*[_run_one(a) for a in agents])

        logger.info(
            "%s多维度审查完成: 成功=%d 失败=%d",
            tag,
            sum(1 for r in dim_results if r.success),
            sum(1 for r in dim_results if not r.success),
        )

        return self._merge_results(dim_results)

    # ------------------------------------------------------------------
    # Result merging
    # ------------------------------------------------------------------

    def _merge_results(self, dim_results: list[DimensionResult]) -> ReviewResult:
        successful = [r for r in dim_results if r.success]
        failed = [r for r in dim_results if not r.success]

        if not successful:
            raise RuntimeError(f"所有审查维度均失败: {[r.error for r in failed]}")

        all_issues = self._deduplicate_issues(successful)
        weighted_score = self._compute_weighted_score(successful, all_issues)
        overall = self._build_overall_assessment(successful, failed)
        all_highlights: list[str] = []
        for r in successful:
            all_highlights.extend(f"{r.icon} {h}" for h in r.highlights)
        total_tool_calls = sum(r.tool_calls_count for r in dim_results)

        dimension_stats = []
        for r in dim_results:
            dimension_stats.append({
                "name": r.dimension_name,
                "display_name": r.display_name,
                "icon": r.icon,
                "score": r.score if r.success else "N/A",
                "issues_count": len(r.issues) if r.success else 0,
                "tool_calls": r.tool_calls_count,
                "duration": r.duration_seconds,
                "success": r.success,
                "error": r.error,
                "assessment": r.overall_assessment if r.success else "",
            })

        return ReviewResult(
            overall_assessment=overall,
            score=weighted_score,
            issues=all_issues,
            highlights=all_highlights,
            tool_calls_count=total_tool_calls,
            dimension_stats=dimension_stats,
        )

    @staticmethod
    def _deduplicate_issues(dim_results: list[DimensionResult]) -> list[dict]:
        """合并所有维度的 issues，对相同位置+相同分类的问题去重。"""
        all_issues: list[dict] = []
        seen: dict[tuple, dict] = {}
        severity_order = {"critical": 0, "major": 1, "minor": 2, "info": 3}

        for r in dim_results:
            for issue in r.issues:
                key = (
                    issue.get("file_path", ""),
                    issue.get("line_start"),
                    issue.get("line_end"),
                    issue.get("category", ""),
                )
                existing = seen.get(key)
                if existing is None:
                    seen[key] = issue
                    all_issues.append(issue)
                else:
                    existing_sev = severity_order.get(existing.get("severity", ""), 3)
                    new_sev = severity_order.get(issue.get("severity", ""), 3)
                    if new_sev < existing_sev or (
                        new_sev == existing_sev
                        and len(issue.get("content", "")) > len(existing.get("content", ""))
                    ):
                        idx = all_issues.index(existing)
                        all_issues[idx] = issue
                        seen[key] = issue

        return all_issues

    @staticmethod
    def _compute_weighted_score(successful: list[DimensionResult], all_issues: list[dict]) -> str:
        from src.core.dimensions import get_all_dimensions

        score_map = {"A": 4, "B": 3, "C": 2, "D": 1}
        weight_map = {d.name: d.weight for d in get_all_dimensions()}

        weighted_sum = sum(
            score_map.get(r.score, 3) * weight_map.get(r.dimension_name, 0.2)
            for r in successful
        )
        total_weight = sum(weight_map.get(r.dimension_name, 0.2) for r in successful)
        avg = weighted_sum / total_weight if total_weight > 0 else 3

        if avg >= 3.5:
            final = "A"
        elif avg >= 2.5:
            final = "B"
        elif avg >= 1.5:
            final = "C"
        else:
            final = "D"

        # Override: any dimension D -> final <= C
        if any(r.score == "D" for r in successful):
            if score_map.get(final, 0) > score_map.get("C", 0):
                final = "C"

        # Override: any critical issue -> final <= C
        if any(issue.get("severity") == "critical" for issue in all_issues):
            if score_map.get(final, 0) > score_map.get("C", 0):
                final = "C"

        return final

    @staticmethod
    def _build_overall_assessment(
        successful: list[DimensionResult], failed: list[DimensionResult],
    ) -> str:
        """拼接各维度评价为统一的整体评价。"""
        parts = []
        for r in successful:
            if r.overall_assessment.strip():
                parts.append(f"**{r.icon} {r.display_name}**：{r.overall_assessment}")

        if failed:
            failed_names = "、".join(r.display_name for r in failed)
            parts.append(f"\n> ⚠️ 以下维度审查未完成：{failed_names}")

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # Parsing helpers (static, unchanged)
    # ------------------------------------------------------------------

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
        """实例方法代理，保持单Agent模式的兼容。"""
        return await _parse_with_retry(self.llm, content, messages, tag)

    @staticmethod
    def _parse_result(content: str) -> ReviewResult:
        """Parse the LLM output into a ReviewResult (同步，兼容外部调用)."""
        result, _, _ = ReviewEngine._try_parse(content)
        if result is not None:
            return result
        return ReviewEngine._fallback_result(content)

    # ------------------------------------------------------------------
    # GitLab comment formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_gitlab_comment(result: ReviewResult, files_count: int) -> str:
        """Format the review result as a GitLab Markdown comment."""
        # If no dimension_stats, use legacy format (single-agent mode)
        if not result.dimension_stats:
            return ReviewEngine._format_legacy_comment(result, files_count)
        return ReviewEngine._format_multi_dimension_comment(result, files_count)

    @staticmethod
    def _format_legacy_comment(result: ReviewResult, files_count: int) -> str:
        """Legacy single-agent comment format."""
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

    @staticmethod
    def _format_multi_dimension_comment(result: ReviewResult, files_count: int) -> str:
        """Multi-dimension comment format with per-dimension sections."""
        severity_icons = {
            "critical": "🔴",
            "major": "🟠",
            "minor": "🟡",
            "info": "🔵",
        }
        score_icons = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}

        dim_count = len(result.dimension_stats)
        score_icon = score_icons.get(result.score, "⚪")
        lines = [
            f"## AI 代码审查报告 {score_icon} 评级: {result.score}",
            "",
            f"**审查文件数**: {files_count} | **发现问题数**: {len(result.issues)} | **审查维度**: {dim_count}",
            "",
            "### 整体评价",
            result.overall_assessment,
            "",
        ]

        # Group issues by dimension
        issues_by_dim: dict[str, list[dict]] = {}
        for issue in result.issues:
            dim_name = issue.get("dimension", "unknown")
            issues_by_dim.setdefault(dim_name, []).append(issue)

        # Per-dimension issue sections (only for dimensions with issues)
        issue_counter = 0
        for stat in result.dimension_stats:
            if not stat["success"]:
                continue
            dim_issues = issues_by_dim.get(stat["name"], [])
            if not dim_issues:
                continue

            dim_score_icon = score_icons.get(stat["score"], "⚪")
            lines.append("---")
            lines.append(f"### {stat['icon']} {stat['display_name']} (评级: {dim_score_icon} {stat['score']})")
            lines.append("")

            for issue in dim_issues:
                issue_counter += 1
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

                lines.append(f"#### {icon} {issue_counter}. [{severity}] {category} — `{location}`")
                lines.append(f"{content}")
                if suggestion:
                    lines.append(f"\n> **建议**: {suggestion}")
                lines.append("")

        # Highlights
        if result.highlights:
            lines.append("### 亮点")
            lines.append("")
            for h in result.highlights:
                lines.append(f"- ✨ {h}")
            lines.append("")

        # Summary table in collapsible details
        lines.append("<details><summary>审查详情</summary>")
        lines.append("")
        lines.append("| 维度 | 评级 | 问题数 | 工具调用 | 耗时 |")
        lines.append("|------|------|--------|----------|------|")
        for stat in result.dimension_stats:
            if stat["success"]:
                dim_score_icon = score_icons.get(stat["score"], "⚪")
                lines.append(
                    f"| {stat['icon']} {stat['display_name']} "
                    f"| {dim_score_icon} {stat['score']} "
                    f"| {stat['issues_count']} "
                    f"| {stat['tool_calls']} "
                    f"| {stat['duration']:.1f}s |"
                )
            else:
                lines.append(
                    f"| {stat['icon']} {stat['display_name']} "
                    f"| ❌ 失败 "
                    f"| - "
                    f"| {stat['tool_calls']} "
                    f"| {stat['duration']:.1f}s |"
                )
        lines.append("")
        lines.append("</details>")
        lines.append("")

        lines.append("---")
        lines.append("*由 AI 代码审查服务自动生成（多维度并行审查）*")

        return "\n".join(lines)
