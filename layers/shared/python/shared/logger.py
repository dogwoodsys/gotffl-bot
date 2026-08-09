"""Structured JSON logging.

Lambda's root logger defaults to WARNING, so every logger here sets INFO
explicitly. Never use print() — it produces unparseable CloudWatch entries.

No PII: log transaction IDs, week numbers, and counts. Never log rendered post
text, team names, or player names. Rendered text lives in DynamoDB, encrypted
at rest, with a TTL.
"""

import json
import logging
import os
import sys
from typing import Any

_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Keys that must never appear in a log line, however they arrive.
_REDACT = frozenset(
    {"text", "post_text", "body", "content", "player", "player_name", "team", "team_name"}
)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            # Type only. A formatted traceback can carry PII from local variables.
            payload["exception"] = record.exc_info[0].__name__ if record.exc_info[0] else "unknown"

        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = "[redacted]" if key in _REDACT else value

        return json.dumps(payload, default=str, sort_keys=True)


class _Adapter(logging.LoggerAdapter):
    """Routes keyword context into extra_fields so the formatter can redact it."""

    def process(self, msg: str, kwargs: Any) -> tuple[str, Any]:
        fields = kwargs.pop("extra", None) or {}
        kwargs["extra"] = {"extra_fields": {**(self.extra or {}), **fields}}
        return msg, kwargs


def get_logger(name: str, **context: Any) -> _Adapter:
    """A JSON logger at INFO. `context` is attached to every line it emits."""
    base = logging.getLogger(name)
    if not base.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        base.addHandler(handler)
        base.propagate = False
    base.setLevel(_LEVEL)
    return _Adapter(base, context)
