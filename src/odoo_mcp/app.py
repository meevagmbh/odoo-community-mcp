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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from odoo_mcp.auth import extract_bearer, key_digest, key_fingerprint
from odoo_mcp.config import Settings
from odoo_mcp.keycheck import KeyChecker, Verdict
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


# Every response is JSON for API clients: no caching (it may hold personal data),
# no MIME sniffing, no framing, no referrer, no active content.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
)
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in SECURITY_HEADERS)


class GatewayMiddleware:
    """Security headers, Host/Origin checks, key check with Odoo and per-key rate limit."""

    def __init__(
        self, app: ASGIApp, settings: Settings, limiter: RateLimiter, keys: KeyChecker
    ) -> None:
        self.app = app
        self.settings = settings
        self.limiter = limiter
        self.keys = keys

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _SECURITY_HEADER_NAMES
                ]
                message["headers"] = [*headers, *SECURITY_HEADERS]
            await send(message)

        if scope["path"] != HEALTH_PATH:
            response = await self._check(scope, Headers(scope=scope))
            if response is not None:
                await response(scope, receive, send_with_headers)
                return
        await self.app(scope, receive, send_with_headers)

    async def _check(self, scope: Scope, headers: Headers) -> Response | None:
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
            return _unauthorized("Send your own Odoo API key as 'Authorization: Bearer <key>'.")

        verdict = await self.keys.check(key)
        if verdict is Verdict.REJECTED:
            audit("auth_failed", reason="key_rejected", key_fp=key_fingerprint(key))
            return _unauthorized(
                "Odoo rejected this API key: it is invalid or expired. Create a new key "
                "in Odoo under Preferences > Account Security."
            )
        if verdict is Verdict.THROTTLED:
            audit("rate_limited", reason="key_checks", key_fp=key_fingerprint(key))
            return _too_many(self.keys.retry_after())
        if verdict is Verdict.UNAVAILABLE:
            audit("auth_failed", reason="odoo_unavailable", key_fp=key_fingerprint(key))
            return _json_error(
                503, "unavailable", "Odoo could not check the API key. Try again later."
            )

        retry_after = self.limiter.hit(key_digest(key))
        if retry_after:
            audit("rate_limited", reason="per_key", key_fp=key_fingerprint(key))
            return _too_many(retry_after)
        return None


def _unauthorized(message: str) -> Response:
    return _json_error(
        401, "unauthorized", message, {"WWW-Authenticate": 'Bearer realm="odoo-mcp"'}
    )


def _too_many(retry_after: int) -> Response:
    return _json_error(
        429, "rate_limited", "Too many requests. Slow down.", {"Retry-After": str(retry_after)}
    )


async def healthz(_request: Request) -> Response:
    return JSONResponse({"status": "ok"})


def create_app(
    settings: Settings, *, odoo_transport: httpx2.AsyncBaseTransport | None = None
) -> Starlette:
    install_log_filters()
    modes = {m.rpartition(":")[2] for m in settings.odoo_models if ":" in m}
    if not settings.odoo_mcp_allow_write and modes & {"rw", "rwd"}:
        logger.warning(
            "ODOO_MODELS has ':rw'/':rwd' entries but ODOO_MCP_ALLOW_WRITE is false: "
            "all models are read-only and no write tools are offered"
        )
    elif not settings.odoo_mcp_allow_delete and "rwd" in modes:
        logger.warning(
            "ODOO_MODELS has ':rwd' entries but ODOO_MCP_ALLOW_DELETE is false: "
            "no delete tool is offered"
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
                keys=KeyChecker(
                    odoo,
                    cache_seconds=settings.auth_cache_seconds,
                    attempts_per_minute=settings.auth_attempts_per_minute,
                ),
            )
        ],
        lifespan=lifespan,
    )
