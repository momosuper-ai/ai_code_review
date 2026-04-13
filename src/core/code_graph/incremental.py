"""基于 git diff 的代码图增量更新。"""

from __future__ import annotations

import os
import subprocess
import time

from src.config import settings
from src.core.code_graph.parser import detect_language, parse_file
from src.core.code_graph.store import CodeGraphStore, DB_FILENAME
from src.utils.logger import logger


# 全量扫描时跳过的目录（与 agent_tools 中保持一致）
_EXCLUDE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    "vendor", ".idea", ".vscode",
}


def _enabled_languages() -> set[str]:
    """从配置中读取启用的语言集合。"""
    raw = getattr(settings, "CODE_GRAPH_LANGUAGES", "python,javascript,typescript,java")
    return {lang.strip() for lang in raw.split(",") if lang.strip()}


def _iter_source_files(repo_dir: str, enabled: set[str], max_files: int) -> list[str]:
    """遍历 ``repo_dir`` 返回受支持语言的源文件相对路径列表。"""
    results: list[str] = []
    for root, dirs, files in os.walk(repo_dir):
        # 原地过滤，避免进入被排除目录
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]
        for name in files:
            if name == DB_FILENAME:
                continue
            lang = detect_language(name)
            if lang is None or lang not in enabled:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, repo_dir).replace("\\", "/")
            results.append(rel)
            if len(results) >= max_files:
                return results
    return results


def _git_diff_files(repo_dir: str, old_sha: str, new_sha: str) -> list[str]:
    """调用 git diff 获取两个 SHA 之间变更的文件列表。"""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{old_sha}..{new_sha}"],
            cwd=repo_dir, capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning("git diff 失败: %s", result.stderr.strip()[:200])
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git diff 异常: %s", exc)
        return []


def _parse_and_store(
    store: CodeGraphStore, repo_dir: str, rel_paths: list[str], enabled: set[str],
) -> tuple[int, int]:
    """解析给定文件列表并写入 store，返回 (已处理文件数, 写入节点数)。"""
    files_ok = 0
    nodes_total = 0
    for rel in rel_paths:
        full = os.path.join(repo_dir, rel)
        if not os.path.isfile(full):
            # 文件被删除：清理旧节点
            store.delete_file(rel.replace("\\", "/"))
            continue
        parsed = parse_file(full, rel, enabled_languages=enabled)
        if parsed is None:
            continue
        nodes_total += store.upsert_file(parsed)
        files_ok += 1
    return files_ok, nodes_total


def update_graph_incremental(
    repo_dir: str,
    old_sha: str | None = None,
    new_sha: str | None = None,
) -> dict:
    """对 ``repo_dir`` 执行增量（或全量）代码图更新。

    - DB 为空或未提供 SHA → 全量扫描
    - 否则 → 基于 git diff 的增量更新

    永不抛异常：失败时记录日志并返回包含 ``error`` 的 summary dict，
    避免阻塞主审查流程。
    """
    if not getattr(settings, "CODE_GRAPH_ENABLED", True):
        return {"skipped": True, "reason": "disabled"}

    if not os.path.isdir(repo_dir):
        return {"skipped": True, "reason": "no_repo_dir"}

    enabled = _enabled_languages()
    max_files = int(getattr(settings, "CODE_GRAPH_MAX_FILES_FULL_SCAN", 5000))

    t0 = time.time()
    try:
        store = CodeGraphStore(repo_dir)
        try:
            store.connect()

            # 决定全量还是增量模式
            full_scan = store.is_empty() or not old_sha or not new_sha
            if full_scan:
                rel_files = _iter_source_files(repo_dir, enabled, max_files)
                if len(rel_files) >= max_files:
                    logger.warning(
                        "仓库文件数超过 CODE_GRAPH_MAX_FILES_FULL_SCAN=%d，跳过首次全量扫描",
                        max_files,
                    )
                    store.commit()
                    return {
                        "skipped": True,
                        "reason": "too_many_files",
                        "total_files": len(rel_files),
                    }
                mode = "full"
            else:
                changed = _git_diff_files(repo_dir, old_sha, new_sha)
                rel_files = [p for p in changed if detect_language(p) in enabled]
                mode = "incremental"

            files_ok, nodes_written = _parse_and_store(store, repo_dir, rel_files, enabled)
            store.commit()
            duration_ms = (time.time() - t0) * 1000
            logger.info(
                "代码图%s更新 文件数=%d 节点数=%d 耗时=%.0f毫秒",
                "全量" if mode == "full" else "增量",
                files_ok, nodes_written, duration_ms,
            )
            return {
                "mode": mode,
                "files_processed": files_ok,
                "nodes_written": nodes_written,
                "duration_ms": duration_ms,
            }
        finally:
            store.close()
    except Exception as exc:
        logger.error("代码图更新失败: %s", exc, exc_info=True)
        return {"error": str(exc)}
