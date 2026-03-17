import asyncio
import os
import subprocess
import time

from filelock import FileLock

from src.config import settings
from src.utils.logger import logger


class RepoManager:
    def __init__(self, base_dir: str | None = None):
        self.base_dir = base_dir or settings.REPOS_BASE_DIR
        os.makedirs(self.base_dir, exist_ok=True)

    def _repo_dir(self, project_path: str) -> str:
        safe_name = project_path.replace("/", "__")
        return os.path.join(self.base_dir, safe_name)

    def _lock_path(self, repo_dir: str) -> str:
        return repo_dir + ".lock"

    def _clone_url(self, project_path: str) -> str:
        base = settings.GITLAB_URL.rstrip("/")
        return f"{base}/{project_path}.git"

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Inject token via header for HTTPS clone
        env["GIT_ASKPASS"] = "echo"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _credential_url(self, project_path: str) -> str:
        """Build clone URL with embedded OAuth2 token."""
        base = settings.GITLAB_URL.rstrip("/")
        # https://oauth2:TOKEN@host/path.git
        if base.startswith("https://"):
            host_path = base[len("https://"):]
            return f"https://oauth2:{settings.GITLAB_TOKEN}@{host_path}/{project_path}.git"
        elif base.startswith("http://"):
            host_path = base[len("http://"):]
            return f"http://oauth2:{settings.GITLAB_TOKEN}@{host_path}/{project_path}.git"
        return self._clone_url(project_path)

    def _run_git(self, args: list[str], cwd: str | None = None) -> str:
        cmd_str = "git " + " ".join(args)
        logger.debug("执行 Git 命令: %s (cwd=%s)", cmd_str, cwd or ".")
        t0 = time.time()
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=300,
            env=self._git_env(),
        )
        duration_ms = (time.time() - t0) * 1000
        if result.returncode != 0:
            logger.error(
                "Git 命令失败: %s 返回码=%d stderr=%s (%.0f毫秒)",
                cmd_str, result.returncode, result.stderr.strip()[:500], duration_ms,
            )
            raise RuntimeError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}"
            )
        logger.debug("Git 命令完成: %s (%.0f毫秒)", cmd_str, duration_ms)
        return result.stdout.strip()

    async def ensure_repo(self, project_path: str, branch: str) -> str:
        """Clone if not exists, fetch+reset if exists. Returns repo dir."""
        repo_dir = self._repo_dir(project_path)
        lock = FileLock(self._lock_path(repo_dir))

        def _sync():
            logger.debug("获取文件锁: %s", self._lock_path(repo_dir))
            with lock:
                if os.path.isdir(os.path.join(repo_dir, ".git")):
                    logger.info("拉取已有仓库: %s", project_path)
                    self._run_git(["fetch", "origin", branch], cwd=repo_dir)
                    self._run_git(
                        ["reset", "--hard", f"origin/{branch}"], cwd=repo_dir
                    )
                else:
                    logger.info("克隆仓库: %s", project_path)
                    url = self._credential_url(project_path)
                    self._run_git(
                        ["clone", "--branch", branch, "--single-branch", url, repo_dir]
                    )
            return repo_dir

        return await asyncio.to_thread(_sync)

    async def update_repo(self, project_path: str, branch: str) -> None:
        """Fetch and reset to latest origin/branch."""
        repo_dir = self._repo_dir(project_path)
        lock = FileLock(self._lock_path(repo_dir))

        def _sync():
            logger.debug("获取文件锁: %s", self._lock_path(repo_dir))
            with lock:
                self._run_git(["fetch", "origin", branch], cwd=repo_dir)
                self._run_git(
                    ["reset", "--hard", f"origin/{branch}"], cwd=repo_dir
                )

        await asyncio.to_thread(_sync)
