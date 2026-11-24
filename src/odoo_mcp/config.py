"""Runtime configuration from environment variables.

There is deliberately no setting for an Odoo API key: every request carries
the caller's own key, and the server never holds one.
"""

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

MODEL_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

CommaList = Annotated[tuple[str, ...], NoDecode]


def _split(value: object) -> object:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    odoo_url: str = Field(
        description="Base URL of the Odoo instance, e.g. https://odoo.example.com"
    )
    odoo_db: str = Field(min_length=1, max_length=63, pattern=r"^[A-Za-z0-9_.-]+$")
    odoo_models: CommaList = Field(description="Comma-separated allowlist of Odoo models")
    odoo_timeout_seconds: float = Field(default=30, gt=0, le=300)
    odoo_allow_insecure_http: bool = False
    odoo_max_response_bytes: int = Field(default=10_000_000, gt=0)

    mcp_host: str = "0.0.0.0"  # noqa: S104 - container listens on all interfaces behind a proxy
    mcp_port: int = Field(default=8000, ge=1, le=65535)
    mcp_allowed_hosts: CommaList = ()
    mcp_allowed_origins: CommaList = ()
    mcp_max_body_bytes: int = Field(default=262_144, gt=0)
    rate_limit_per_minute: int = Field(default=120, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("odoo_models", "mcp_allowed_hosts", "mcp_allowed_origins", mode="before")
    @classmethod
    def _split_lists(cls, value: object) -> object:
        return _split(value)

    @field_validator("odoo_models")
    @classmethod
    def _check_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("ODOO_MODELS must list at least one model")
        for model in value:
            if not MODEL_NAME.fullmatch(model):
                raise ValueError(f"invalid model name in ODOO_MODELS: {model!r}")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def _check_url(self) -> Settings:
        url = self.odoo_url.strip()
        if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", url):
            raise ValueError("ODOO_URL must be an absolute http(s) URL without query or fragment")
        if url.startswith("http://") and not self.odoo_allow_insecure_http:
            raise ValueError(
                "ODOO_URL must use https (set ODOO_ALLOW_INSECURE_HTTP=true only for "
                "a trusted internal network)"
            )
        return self

    @property
    def odoo_base_url(self) -> str:
        return self.odoo_url.strip().rstrip("/")
