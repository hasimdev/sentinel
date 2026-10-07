"""Structured JSON logging: every line carries service, env, version and commit_sha."""

import json
import logging
from datetime import UTC, datetime

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


def configure_logging(tags: dict[str, str]) -> logging.Logger:
    logger = logging.getLogger("shoplite")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.filters.clear()

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.addFilter(TagFilter(tags))
    return logger
