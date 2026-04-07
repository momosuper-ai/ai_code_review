"""Tests for context_manager: token estimation and message trimming."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.core.context_manager import (
    _build_tool_call_index,
    _copy_message,
    _identify_tool_call_groups,
    _summarize_tool_result,
    estimate_message_tokens,
    estimate_tokens,
    trim_messages,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_system(content: str = "You are a reviewer.") -> dict:
    return {"role": "system", "content": content}


def _make_user(content: str = "Review this diff.") -> dict:
    return {"role": "user", "content": content}


def _make_assistant_with_tools(tool_calls: list[dict]) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": tool_calls}


def _make_tool_call(tc_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _make_tool_result(tc_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tc_id, "content": content}


def _make_assistant_text(content: str = "Here is my review.") -> dict:
    return {"role": "assistant", "content": content}


def _make_chat_completion_message(content=None, tool_calls=None):
    """Create a mock ChatCompletionMessage object."""
    msg = MagicMock()
    msg.role = "assistant"
    msg.content = content
    msg.tool_calls = tool_calls
    msg.tool_call_id = None
    return msg


def _make_chat_completion_tool_call(tc_id, name, arguments):
    """Create a mock tool call object."""
    tc = MagicMock()
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    return tc


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_none_returns_zero(self):
        assert estimate_tokens(None) == 0

    def test_short_string(self):
        # "ab" => len=2, /3.0 = 0.66 => max(1, 0) = 1
        assert estimate_tokens("ab") == 1

    def test_longer_string(self):
        text = "a" * 300
        # 300 / 3.0 = 100
        assert estimate_tokens(text) == 100


# ---------------------------------------------------------------------------
# estimate_message_tokens
# ---------------------------------------------------------------------------

class TestEstimateMessageTokens:
    def test_simple_user_message(self):
        msg = {"role": "user", "content": "Hello"}
        tokens = estimate_message_tokens(msg)
        # overhead(4) + role("user"=4/3~1) + content("Hello"=5/3~1)
        assert tokens > 0

    def test_message_with_tool_calls(self):
        tc = _make_tool_call("tc1", "read_file", {"path": "src/main.py"})
        msg = _make_assistant_with_tools([tc])
        tokens = estimate_message_tokens(msg)
        assert tokens > 4  # overhead + tool call data

    def test_chat_completion_message_object(self):
        tc = _make_chat_completion_tool_call("tc1", "search_code", {"pattern": "def foo"})
        obj = _make_chat_completion_message(content=None, tool_calls=[tc])
        tokens = estimate_message_tokens(obj)
        assert tokens > 4


# ---------------------------------------------------------------------------
# _copy_message
# ---------------------------------------------------------------------------

class TestCopyMessage:
    def test_copy_dict(self):
        original = {"role": "user", "content": "hello"}
        copied = _copy_message(original)
        assert copied == original
        # Must be a separate object
        copied["content"] = "changed"
        assert original["content"] == "hello"

    def test_copy_chat_completion_message(self):
        tc = _make_chat_completion_tool_call("tc1", "read_file", {"path": "a.py"})
        obj = _make_chat_completion_message(content=None, tool_calls=[tc])
        copied = _copy_message(obj)
        assert isinstance(copied, dict)
        assert copied["role"] == "assistant"
        assert len(copied["tool_calls"]) == 1
        assert copied["tool_calls"][0]["function"]["name"] == "read_file"


# ---------------------------------------------------------------------------
# _build_tool_call_index
# ---------------------------------------------------------------------------

class TestBuildToolCallIndex:
    def test_builds_index(self):
        tc = _make_tool_call("tc1", "read_file", {"path": "src/main.py"})
        messages = [
            _make_system(),
            _make_user(),
            _make_assistant_with_tools([tc]),
            _make_tool_result("tc1", "file content..."),
        ]
        index = _build_tool_call_index(messages)
        assert "tc1" in index
        name, args = index["tc1"]
        assert name == "read_file"
        assert args["path"] == "src/main.py"

    def test_ignores_non_assistant(self):
        messages = [_make_system(), _make_user()]
        index = _build_tool_call_index(messages)
        assert len(index) == 0


# ---------------------------------------------------------------------------
# _summarize_tool_result
# ---------------------------------------------------------------------------

class TestSummarizeToolResult:
    def test_read_file_with_lines(self):
        index = {"tc1": ("read_file", {"path": "src/main.py", "start_line": 10, "end_line": 50})}
        result = _summarize_tool_result("tc1", 3200, index)
        assert "src/main.py" in result
        assert "第10-50行" in result
        assert "3200" in result

    def test_read_file_without_lines(self):
        index = {"tc1": ("read_file", {"path": "src/main.py"})}
        result = _summarize_tool_result("tc1", 5000, index)
        assert "src/main.py" in result
        assert "5000" in result

    def test_search_code(self):
        index = {"tc1": ("search_code", {"pattern": "def process", "file_pattern": "*.py"})}
        result = _summarize_tool_result("tc1", 1500, index)
        assert "def process" in result
        assert "*.py" in result

    def test_find_definition(self):
        index = {"tc1": ("find_definition", {"symbol": "ReviewEngine"})}
        result = _summarize_tool_result("tc1", 2000, index)
        assert "ReviewEngine" in result

    def test_list_directory(self):
        index = {"tc1": ("list_directory", {"path": "src/core"})}
        result = _summarize_tool_result("tc1", 800, index)
        assert "src/core" in result

    def test_get_file_info(self):
        index = {"tc1": ("get_file_info", {"path": "src/main.py"})}
        result = _summarize_tool_result("tc1", 200, index)
        assert "src/main.py" in result

    def test_unknown_tool(self):
        index = {"tc1": ("custom_tool", {"x": 1})}
        result = _summarize_tool_result("tc1", 500, index)
        assert "custom_tool" in result

    def test_missing_tool_call_id(self):
        index = {}
        result = _summarize_tool_result("missing_id", 100, index)
        assert "100" in result


# ---------------------------------------------------------------------------
# _identify_tool_call_groups
# ---------------------------------------------------------------------------

class TestIdentifyToolCallGroups:
    def test_single_group(self):
        tc = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        messages = [
            _make_system(),                             # 0
            _make_user(),                               # 1
            _make_assistant_with_tools([tc]),            # 2
            _make_tool_result("tc1", "content"),        # 3
            _make_assistant_text("review"),              # 4
        ]
        groups = _identify_tool_call_groups(messages, 2, 4)
        assert len(groups) == 1
        assert groups[0] == (2, [3])

    def test_multiple_groups(self):
        tc1 = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        tc2 = _make_tool_call("tc2", "read_file", {"path": "b.py"})
        messages = [
            _make_system(),                             # 0
            _make_user(),                               # 1
            _make_assistant_with_tools([tc1]),           # 2
            _make_tool_result("tc1", "content1"),       # 3
            _make_assistant_with_tools([tc2]),           # 4
            _make_tool_result("tc2", "content2"),       # 5
            _make_assistant_text("final"),               # 6
        ]
        groups = _identify_tool_call_groups(messages, 2, 6)
        assert len(groups) == 2
        assert groups[0] == (2, [3])
        assert groups[1] == (4, [5])


# ---------------------------------------------------------------------------
# trim_messages — no trimming needed
# ---------------------------------------------------------------------------

class TestTrimNoAction:
    def test_short_messages_no_trim(self):
        messages = [_make_system(), _make_user("short diff")]
        result = trim_messages(messages)
        assert result is messages  # same object, no copy

    def test_within_budget(self):
        messages = [
            _make_system("sys"),
            _make_user("diff"),
            _make_assistant_text("review"),
        ]
        result = trim_messages(messages)
        assert result is messages


# ---------------------------------------------------------------------------
# trim_messages — Phase 1: truncate tool results
# ---------------------------------------------------------------------------

class TestTrimPhase1:
    @patch("src.core.context_manager.settings")
    def test_truncate_tool_results(self, mock_settings):
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 10000
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0  # 1:1 for easy math

        big_content = "x" * 5000
        tc1 = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        tc2 = _make_tool_call("tc2", "read_file", {"path": "b.py"})
        messages = [
            _make_system("s" * 50),                     # 0
            _make_user("u" * 50),                       # 1
            _make_assistant_with_tools([tc1]),           # 2 middle
            _make_tool_result("tc1", big_content),      # 3 middle - big
            _make_assistant_with_tools([tc2]),           # 4 middle
            _make_tool_result("tc2", big_content),      # 5 middle - big
            _make_user("follow up"),                     # 6 tail
            _make_assistant_text("part1"),               # 7 tail
            _make_user("more"),                          # 8 tail
            _make_assistant_text("done"),                # 9 tail
        ]

        result = trim_messages(messages, reserve_for_completion=100)
        # Tool results in middle zone should be truncated
        truncated_tools = [m for m in result if m.get("role") == "tool" and "截断" in m.get("content", "")]
        assert len(truncated_tools) > 0

    @patch("src.core.context_manager.settings")
    def test_original_not_modified(self, mock_settings):
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 10000
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        big_content = "x" * 5000
        tc1 = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        tc2 = _make_tool_call("tc2", "read_file", {"path": "b.py"})
        messages = [
            _make_system("s" * 50),
            _make_user("u" * 50),
            _make_assistant_with_tools([tc1]),
            _make_tool_result("tc1", big_content),
            _make_assistant_with_tools([tc2]),
            _make_tool_result("tc2", big_content),
            _make_user("follow up"),
            _make_assistant_text("part1"),
            _make_user("more"),
            _make_assistant_text("done"),
        ]

        trim_messages(messages, reserve_for_completion=100)
        # Original must not be modified
        assert len(messages[3]["content"]) == 5000
        assert len(messages[5]["content"]) == 5000


# ---------------------------------------------------------------------------
# trim_messages — Phase 2: summarize tool results
# ---------------------------------------------------------------------------

class TestTrimPhase2:
    @patch("src.core.context_manager.settings")
    def test_summarize_replaces_content(self, mock_settings):
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 4000
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        # Phase 1 truncates to 2000 chars each, but budget is tight enough
        # that phase 2 summary replacement is still needed
        tc1 = _make_tool_call("tc1", "read_file", {"path": "src/main.py"})
        tc2 = _make_tool_call("tc2", "read_file", {"path": "src/app.py"})
        messages = [
            _make_system("s" * 30),                     # 0
            _make_user("u" * 30),                       # 1
            _make_assistant_with_tools([tc1]),           # 2
            _make_tool_result("tc1", "x" * 3000),       # 3 middle
            _make_assistant_with_tools([tc2]),           # 4
            _make_tool_result("tc2", "y" * 3000),       # 5 middle
            _make_user("next"),                          # 6 tail
            _make_assistant_text("part1"),               # 7 tail
            _make_user("more"),                          # 8 tail
            _make_assistant_text("review"),              # 9 tail
        ]

        result = trim_messages(messages, reserve_for_completion=50)
        # Find tool messages that got summarized (phase 2 replaces with summary)
        summarized = [m for m in result if m.get("role") == "tool" and "已省略" in m.get("content", "")]
        assert len(summarized) > 0
        assert any("src/main.py" in m["content"] for m in summarized)


# ---------------------------------------------------------------------------
# trim_messages — Phase 3: remove tool call groups
# ---------------------------------------------------------------------------

class TestTrimPhase3:
    @patch("src.core.context_manager.settings")
    def test_removes_oldest_group(self, mock_settings):
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 400
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        tc1 = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        tc2 = _make_tool_call("tc2", "read_file", {"path": "b.py"})
        messages = [
            _make_system("s" * 20),                     # 0: protected
            _make_user("u" * 20),                       # 1: protected
            _make_assistant_with_tools([tc1]),           # 2: middle (old)
            _make_tool_result("tc1", "r" * 2000),       # 3: middle (old)
            _make_assistant_with_tools([tc2]),           # 4: tail
            _make_tool_result("tc2", "r" * 50),         # 5: tail
            _make_user("follow up"),                     # 6: tail
            _make_assistant_text("final review"),        # 7: tail
        ]

        result = trim_messages(messages, reserve_for_completion=50)
        # Oldest group (tc1) should be removed
        roles = [m["role"] for m in result]
        # tc1 tool result should not be present
        tool_contents = [m.get("content", "") for m in result if m.get("role") == "tool"]
        assert not any("r" * 2000 in c for c in tool_contents)

    @patch("src.core.context_manager.settings")
    def test_no_orphan_tool_messages(self, mock_settings):
        """裁剪后不应出现孤立的 tool 消息。"""
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 400
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        tc1 = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        tc2 = _make_tool_call("tc2", "read_file", {"path": "b.py"})
        messages = [
            _make_system("s" * 20),
            _make_user("u" * 20),
            _make_assistant_with_tools([tc1]),
            _make_tool_result("tc1", "r" * 2000),
            _make_assistant_with_tools([tc2]),
            _make_tool_result("tc2", "r" * 50),
            _make_user("follow up"),
            _make_assistant_text("final review"),
        ]

        result = trim_messages(messages, reserve_for_completion=50)
        # Every tool message should have a preceding assistant with tool_calls
        for i, msg in enumerate(result):
            if msg.get("role") == "tool":
                # Find the assistant message that initiated this tool call
                found_assistant = False
                for j in range(i - 1, -1, -1):
                    if result[j].get("role") == "assistant" and result[j].get("tool_calls"):
                        found_assistant = True
                        break
                    if result[j].get("role") != "tool":
                        break
                assert found_assistant, f"Orphan tool message at index {i}"


# ---------------------------------------------------------------------------
# trim_messages — Phase 4: truncate diff
# ---------------------------------------------------------------------------

class TestTrimPhase4:
    @patch("src.core.context_manager.settings")
    def test_truncate_large_diff(self, mock_settings):
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 300
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        # Build a large diff with multiple files
        diff_parts = ["以下是本次代码变更的 diff 内容，请进行审查：\n"]
        for i in range(20):
            diff_parts.append(f"### file_{i}.py\n```diff\n{'+line' * 200}\n```\n")
        large_diff = "\n".join(diff_parts)

        messages = [
            _make_system("s" * 10),
            _make_user(large_diff),
        ]

        result = trim_messages(messages, reserve_for_completion=50)
        user_content = result[1]["content"]
        assert "截断" in user_content
        assert len(user_content) < len(large_diff)


# ---------------------------------------------------------------------------
# trim_messages — mixed message types (dict + ChatCompletionMessage)
# ---------------------------------------------------------------------------

class TestTrimMixedTypes:
    @patch("src.core.context_manager.settings")
    def test_handles_chat_completion_message(self, mock_settings):
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 1000
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        tc_obj = _make_chat_completion_tool_call("tc1", "read_file", {"path": "a.py"})
        assistant_obj = _make_chat_completion_message(content=None, tool_calls=[tc_obj])

        messages = [
            _make_system("s" * 50),
            _make_user("u" * 50),
            assistant_obj,  # ChatCompletionMessage object
            _make_tool_result("tc1", "x" * 5000),
            _make_assistant_text("done"),
        ]

        result = trim_messages(messages, reserve_for_completion=100)
        # Should not raise and should return dicts
        for msg in result:
            assert isinstance(msg, dict)


# ---------------------------------------------------------------------------
# trim_messages — edge: only system + user (no tool calls)
# ---------------------------------------------------------------------------

class TestTrimEdgeCases:
    def test_only_system_and_user_within_budget(self):
        messages = [_make_system(), _make_user()]
        result = trim_messages(messages)
        assert result is messages

    @patch("src.core.context_manager.settings")
    def test_protected_messages_preserved(self, mock_settings):
        """System prompt and first user message must always be preserved."""
        mock_settings.LLM_MAX_CONTEXT_TOKENS = 200
        mock_settings.LLM_CONTEXT_THRESHOLD = 0.9
        mock_settings.LLM_CHARS_PER_TOKEN = 1.0

        tc = _make_tool_call("tc1", "read_file", {"path": "a.py"})
        messages = [
            _make_system("important system prompt"),
            _make_user("important diff content"),
            _make_assistant_with_tools([tc]),
            _make_tool_result("tc1", "x" * 5000),
            _make_assistant_text("final"),
        ]

        result = trim_messages(messages, reserve_for_completion=10)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "important system prompt"
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "important diff content"
