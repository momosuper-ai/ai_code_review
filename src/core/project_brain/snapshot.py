"""Credential-free snapshots exported from pinned Git objects."""

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from filelock import FileLock

_EXCLUDE_DIRS = {
    ".git", "node_modules", "vendor", "__pycache__", ".venv", "venv", "dist",
    "build", "target", ".pytest_cache", ".mypy_cache", ".idea", ".vscode",
}


def safe_relative(path: str) -> str:
    """拒绝绝对路径、遍历、Windows 分隔符和控制字符。"""
    p = PurePosixPath(path)
    if (
        not path or p.is_absolute() or "\\" in path or ":" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(char) < 32 for char in path)
        or any(PureWindowsPath(part).is_reserved() or part.endswith((".", " ")) for part in p.parts)
    ):
        raise ValueError("路径不在允许的相对目录内")
    return path


def source_lines(text: str) -> list[str]:
    """Split physical source lines without treating Unicode literals as breaks."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def allowed_source(path: str) -> bool:
    """统一的源码输入集合，生成文档和秘密文件不参与指纹。"""
    safe_relative(path)
    p = PurePosixPath(path)
    if any(part in _EXCLUDE_DIRS for part in p.parts):
        return False
    if path == "PROJECT_BRAIN.md" or path.startswith("docs/project-brain/"):
        return False
    if p.name == "AI_README.md" or p.name == ".code_graph.db":
        return False
    if p.name.startswith(".env") and p.name != ".env.example":
        return False
    if p.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".crt", ".jks"}:
        return False
    if p.name.startswith("id_rsa") or p.name.startswith("id_ed25519"):
        return False
    # 未知扩展名也记录覆盖与 blob，不能因未适配语言而漏掉新鲜度变化。
    return True


def source_fingerprint(files: dict[str, dict]) -> str:
    """计算所有有效输入路径和 blob 的稳定指纹。"""
    values = sorted((p, v["blob_sha"], v.get("mode", "100644")) for p, v in files.items())
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class RepoSnapshot:
    """一个固定提交的导出目录与文件清单。"""

    path: str
    commit_sha: str
    files: dict[str, dict]
    issues: list[str]
    fingerprint: str


def run_git(args: list[str], cwd: Path | None = None) -> bytes:
    """执行 Git 对象操作，不输出含凭据的命令或 stderr。"""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, timeout=300,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise RuntimeError("Git 快照操作失败，请检查仓库权限和提交是否可获取")
    return result.stdout


def read_snapshot(path: str | Path) -> RepoSnapshot:
    """读取已完成的快照清单。"""
    root = Path(path)
    data = json.loads((root / ".brain-snapshot.json").read_text(encoding="utf-8"))
    return RepoSnapshot(str(root), data["commit_sha"], data["files"], data["issues"], data["fingerprint"])


def export_snapshot(
    cache: Path, destination: Path, commit_sha: str, max_files: int,
) -> RepoSnapshot:
    """仅从 Git blob 导出文件，禁止符号链接、子模块和 LFS 指针。"""
    if not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", commit_sha):
        raise ValueError("必须提供完整 Git 提交 SHA")
    if run_git(["cat-file", "-t", commit_sha], cache).strip() != b"commit":
        raise ValueError("SHA 必须对应 commit 对象")
    entries = run_git(["ls-tree", "-r", "-z", commit_sha], cache).split(b"\0")
    selected: list[tuple[str, str, str]] = []
    skipped: dict[str, dict] = {}
    issues: list[str] = []
    for raw in entries:
        if not raw:
            continue
        header, name = raw.split(b"\t", 1)
        mode, kind, blob = header.decode("ascii").split()
        path = name.decode("utf-8", errors="strict")
        safe_relative(path)
        if mode == "160000" or mode == "120000":
            issues.append(f"跳过子模块或符号链接：{path}")
            if allowed_source(path):
                skipped[path] = {"blob_sha": blob, "mode": mode, "size": 0, "lines": 0, "available": False}
        elif kind == "blob" and allowed_source(path):
            selected.append((path, blob, mode))
    if len(selected) + len(skipped) > max_files:
        raise ValueError(f"源码文件数 {len(selected)} 超过上限 {max_files}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(destination) + ".lock"):
        if (destination / ".brain-snapshot.json").exists():
            existing = read_snapshot(destination)
            if existing.commit_sha != commit_sha:
                raise ValueError("快照提交与构建不一致")
            return existing
        staging = destination.with_name(destination.name + ".tmp-" + uuid4().hex)
        staging.mkdir()
        files: dict[str, dict] = dict(skipped)
        process = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=cache,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            for path, blob, mode in selected:
                process.stdin.write((blob + "\n").encode())
                process.stdin.flush()
                actual, kind, size_text = process.stdout.readline().decode().strip().split()
                size = int(size_text)
                if actual != blob or kind != "blob":
                    raise ValueError("Git 对象响应不匹配")
                if size > 1_048_576:
                    remaining = size
                    while remaining:
                        chunk = process.stdout.read(min(remaining, 65536))
                        if not chunk:
                            raise RuntimeError("Git 对象响应被截断")
                        remaining -= len(chunk)
                    content = None
                    issues.append(f"文件超过 1 MiB，未提取：{path}")
                else:
                    content = process.stdout.read(size)
                process.stdout.read(1)
                # 指纹包含跳过的超大文件/二进制，变化仍会使知识过期。
                files[path] = {"blob_sha": blob, "mode": mode, "size": size, "lines": 0, "available": False}
                if content is None:
                    continue
                if b"\0" in content or content.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    issues.append(f"跳过二进制或 LFS 指针：{path}")
                    continue
                try:
                    text = content.decode("utf-8-sig")
                except UnicodeDecodeError:
                    issues.append(f"文件不是 UTF-8，未提取：{path}")
                    continue
                full = staging / path
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_bytes(content)
                full.chmod(0o444)
                files[path].update(lines=max(1, len(source_lines(text))), available=True)
            data = {
                "commit_sha": commit_sha, "files": files, "issues": issues,
                "fingerprint": source_fingerprint(files),
            }
            (staging / ".brain-snapshot.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            staging.rename(destination)
        except BaseException:
            # 仅清理本次创建且位于目标目录内的暂存目录。
            if staging.is_symlink() or staging.resolve().parent != destination.parent.resolve():
                raise ValueError("拒绝清理越界暂存目录") from None
            shutil.rmtree(staging, onerror=lambda fn, p, info: (os.chmod(p, 0o600), fn(p)))
            raise
        finally:
            process.stdin.close()
            process.stdout.close()
            process.wait(timeout=30)
        return read_snapshot(destination)
