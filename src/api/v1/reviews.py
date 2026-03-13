from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import get_db
from src.models.review import Review, ReviewComment, ReviewFeedback, FeedbackType
from src.schemas.review import FeedbackCreate, ReviewListOut, ReviewOut

router = APIRouter(prefix="/reviews", tags=["Reviews"])


@router.get("", response_model=list[ReviewListOut])
async def list_reviews(
    project_id: int | None = None,
    status: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """查询审查记录列表。"""
    stmt = select(Review).order_by(Review.created_at.desc()).limit(limit).offset(offset)
    if project_id:
        stmt = stmt.where(Review.project_id == project_id)
    if status:
        stmt = stmt.where(Review.status == status)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{review_id}", response_model=ReviewOut)
async def get_review(review_id: int, db: AsyncSession = Depends(get_db)):
    """查看审查详情。"""
    stmt = select(Review).where(Review.id == review_id).options(selectinload(Review.comments))
    result = await db.execute(stmt)
    review = result.scalar_one_or_none()
    if not review:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return review


@router.post("/{review_id}/feedback")
async def submit_feedback(review_id: int, feedback: FeedbackCreate, db: AsyncSession = Depends(get_db)):
    """提交人工反馈。"""
    # 验证审查记录存在
    stmt = select(Review).where(Review.id == review_id)
    result = await db.execute(stmt)
    review = result.scalar_one_or_none()
    if not review:
        raise HTTPException(status_code=404, detail="审查记录不存在")

    # 验证评论存在
    stmt = select(ReviewComment).where(ReviewComment.id == feedback.comment_id, ReviewComment.review_id == review_id)
    result = await db.execute(stmt)
    comment = result.scalar_one_or_none()
    if not comment:
        raise HTTPException(status_code=404, detail="审查评论不存在")

    try:
        feedback_type = FeedbackType(feedback.feedback_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的反馈类型，应为 accepted 或 rejected")

    fb = ReviewFeedback(
        review_id=review_id,
        comment_id=feedback.comment_id,
        feedback_type=feedback_type,
        reason=feedback.reason,
    )
    db.add(fb)
    await db.commit()

    return {"message": "反馈已提交"}
