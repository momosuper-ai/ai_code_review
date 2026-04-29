"""Unit tests for webhook handlers."""

from unittest.mock import MagicMock, patch

import pytest

from src.api.v1.webhooks import _handle_mr, _handle_push

_NULL_SHA = "0" * 40


def _push_payload(
    branch="feature/foo",
    before="a" * 40,
    after="b" * 40,
    total_commits=1,
    user_username="dev_user",
):
    payload = {
        "ref": f"refs/heads/{branch}",
        "before": before,
        "after": after,
        "total_commits_count": total_commits,
        "user_name": "dev",
        "project": {
            "id": 1,
            "path_with_namespace": "group/repo",
        },
    }
    if user_username is not None:
        payload["user_username"] = user_username
    return payload


def _mr_payload(
    action="open",
    target_branch="main",
    source_branch="feature/bar",
):
    return {
        "object_attributes": {
            "action": action,
            "target_branch": target_branch,
            "source_branch": source_branch,
            "iid": 42,
            "title": "Test MR",
            "last_commit": {"id": "c" * 40},
        },
        "project": {
            "id": 1,
            "path_with_namespace": "group/repo",
        },
        "user": {"username": "dev"},
    }


class TestHandlePush:
    @patch("src.api.v1.webhooks.run_review", create=True)
    def test_feature_branch_accepted(self, _mock):
        with patch("src.tasks.review_task.run_review") as mock_task:
            mock_task.delay.return_value = MagicMock(id="task-1")
            result = _handle_push(_push_payload(branch="feature/foo"))
        assert result["status"] == "accepted"

    @patch("src.api.v1.webhooks.run_review", create=True)
    def test_main_branch_accepted(self, _mock):
        with patch("src.tasks.review_task.run_review") as mock_task:
            mock_task.delay.return_value = MagicMock(id="task-2")
            result = _handle_push(_push_payload(branch="main"))
        assert result["status"] == "accepted"

    def test_branch_creation_ignored(self):
        result = _handle_push(_push_payload(before=_NULL_SHA))
        assert result["status"] == "ignored"
        assert "branch creation" in result["reason"]

    def test_zero_commits_ignored(self):
        result = _handle_push(_push_payload(total_commits=0))
        assert result["status"] == "ignored"
        assert "no commits" in result["reason"]

    def test_ai_readme_temp_branch_ignored(self):
        """防递归：push 到 ai-readme/ 前缀分支的事件直接 ignored。"""
        result = _handle_push(_push_payload(branch="ai-readme/1234-abc12345"))
        assert result["status"] == "ignored"
        assert result["reason"] == "ai_readme_temp_branch"

    def test_user_username_passed_to_task(self):
        """push 事件应将 user_username 作为 author_username 透传给 run_review。

        审查完成后 commit comment 会用此字段在评论开头插入 @username，
        触发 GitLab mention 邮件通知给提交人。
        """
        with patch("src.tasks.review_task.run_review") as mock_task:
            mock_task.delay.return_value = MagicMock(id="task-u1")
            _handle_push(_push_payload(branch="feature/foo", user_username="alice"))
        assert mock_task.delay.call_args.kwargs["author_username"] == "alice"

    def test_missing_user_username_passes_empty_string(self):
        """payload 不带 user_username 时应透传空串（fail-open）。"""
        with patch("src.tasks.review_task.run_review") as mock_task:
            mock_task.delay.return_value = MagicMock(id="task-u2")
            _handle_push(_push_payload(branch="feature/foo", user_username=None))
        assert mock_task.delay.call_args.kwargs["author_username"] == ""


class TestHandleMR:
    @patch("src.tasks.review_task.run_review")
    def test_merge_accepted_with_target_branch(self, mock_task):
        mock_task.delay.return_value = MagicMock(id="task-5")
        result = _handle_mr(_mr_payload(action="merge"))
        assert result["status"] == "accepted"
        call_kwargs = mock_task.delay.call_args[1]
        assert call_kwargs["branch"] == "main"
        assert call_kwargs["mr_action"] == "merge"

    def test_open_ignored(self):
        """MR open 不再触发审查（新规则：仅 merge 触发）。"""
        result = _handle_mr(_mr_payload(action="open"))
        assert result["status"] == "ignored"
        assert "not reviewable" in result["reason"]

    def test_update_ignored(self):
        result = _handle_mr(_mr_payload(action="update"))
        assert result["status"] == "ignored"
        assert "not reviewable" in result["reason"]

    def test_reopen_ignored(self):
        result = _handle_mr(_mr_payload(action="reopen"))
        assert result["status"] == "ignored"
        assert "not reviewable" in result["reason"]

    def test_close_ignored(self):
        result = _handle_mr(_mr_payload(action="close"))
        assert result["status"] == "ignored"
        assert "not reviewable" in result["reason"]

    def test_target_not_main_branch_ignored(self):
        result = _handle_mr(_mr_payload(action="merge", target_branch="develop"))
        assert result["status"] == "ignored"
        assert "target_branch" in result["reason"]

    def test_ai_readme_source_branch_ignored(self):
        """防递归：source_branch 以 ai-readme/ 开头的 MR 直接 ignored。"""
        payload = _mr_payload(action="merge", source_branch="ai-readme/1234-abc12345")
        result = _handle_mr(payload)
        assert result["status"] == "ignored"
        assert result["reason"] == "ai_readme_mr"

    def test_ai_readme_title_ignored(self):
        """防递归：title 以 [AI-README] 开头的 MR 直接 ignored。"""
        payload = _mr_payload(action="merge")
        payload["object_attributes"]["title"] = "[AI-README] 更新 AI_README"
        result = _handle_mr(payload)
        assert result["status"] == "ignored"
        assert result["reason"] == "ai_readme_mr"


class TestMergePushSuppression:
    """MR merge 同步发送的 push 事件应被抑制，避免重复审查。"""

    @patch("src.api.v1.webhooks._mark_mr_merge")
    @patch("src.tasks.review_task.run_review")
    def test_mr_merge_writes_suppress_mark(self, mock_task, mock_mark):
        mock_task.delay.return_value = MagicMock(id="task-m1")
        _handle_mr(_mr_payload(action="merge"))
        mock_mark.assert_called_once()
        args = mock_mark.call_args[0]
        assert args[0] == 1  # project_id
        assert args[1] == "c" * 40  # commit_sha

    @patch("src.api.v1.webhooks._mark_mr_merge")
    def test_mr_non_merge_does_not_write_mark(self, mock_mark):
        """非 merge action 已在入口 ignored，不会写入 Redis 标记。"""
        _handle_mr(_mr_payload(action="update"))
        mock_mark.assert_not_called()

    @patch("src.api.v1.webhooks._is_mr_merge_dispatched", return_value=True)
    def test_push_suppressed_when_mark_present(self, _mock_check):
        result = _handle_push(_push_payload(branch="main"))
        assert result["status"] == "ignored"
        assert "suppressed by MR merge event" in result["reason"]

    @patch("src.api.v1.webhooks._is_mr_merge_dispatched", return_value=False)
    @patch("src.tasks.review_task.run_review")
    def test_push_processed_when_no_mark(self, mock_task, _mock_check):
        mock_task.delay.return_value = MagicMock(id="task-p1")
        result = _handle_push(_push_payload(branch="main"))
        assert result["status"] == "accepted"
