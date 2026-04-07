"""Agent tools for code review — sandboxed to the reviewed repo only."""

import os
import re
import subprocess
from dataclasses import dataclass, field

from src.utils.logger import logger

# File extensions considered as code
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".kt", ".go", ".rs",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala",
    ".sh", ".bash", ".zsh", ".sql", ".html", ".css", ".scss", ".less",
    ".vue", ".svelte", ".yaml", ".yml", ".toml", ".json", ".xml",
    ".md", ".rst", ".txt", ".cfg", ".ini", ".conf", ".env",
    ".dockerfile", ".tf", ".proto", ".graphql", ".r", ".lua",
}

_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", ".idea", ".vscode",
}

# Language-specific definition patterns
_DEF_PATTERNS: list[tuple[str, str]] = [
    # Python
    (r"^\s*(class|def|async\s+def)\s+{symbol}", "py"),
    # JS/TS
    (r"^\s*(export\s+)?(function|class|const|let|var|interface|type|enum)\s+{symbol}", "js,ts,jsx,tsx"),
    # Java/Kotlin/C#
    (r"^\s*(public|private|protected|internal|abstract|static|final|override|suspend|fun|data\s+class|class|interface|enum)\s+.*\b{symbol}\b", "java,kt,cs"),
    # Go
    (r"^\s*(func|type|var|const)\s+(\(.*\)\s+)?{symbol}", "go"),
    # Rust
    (r"^\s*(pub\s+)?(fn|struct|enum|trait|type|const|static|impl)\s+{symbol}", "rs"),
    # C/C++
    (r"^\s*(class|struct|enum|typedef|namespace|#define)\s+{symbol}", "c,cpp,h,hpp"),
    # Ruby
    (r"^\s*(class|module|def)\s+{symbol}", "rb"),
    # PHP
    (r"^\s*(class|function|interface|trait|enum)\s+{symbol}", "php"),
]


@dataclass
class ToolResult:
    """Result from an agent tool execution."""
    success: bool
    output: str
    truncated: bool = False


class AgentToolExecutor:
    """Sandboxed tool executor scoped to a single repo directory."""

    def __init__(self, repo_path: str):
        self.repo_path = os.path.realpath(repo_path)
        if not os.path.isdir(self.repo_path):
            raise ValueError(f"Repo path does not exist: {self.repo_path}")

    def _safe_resolve(self, relative_path: str) -> str:
        """Resolve path and verify it stays within repo_path."""
        # Normalize and join
        cleaned = relative_path.lstrip("/").lstrip("\\")
        full = os.path.realpath(os.path.join(self.repo_path, cleaned))
        if not full.startswith(self.repo_path):
            raise ValueError(f"Path traversal detected: {relative_path}")
        return full

    @staticmethod
    def _truncate(text: str, max_chars: int = 20000, max_lines: int = 500) -> tuple[str, bool]:
        """Truncate output to fit within limits."""
        lines = text.split("\n")
        truncated = False
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            truncated = True
        result = "\n".join(lines)
        if len(result) > max_chars:
            result = result[:max_chars]
            truncated = True
        return result, truncated

    def _is_excluded_dir(self, name: str) -> bool:
        return name in _EXCLUDE_DIRS

    # ── Tool: read_file ──────────────────────────────────────────────

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        """Read a file's content, optionally a line range."""
        try:
            full_path = self._safe_resolve(path)
            if not os.path.isfile(full_path):
                return ToolResult(success=False, output=f"File not found: {path}")

            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()

            if start_line is not None or end_line is not None:
                s = max((start_line or 1) - 1, 0)
                e = end_line or len(lines)
                selected = lines[s:e]
            else:
                selected = lines

            numbered = [
                f"{i + (start_line or 1):4d} | {line.rstrip()}"
                for i, line in enumerate(selected)
            ]
            content = "\n".join(numbered)
            content, truncated = self._truncate(content)
            return ToolResult(success=True, output=content, truncated=truncated)
        except ValueError as e:
            return ToolResult(success=False, output=str(e))
        except Exception as e:
            return ToolResult(success=False, output=f"Error reading file: {e}")

    # ── Tool: search_code ────────────────────────────────────────────

    def search_code(
        self,
        pattern: str,
        file_pattern: str | None = None,
        max_results: int = 30,
    ) -> ToolResult:
        """Search for a pattern in the codebase using grep."""
        try:
            cmd = [
                "grep", "-rn",
                "-m", str(min(max_results, 30)),
            ]

            if file_pattern:
                cmd.append(f"--include={file_pattern}")

            # Exclude dirs
            for d in _EXCLUDE_DIRS:
                cmd.append(f"--exclude-dir={d}")

            cmd.extend([pattern, self.repo_path])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30
            )

            if result.returncode not in (0, 1):
                return ToolResult(success=False, output=f"grep error: {result.stderr.strip()}")

            output = result.stdout.strip()
            if not output:
                return ToolResult(success=True, output="No matches found.")

            # Make paths relative to repo
            lines = []
            for line in output.split("\n")[:max_results]:
                line = line.replace(self.repo_path + os.sep, "")
                line = line.replace(self.repo_path + "/", "")
                lines.append(line)

            content = "\n".join(lines)
            content, truncated = self._truncate(content, max_chars=10000, max_lines=max_results)
            return ToolResult(success=True, output=content, truncated=truncated)
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, output="Search timed out.")
        except Exception as e:
            return ToolResult(success=False, output=f"Search error: {e}")

    # ── Tool: list_directory ─────────────────────────────────────────

    def list_directory(
        self,
        path: str = ".",
        recursive: bool = False,
        max_depth: int = 3,
    ) -> ToolResult:
        """List directory contents."""
        try:
            full_path = self._safe_resolve(path)
            if not os.path.isdir(full_path):
                return ToolResult(success=False, output=f"Not a directory: {path}")

            entries: list[str] = []
            limit = 200

            if recursive:
                self._walk_dir(full_path, "", 0, max_depth, entries, limit)
            else:
                for name in sorted(os.listdir(full_path)):
                    if self._is_excluded_dir(name):
                        continue
                    entry_path = os.path.join(full_path, name)
                    suffix = "/" if os.path.isdir(entry_path) else ""
                    entries.append(f"{name}{suffix}")
                    if len(entries) >= limit:
                        break

            truncated = len(entries) >= limit
            content = "\n".join(entries)
            return ToolResult(success=True, output=content, truncated=truncated)
        except ValueError as e:
            return ToolResult(success=False, output=str(e))
        except Exception as e:
            return ToolResult(success=False, output=f"Error listing directory: {e}")

    def _walk_dir(
        self,
        base: str,
        prefix: str,
        depth: int,
        max_depth: int,
        entries: list[str],
        limit: int,
    ):
        if depth > max_depth or len(entries) >= limit:
            return
        try:
            items = sorted(os.listdir(base))
        except PermissionError:
            return
        for name in items:
            if self._is_excluded_dir(name):
                continue
            if len(entries) >= limit:
                return
            rel = os.path.join(prefix, name) if prefix else name
            full = os.path.join(base, name)
            if os.path.isdir(full):
                entries.append(f"{rel}/")
                self._walk_dir(full, rel, depth + 1, max_depth, entries, limit)
            else:
                entries.append(rel)

    # ── Tool: find_definition ────────────────────────────────────────

    def find_definition(
        self,
        symbol: str,
        file_pattern: str | None = None,
    ) -> ToolResult:
        """Find symbol definitions using language-aware regex patterns."""
        try:
            safe_symbol = re.escape(symbol)
            results: list[str] = []

            for root, dirs, files in os.walk(self.repo_path):
                dirs[:] = [d for d in dirs if not self._is_excluded_dir(d)]
                for fname in files:
                    if file_pattern and not self._match_glob(fname, file_pattern):
                        continue
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in _CODE_EXTENSIONS:
                        continue

                    fpath = os.path.join(root, fname)
                    rel_path = os.path.relpath(fpath, self.repo_path)

                    try:
                        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                            lines = f.readlines()
                    except Exception:
                        continue

                    for pat_template, lang_exts in _DEF_PATTERNS:
                        exts = {f".{e}" for e in lang_exts.split(",")}
                        if ext not in exts:
                            continue
                        pat = pat_template.format(symbol=safe_symbol)
                        for i, line in enumerate(lines):
                            if re.search(pat, line):
                                # Show context: 3 lines before/after
                                start = max(0, i - 3)
                                end = min(len(lines), i + 4)
                                context_lines = []
                                for j in range(start, end):
                                    marker = ">>>" if j == i else "   "
                                    context_lines.append(
                                        f"{marker} {j + 1:4d} | {lines[j].rstrip()}"
                                    )
                                results.append(
                                    f"📍 {rel_path}:{i + 1}\n" + "\n".join(context_lines)
                                )

                    if len(results) >= 10:
                        break
                if len(results) >= 10:
                    break

            if not results:
                return ToolResult(success=True, output=f"No definition found for: {symbol}")

            content = "\n\n".join(results)
            content, truncated = self._truncate(content)
            return ToolResult(success=True, output=content, truncated=truncated)
        except Exception as e:
            return ToolResult(success=False, output=f"Error finding definition: {e}")

    @staticmethod
    def _match_glob(filename: str, pattern: str) -> bool:
        """Simple glob matching for file patterns like *.py."""
        import fnmatch
        return fnmatch.fnmatch(filename, pattern)

    # ── Tool: get_file_info ──────────────────────────────────────────

    def get_file_info(self, paths: list[str]) -> ToolResult:
        """Get metadata for one or more files."""
        try:
            infos: list[str] = []
            for p in paths[:20]:  # limit to 20 files
                try:
                    full_path = self._safe_resolve(p)
                except ValueError as e:
                    infos.append(f"{p}: {e}")
                    continue

                if not os.path.exists(full_path):
                    infos.append(f"{p}: not found")
                    continue

                stat = os.stat(full_path)
                size = stat.st_size
                ext = os.path.splitext(p)[1].lower()
                is_binary = self._is_binary(full_path)
                line_count = 0
                if not is_binary and os.path.isfile(full_path):
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            line_count = sum(1 for _ in f)
                    except Exception:
                        pass

                lang = ext.lstrip(".") if ext else "unknown"
                infos.append(
                    f"{p}: size={size}B, lines={line_count}, "
                    f"lang={lang}, binary={is_binary}"
                )

            return ToolResult(success=True, output="\n".join(infos))
        except Exception as e:
            return ToolResult(success=False, output=f"Error getting file info: {e}")

    @staticmethod
    def _is_binary(filepath: str) -> bool:
        try:
            with open(filepath, "rb") as f:
                chunk = f.read(8192)
                return b"\x00" in chunk
        except Exception:
            return True

    # ── OpenAI tool definitions for the LLM ──────────────────────────

    @staticmethod
    def tool_definitions() -> list[dict]:
        """Return OpenAI function-calling tool definitions."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": (
                        "读取仓库中指定文件的内容。"
                        "当你需要查看 diff 中引用的代码的完整上下文、理解函数或类的完整实现、"
                        "检查导入关系或配置内容时，请使用此工具。"
                        "支持通过行号范围读取文件的指定片段，避免一次性加载过大的文件。"
                        "返回结果带有行号标注，方便你在审查评论中精确引用代码位置。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "相对于仓库根目录的文件路径，例如 'src/main.py' 或 'config/settings.yaml'",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "起始行号（从 1 开始计数）。如果只想读取文件的某一部分，请指定此参数。省略则从文件开头读取",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "结束行号（包含该行）。与 start_line 配合使用来读取指定范围。省略则读取到文件末尾",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": (
                        "在整个代码仓库中搜索匹配指定模式的文本（基于 grep，支持正则表达式）。"
                        "适用场景：查找某个函数/变量/类在其他文件中的调用和引用；"
                        "追踪某个配置项或字符串常量的使用位置；"
                        "查找与当前审查代码相关的上下游依赖。"
                        "搜索会自动排除 .git、node_modules、__pycache__ 等非代码目录。"
                        "返回结果包含文件路径、行号和匹配行的内容。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "搜索模式，支持正则表达式。例如：'def process_' 搜索所有以 process_ 开头的函数定义，'TODO|FIXME' 搜索待办标记",
                            },
                            "file_pattern": {
                                "type": "string",
                                "description": "文件名通配符过滤器，用于限定搜索范围。例如 '*.py' 只搜索 Python 文件，'*.ts' 只搜索 TypeScript 文件。省略则搜索所有文件",
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "返回的最大结果数量，默认 30，上限 30。如果搜索结果过多，建议缩小搜索范围或使用更精确的模式",
                            },
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_directory",
                    "description": (
                        "列出仓库中指定目录的内容，了解项目的文件结构和组织方式。"
                        "适用场景：初始了解项目的整体目录结构；"
                        "查看某个模块/包下有哪些文件；确认某个文件或目录是否存在。"
                        "目录名后会带 '/' 后缀以便区分文件和目录。"
                        "自动排除 .git、node_modules、__pycache__ 等无关目录。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "相对于仓库根目录的目录路径。默认为 '.'（仓库根目录）。例如 'src/core' 或 'tests'",
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "是否递归列出子目录内容。设为 true 可一次性查看目录树结构，默认 false 仅列出当前层级",
                            },
                            "max_depth": {
                                "type": "integer",
                                "description": "递归列出时的最大深度，默认 3，上限 3。仅在 recursive 为 true 时生效",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_definition",
                    "description": (
                        "查找指定符号（函数、类、类型、接口、常量等）在代码中的定义位置。"
                        "使用语言感知的正则模式进行匹配，支持 Python、JavaScript/TypeScript、"
                        "Java、Kotlin、C#、Go、Rust、C/C++、Ruby、PHP 等语言。"
                        "适用场景：查看 diff 中调用的函数的原始定义；理解某个类或接口的结构；"
                        "确认某个类型或常量的声明方式。"
                        "返回结果包含定义所在的文件路径、行号，以及定义处前后各 3 行的上下文代码。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "要查找的符号名称，例如函数名 'process_webhook'、类名 'ReviewEngine'、类型名 'ToolResult'",
                            },
                            "file_pattern": {
                                "type": "string",
                                "description": "文件名通配符过滤器，用于缩小搜索范围。例如 '*.py' 只在 Python 文件中查找。省略则搜索所有支持的代码文件",
                            },
                        },
                        "required": ["symbol"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_file_info",
                    "description": (
                        "获取一个或多个文件的元数据信息，包括文件大小、代码行数、编程语言和是否为二进制文件。"
                        "适用场景：在读取文件前先了解文件大小以决定是否需要分段读取；"
                        "快速判断文件类型和规模；批量检查多个文件的基本信息。"
                        "单次调用最多支持 20 个文件路径。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "相对于仓库根目录的文件路径列表，例如 ['src/main.py', 'README.md', 'package.json']",
                            },
                        },
                        "required": ["paths"],
                    },
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> ToolResult:
        """Dispatch a tool call by name."""
        required_params = {
            "read_file": ["path"],
            "search_code": ["pattern"],
            "find_definition": ["symbol"],
            "get_file_info": ["paths"],
        }
        if name in required_params:
            missing = [p for p in required_params[name] if p not in arguments]
            if missing:
                return ToolResult(
                    success=False,
                    output=f"缺少必需参数: {', '.join(missing)}",
                )

        dispatch = {
            "read_file": lambda args: self.read_file(
                args["path"], args.get("start_line"), args.get("end_line")
            ),
            "search_code": lambda args: self.search_code(
                args["pattern"], args.get("file_pattern"), args.get("max_results", 30)
            ),
            "list_directory": lambda args: self.list_directory(
                args.get("path", "."), args.get("recursive", False), args.get("max_depth", 3)
            ),
            "find_definition": lambda args: self.find_definition(
                args["symbol"], args.get("file_pattern")
            ),
            "get_file_info": lambda args: self.get_file_info(args["paths"]),
        }

        handler = dispatch.get(name)
        if handler is None:
            return ToolResult(success=False, output=f"Unknown tool: {name}")

        try:
            return handler(arguments)
        except Exception as e:
            logger.exception("Tool execution error: %s", name)
            return ToolResult(success=False, output=f"Tool error: {e}")
