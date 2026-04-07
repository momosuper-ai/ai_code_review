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
):
    return {
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


class TestHandleMR:
    @patch("src.tasks.review_task.run_review")
    def test_open_accepted_with_source_branch(self, mock_task):
        mock_task.delay.return_value = MagicMock(id="task-3")
        result = _handle_mr(_mr_payload(action="open"))
        assert result["status"] == "accepted"
        call_kwargs = mock_task.delay.call_args[1]
        assert call_kwargs["branch"] == "feature/bar"

    @patch("src.tasks.review_task.run_review")
    def test_update_accepted_with_source_branch(self, mock_task):
        mock_task.delay.return_value = MagicMock(id="task-4")
        result = _handle_mr(_mr_payload(action="update"))
        assert result["status"] == "accepted"
        call_kwargs = mock_task.delay.call_args[1]
        assert call_kwargs["branch"] == "feature/bar"

    @patch("src.tasks.review_task.run_review")
    def test_merge_accepted_with_target_branch(self, mock_task):
        mock_task.delay.return_value = MagicMock(id="task-5")
        result = _handle_mr(_mr_payload(action="merge"))
        assert result["status"] == "accepted"
        call_kwargs = mock_task.delay.call_args[1]
        assert call_kwargs["branch"] == "main"

    @patch("src.tasks.review_task.run_review")
    def test_reopen_accepted(self, mock_task):
        mock_task.delay.return_value = MagicMock(id="task-6")
        result = _handle_mr(_mr_payload(action="reopen"))
        assert result["status"] == "accepted"

    def test_close_ignored(self):
        result = _handle_mr(_mr_payload(action="close"))
        assert result["status"] == "ignored"
        assert "not reviewable" in result["reason"]

    def test_target_not_main_branch_ignored(self):
        result = _handle_mr(_mr_payload(action="open", target_branch="develop"))
        assert result["status"] == "ignored"
        assert "target_branch" in result["reason"]
