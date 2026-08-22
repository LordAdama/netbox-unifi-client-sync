from __future__ import annotations

import json
import logging


class JsonLogFormatter(logging.Formatter):
    """Emits one JSON object per log line, for log shippers/aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str, log_format: str) -> None:
    handler = logging.StreamHandler()
    if log_format.lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    # Set handlers directly rather than logging.basicConfig(), which is a
    # no-op once the root logger already has a handler (e.g. under pytest,
    # or if some imported library configured logging first) — we want our
    # formatter to take effect unconditionally.
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
