"""Shared structured JSON logging: every line carries service, env, version and commit_sha."""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

TAG_FIELDS = ("service", "env", "version", "commit_sha")


class TagFilter(logging.Filter):
    """Stamps the standard tags onto every log record."""

    def __init__(self, tags: dict[str, str]) -> None:
        super().__init__()
        self.tags = tags

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in self.tags.items():
            setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """Renders a log record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for field in TAG_FIELDS:
            payload[field] = getattr(record, field, None)
        # Extra per-event details, passed as logger.info(..., extra={"fields": {...}}).
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload)


def configure_logging(
    tags: dict[str, str], log_file: str | None = None, name: str = "shoplite"
) -> logging.Logger:
    """Log JSON to the terminal and, if log_file is set, also to that file (for Loki)."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    for old in logger.handlers:
        old.close()
    logger.handlers.clear()
    logger.filters.clear()

    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.addFilter(TagFilter(tags))
    return logger
