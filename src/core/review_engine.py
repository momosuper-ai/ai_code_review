import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.diff_parser import parse_diff_changes
from src.core.gitlab_client import gitlab_client
from src.core.llm_client import llm_client
from src.core.prompt_builder import build_review_prompt, build_summary_prompt
from src.models import async_session
from src.models.project import Project
from src.models.review import Review, ReviewComment, ReviewStatus, Severity
from src.models.rule import ReviewRule
from src.utils.logger import logger


async def get_project_rules(db: AsyncSession, project_id: int, language: str) -> str | None:
    """获取项目的自定义审查规则。"""
    stmt = select(ReviewRule).where(
        ReviewRule.project_id == project_id,
        ReviewRule.enabled == True,  # noqa: E712
        ReviewRule.language.in_([language, "*"]),
    )
    result = await db.execute(stmt)
    rules = result.scalars().all()
    if not rules:
        return None
    return "\n".join(r.rule_content for r in rules)


def parse_llm_response(response: str) -> dict:
    """解析 LLM 返回的 JSON 结果。"""
    # 尝试提取 JSON 代码块
    if "```json" in response:
        start = response.index("```json") + 7
        end = response.index("```", start)
        response = response[start:end].strip()
    elif "```" in response:
        start = response.index("```") + 3
        end = response.index("```", start)
        response = response[start:end].strip()

    try:
        return json.loads(response)
    except json.JSONDecodeError:
        logger.warning(f"LLM 返回的不是有效 JSON: {response[:200]}")
        return {"comments": [], "summary": "AI 审查结果解析失败"}


async def run_review(project_id: int, mr_iid: int) -> int:
    """执行 AI 代码审查的主流程。

    Args:
        project_id: GitLab 项目 ID
        mr_iid: MR IID

    Returns:
        审查记录 ID
    """
    async with async_session() as db:
        # 查找项目配置
        stmt = select(Project).where(Project.gitlab_project_id == project_id)
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()

        if not project or not project.enabled:
            logger.info(f"项目 {project_id} 未注册或未启用 AI 审查")
            return 0

        # 获取 MR 信息
        mr_info = await gitlab_client.get_mr_info(project_id, mr_iid)

        # 创建审查记录
        review = Review(
            project_id=project.id,
            merge_request_iid=mr_iid,
            merge_request_title=mr_info.get("title", ""),
            author=mr_info.get("author", {}).get("username", "unknown"),
            source_branch=mr_info.get("source_branch", ""),
            target_branch=mr_info.get("target_branch", ""),
            status=ReviewStatus.IN_PROGRESS,
        )
        db.add(review)
        await db.flush()
        review_id = review.id

        try:
            # 获取 MR 变更
            changes = await gitlab_client.get_mr_changes(project_id, mr_iid)
            parsed_changes = parse_diff_changes(changes)

            if not parsed_changes:
                review.status = ReviewStatus.COMPLETED
                review.summary = "无需审查的代码变更"
                review.completed_at = datetime.now(timezone.utc)
                await db.commit()
                return review_id

            base_sha = mr_info.get("diff_refs", {}).get("base_sha", "")
            head_sha = mr_info.get("diff_refs", {}).get("head_sha", "")
            file_summaries = []

            # 逐文件审查
            for change in parsed_changes:
                file_path = change["file_path"]
                diff = change["diff"]

                # 获取项目规则
                from src.core.prompt_builder import detect_language

                lang = detect_language(file_path)
                custom_rules = await get_project_rules(db, project.id, lang)

                # 构建 Prompt 并调用 LLM
                prompt = build_review_prompt(file_path, diff, custom_rules)
                llm_response = await llm_client.review_code(prompt)
                review_result = parse_llm_response(llm_response)

                file_summary = {"file_path": file_path, "summary": review_result.get("summary", "")}
                file_summaries.append(file_summary)

                # 保存评论到数据库并发布到 GitLab
                for comment_data in review_result.get("comments", []):
                    severity_str = comment_data.get("severity", "info")
                    try:
                        severity = Severity(severity_str)
                    except ValueError:
                        severity = Severity.INFO

                    comment = ReviewComment(
                        review_id=review_id,
                        file_path=file_path,
                        line_number=comment_data.get("line"),
                        severity=severity,
                        category=comment_data.get("category", "maintainability"),
                        content=comment_data.get("content", ""),
                        suggestion=comment_data.get("suggestion"),
                    )
                    db.add(comment)

                    # 发布行内评论到 GitLab
                    line_num = comment_data.get("line")
                    if line_num and base_sha and head_sha:
                        severity_emoji = {"critical": "🔴", "major": "🟠", "minor": "🟡", "info": "🔵"}.get(
                            severity_str, "🔵"
                        )
                        body = (
                            f"{severity_emoji} **[{severity_str.upper()}]** {comment_data.get('content', '')}"
                        )
                        if comment_data.get("suggestion"):
                            body += f"\n\n💡 **建议**：{comment_data['suggestion']}"
                        await gitlab_client.post_mr_discussion(
                            project_id, mr_iid, file_path, line_num, body, base_sha, head_sha
                        )

            # 生成总结评论
            summary_prompt = build_summary_prompt(file_summaries)
            summary = await llm_client.review_code(summary_prompt, max_tokens=2048)
            await gitlab_client.post_mr_note(project_id, mr_iid, summary)

            # 添加标签
            await gitlab_client.add_mr_label(project_id, mr_iid, ["ai-reviewed"])

            # 更新审查状态
            review.status = ReviewStatus.COMPLETED
            review.summary = summary
            review.completed_at = datetime.now(timezone.utc)
            await db.commit()

            logger.info(f"AI 审查完成: 项目={project_id}, MR={mr_iid}, 审查ID={review_id}")
            return review_id

        except Exception as e:
            review.status = ReviewStatus.FAILED
            review.error_message = str(e)[:1000]
            await db.commit()
            logger.error(f"AI 审查失败: 项目={project_id}, MR={mr_iid}, 错误={e}")
            raise
