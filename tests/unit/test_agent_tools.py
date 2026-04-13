import os

import pytest

from src.core.agent_tools import AgentToolExecutor, ToolResult


class TestSafeResolve:
    def test_normal_path(self, executor, temp_repo):
        result = executor._safe_resolve("main.py")
        assert result == os.path.realpath(os.path.join(str(temp_repo), "main.py"))

    def test_traversal_blocked(self, executor):
        with pytest.raises(ValueError, match="Path traversal"):
            executor._safe_resolve("../../etc/passwd")

    def test_leading_slash_stripped(self, executor, temp_repo):
        result = executor._safe_resolve("/main.py")
        assert result.startswith(str(temp_repo))


class TestReadFile:
    def test_read_existing_file(self, executor):
        result = executor.read_file("main.py")
        assert result.success
        assert "hello" in result.output

    def test_read_nonexistent_file(self, executor):
        result = executor.read_file("nonexistent.py")
        assert not result.success
        assert "not found" in result.output.lower()

    def test_read_with_line_range(self, executor):
        result = executor.read_file("main.py", start_line=1, end_line=2)
        assert result.success
        lines = result.output.strip().split("\n")
        assert len(lines) == 2

    def test_read_path_traversal(self, executor):
        result = executor.read_file("../../etc/passwd")
        assert not result.success
        assert "traversal" in result.output.lower()


class TestSearchCode:
    def test_search_pattern(self, executor):
        result = executor.search_code("hello")
        assert result.success
        assert "main.py" in result.output

    def test_search_no_matches(self, executor):
        result = executor.search_code("zzz_nonexistent_pattern_zzz")
        assert result.success
        assert "No matches" in result.output

    def test_search_with_file_pattern(self, executor):
        result = executor.search_code("import", file_pattern="*.py")
        assert result.success


class TestListDirectory:
    def test_list_root(self, executor):
        result = executor.list_directory()
        assert result.success
        assert "main.py" in result.output
        assert "src/" in result.output
        # .git should be excluded
        assert ".git/" not in result.output

    def test_list_subdirectory(self, executor):
        result = executor.list_directory("src")
        assert result.success
        assert "app.py" in result.output

    def test_list_recursive(self, executor):
        result = executor.list_directory(".", recursive=True)
        assert result.success
        assert "app.py" in result.output

    def test_list_nonexistent(self, executor):
        result = executor.list_directory("nonexistent")
        assert not result.success


class TestFindDefinition:
    def test_find_function(self, executor):
        result = executor.find_definition("hello", file_pattern="*.py")
        assert result.success
        assert "main.py" in result.output

    def test_find_class(self, executor):
        result = executor.find_definition("Foo", file_pattern="*.py")
        assert result.success
        assert "main.py" in result.output

    def test_find_nonexistent(self, executor):
        result = executor.find_definition("NonExistentSymbol")
        assert result.success
        assert "No definition found" in result.output


class TestGetFileInfo:
    def test_get_info(self, executor):
        result = executor.get_file_info(["main.py", "utils.py"])
        assert result.success
        assert "main.py" in result.output
        assert "utils.py" in result.output
        assert "lines=" in result.output

    def test_get_info_nonexistent(self, executor):
        result = executor.get_file_info(["nonexistent.py"])
        assert result.success
        assert "not found" in result.output


class TestExecuteTool:
    def test_dispatch_read_file(self, executor):
        result = executor.execute_tool("read_file", {"path": "main.py"})
        assert result.success

    def test_dispatch_unknown_tool(self, executor):
        result = executor.execute_tool("unknown_tool", {})
        assert not result.success
        assert "Unknown tool" in result.output

    def test_tool_definitions_format(self):
        defs = AgentToolExecutor.tool_definitions()
        assert len(defs) == 6
        for d in defs:
            assert d["type"] == "function"
            assert "name" in d["function"]
            assert "parameters" in d["function"]


class TestTruncation:
    def test_truncate_by_lines(self):
        text = "\n".join(f"line {i}" for i in range(1000))
        result, truncated = AgentToolExecutor._truncate(text, max_lines=10)
        assert truncated
        assert len(result.split("\n")) == 10

    def test_truncate_by_chars(self):
        text = "x" * 50000
        result, truncated = AgentToolExecutor._truncate(text, max_chars=100)
        assert truncated
        assert len(result) == 100

    def test_no_truncation_needed(self):
        text = "short text"
        result, truncated = AgentToolExecutor._truncate(text)
        assert not truncated
        assert result == text
