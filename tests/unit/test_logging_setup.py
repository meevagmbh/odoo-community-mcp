import json
import logging
from typing import Any

import pytest

from odoo_mcp import __main__ as entrypoint
from odoo_mcp.logging_setup import JsonFormatter, audit, configure_logging, redact


def test_redact_removes_bearer_tokens() -> None:
    text = "header Authorization: Bearer abcdef0123456789abcdef and bearer xyz123456789=="
    redacted = redact(text)
    assert "abcdef0123456789" not in redacted
    assert "xyz123456789" not in redacted
    assert redacted.count("[REDACTED]") == 2


def test_json_formatter_emits_utc_and_audit_fields() -> None:
    record = logging.LogRecord("odoo_mcp.audit", logging.INFO, __file__, 1, "tool_call", (), None)
    record.audit = {"event": "tool_call", "key_fp": "deadbeef"}
    entry = json.loads(JsonFormatter().format(record))
    assert entry["ts"].endswith("+00:00")
    assert entry["event"] == "tool_call"
    assert entry["key_fp"] == "deadbeef"


def test_json_formatter_redacts_and_hides_exception_details() -> None:
    exc = RuntimeError("secret upstream detail")
    exc_info = (RuntimeError, exc, None)
    record = logging.LogRecord(
        "x", logging.ERROR, __file__, 1, "failed with Bearer %s", ("a" * 40,), exc_info
    )
    line = JsonFormatter().format(record)
    assert "a" * 40 not in line
    assert "secret upstream detail" not in line
    assert json.loads(line)["exc_type"] == "RuntimeError"


def test_configure_logging_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        configure_logging("INFO")
        audit("tool_call", key_fp="cafebabe", tool="list_models")
        entry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert entry["key_fp"] == "cafebabe"
        assert logging.getLogger("httpx2").level == logging.WARNING
    finally:
        root.handlers[:], level = saved
        root.setLevel(level)


def test_main_starts_uvicorn_without_server_header(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "ODOO_URL": "https://odoo.example.com",
        "ODOO_DB": "example",
        "ODOO_MODELS": "res.partner",
        "MCP_PORT": "9000",
    }.items():
        monkeypatch.setenv(name, value)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda _app, **kw: calls.append(kw))
    monkeypatch.setattr(entrypoint, "configure_logging", lambda _level: None)

    entrypoint.main()

    (kwargs,) = calls
    assert kwargs["port"] == 9000
    assert kwargs["server_header"] is False
    assert kwargs["access_log"] is False
