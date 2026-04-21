"""Unit tests for GitLabClient helper methods (action flip / file existence)."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.core.gitlab_client import GitLabClient


class TestTryFlipActions:
    def test_flip_create_to_update_on_already_exists(self):
        actions = [
            {"action": "create", "file_path": "AI_README.md", "content": "x"},
            {"action": "create", "file_path": "src/AI_README.md", "content": "y"},
        ]
        flipped = GitLabClient._try_flip_actions(
            actions, '{"message":"A file with this name already exists"}',
        )
        assert flipped is not None
        assert all(a["action"] == "update" for a in flipped)
        # 原 actions 未被修改
        assert all(a["action"] == "create" for a in actions)

    def test_flip_update_to_create_on_not_exist(self):
        actions = [
            {"action": "update", "file_path": "AI_README.md", "content": "x"},
        ]
        flipped = GitLabClient._try_flip_actions(
            actions, '{"message":"A file with this name doesn\'t exist"}',
        )
        assert flipped is not None
        assert flipped[0]["action"] == "create"

    def test_flip_does_not_exist_variant(self):
        actions = [{"action": "update", "file_path": "AI_README.md", "content": "x"}]
        flipped = GitLabClient._try_flip_actions(
            actions, "A file with this name does not exist",
        )
        assert flipped is not None
        assert flipped[0]["action"] == "create"

    def test_mixed_actions_only_flip_matching(self):
        """已经是 update 的 action 在 already_exists 场景下保持不变。"""
        actions = [
            {"action": "create", "file_path": "AI_README.md", "content": "x"},
            {"action": "update", "file_path": "src/AI_README.md", "content": "y"},
        ]
        flipped = GitLabClient._try_flip_actions(
            actions, "A file with this name already exists",
        )
        assert flipped is not None
        assert flipped[0]["action"] == "update"
        assert flipped[1]["action"] == "update"  # 保持不变

    def test_unrelated_error_returns_none(self):
        actions = [{"action": "create", "file_path": "AI_README.md", "content": "x"}]
        assert GitLabClient._try_flip_actions(
            actions, '{"message":"Authentication failed"}',
        ) is None
        assert GitLabClient._try_flip_actions(actions, "") is None

    def test_no_flip_needed_returns_none(self):
        """如果错误是 already_exists 但所有 action 都已是 update，没有需要翻转的 → None。"""
        actions = [{"action": "update", "file_path": "AI_README.md", "content": "x"}]
        assert GitLabClient._try_flip_actions(
            actions, "A file with this name already exists",
        ) is None


class TestFileExists:
    @pytest.fixture
    def client(self):
        return GitLabClient()

    def _mock_client_ctx(self, client: GitLabClient, response: httpx.Response):
        """替换 client._client 为返回指定 response 的 AsyncMock context manager。"""
        mock_httpx = MagicMock()
        mock_httpx.head = AsyncMock(return_value=response)
        mock_httpx.__aenter__ = AsyncMock(return_value=mock_httpx)
        mock_httpx.__aexit__ = AsyncMock(return_value=None)
        client._client = MagicMock(return_value=mock_httpx)

    @pytest.mark.asyncio
    async def test_file_exists_returns_true_on_200(self, client):
        req = httpx.Request("HEAD", "http://x/api/v4/projects/1/repository/files/AI_README.md")
        resp = httpx.Response(200, request=req)
        self._mock_client_ctx(client, resp)
        result = await client.file_exists(1, "AI_README.md", "main")
        assert result is True

    @pytest.mark.asyncio
    async def test_file_exists_returns_false_on_404(self, client):
        req = httpx.Request("HEAD", "http://x/api/v4/projects/1/repository/files/AI_README.md")
        resp = httpx.Response(404, request=req)
        self._mock_client_ctx(client, resp)
        result = await client.file_exists(1, "AI_README.md", "main")
        assert result is False

    @pytest.mark.asyncio
    async def test_file_exists_raises_on_5xx(self, client):
        req = httpx.Request("HEAD", "http://x/api/v4/projects/1/repository/files/AI_README.md")
        resp = httpx.Response(500, request=req, text="server error")
        self._mock_client_ctx(client, resp)
        with pytest.raises(httpx.HTTPStatusError):
            await client.file_exists(1, "AI_README.md", "main")
