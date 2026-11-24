"""JSON-lines logging to stdout with UTC timestamps and credential redaction."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

AUDIT_LOGGER = "odoo_mcp.audit"

_BEARER = re.compile(r"(bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)


def redact(text: str) -> str:
    return _BEARER.sub(r"\1[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        audit = getattr(record, "audit", None)
        if isinstance(audit, dict):
            entry.update(audit)
        if record.exc_info and record.exc_info[0] is not None:
            # Exception type only: messages and tracebacks may carry upstream data.
            entry["exc_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


class OmitToolErrorText(logging.Filter):
    """The MCP SDK logs each tool error's text. Ours can quote Odoo business messages or
    client input, so only the tool name stays in the log; the audit event has the rest."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg == "Tool %r failed: %r" and isinstance(record.args, tuple):
            record.msg = "Tool %r failed (details in the audit log)"
            record.args = record.args[:1]
        return True


_SDK_TOOL_LOGGER = "mcp.server.mcpserver.server"
_tool_error_filter = OmitToolErrorText()


def install_log_filters() -> None:
    logger = logging.getLogger(_SDK_TOOL_LOGGER)
    if _tool_error_filter not in logger.filters:
        logger.addFilter(_tool_error_filter)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Transport internals are never useful at INFO and could grow verbose.
    for name in ("httpx2", "httpcore2", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True


def audit(event: str, **fields: Any) -> None:
    """Emit one audit event (who = key fingerprint, what, outcome)."""
    logging.getLogger(AUDIT_LOGGER).info(event, extra={"audit": {"event": event, **fields}})
