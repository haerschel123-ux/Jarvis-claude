"""Logging with rotation and automatic secret redaction (Spec §63, §105).

Four log files, as the specification requires:

* ``app.log``    — everything at INFO and above
* ``error.log``  — WARNING and above, for quick triage
* ``tools.log``  — the ``jarvis.tools`` logger: every tool invocation and result
* ``audit.log``  — the ``jarvis.audit`` logger: structured JSON lines, one per action

Every record passes through :class:`RedactionFilter`, so a credential that slips into a log
call is masked before it ever reaches disk or the console.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.paths import PATHS
from core.redaction import redact_text, redact_value

ROOT_LOGGER = "jarvis"
TOOLS_LOGGER = "jarvis.tools"
AUDIT_LOGGER = "jarvis.audit"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

_configured = False


class RedactionFilter(logging.Filter):
    """Masks credentials in the message, its arguments and any structured payload."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = redact_value(record.args)
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_text(a) if isinstance(a, str) else a for a in record.args
                )
        payload = getattr(record, "payload", None)
        if payload is not None:
            record.payload = redact_value(payload)
        return True


class ConsoleFormatter(logging.Formatter):
    """Compact, readable console output."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s  %(levelname)-7s %(name)-22s %(message)s", "%H:%M:%S")


class AuditFormatter(logging.Formatter):
    """One JSON object per line, so the audit trail stays machine-readable (Spec §89)."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "message": record.getMessage(),
        }
        payload = getattr(record, "payload", None)
        if isinstance(payload, dict):
            entry.update(payload)
        return json.dumps(entry, ensure_ascii=False, default=str)


def _rotating(path: Path, level: int, formatter: logging.Formatter) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8", delay=True
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    handler.addFilter(RedactionFilter())
    return handler


def setup_logging(debug: bool = False, console: bool = True) -> logging.Logger:
    """Configure the JARVIS logging tree. Idempotent."""
    global _configured
    root = logging.getLogger(ROOT_LOGGER)
    if _configured:
        root.setLevel(logging.DEBUG if debug else logging.INFO)
        return root

    PATHS.ensure()
    log_dir = PATHS.logs
    file_format = logging.Formatter(
        "%(asctime)s  %(levelname)-7s %(name)s  %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.addHandler(_rotating(log_dir / "app.log", logging.INFO, file_format))
    root.addHandler(_rotating(log_dir / "error.log", logging.WARNING, file_format))

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setLevel(logging.DEBUG if debug else logging.INFO)
        stream.setFormatter(ConsoleFormatter())
        stream.addFilter(RedactionFilter())
        root.addHandler(stream)

    tools = logging.getLogger(TOOLS_LOGGER)
    tools.setLevel(logging.INFO)
    tools.addHandler(_rotating(log_dir / "tools.log", logging.INFO, file_format))

    audit = logging.getLogger(AUDIT_LOGGER)
    audit.setLevel(logging.INFO)
    audit.propagate = False  # audit entries belong only in audit.log
    for handler in list(audit.handlers):
        audit.removeHandler(handler)
    audit.addHandler(_rotating(log_dir / "audit.log", logging.INFO, AuditFormatter()))

    # Uvicorn's own loggers are noisy at INFO for every request; keep access logs at WARNING.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
    root.info("Logging initialised (directory: %s)", log_dir)
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a child of the JARVIS root logger."""
    if name.startswith(ROOT_LOGGER):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


def audit(action: str, **fields: Any) -> None:
    """Write one structured audit entry (Spec §89).

    Example::

        audit("tool.completed", agent="computer", tool="mouse_click",
              permission="ALLOW", result="success")
    """
    logging.getLogger(AUDIT_LOGGER).info(action, extra={"payload": fields})


def reset_logging_for_tests() -> None:
    """Tear the logging tree down so a test can reconfigure it against a temp directory."""
    global _configured
    for name in (ROOT_LOGGER, TOOLS_LOGGER, AUDIT_LOGGER):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)
    _configured = False
