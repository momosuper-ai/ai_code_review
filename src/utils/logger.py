import json
import logging
import logging.handlers
import os
import sys

from src.config import settings


class JSONFormatter(logging.Formatter):
    """输出 JSON Lines 格式，每行一个 JSON 对象。"""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict = {
            "ts": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if hasattr(record, "ctx"):
            log_data.update(record.ctx)
        if record.exc_info and record.exc_info[0] is not None:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data, ensure_ascii=False)


def with_context(**ctx) -> dict:
    """返回可传给 logger.info(..., extra={"ctx": {...}}) 的 extra 字典。

    用法:
        logger.info("消息", extra=with_context(review_id=7, duration_ms=120))
    """
    return {"ctx": ctx}


def setup_logger(name: str = "obc_review") -> logging.Logger:
    _logger = logging.getLogger(name)
    if _logger.handlers:
        return _logger

    _logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO))

    # 选择格式化器
    if settings.LOG_FORMAT == "json":
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    # stdout handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    _logger.addHandler(stream_handler)

    # 可选的文件 handler（RotatingFileHandler）
    if settings.LOG_FILE:
        log_dir = os.path.dirname(settings.LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            settings.LOG_FILE,
            maxBytes=settings.LOG_FILE_MAX_BYTES,
            backupCount=settings.LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        _logger.addHandler(file_handler)

    return _logger


logger = setup_logger()
