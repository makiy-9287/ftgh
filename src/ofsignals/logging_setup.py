"""structlog configuration with journald-friendly output and secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

import structlog

# Anything that looks like an API key / bot token gets scrubbed before it can
# reach journald or a log file.
_SECRET_PATTERNS = (
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),   # Telegram bot token
    re.compile(r"\b[A-Za-z0-9]{60,72}\b"),            # Binance key/secret
)


def _redact(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key, value in event_dict.items():
        if isinstance(value, str):
            for pattern in _SECRET_PATTERNS:
                value = pattern.sub("<redacted>", value)
            event_dict[key] = value
    return event_dict


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Console output goes to journald; a rotating JSON file mirrors it."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_dir / "ofsignals.log",
                maxBytes=25 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    # Third-party noise control.
    for noisy in ("ccxt", "asyncio", "httpx", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
