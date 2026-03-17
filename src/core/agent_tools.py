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
                    "description": "Read the content of a file in the repository. Use to understand the full context of code referenced in the diff.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative file path from repo root",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "Start line number (1-based, optional)",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "End line number (inclusive, optional)",
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
                    "description": "Search for a text pattern in the codebase using grep. Useful for finding usages, references, or related code.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {
                                "type": "string",
                                "description": "Search pattern (regex supported)",
                            },
                            "file_pattern": {
                                "type": "string",
                                "description": "File glob filter, e.g. '*.py' (optional)",
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Max results to return (default 30, max 30)",
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
                    "description": "List contents of a directory. Use to understand project structure.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Relative directory path (default: repo root)",
                            },
                            "recursive": {
                                "type": "boolean",
                                "description": "List recursively (default: false)",
                            },
                            "max_depth": {
                                "type": "integer",
                                "description": "Max recursion depth (default 3, max 3)",
                            },
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_definition",
                    "description": "Find where a symbol (function, class, type) is defined. Supports Python, JS/TS, Java, Go, Rust, C/C++, Ruby, PHP.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "symbol": {
                                "type": "string",
                                "description": "The symbol name to find",
                            },
                            "file_pattern": {
                                "type": "string",
                                "description": "File glob filter, e.g. '*.py' (optional)",
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
                    "description": "Get metadata (size, line count, language, binary status) for one or more files.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "paths": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "List of relative file paths",
                            },
                        },
                        "required": ["paths"],
                    },
                },
            },
        ]

    def execute_tool(self, name: str, arguments: dict) -> ToolResult:
        """Dispatch a tool call by name."""
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
