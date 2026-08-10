"""Structured logging setup."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from aether.security.redaction import SecretRedactor


_redactor: SecretRedactor | None = None


def set_redactor(redactor: SecretRedactor) -> None:
    global _redactor
    _redactor = redactor


def _redact_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    if _redactor is None:
        return event_dict
    for k, v in list(event_dict.items()):
        if isinstance(v, str):
            event_dict[k] = _redactor.redact(v)
    return event_dict


def setup_logging(level: str = "INFO", json_logs: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO))
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _redact_processor,
        structlog.processors.StackInfoRenderer(),
    ]
    if json_logs:
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper(), logging.INFO)),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "aether") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
