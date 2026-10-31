"""Structured logging.

One JSON object per line on stderr so runs are machine-greppable, plus an
optional plain-text mirror for humans reading the terminal.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time


class JsonLogFormatter(logging.Formatter):
    """Minimal JSON lines formatter with support for ``extra={...}``."""

    _RESERVED = set(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    ) | {"message", "asctime", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    value = repr(value)
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None, json_logs: bool | None = None) -> None:
    """Configure the root logger. Idempotent."""
    level = (level or os.environ.get("IRIS_LOG_LEVEL") or "INFO").upper()
    if json_logs is None:
        json_logs = (os.environ.get("IRIS_LOG_JSON", "1").lower()
                     not in {"0", "false", "no"})

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonLogFormatter()
        if json_logs
        else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    # Third-party chatter is not evidence.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
