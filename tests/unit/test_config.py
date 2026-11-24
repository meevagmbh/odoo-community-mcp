import pytest
from pydantic import ValidationError

from odoo_mcp.config import Settings

BASE_ENV = {
    "ODOO_URL": "https://odoo.example.com",
    "ODOO_DB": "example",
    "ODOO_MODELS": "res.partner, sale.order ,account.move",
}


def make(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    for name, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)
    return Settings()  # type: ignore[call-arg]


def test_parses_model_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(monkeypatch)
    assert settings.odoo_models == ("res.partner", "sale.order", "account.move")


def test_model_access_and_write_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(monkeypatch, ODOO_MODELS="res.partner, sale.*:rw")
    assert not settings.odoo_mcp_allow_write
    assert not settings.model_access().can_write("sale.order")
    enabled = make(monkeypatch, ODOO_MODELS="res.partner, sale.*:rw", ODOO_MCP_ALLOW_WRITE="true")
    assert enabled.model_access().can_write("sale.order")
    assert not enabled.model_access().can_write("res.partner")


def test_delete_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    write_only = make(monkeypatch, ODOO_MODELS="res.partner:rwd", ODOO_MCP_ALLOW_WRITE="true")
    assert not write_only.odoo_mcp_allow_delete
    assert not write_only.model_access().can_delete("res.partner")
    both = make(
        monkeypatch,
        ODOO_MODELS="res.partner:rwd",
        ODOO_MCP_ALLOW_WRITE="true",
        ODOO_MCP_ALLOW_DELETE="true",
    )
    assert both.model_access().can_delete("res.partner")


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(monkeypatch)
    assert settings.odoo_timeout_seconds == 30
    assert settings.rate_limit_per_minute == 120
    assert settings.mcp_port == 8000
    assert settings.mcp_allowed_hosts == ()
    assert settings.mcp_allowed_origins == ()


def test_odoo_url_trailing_slash_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(monkeypatch, ODOO_URL="https://odoo.example.com/")
    assert settings.odoo_base_url == "https://odoo.example.com"


def test_plain_http_to_odoo_is_rejected_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="https"):
        make(monkeypatch, ODOO_URL="http://odoo.example.com")


def test_plain_http_to_odoo_can_be_allowed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(monkeypatch, ODOO_URL="http://odoo:8069", ODOO_ALLOW_INSECURE_HTTP="true")
    assert settings.odoo_base_url == "http://odoo:8069"


@pytest.mark.parametrize(
    "models", ["", " , ", "res.partner,Bad Model", "res.partner;drop", "res.partner:x", "*:rw"]
)
def test_invalid_model_allowlist_is_rejected(monkeypatch: pytest.MonkeyPatch, models: str) -> None:
    with pytest.raises(ValidationError):
        make(monkeypatch, ODOO_MODELS=models)


def test_missing_required_settings_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BASE_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_no_odoo_key_setting_exists() -> None:
    assert not any("key" in name for name in Settings.model_fields)


def test_lists_are_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make(
        monkeypatch,
        MCP_ALLOWED_HOSTS="mcp.example.com, mcp.example.com:443",
        MCP_ALLOWED_ORIGINS="https://app.example.com",
    )
    assert settings.mcp_allowed_hosts == ("mcp.example.com", "mcp.example.com:443")
    assert settings.mcp_allowed_origins == ("https://app.example.com",)
