"""Unit tests for readme task idempotency helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.tasks.readme_task import _has_completed_readme, _submit_via_temp_branch_mr


class TestHasCompletedReadme:
    """验证 (project_id, branch, commit_sha) 的 completed 记录查询。"""

    @patch("src.tasks.readme_task.SyncSession")
    def test_returns_true_when_completed_record_exists(self, mock_session_cls):
        session_ctx = MagicMock()
        session_ctx.execute.return_value.scalar_one_or_none.return_value = 42
        mock_session_cls.return_value.__enter__.return_value = session_ctx

        assert _has_completed_readme(1, "main", "a" * 40) is True

    @patch("src.tasks.readme_task.SyncSession")
    def test_returns_false_when_no_record(self, mock_session_cls):
        session_ctx = MagicMock()
        session_ctx.execute.return_value.scalar_one_or_none.return_value = None
        mock_session_cls.return_value.__enter__.return_value = session_ctx

        assert _has_completed_readme(1, "main", "a" * 40) is False

    @patch("src.tasks.readme_task.SyncSession", side_effect=RuntimeError("db down"))
    def test_fail_open_on_exception(self, _mock_session_cls):
        # DB 故障时 fail-open 返回 False，不抛异常
        assert _has_completed_readme(1, "main", "a" * 40) is False


def _make_gitlab_mock(
    *,
    create_commit_return: dict | None = None,
    create_commit_side_effect=None,
    create_mr_return: dict | None = None,
    accept_side_effect=None,
):
    mock = MagicMock()
    mock.create_branch = AsyncMock(return_value={})
    mock.delete_branch = AsyncMock(return_value=None)
    if create_commit_side_effect is not None:
        mock.create_commit = AsyncMock(side_effect=create_commit_side_effect)
    else:
        mock.create_commit = AsyncMock(
            return_value=create_commit_return or {"id": "deadbeef" * 5},
        )
    mock.create_merge_request = AsyncMock(
        return_value=create_mr_return or {"iid": 42, "web_url": "http://x/mr/42"},
    )
    if accept_side_effect is not None:
        mock.accept_merge_request = AsyncMock(side_effect=accept_side_effect)
    else:
        mock.accept_merge_request = AsyncMock(return_value={"state": "merged"})
    return mock


class TestSubmitViaTempBranchMR:
    """受保护主分支：临时分支 → commit → MR → 自动合并。"""

    @pytest.mark.asyncio
    async def test_full_flow_with_auto_merge(self):
        gitlab = _make_gitlab_mock(create_commit_return={"id": "abc123" + "0" * 34})
        with patch.object(settings, "README_MR_AUTO_MERGE", True):
            commit_sha, mr_iid = await _submit_via_temp_branch_mr(
                gitlab=gitlab,
                project_id=1,
                project_name="group/repo",
                main_branch="main",
                commit_sha="deadbeef" + "0" * 32,
                gitlab_actions=[{"action": "update", "file_path": "AI_README.md", "content": "x"}],
                commit_msg="[AI-README] 更新",
            )
        assert commit_sha == "abc123" + "0" * 34
        assert mr_iid == 42
        gitlab.create_branch.assert_awaited_once()
        branch_arg = gitlab.create_branch.await_args[0]
        assert branch_arg[0] == 1
        assert branch_arg[1].startswith(settings.README_TEMP_BRANCH_PREFIX)
        assert "deadbeef" in branch_arg[1]
        gitlab.create_commit.assert_awaited_once()
        gitlab.create_merge_request.assert_awaited_once()
        gitlab.accept_merge_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_auto_merge_disabled_skips_accept(self):
        gitlab = _make_gitlab_mock()
        with patch.object(settings, "README_MR_AUTO_MERGE", False):
            _, mr_iid = await _submit_via_temp_branch_mr(
                gitlab=gitlab,
                project_id=1,
                project_name="group/repo",
                main_branch="main",
                commit_sha="c" * 40,
                gitlab_actions=[{"action": "update", "file_path": "AI_README.md", "content": "x"}],
                commit_msg="msg",
            )
        assert mr_iid == 42
        gitlab.accept_merge_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_failure_triggers_branch_cleanup(self):
        """create_commit 抛异常 → 清理临时分支 → 重新抛出。"""
        gitlab = _make_gitlab_mock(create_commit_side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await _submit_via_temp_branch_mr(
                gitlab=gitlab,
                project_id=1,
                project_name="group/repo",
                main_branch="main",
                commit_sha="c" * 40,
                gitlab_actions=[{"action": "update", "file_path": "AI_README.md", "content": "x"}],
                commit_msg="msg",
            )
        gitlab.delete_branch.assert_awaited_once()
        gitlab.create_merge_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_merge_failure_is_non_fatal(self):
        """accept_merge_request 失败仅 warning，返回正常的 (commit_sha, mr_iid)。"""
        gitlab = _make_gitlab_mock(accept_side_effect=RuntimeError("pipeline required"))
        with patch.object(settings, "README_MR_AUTO_MERGE", True):
            commit_sha, mr_iid = await _submit_via_temp_branch_mr(
                gitlab=gitlab,
                project_id=1,
                project_name="group/repo",
                main_branch="main",
                commit_sha="c" * 40,
                gitlab_actions=[{"action": "update", "file_path": "AI_README.md", "content": "x"}],
                commit_msg="msg",
            )
        assert mr_iid == 42
        assert commit_sha
        gitlab.accept_merge_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mr_creation_no_iid_skips_auto_merge(self):
        gitlab = _make_gitlab_mock(create_mr_return={"web_url": "http://x/mr/none"})
        with patch.object(settings, "README_MR_AUTO_MERGE", True):
            _, mr_iid = await _submit_via_temp_branch_mr(
                gitlab=gitlab,
                project_id=1,
                project_name="group/repo",
                main_branch="main",
                commit_sha="c" * 40,
                gitlab_actions=[{"action": "update", "file_path": "AI_README.md", "content": "x"}],
                commit_msg="msg",
            )
        assert mr_iid is None
        gitlab.accept_merge_request.assert_not_called()
