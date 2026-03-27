from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings
from src.utils.logger import logger

# Async engine/session，供 FastAPI 使用
engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _create_sync_engine():
    """创建同步数据库引擎。"""
    return create_engine(
        settings.DATABASE_URL_SYNC,
        echo=False,
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_timeout=10,
        connect_args={"connect_timeout": 5},
    )


# 同步 engine/session，供 Celery worker 使用（模块级单例，worker 生命周期内复用）
sync_engine = _create_sync_engine()
SyncSession = sessionmaker(sync_engine, class_=Session, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def ensure_database_exists() -> None:
    """连接默认 postgres 库，确保目标数据库存在。"""
    target_db = settings.DATABASE_URL.rsplit("/", 1)[-1]
    admin_url = settings.DATABASE_URL_SYNC.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            result = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :dbname"),
                {"dbname": target_db},
            )
            if not result.scalar():
                conn.execute(text(f'CREATE DATABASE "{target_db}"'))
                logger.info("数据库 %s 已自动创建", target_db)
    finally:
        admin_engine.dispose()


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
