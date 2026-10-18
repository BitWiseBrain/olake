"""Structured JSON logging with request IDs and latency tracking."""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

# Keys to never log
SENSITIVE_KEYS = {
    "openrouter_api_key", "api_key", "admin_token", "github_token",
    "authorization", "x-api-key", "key",
}


def _redact(data: dict) -> dict:
    return {k: "***" if k.lower() in SENSITIVE_KEYS else v for k, v in data.items()}


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(""),
        }
        if record.exc_info:
            log_data["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra", None)
        if extra:
            log_data.update(_redact(extra) if isinstance(extra, dict) else {"extra": extra})
        return json.dumps(log_data, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for noisy in ("httpx", "httpcore", "uvicorn.access", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def new_request_id() -> str:
    rid = str(uuid.uuid4())[:8]
    request_id_var.set(rid)
    return rid


class LatencyTimer:
    """Track per-stage latency in a single request."""

    def __init__(self) -> None:
        self._marks: dict[str, float] = {}
        self._start = time.perf_counter()

    def mark(self, stage: str) -> None:
        self._marks[stage] = round((time.perf_counter() - self._start) * 1000, 1)

    def to_dict(self) -> dict[str, float]:
        return dict(self._marks)

    def total_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 1)
