"""Structured JSON logging with request/correlation IDs."""
import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

from . import config

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_var: ContextVar[str] = ContextVar("user", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "service": config.SERVICE_NAME,
            "instance": config.INSTANCE_ID,
            "mode": config.MODE,
            "request_id": request_id_var.get(),
            "user": user_var.get(),
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(entry, default=str)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "psycopg.pool"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger(config.SERVICE_NAME)


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    logging.getLogger(config.SERVICE_NAME).log(level, event, extra={"fields": fields})
