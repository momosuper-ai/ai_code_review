"""AI_README.md auto-generation engine."""

import asyncio
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date

from src.core.agent_tools import AgentToolExecutor
from src.core.llm_client import LLMClient, TokenUsage
from src.config import settings
from src.utils.logger import logger

# 代码文件扩展名（用于行数统计）
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".c", ".cpp", ".h", ".cs", ".rb", ".php", ".swift", ".kt",
    ".scala", ".vue", ".svelte",
}

# 结构指纹关注的配置文件
_KEY_CONFIG_FILES = {
    "package.json", "pyproject.toml", "setup.py", "Cargo.toml",
    "go.mod", "pom.xml", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", "Makefile", "requirements.txt",
}

# 子目录排除列表
_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", ".idea", ".vscode", ".next", ".nuxt", "target",
    "bin", "obj", ".cache", "coverage", ".eggs",
}

# 只读工具名称（排除 get_blast_radius）
_READONLY_TOOLS = {"read_file", "search_code", "list_directory", "find_definition", "get_file_info"}

# 检测 LLM 输出中残留的工具调用文本（Devstral 等模型在 tools=None 时可能以文本形式输出）
# 仅清除紧随 [TOOL_CALLS] 的 JSON 数组/对象结构（允许工具名前缀），
# 非贪婪匹配避免吞噬后续 Markdown 正文
_TOOL_CALL_PATTERN = re.compile(
    r"\[TOOL_CALLS\][^\[\{\n]*(?:\[\s*\{.*?\}\s*\]|\{.*?\})",
    re.DOTALL,
)


@dataclass
class FileAction:
    """表示一个需要提交的文件操作。"""
    file_path: str
    content: str
    action: str  # "create" | "update"
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass
class ReadmeResult:
    """文档生成的整体结果。"""
    actions: list[FileAction] = field(default_factory=list)
    skipped_dirs: list[str] = field(default_factory=list)
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0


class ReadmeGenerator:
    """AI_README.md 文档自动生成引擎。"""

    def __init__(self, repo_path: str):
        self.repo_path = os.path.realpath(repo_path)
        self.llm = LLMClient()  # 仅使用本地模型
        self.tools = AgentToolExecutor(repo_path)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    async def run(self) -> ReadmeResult:
        """主入口：先子目录后根目录，根目录可引用子目录文档摘要。"""
        result = ReadmeResult()

        # 1. 先生成子目录文档
        t0 = time.time()
        subdir_actions, skipped = await self.generate_subdir_readmes()
        result.actions.extend(subdir_actions)
        result.skipped_dirs = skipped
        logger.info(
            "子目录文档生成完成: 生成=%d 跳过=%d (%.1f秒)",
            len(subdir_actions), len(skipped), time.time() - t0,
        )

        # 2. 再生成根目录文档（可引用子目录文档摘要）
        t0 = time.time()
        root_action = await self.generate_root_readme(subdir_actions)
        if root_action:
            result.actions.insert(0, root_action)  # 根目录放在列表首位
        logger.info("根目录文档生成完成 (%.1f秒)", time.time() - t0)

        result.total_prompt_tokens = self.total_prompt_tokens
        result.total_completion_tokens = self.total_completion_tokens
        return result

    @staticmethod
    def _validate_content(content: str) -> str:
        """校验并清理 LLM 输出中的工具调用文本污染。"""
        cleaned = _TOOL_CALL_PATTERN.sub("", content).strip()
        if cleaned != content.strip():
            logger.warning(
                "README Agent: 检测到输出中包含工具调用文本，已清理 (原长=%d 清理后=%d)",
                len(content), len(cleaned),
            )
        return cleaned

    @staticmethod
    def _build_subdir_summary(subdir_actions: list[FileAction] | None) -> str:
        """从内存中的子目录 FileAction 构建摘要文本。"""
        if not subdir_actions:
            return ""
        parts = []
        for act in subdir_actions:
            lines = act.content.split("\n")[:30]
            summary = "\n".join(lines)
            if len(act.content.split("\n")) > 30:
                summary += "\n...(更多内容请用工具查看)"
            parts.append(f"### {act.file_path}\n{summary}")
        return "\n\n".join(parts)

    def _get_readonly_tool_definitions(self) -> list[dict]:
        """获取只读工具定义（排除 get_blast_radius）。"""
        return [t for t in AgentToolExecutor.tool_definitions() if t["function"]["name"] in _READONLY_TOOLS]

    async def _run_agent_loop(
        self, system_prompt: str, user_prompt: str, max_rounds: int,
    ) -> tuple[str, TokenUsage]:
        """Agent 工具调用循环（首次生成模式）。"""
        tool_defs = self._get_readonly_tool_definitions()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        usage = TokenUsage()

        for round_num in range(1, max_rounds + 1):
            chat_result = await self.llm.chat(
                messages, tools=tool_defs,
                max_tokens=settings.README_LLM_MAX_TOKENS,
            )
            response = chat_result.message
            usage += chat_result.usage

            if not response.tool_calls:
                content = self._validate_content(response.content or "")
                logger.info(
                    "README Agent: round=%d/%d LLM 输出最终文档 (长度=%d)",
                    round_num, max_rounds, len(content),
                )
                return content, usage

            # 记录本轮工具调用概览
            tool_names = [tc.function.name for tc in response.tool_calls]
            logger.info(
                "README Agent: round=%d/%d 工具调用=%s",
                round_num, max_rounds, tool_names,
            )

            messages.append(response)
            for tc in response.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_result = self.tools.execute_tool(tc.function.name, args)
                logger.info(
                    "README Agent:   tool=%s args=%s success=%s output_len=%d",
                    tc.function.name, args, tool_result.success, len(tool_result.output),
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result.output,
                })

        # 达到 max_rounds → 追加明确的终止指令 + 禁用 tools，强制输出文档
        logger.warning(
            "README Agent: 达到最大轮次 %d，发送终止指令强制输出文档", max_rounds,
        )
        messages.append({
            "role": "user",
            "content": (
                "【系统通知】已达到最大探索轮次，工具已全部禁用。\n"
                "请立即基于上方对话历史中已收集到的全部工具结果，"
                "整理并输出完整的 AI_README.md 文档。\n\n"
                "严格要求：\n"
                "1. 直接以 Markdown 正文开始输出，第一行是 "
                "`> 本文档由 AI 自动生成，基于代码库静态分析，仅供参考。最后更新时间：...`；\n"
                "2. 禁止再次调用任何工具；\n"
                "3. 禁止在输出中包含 `[TOOL_CALLS]`、```json 代码块、"
                "`<tool_call>` 等任何工具调用语法；\n"
                "4. 如果某些章节信息不足，基于已收集信息尽力撰写即可，"
                "不要留空、不要写 TODO 占位。"
            ),
        })
        chat_result = await self.llm.chat(
            messages, tools=None, response_format=None,
            max_tokens=settings.README_LLM_MAX_TOKENS,
        )
        usage += chat_result.usage
        content = self._validate_content(chat_result.message.content or "")

        # 兜底：极端情况下输出仍然为空/过短，追加一次更强硬的指令
        if len(content.strip()) < 100:
            logger.warning(
                "README Agent: 首次终止指令后输出仍过短 (%d 字符)，追加兜底指令",
                len(content.strip()),
            )
            messages.append(chat_result.message)
            messages.append({
                "role": "user",
                "content": (
                    "你上一次的输出不符合要求（为空或过短）。"
                    "请重新输出完整的 AI_README.md 文档，"
                    "从 `> 本文档由 AI 自动生成` 开始，使用纯 Markdown，"
                    "绝对不要输出 [TOOL_CALLS] 或任何工具调用语法。"
                ),
            })
            chat_result = await self.llm.chat(
                messages, tools=None, response_format=None,
                max_tokens=settings.README_LLM_MAX_TOKENS,
            )
            usage += chat_result.usage
            content = self._validate_content(chat_result.message.content or "")

        return content, usage

    async def _run_single_call(
        self, system_prompt: str, user_prompt: str,
    ) -> tuple[str, TokenUsage]:
        """单次 LLM 调用（更新模式）。"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        chat_result = await self.llm.chat(
            messages, tools=None, response_format=None,
            max_tokens=settings.README_LLM_MAX_TOKENS,
        )
        content = self._validate_content(chat_result.message.content or "")
        return content, chat_result.usage

    async def generate_root_readme(self, subdir_actions: list[FileAction] | None = None) -> FileAction | None:
        """生成或更新根目录 AI_README.md。"""
        readme_path = os.path.join(self.repo_path, "AI_README.md")
        existing_content = self._read_file_safe(readme_path)
        fingerprint = self._compute_root_fingerprint()
        today = date.today().isoformat()

        if existing_content is None:
            # 首次生成：Agent 工具调用循环，注入子目录文档摘要
            logger.info("根目录 AI_README.md 不存在，启动 Agent 循环生成")
            system_prompt = _ROOT_SYSTEM_PROMPT_NEW.format(date=today)
            subdir_context = self._build_subdir_summary(subdir_actions)
            user_prompt = (
                "请为这个代码项目生成 AI_README.md 文档。项目根目录是当前仓库根路径。\n"
                "请先用工具探索项目，收集足够信息后再输出文档。\n"
            )
            if subdir_context:
                user_prompt += (
                    "\n以下是各子目录模块的文档摘要（已自动生成），可作为参考，"
                    "你仍然可以用工具查看更多细节：\n\n" + subdir_context
                )
            content, usage = await self._run_agent_loop(
                system_prompt, user_prompt, settings.README_AGENT_MAX_ROUNDS,
            )
            self.total_prompt_tokens += usage.prompt_tokens
            self.total_completion_tokens += usage.completion_tokens
            content = _strip_markdown_wrapper(content)
            if len(content) < 100:
                logger.warning("根目录文档内容过短 (%d 字符)，跳过生成", len(content))
                return None
            return FileAction(
                file_path="AI_README.md", content=content,
                action="create",
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )
        else:
            # 已存在：检查指纹是否变化
            last_fp = self._get_last_fingerprint_from_db()
            if last_fp == fingerprint:
                logger.info("根目录结构指纹未变化，跳过更新")
                return None

            # 增量更新：预收集信息 + 单次 LLM 调用
            logger.info("根目录结构指纹变化，启动增量更新")
            system_prompt = _ROOT_SYSTEM_PROMPT_UPDATE.format(date=today)
            collected_info = self._collect_root_info(subdir_actions)
            user_prompt = _UPDATE_USER_PROMPT.format(
                existing_readme=existing_content,
                directory_tree=collected_info["directory_tree"],
                collected_files=collected_info["key_files_content"],
                subdir_docs=collected_info.get("subdir_docs", ""),
            )
            content, usage = await self._run_single_call(system_prompt, user_prompt)
            self.total_prompt_tokens += usage.prompt_tokens
            self.total_completion_tokens += usage.completion_tokens

            if "NO_UPDATE_NEEDED" in content.strip():
                logger.info("LLM 判断根目录文档无需更新")
                return None

            content = _strip_markdown_wrapper(content)
            if len(content) < 100:
                logger.warning("根目录更新文档内容过短 (%d 字符)，跳过更新", len(content))
                return None
            return FileAction(
                file_path="AI_README.md", content=content,
                action="update",
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )

    async def generate_subdir_readme(self, subdir: str) -> FileAction | None:
        """生成或更新单个子目录 AI_README.md。"""
        subdir_full = os.path.join(self.repo_path, subdir)
        readme_path = os.path.join(subdir_full, "AI_README.md")
        existing_content = self._read_file_safe(readme_path)
        today = date.today().isoformat()
        code_lines = self._count_code_lines(subdir_full)
        readme_file_path = f"{subdir}/AI_README.md"

        if existing_content is None:
            logger.info("子目录 %s AI_README.md 不存在，启动 Agent 循环生成", subdir)
            system_prompt = _SUBDIR_SYSTEM_PROMPT_NEW.format(
                subdir_path=subdir, date=today,
            )
            user_prompt = (
                f"请为 `{subdir}/` 目录生成 AI_README.md 文档。\n"
                f"该目录位于项目根目录下，包含约 {code_lines} 行代码。\n"
                f"请先用工具探索该目录，收集足够信息后再输出文档。"
            )
            content, usage = await self._run_agent_loop(
                system_prompt, user_prompt, settings.README_AGENT_MAX_ROUNDS,
            )
            self.total_prompt_tokens += usage.prompt_tokens
            self.total_completion_tokens += usage.completion_tokens
            content = _strip_markdown_wrapper(content)
            if len(content) < 100:
                logger.warning("子目录 %s 文档内容过短 (%d 字符)，跳过生成", subdir, len(content))
                return None
            return FileAction(
                file_path=readme_file_path, content=content,
                action="create",
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )
        else:
            fingerprint = self._compute_subdir_fingerprint(subdir_full)
            last_fp = self._get_last_subdir_fingerprint(subdir)
            if last_fp == fingerprint:
                logger.info("子目录 %s 结构指纹未变化，跳过更新", subdir)
                return None

            logger.info("子目录 %s 结构指纹变化，启动增量更新", subdir)
            system_prompt = _SUBDIR_SYSTEM_PROMPT_UPDATE.format(
                subdir_path=subdir, date=today,
            )
            collected_info = self._collect_subdir_info(subdir)
            user_prompt = _UPDATE_USER_PROMPT.format(
                existing_readme=existing_content,
                directory_tree=collected_info["directory_tree"],
                collected_files=collected_info["key_files_content"],
                subdir_docs="",
            )
            content, usage = await self._run_single_call(system_prompt, user_prompt)
            self.total_prompt_tokens += usage.prompt_tokens
            self.total_completion_tokens += usage.completion_tokens

            if "NO_UPDATE_NEEDED" in content.strip():
                logger.info("LLM 判断子目录 %s 文档无需更新", subdir)
                return None

            content = _strip_markdown_wrapper(content)
            if len(content) < 100:
                logger.warning("子目录 %s 更新文档内容过短 (%d 字符)，跳过更新", subdir, len(content))
                return None
            return FileAction(
                file_path=readme_file_path, content=content,
                action="update",
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
            )

    async def generate_subdir_readmes(self) -> tuple[list[FileAction], list[str]]:
        """扫描子目录，对满足条件的并发生成 AI_README.md。"""
        actions: list[FileAction] = []
        skipped: list[str] = []

        subdirs = self._list_eligible_subdirs()
        if not subdirs:
            return actions, skipped

        sem = asyncio.Semaphore(settings.README_CONCURRENCY)

        async def _gen(subdir: str) -> FileAction | None:
            async with sem:
                try:
                    return await self.generate_subdir_readme(subdir)
                except Exception:
                    logger.error("子目录 %s 文档生成失败", subdir, exc_info=True)
                    return None

        tasks = [_gen(sd) for sd in subdirs]
        results = await asyncio.gather(*tasks)
        for sd, result in zip(subdirs, results):
            if result:
                actions.append(result)
            else:
                skipped.append(sd)

        return actions, skipped

    def _list_eligible_subdirs(self) -> list[str]:
        """列出根目录下满足条件的直接子目录。"""
        subdirs = []
        try:
            entries = sorted(os.listdir(self.repo_path))
        except OSError:
            return subdirs

        for name in entries:
            if name.startswith(".") or name in _EXCLUDE_DIRS:
                continue
            full = os.path.join(self.repo_path, name)
            if not os.path.isdir(full):
                continue
            code_lines = self._count_code_lines(full)
            if code_lines >= settings.README_MIN_CODE_LINES:
                subdirs.append(name)
            if len(subdirs) >= settings.README_MAX_SUBDIRS:
                break
        return subdirs

    def _count_code_lines(self, dir_path: str) -> int:
        """统计目录下代码文件的总行数。"""
        total = 0
        for root, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in _CODE_EXTENSIONS:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        total += sum(1 for _ in f)
                except OSError:
                    pass
        return total

    def _compute_root_fingerprint(self) -> str:
        """计算根目录结构指纹。"""
        h = hashlib.sha256()

        # 根目录文件名和子目录名
        try:
            entries = sorted(os.listdir(self.repo_path))
        except OSError:
            entries = []
        filtered = [e for e in entries if not e.startswith(".") and e not in _EXCLUDE_DIRS]
        h.update("|".join(filtered).encode())

        # 关键配置文件内容
        for cfg_name in sorted(_KEY_CONFIG_FILES):
            cfg_path = os.path.join(self.repo_path, cfg_name)
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8", errors="replace") as f:
                        h.update(f.read().encode())
                except OSError:
                    pass

        return h.hexdigest()

    def _compute_subdir_fingerprint(self, subdir_path: str) -> str:
        """计算子目录结构指纹。"""
        h = hashlib.sha256()
        file_entries = []
        for root, dirs, files in os.walk(subdir_path):
            dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                if ext in _CODE_EXTENSIONS:
                    rel = os.path.relpath(os.path.join(root, fname), subdir_path)
                    file_entries.append(rel)
        h.update("|".join(sorted(file_entries)).encode())

        # 各文件内容 hash
        for rel in sorted(file_entries):
            fpath = os.path.join(subdir_path, rel)
            try:
                with open(fpath, "rb") as f:
                    h.update(hashlib.md5(f.read()).digest())
            except OSError:
                pass
        return h.hexdigest()

    def _collect_root_info(self, subdir_actions: list[FileAction] | None = None) -> dict:
        """预收集根目录信息（用于更新模式）。"""
        # 目录树
        tree_result = self.tools.list_directory(".", recursive=True, max_depth=2)
        directory_tree = tree_result.output if tree_result.success else ""

        # 关键配置文件内容
        parts = []
        for cfg_name in sorted(_KEY_CONFIG_FILES):
            cfg_path = os.path.join(self.repo_path, cfg_name)
            if os.path.isfile(cfg_path):
                result = self.tools.read_file(cfg_name, end_line=80)
                if result.success:
                    parts.append(f"### {cfg_name}\n```\n{result.output}\n```")

        # 入口文件前 80 行
        for entry_name in ("main.py", "app.py", "index.ts", "index.js", "src/main.py", "src/index.ts"):
            entry_path = os.path.join(self.repo_path, entry_name)
            if os.path.isfile(entry_path):
                result = self.tools.read_file(entry_name, end_line=80)
                if result.success:
                    parts.append(f"### {entry_name} (入口文件)\n```\n{result.output}\n```")
                break

        # 子目录文档摘要（显式说明增量语义，防止 LLM 误判为"缺失即删除"）
        subdir_summary = self._build_subdir_summary(subdir_actions)
        if subdir_summary:
            subdir_docs = (
                "> **增量语义提示**：以下仅列出本次结构发生变化、已重新生成文档的子目录。"
                "未在此列出的子目录表示本次无变化，请在更新文档时**原样保留**现有 "
                "AI_README.md 中对这些子目录的描述，不得删改或编造。\n\n"
                + subdir_summary
            )
        else:
            subdir_docs = (
                "> **本次无子目录变化**：所有已记录的子目录结构指纹未变，"
                "现有 AI_README.md 中对所有子目录的描述均为准确状态，"
                "请在更新文档时**完整保留**子目录相关的所有段落。"
            )

        return {
            "directory_tree": directory_tree,
            "key_files_content": "\n\n".join(parts),
            "subdir_docs": subdir_docs,
        }

    def _collect_subdir_info(self, subdir: str) -> dict:
        """预收集子目录信息（用于更新模式）。"""
        tree_result = self.tools.list_directory(subdir, recursive=True, max_depth=2)
        directory_tree = tree_result.output if tree_result.success else ""

        parts = []
        subdir_full = os.path.join(self.repo_path, subdir)
        try:
            files = sorted(os.listdir(subdir_full))
        except OSError:
            files = []

        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _CODE_EXTENSIONS:
                continue
            rel_path = f"{subdir}/{fname}"
            # 入口文件读取完整内容，其他前 80 行
            is_entry = fname in ("__init__.py", "index.ts", "index.js", "mod.rs", "lib.rs")
            result = self.tools.read_file(rel_path, end_line=None if is_entry else 80)
            if result.success:
                label = " (入口文件)" if is_entry else ""
                parts.append(f"### {fname}{label}\n```\n{result.output}\n```")

        return {
            "directory_tree": directory_tree,
            "key_files_content": "\n\n".join(parts),
        }

    def _get_last_fingerprint_from_db(self) -> str | None:
        """从数据库查询上次根目录指纹（无记录返回 None 触发更新）。"""
        # 延迟导入，避免循环依赖
        try:
            from src.models import SyncSession
            from src.models.readme_record import ReadmeRecord
            with SyncSession() as session:
                record = (
                    session.query(ReadmeRecord)
                    .filter(ReadmeRecord.root_generated.is_(True))
                    .order_by(ReadmeRecord.created_at.desc())
                    .first()
                )
                return record.structure_fingerprint if record else None
        except Exception:
            logger.warning("查询根目录指纹失败，视为需要更新", exc_info=True)
            return None

    def _get_last_subdir_fingerprint(self, subdir: str) -> str | None:
        """从数据库查询子目录的上次指纹。

        使用 ReadmeRecord 中最新记录的 structure_fingerprint 作为近似判断。
        子目录级别的精确追踪留给后续迭代。
        """
        # 子目录暂不做精确 DB 追踪，首次总是生成，后续通过根目录指纹间接控制
        return None

    @staticmethod
    def _read_file_safe(path: str) -> str | None:
        """安全读取文件，不存在返回 None。"""
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return None

    async def aclose(self):
        """关闭 LLM 客户端。"""
        await self.llm.aclose()


def _strip_markdown_wrapper(text: str) -> str:
    """去除 LLM 输出中可能的 markdown 代码块包裹。"""
    stripped = text.strip()
    if stripped.startswith("```markdown"):
        stripped = stripped[len("```markdown"):].strip()
    elif stripped.startswith("```md"):
        stripped = stripped[len("```md"):].strip()
    elif stripped.startswith("```"):
        stripped = stripped[3:].strip()
    if stripped.endswith("```"):
        stripped = stripped[:-3].strip()
    return stripped


# ── Prompt 模板 ───────────────────────────────────────────────────────────

_ROOT_SYSTEM_PROMPT_NEW = """\
你是一位专业的技术文档工程师。你的任务是通过探索代码仓库，为项目生成一份完整的 AI_README.md 文档。

## 工作方式
你可以使用以下工具来探索项目代码库：
- `list_directory` — 浏览目录结构，了解项目组织方式
- `read_file` — 读取文件内容，理解代码实现
- `search_code` — 搜索代码模式（如 API 端点、路由定义、类定义等）
- `find_definition` — 查找函数或类的定义
- `get_file_info` — 获取文件大小、行数等元数据

建议探索步骤：
1. 先用 list_directory 查看项目整体结构
2. 读取关键配置文件（如 package.json、pyproject.toml、Dockerfile 等）了解技术栈
3. 读取入口文件（如 main.py、index.ts 等）了解启动流程
4. 用 search_code 搜索 API 端点、路由注册等关键模式
5. 用 find_definition 查找核心类和函数
6. 如果存在 README.md，读取它作为参考

当你认为已收集足够信息时，直接输出完整的 AI_README.md 文档内容（不要用工具了）。

## 文档结构要求
1. **项目概述** — 项目的功能、目的和业务价值
2. **技术栈** — 使用的主要语言、框架、中间件
3. **项目结构** — 目录组织和各模块职责说明
4. **快速开始** — 环境准备、构建、部署和运行方法
5. **架构说明** — 核心架构设计、数据流、关键组件交互
6. **API/服务说明** — 如有对外接口，列出端点和功能
7. **配置说明** — 关键配置项和环境变量
8. **开发指南** — 开发、测试、调试方法

## 输出规范
- 使用中文书写
- 输出纯 Markdown 格式，不要用代码块包裹整个输出
- 内容务必准确，不确定的信息不要编造
- 文档开头添加：`> 本文档由 AI 自动生成，基于代码库静态分析，仅供参考。最后更新时间：{date}`
- 在输出最终文档时，必须是纯 Markdown 正文，绝对不要输出 `[TOOL_CALLS]`、`<tool_call>` 或任何形如工具调用的语法
"""

_ROOT_SYSTEM_PROMPT_UPDATE = """\
你是一位专业的技术文档工程师。你的任务是根据提供的项目信息，判断现有的 AI_README.md 是否需要更新。

## 判断规则
- 如果项目结构、技术栈、核心模块、配置项等发生了实质性变化，需要更新
- 如果仅是细微的代码修改不影响文档描述的准确性，则不需要更新

## 子目录摘要的增量语义（重要）
- `## 子目录模块文档摘要` 章节采用**增量传递**：仅列出本次结构发生变化、已重新生成文档的子目录
- **未出现在该章节中的子目录，表示本次无任何变化**，其文档和结构保持稳定
- 对于未出现的子目录：必须**完全保留**现有 AI_README.md 中对它们的描述文字，不得删除、改写或编造
- 如果该章节显示"本次无子目录变化"，说明所有子模块均稳定，现有文档中关于子目录的所有段落都应原样保留
- 仅允许对摘要中**实际出现**的子目录条目进行描述更新，且更新内容应与摘要一致

## 输出规则
- 如果需要更新：输出完整的更新后文档（Markdown 格式）
- 如果不需要更新：仅输出 `NO_UPDATE_NEEDED`（不要输出其他内容）

## 文档结构要求
1. **项目概述** — 项目的功能、目的和业务价值
2. **技术栈** — 使用的主要语言、框架、中间件
3. **项目结构** — 目录组织和各模块职责说明
4. **快速开始** — 环境准备、构建、部署和运行方法
5. **架构说明** — 核心架构设计、数据流、关键组件交互
6. **API/服务说明** — 如有对外接口，列出端点和功能
7. **配置说明** — 关键配置项和环境变量
8. **开发指南** — 开发、测试、调试方法

## 输出规范
- 使用中文书写
- 输出纯 Markdown 格式，不要用代码块包裹整个输出
- 保留现有文档中仍然准确的内容，仅修改过时或缺失的部分
- **对于"子目录模块文档摘要"未涵盖的子目录，完全保留现有文档中的相关描述**
- 文档开头的更新时间改为：{date}
"""

_SUBDIR_SYSTEM_PROMPT_NEW = """\
你是一位专业的技术文档工程师。你的任务是通过探索代码目录，为该模块生成一份详细的 AI_README.md 文档。

## 工作方式
你可以使用以下工具来探索目录代码：
- `list_directory` — 浏览目录下的文件
- `read_file` — 读取文件内容，理解函数实现
- `search_code` — 搜索函数调用、类使用等模式
- `find_definition` — 查找函数或类的完整定义
- `get_file_info` — 获取文件元数据

你的探索范围主要在 `{subdir_path}/` 目录下，但可以搜索其他目录来了解依赖关系。

建议探索步骤：
1. 先用 list_directory 查看目录下的文件列表
2. 读取入口文件（如 __init__.py、index.ts 等）了解模块对外接口
3. 逐个读取主要代码文件，理解每个函数/类的作用
4. 用 search_code 搜索该模块被其他模块引用的情况

当你认为已收集足够信息时，直接输出完整的 AI_README.md 文档内容。

## 文档结构要求
1. **模块概述** — 该目录/模块的职责和功能定位
2. **文件清单** — 列出每个文件及其主要作用
3. **核心类与函数** — 详细说明重要的类、函数：
   - 功能描述
   - 参数说明（含类型）
   - 返回值说明
   - 使用示例（如适用）
4. **接口说明** — 对外暴露的 API、工具函数或组件，说明调用方式
5. **内部流程** — 模块内部的关键处理流程
6. **依赖关系** — 该模块依赖和被依赖的其他模块

## 输出规范
- 使用中文书写
- 输出纯 Markdown 格式，不要用代码块包裹整个输出
- 侧重每个函数的具体作用、参数含义、接口用途和使用方法
- 内容务必准确，不确定的信息不要编造
- 文档开头添加：`> 本文档由 AI 自动生成，基于代码库静态分析，仅供参考。最后更新时间：{date}`
- 在输出最终文档时，必须是纯 Markdown 正文，绝对不要输出 `[TOOL_CALLS]`、`<tool_call>` 或任何形如工具调用的语法
"""

_SUBDIR_SYSTEM_PROMPT_UPDATE = """\
你是一位专业的技术文档工程师。你的任务是根据提供的模块信息，判断现有的 AI_README.md 是否需要更新。

## 模块路径
`{subdir_path}/`

## 判断规则
- 如果模块的文件结构、核心类/函数、接口等发生了实质性变化，需要更新
- 如果仅是细微的代码修改不影响文档描述的准确性，则不需要更新

## 输出规则
- 如果需要更新：输出完整的更新后文档（Markdown 格式）
- 如果不需要更新：仅输出 `NO_UPDATE_NEEDED`（不要输出其他内容）

## 文档结构要求
1. **模块概述** — 该目录/模块的职责和功能定位
2. **文件清单** — 列出每个文件及其主要作用
3. **核心类与函数** — 详细说明重要的类、函数
4. **接口说明** — 对外暴露的 API、工具函数或组件
5. **内部流程** — 模块内部的关键处理流程
6. **依赖关系** — 该模块依赖和被依赖的其他模块

## 输出规范
- 使用中文书写
- 输出纯 Markdown 格式，不要用代码块包裹整个输出
- 保留现有文档中仍然准确的内容，仅修改过时或缺失的部分
- 文档开头的更新时间改为：{date}
"""

_UPDATE_USER_PROMPT = """\
以下是项目当前的信息，请判断现有的 AI_README.md 是否需要更新。

## 现有 AI_README.md
{existing_readme}

## 当前项目结构
{directory_tree}

## 关键文件内容
{collected_files}

## 子目录模块文档摘要
{subdir_docs}
"""
