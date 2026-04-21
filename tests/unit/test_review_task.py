"""Unit tests for review task dedup helpers."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from src.config import settings
from src.models.review import ReviewStatus
from src.tasks.review_task import _find_recent_review, _should_dispatch_readme


class TestFindRecentReview:
    """验证 (project_id, commit_sha) 近 30 分钟去重查询。"""

    @patch("src.tasks.review_task.SyncSession")
    def test_returns_existing_review(self, mock_session_cls):
        fake_review = MagicMock()
        fake_review.id = 99
        fake_review.status = ReviewStatus.completed
        fake_review.created_at = datetime.utcnow() - timedelta(minutes=5)

        session_ctx = MagicMock()
        session_ctx.execute.return_value.scalar_one_or_none.return_value = fake_review
        mock_session_cls.return_value.__enter__.return_value = session_ctx

        result = _find_recent_review(1, "a" * 40)
        assert result is fake_review

    @patch("src.tasks.review_task.SyncSession")
    def test_returns_none_when_no_match(self, mock_session_cls):
        session_ctx = MagicMock()
        session_ctx.execute.return_value.scalar_one_or_none.return_value = None
        mock_session_cls.return_value.__enter__.return_value = session_ctx

        result = _find_recent_review(1, "a" * 40)
        assert result is None

    @patch("src.tasks.review_task.SyncSession", side_effect=RuntimeError("db down"))
    def test_fail_open_on_exception(self, _mock_session_cls):
        # DB 故障时 fail-open 返回 None，不抛异常
        assert _find_recent_review(1, "a" * 40) is None


class TestShouldDispatchReadme:
    """README 触发矩阵：push 到主分支 或 MR merge 到主分支。"""

    def test_push_to_main_dispatches(self):
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("push", None, "main") is True

    def test_push_to_feature_does_not_dispatch(self):
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("push", None, "feature/foo") is False

    def test_merge_to_main_dispatches(self):
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("merge_request", "merge", "main") is True

    def test_merge_to_feature_does_not_dispatch(self):
        """target_branch 非主分支在 webhook 层已拦截，task 层也兜底。"""
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("merge_request", "merge", "develop") is False

    def test_mr_open_does_not_dispatch(self):
        """虽然 webhook 层已过滤 open，但 task 层仍应防御性校验。"""
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("merge_request", "open", "main") is False

    def test_mr_update_does_not_dispatch(self):
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("merge_request", "update", "main") is False

    def test_mr_reopen_does_not_dispatch(self):
        with patch.object(settings, "README_GENERATION_ENABLED", True):
            assert _should_dispatch_readme("merge_request", "reopen", "main") is False

    def test_respects_disabled_flag(self):
        with patch.object(settings, "README_GENERATION_ENABLED", False):
            assert _should_dispatch_readme("push", None, "main") is False
            assert _should_dispatch_readme("merge_request", "merge", "main") is False
