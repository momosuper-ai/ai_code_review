from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models import get_session
from src.models.review import Review, ReviewComment
from src.utils.rate_limit import rate_limit_dependency
from src.utils.security import require_api_key

router = APIRouter(
    dependencies=[
        Depends(require_api_key),
        Depends(rate_limit_dependency("60/minute")),
    ]
)


@router.get("")
async def list_reviews(
    project_id: int | None = Query(None, description="Filter by GitLab project ID"),
    status: str | None = Query(None, description="Filter by status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """List review records with pagination."""
    query = select(Review).order_by(Review.created_at.desc())

    if project_id is not None:
        query = query.where(Review.gitlab_project_id == project_id)
    if status is not None:
        query = query.where(Review.status == status)

    # Count
    count_query = select(func.count()).select_from(query.subquery())
    total = (await session.execute(count_query)).scalar() or 0

    # Paginate
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(query)
    reviews = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": r.id,
                "project_name": r.project_name,
                "trigger_type": r.trigger_type.value,
                "merge_request_iid": r.merge_request_iid,
                "merge_request_title": r.merge_request_title,
                "commit_sha": r.commit_sha,
                "branch": r.branch,
                "author": r.author,
                "status": r.status.value,
                "files_reviewed": r.files_reviewed,
                "issues_found": r.issues_found,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in reviews
        ],
    }


@router.get("/{review_id}")
async def get_review(
    review_id: int,
    session: AsyncSession = Depends(get_session),
):
    """Get a review with its comments."""
    query = (
        select(Review)
        .options(selectinload(Review.comments))
        .where(Review.id == review_id)
    )
    result = await session.execute(query)
    review = result.scalar_one_or_none()

    if review is None:
        raise HTTPException(status_code=404, detail="Review not found")

    return {
        "id": review.id,
        "project_name": review.project_name,
        "trigger_type": review.trigger_type.value,
        "merge_request_iid": review.merge_request_iid,
        "merge_request_title": review.merge_request_title,
        "commit_sha": review.commit_sha,
        "branch": review.branch,
        "author": review.author,
        "status": review.status.value,
        "summary": review.summary,
        "files_reviewed": review.files_reviewed,
        "issues_found": review.issues_found,
        "error_message": review.error_message,
        "total_prompt_tokens": review.total_prompt_tokens,
        "total_completion_tokens": review.total_completion_tokens,
        "total_cached_tokens": review.total_cached_tokens,
        "created_at": review.created_at.isoformat() if review.created_at else None,
        "completed_at": review.completed_at.isoformat() if review.completed_at else None,
        "comments": [
            {
                "id": c.id,
                "file_path": c.file_path,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "severity": c.severity.value,
                "category": c.category,
                "content": c.content,
                "suggestion": c.suggestion,
                "code_snippet": c.code_snippet,
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in review.comments
        ],
    }
