"""Preserve final HMAC-only audit receipts inside the worker's private volume.

This observer never reads source, packet contexts or model responses and never
contacts a database, provider or network. It does not participate in recipes.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.project_brain.privacy_provenance import MAX_INDEX_BYTES, parse_fingerprint_index

_MAX_MANIFEST_BYTES = 2 * 1024 * 1024


def _child(root: Path, relative: str) -> Path:
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("审计收据路径越界")
    current = path
    while current != root.parent:
        if current.is_symlink():
            raise ValueError("审计收据目录禁止符号链接")
        current = current.parent
    return path


def _atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _manifest(directory: Path, build_id: str) -> tuple[str, str, tuple[int, int]] | None:
    path = _child(directory, "repository/docs/project-brain/manifest.json")
    if not path.exists():
        return None
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise ValueError("审计正式清单过大")
    content = path.read_bytes()
    manifest = json.loads(content)
    commit = manifest.get("source_commit", "")
    if manifest.get("build_id") != build_id or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise ValueError("审计正式清单身份无效")
    identity = directory.stat()
    return commit, hashlib.sha256(content).hexdigest(), (identity.st_dev, identity.st_ino)


def _capture(source: Path, destination: Path, require_index: bool = False) -> dict:
    if not source.is_dir() or not _child(source, "key").exists():
        raise FileNotFoundError("审计隐私源收据尚不可用")
    with FileLock(str(_child(source, "index.lock")), timeout=10):
        key = _child(source, "key").read_bytes()
        index = _child(source, "fingerprints.json")
        if require_index and not index.exists():
            raise FileNotFoundError("审计最终隐私索引已不可用")
        if len(key) != 32 or (index.exists() and index.stat().st_size > MAX_INDEX_BYTES):
            raise ValueError("审计隐私源收据无效")
        raw = index.read_bytes() if index.exists() else b"{}"
        try:
            values, _ = parse_fingerprint_index(raw)
        except ValueError:
            raise ValueError("审计隐私指纹无效") from None
    saved_key = _child(destination, "key")
    if saved_key.exists() and saved_key.read_bytes() != key:
        raise ValueError("审计隐私收据密钥发生变化")
    if not saved_key.exists():
        _atomic(saved_key, key)
    os.chmod(saved_key, 0o600)
    saved_index = _child(destination, "fingerprints.json")
    if not saved_index.exists() or saved_index.read_bytes() != raw:
        _atomic(saved_index, raw)
    os.chmod(saved_index, 0o600)
    return {"fingerprint_count": len(values), "index_sha256": hashlib.sha256(raw).hexdigest(),
            "source_index_present": index.exists()}


def watch_receipt(build_id: str, work_dir: Path | str, artifact_dir: Path | str,
                  timeout_seconds: float = 3600, poll_seconds: float = 0.2) -> dict:
    """Copy validated HMAC state, confirming final assembly or failing closed.

    A captured staging manifest is final only when its directory inode and
    manifest digest match the eventual formal directory. Assembly performs all
    privacy scans before writing that manifest and renames staging atomically.
    An active-only copy cannot become a final receipt after source cleanup.
    """
    if str(UUID(build_id)) != build_id or timeout_seconds <= 0 or not 0.05 <= poll_seconds <= 5:
        raise ValueError("审计收据参数无效")
    work = Path(work_dir)
    artifacts = Path(artifact_dir)
    if not str(work_dir) or not work.is_dir() or not artifacts.is_dir():
        raise ValueError("审计必须使用已有的 worker 私有工作卷")
    if work.resolve() == artifacts.resolve() or work.resolve().is_relative_to(artifacts.resolve()):
        raise ValueError("审计收据不能写入正式产物卷")
    source = _child(work, f"work/{build_id}/privacy")
    destination = _child(work, f"audits/{build_id}/privacy")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (destination, destination.parent, destination.parent.parent):
        os.chmod(directory, 0o700)
    formal = _child(artifacts, f"builds/{build_id}")
    started = time.monotonic()
    last: dict | None = None
    staged: tuple[str, str, tuple[int, int]] | None = None
    saw_source = False

    def result(state: str, marker=None) -> dict:
        report = {"build_id": build_id, "state": state, "final_ledger_captured": state == "final",
                  "fingerprint_count": (last or {}).get("fingerprint_count", 0),
                  "contains_source_or_context": False}
        if marker:
            report.update(source_commit=marker[0], manifest_sha256=marker[1])
        if last:
            report["index_sha256"] = last["index_sha256"]
        _atomic(_child(destination, "audit_receipt.json"), json.dumps(report, sort_keys=True).encode())
        return report

    while time.monotonic() - started < timeout_seconds:
        final_marker = _manifest(formal, build_id) if formal.exists() else None
        candidate = None
        for directory in sorted(_child(artifacts, "builds").glob(build_id + ".tmp-*")):
            marker = _manifest(directory, build_id)
            if marker:
                candidate = marker
        try:
            captured = _capture(source, destination, require_index=final_marker is not None)
            # An absent index is legitimately empty before final rename. It
            # cannot prove emptiness during concurrent source cleanup.
            if not captured["source_index_present"] and formal.exists():
                raise FileNotFoundError("审计最终隐私索引已不可用")
            last = captured
            saw_source = True
            if final_marker:
                return result("final", final_marker)
            if candidate:
                staged = candidate
        except FileNotFoundError:
            if final_marker:
                if last is not None and staged == final_marker:
                    return result("final", final_marker)
                return result("final_ledger_unavailable", final_marker)
            if saw_source and not source.exists():
                return result("source_removed_before_final")
        time.sleep(poll_seconds)
    return result("timeout")


def main() -> None:
    """Observe one explicitly named build using configured private roots."""
    parser = argparse.ArgumentParser(description="worker 私有 HMAC 审计收据观察器")
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    parser.add_argument("--poll-seconds", type=float, default=0.2)
    arguments = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.config import settings

    if not settings.PROJECT_BRAIN_WORK_DIR:
        raise SystemExit("审计收据需要显式 worker 私有 PROJECT_BRAIN_WORK_DIR")
    try:
        report = watch_receipt(arguments.build_id, settings.PROJECT_BRAIN_WORK_DIR,
                               settings.PROJECT_BRAIN_BASE_DIR, arguments.timeout_seconds, arguments.poll_seconds)
    except (ValueError, OSError, json.JSONDecodeError):
        print(json.dumps({"build_id": arguments.build_id, "state": "invalid_or_unavailable_receipt",
                          "final_ledger_captured": False}))
        raise SystemExit(2) from None
    print(json.dumps(report))
    raise SystemExit(0 if report["final_ledger_captured"] else 2)


if __name__ == "__main__":
    main()
