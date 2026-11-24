"""ASGI application: HTTP guard layer in front of a stateless MCP endpoint."""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from odoo_mcp.auth import extract_bearer, key_digest, key_fingerprint
from odoo_mcp.config import Settings
from odoo_mcp.logging_setup import audit, install_log_filters
from odoo_mcp.odoo_client import VERSION, OdooClient
from odoo_mcp.ratelimit import RateLimiter
from odoo_mcp.tools import register_tools

logger = logging.getLogger(__name__)

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"

INSTRUCTIONS = (
    "Access to an Odoo database with the caller's own permissions. "
    "Start with list_models (it shows which models are read-only and which are writable), "
    "then get_fields for a model, then search_records, count_records or read_records. "
    "If write tools are listed, confirm changes with the user before calling them."
)


def _json_error(
    status: int, error: str, message: str, headers: dict[str, str] | None = None
) -> Response:
    return Response(
        json.dumps({"error": error, "message": message}),
        status_code=status,
        media_type="application/json; charset=utf-8",
        headers=headers,
    )


class GatewayMiddleware:
    """Host/Origin checks, mandatory bearer and per-key rate limit for the MCP path."""

    def __init__(self, app: ASGIApp, settings: Settings, limiter: RateLimiter) -> None:
        self.app = app
        self.settings = settings
        self.limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == HEALTH_PATH:
            await self.app(scope, receive, send)
            return
        response = self._check(scope, Headers(scope=scope))
        if response is not None:
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)

    def _check(self, scope: Scope, headers: Headers) -> Response | None:
        hosts = self.settings.mcp_allowed_hosts
        if hosts and headers.get("host") not in hosts:
            audit("request_rejected", reason="host_not_allowed")
            return _json_error(421, "misdirected_request", "Host not allowed.")

        origin = headers.get("origin")
        if origin is not None and origin not in self.settings.mcp_allowed_origins:
            audit("request_rejected", reason="origin_not_allowed")
            return _json_error(403, "forbidden", "Origin not allowed.")

        if not scope["path"].startswith(MCP_PATH):
            return None

        key = extract_bearer(headers.get("authorization"))
        if key is None:
            audit("auth_failed", reason="missing_or_malformed_bearer")
            return _json_error(
                401,
                "unauthorized",
                "Send your own Odoo API key as 'Authorization: Bearer <key>'.",
                {"WWW-Authenticate": 'Bearer realm="odoo-mcp"'},
            )

        retry_after = self.limiter.hit(key_digest(key))
        if retry_after:
            audit("rate_limited", key_fp=key_fingerprint(key))
            return _json_error(
                429,
                "rate_limited",
                "Too many requests. Slow down.",
                {"Retry-After": str(retry_after)},
            )
        return None


async def healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def create_app(
    settings: Settings, *, odoo_transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    install_log_filters()
    if not settings.odoo_mcp_allow_write and any(m.endswith(":rw") for m in settings.odoo_models):
        logger.warning(
            "ODOO_MODELS has ':rw' entries but ODOO_MCP_ALLOW_WRITE is false: "
            "all models are read-only and no write tools are offered"
        )
    odoo = OdooClient(
        settings.odoo_base_url,
        settings.odoo_db,
        settings.odoo_timeout_seconds,
        max_response_bytes=settings.odoo_max_response_bytes,
        transport=odoo_transport,
    )
    server: MCPServer = MCPServer(
        name="odoo-community-mcp",
        version=VERSION,
        instructions=INSTRUCTIONS,
        log_level=settings.log_level,
    )
    register_tools(server, settings, odoo)
    mcp_app = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        max_request_body_size=settings.mcp_max_body_bytes,
        # Host/Origin checks happen in GatewayMiddleware for every path.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with server.session_manager.run():
            try:
                yield
            finally:
                await odoo.aclose()

    return Starlette(
        routes=[Route(HEALTH_PATH, healthz, methods=["GET"]), Mount("/", app=mcp_app)],
        middleware=[
            Middleware(
                GatewayMiddleware,
                settings=settings,
                limiter=RateLimiter(settings.rate_limit_per_minute),
            )
        ],
        lifespan=lifespan,
    )
