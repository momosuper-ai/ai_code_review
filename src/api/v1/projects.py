from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import get_db
from src.models.project import Project
from src.models.rule import ReviewRule
from src.schemas.review import ProjectCreate, ProjectOut, RuleCreate, RuleOut

router = APIRouter(prefix="/projects", tags=["Projects"])


@router.get("", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(get_db)):
    """获取所有项目列表。"""
    stmt = select(Project).order_by(Project.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=ProjectOut)
async def create_project(project: ProjectCreate, db: AsyncSession = Depends(get_db)):
    """注册新项目。"""
    # 检查是否已存在
    stmt = select(Project).where(Project.gitlab_project_id == project.gitlab_project_id)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="该 GitLab 项目已注册")

    db_project = Project(**project.model_dump())
    db.add(db_project)
    await db.commit()
    await db.refresh(db_project)
    return db_project


@router.get("/{project_id}/rules", response_model=list[RuleOut])
async def list_rules(project_id: int, db: AsyncSession = Depends(get_db)):
    """获取项目审查规则。"""
    stmt = select(ReviewRule).where(ReviewRule.project_id == project_id).order_by(ReviewRule.language)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("/{project_id}/rules", response_model=RuleOut)
async def create_rule(project_id: int, rule: RuleCreate, db: AsyncSession = Depends(get_db)):
    """创建项目审查规则。"""
    # 验证项目存在
    stmt = select(Project).where(Project.id == project_id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    db_rule = ReviewRule(project_id=project_id, **rule.model_dump())
    db.add(db_rule)
    await db.commit()
    await db.refresh(db_rule)
    return db_rule


@router.put("/{project_id}/rules/{rule_id}", response_model=RuleOut)
async def update_rule(project_id: int, rule_id: int, rule: RuleCreate, db: AsyncSession = Depends(get_db)):
    """更新项目审查规则。"""
    stmt = select(ReviewRule).where(ReviewRule.id == rule_id, ReviewRule.project_id == project_id)
    result = await db.execute(stmt)
    db_rule = result.scalar_one_or_none()
    if not db_rule:
        raise HTTPException(status_code=404, detail="规则不存在")

    db_rule.language = rule.language
    db_rule.rule_content = rule.rule_content
    db_rule.enabled = rule.enabled
    await db.commit()
    await db.refresh(db_rule)
    return db_rule
