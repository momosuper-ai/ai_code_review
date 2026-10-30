"""Verify database connections belong to individual prefork worker processes."""

import multiprocessing
import os
from multiprocessing.connection import Connection

import pytest
from celery.signals import worker_process_init
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.config import settings
from src.models import SyncSession, sync_engine
from src.tasks.celery_app import celery_app  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("BRAIN_LOCAL_TEST") != "1" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="仅在隔离 PostgreSQL 的 Linux prefork 环境验证",
)


def _child_backend_pid(connection: Connection) -> None:
    worker_process_init.send(sender=None)
    with SyncSession() as session:
        connection.send(session.scalar(text("SELECT pg_backend_pid()")))
    connection.close()


def test_prefork_workers_do_not_reuse_parent_connection() -> None:
    """预热的父进程连接不得被两个子进程继续复用。"""
    url = make_url(settings.DATABASE_URL_SYNC)
    assert (url.host, url.database, url.username) == ("postgres", "obc_brain_local", "brain_local")
    sync_engine.dispose()
    with SyncSession() as session:
        parent_pid = session.scalar(text("SELECT pg_backend_pid()"))
    context = multiprocessing.get_context("fork")
    child_pids = []
    for _ in range(2):
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(target=_child_backend_pid, args=(sending,))
        process.start()
        sending.close()
        try:
            assert receiving.poll(10), "子进程未能建立数据库连接"
            child_pids.append(receiving.recv())
            process.join(10)
            assert process.exitcode == 0
        finally:
            receiving.close()
            if process.is_alive():
                process.terminate()
                process.join(10)
            process.close()
    assert parent_pid not in child_pids, "子进程复用了父进程的 PostgreSQL 连接"
    assert len(set(child_pids)) == 2, "两个子进程共享 PostgreSQL 连接"
    with SyncSession() as session:
        assert session.scalar(text("SELECT pg_backend_pid()")) == parent_pid
