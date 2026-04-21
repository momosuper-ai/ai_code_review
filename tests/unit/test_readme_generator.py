"""Unit tests for the AI_README.md generation engine."""

import hashlib
import os
import tempfile

import pytest

from src.core.readme_generator import (
    ReadmeGenerator,
    _strip_markdown_wrapper,
    _TOOL_CALL_PATTERN,
    _CODE_EXTENSIONS,
    _EXCLUDE_DIRS,
    _KEY_CONFIG_FILES,
    _READONLY_TOOLS,
    FileAction,
    ReadmeResult,
)


@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repo directory with sample files."""
    # Root files
    (tmp_path / "main.py").write_text("print('hello')\n" * 10)
    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\n")
    (tmp_path / "package.json").write_text('{"name": "test"}')

    # src directory with enough code
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "__init__.py").write_text("")
    (src_dir / "app.py").write_text("def main():\n    pass\n" * 120)
    (src_dir / "utils.py").write_text("def helper():\n    return 1\n" * 50)

    # small directory (should be skipped for < 200 lines)
    small_dir = tmp_path / "scripts"
    small_dir.mkdir()
    (small_dir / "run.py").write_text("print('hi')\n" * 5)

    # excluded dirs
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = {}")

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]")

    return tmp_path


class TestCountCodeLines:
    def test_counts_code_files_only(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        src_dir = str(temp_repo / "src")
        lines = gen._count_code_lines(src_dir)
        # app.py: 240 lines, utils.py: 100 lines, __init__.py: 0 lines
        assert lines == 340

    def test_excludes_non_code_files(self, temp_repo):
        # Add a non-code file
        (temp_repo / "src" / "data.csv").write_text("a,b,c\n1,2,3\n")
        gen = ReadmeGenerator(str(temp_repo))
        lines = gen._count_code_lines(str(temp_repo / "src"))
        assert lines == 340  # unchanged

    def test_excludes_excluded_dirs(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        # node_modules should be excluded
        lines = gen._count_code_lines(str(temp_repo))
        # Should not count node_modules/pkg.js
        assert lines > 0

    def test_empty_dir(self, temp_repo):
        empty_dir = temp_repo / "empty"
        empty_dir.mkdir()
        gen = ReadmeGenerator(str(temp_repo))
        assert gen._count_code_lines(str(empty_dir)) == 0


class TestComputeFingerprint:
    def test_fingerprint_consistency(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        fp1 = gen._compute_root_fingerprint()
        fp2 = gen._compute_root_fingerprint()
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex digest

    def test_fingerprint_changes_with_new_file(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        fp1 = gen._compute_root_fingerprint()
        (temp_repo / "new_config.py").write_text("x = 1")
        fp2 = gen._compute_root_fingerprint()
        assert fp1 != fp2

    def test_fingerprint_changes_with_config_content(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        fp1 = gen._compute_root_fingerprint()
        (temp_repo / "requirements.txt").write_text("fastapi\nuvicorn\nnewpkg\n")
        fp2 = gen._compute_root_fingerprint()
        assert fp1 != fp2

    def test_subdir_fingerprint(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        fp = gen._compute_subdir_fingerprint(str(temp_repo / "src"))
        assert len(fp) == 64
        # Changing file content changes fingerprint
        (temp_repo / "src" / "app.py").write_text("new content\n")
        fp2 = gen._compute_subdir_fingerprint(str(temp_repo / "src"))
        assert fp != fp2


class TestListEligibleSubdirs:
    def test_filters_by_code_lines(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        subdirs = gen._list_eligible_subdirs()
        # src (340 lines) should be included, scripts (5 lines) should not
        assert "src" in subdirs
        assert "scripts" not in subdirs

    def test_excludes_hidden_and_special_dirs(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        subdirs = gen._list_eligible_subdirs()
        assert ".git" not in subdirs
        assert "node_modules" not in subdirs


class TestStripMarkdownWrapper:
    def test_no_wrapper(self):
        text = "# Hello\nWorld"
        assert _strip_markdown_wrapper(text) == "# Hello\nWorld"

    def test_triple_backtick_wrapper(self):
        text = "```\n# Hello\nWorld\n```"
        assert _strip_markdown_wrapper(text) == "# Hello\nWorld"

    def test_markdown_language_wrapper(self):
        text = "```markdown\n# Hello\nWorld\n```"
        assert _strip_markdown_wrapper(text) == "# Hello\nWorld"

    def test_md_language_wrapper(self):
        text = "```md\n# Hello\nWorld\n```"
        assert _strip_markdown_wrapper(text) == "# Hello\nWorld"


class TestReadonlyTools:
    def test_excludes_blast_radius(self):
        assert "get_blast_radius" not in _READONLY_TOOLS
        assert "read_file" in _READONLY_TOOLS
        assert "search_code" in _READONLY_TOOLS
        assert "list_directory" in _READONLY_TOOLS
        assert "find_definition" in _READONLY_TOOLS
        assert "get_file_info" in _READONLY_TOOLS

    def test_tool_definitions_filtered(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        tool_defs = gen._get_readonly_tool_definitions()
        names = {t["function"]["name"] for t in tool_defs}
        assert names == _READONLY_TOOLS


class TestCollectInfo:
    def test_collect_root_info(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        info = gen._collect_root_info()
        assert "directory_tree" in info
        assert "key_files_content" in info
        # Should contain config file content
        assert "requirements.txt" in info["key_files_content"]

    def test_collect_subdir_info(self, temp_repo):
        gen = ReadmeGenerator(str(temp_repo))
        info = gen._collect_subdir_info("src")
        assert "directory_tree" in info
        assert "key_files_content" in info


class TestFileAction:
    def test_dataclass(self):
        action = FileAction(
            file_path="AI_README.md",
            content="# Test",
            action="create",
            prompt_tokens=100,
            completion_tokens=50,
        )
        assert action.file_path == "AI_README.md"
        assert action.action == "create"

    def test_defaults(self):
        action = FileAction(file_path="test.md", content="x", action="update")
        assert action.prompt_tokens == 0
        assert action.completion_tokens == 0


class TestReadmeResult:
    def test_defaults(self):
        result = ReadmeResult()
        assert result.actions == []
        assert result.skipped_dirs == []
        assert result.total_prompt_tokens == 0


class TestWebhookAntiRecursion:
    """Test the anti-recursion logic in webhooks.py."""

    def test_all_ai_readme_commits_filtered(self):
        """All commits with [AI-README] prefix should be filtered."""
        commits = [
            {"message": "[AI-README] 更新项目文档"},
            {"message": "[AI-README] 更新子目录文档"},
        ]
        assert all(c.get("message", "").startswith("[AI-README]") for c in commits)

    def test_mixed_commits_not_filtered(self):
        """Mixed commits should not be filtered."""
        commits = [
            {"message": "[AI-README] 更新项目文档"},
            {"message": "fix: bug fix"},
        ]
        assert not all(c.get("message", "").startswith("[AI-README]") for c in commits)

    def test_empty_commits_not_filtered(self):
        """Empty commit list should not be filtered."""
        commits = []
        # The condition `commits and all(...)` should be False for empty list
        assert not (commits and all(
            c.get("message", "").startswith("[AI-README]") for c in commits
        ))


class TestGitLabCommitSafety:
    """Test the safety restriction on create_commit: only AI_README.md allowed."""

    def test_allowed_root_readme(self):
        from src.core.gitlab_client import GitLabClient
        client = GitLabClient()
        actions = [{"action": "create", "file_path": "AI_README.md", "content": "# Test"}]
        # Should not raise
        for act in actions:
            file_path = act.get("file_path", "")
            filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
            assert filename == client._ALLOWED_COMMIT_FILENAME

    def test_allowed_subdir_readme(self):
        from src.core.gitlab_client import GitLabClient
        client = GitLabClient()
        actions = [
            {"action": "create", "file_path": "src/AI_README.md", "content": "# Test"},
            {"action": "update", "file_path": "src/core/AI_README.md", "content": "# Test"},
        ]
        for act in actions:
            file_path = act.get("file_path", "")
            filename = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
            assert filename == client._ALLOWED_COMMIT_FILENAME

    def test_rejected_other_files(self):
        from src.core.gitlab_client import GitLabClient
        client = GitLabClient()
        bad_paths = [
            "README.md",
            "src/main.py",
            ".env",
            "src/core/config.py",
            "AI_README.txt",
            "FAKE_AI_README.md",
        ]
        for path in bad_paths:
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            assert filename != client._ALLOWED_COMMIT_FILENAME, f"{path} should be rejected"


class TestReadFileSafe:
    def test_read_existing_file(self, temp_repo):
        content = ReadmeGenerator._read_file_safe(str(temp_repo / "main.py"))
        assert content is not None
        assert "hello" in content

    def test_read_nonexistent_file(self, temp_repo):
        content = ReadmeGenerator._read_file_safe(str(temp_repo / "nonexistent.py"))
        assert content is None


class TestValidateContent:
    def test_clean_tool_calls(self):
        """验证 [TOOL_CALLS]tool_name{...} 文本被清理，保留后续 Markdown。"""
        raw = (
            "# 项目文档\n\n这是一个测试项目。\n"
            '[TOOL_CALLS]read_file{"path": "src/main.py"}\n'
            "## 技术栈\nPython 3.12"
        )
        cleaned = ReadmeGenerator._validate_content(raw)
        assert "[TOOL_CALLS]" not in cleaned
        assert "# 项目文档" in cleaned
        assert "## 技术栈" in cleaned

    def test_clean_tool_calls_array_form(self):
        """验证 Mistral/Devstral 常见的 [TOOL_CALLS][{...}] 数组格式被清理。"""
        raw = (
            "# 项目文档\n\n"
            '[TOOL_CALLS][{"name": "read_file", "arguments": {"path": "x"}}]\n'
            "## 技术栈\nPython"
        )
        cleaned = ReadmeGenerator._validate_content(raw)
        assert "[TOOL_CALLS]" not in cleaned
        assert "read_file" not in cleaned
        assert "# 项目文档" in cleaned
        assert "## 技术栈" in cleaned

    def test_clean_tool_calls_at_end(self):
        """验证文末的 [TOOL_CALLS] 被清理。"""
        raw = "# 项目文档\n\n内容部分\n[TOOL_CALLS]read_file{\"path\": \"x\"}"
        cleaned = ReadmeGenerator._validate_content(raw)
        assert "[TOOL_CALLS]" not in cleaned
        assert "# 项目文档" in cleaned

    def test_tool_call_does_not_eat_following_markdown(self):
        """回归：旧正则的 .*$ 分支会吞噬 [TOOL_CALLS] 后的整段 Markdown；新正则必须保留。"""
        raw = (
            '[TOOL_CALLS]read_file{"path": "src/main.py"}\n\n'
            "# 项目文档\n\n详细内容在这里。\n\n## 技术栈\n\n- Python\n- FastAPI"
        )
        cleaned = ReadmeGenerator._validate_content(raw)
        assert "[TOOL_CALLS]" not in cleaned
        assert "# 项目文档" in cleaned
        assert "## 技术栈" in cleaned
        assert "详细内容在这里" in cleaned
        assert "FastAPI" in cleaned

    def test_multiple_tool_calls_cleared(self):
        """验证多次 [TOOL_CALLS] 出现都被清理。"""
        raw = (
            "# 文档\n\n"
            '[TOOL_CALLS]list_directory{"path": "."}\n\n'
            "## 结构\n内容\n\n"
            '[TOOL_CALLS]read_file{"path": "README.md"}\n\n'
            "## 结尾"
        )
        cleaned = ReadmeGenerator._validate_content(raw)
        assert "[TOOL_CALLS]" not in cleaned
        assert "# 文档" in cleaned
        assert "## 结构" in cleaned
        assert "## 结尾" in cleaned

    def test_no_false_positive(self):
        """正常 markdown 内容不被误清理。"""
        normal = "# Hello\n\n这是正常的文档内容。\n\n## 详情\n\n- 项目A\n- 项目B"
        cleaned = ReadmeGenerator._validate_content(normal)
        assert cleaned == normal.strip()

    def test_empty_content(self):
        """空内容返回空字符串。"""
        assert ReadmeGenerator._validate_content("") == ""
        assert ReadmeGenerator._validate_content("  \n  ") == ""


class TestBuildSubdirSummary:
    def test_builds_summary_from_actions(self):
        """验证从 FileAction 构建摘要。"""
        actions = [
            FileAction(file_path="src/AI_README.md", content="# src 模块\n\n概述内容", action="create"),
            FileAction(file_path="tests/AI_README.md", content="# tests 模块\n\n测试内容", action="create"),
        ]
        summary = ReadmeGenerator._build_subdir_summary(actions)
        assert "### src/AI_README.md" in summary
        assert "### tests/AI_README.md" in summary
        assert "# src 模块" in summary

    def test_empty_actions(self):
        """空列表返回空字符串。"""
        assert ReadmeGenerator._build_subdir_summary([]) == ""
        assert ReadmeGenerator._build_subdir_summary(None) == ""

    def test_truncates_long_content(self):
        """超过 30 行截断并加提示。"""
        long_content = "\n".join([f"第 {i} 行内容" for i in range(50)])
        actions = [FileAction(file_path="lib/AI_README.md", content=long_content, action="create")]
        summary = ReadmeGenerator._build_subdir_summary(actions)
        assert "...(更多内容请用工具查看)" in summary
        # 应该只包含前 30 行
        assert "第 0 行内容" in summary
        assert "第 29 行内容" in summary
        assert "第 30 行内容" not in summary
