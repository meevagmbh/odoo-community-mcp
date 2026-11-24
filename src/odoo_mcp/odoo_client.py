"""Minimal client for Odoo's JSON-2 API (`POST /json/2/<model>/<method>`).

The client keeps a shared connection pool but no per-user state: no cookies,
no stored keys, no redirects. Every call receives the caller's key explicitly.
Odoo error bodies (message, arguments, traceback) never leave this module.
"""

import json
import logging
import re
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx2

logger = logging.getLogger(__name__)

try:
    VERSION = version("odoo-community-mcp")
except PackageNotFoundError:  # pragma: no cover - only when running from a bare checkout
    VERSION = "0"

USER_AGENT = f"odoo-community-mcp/{VERSION}"


class OdooError(Exception):
    """A failed Odoo call. `str(error)` is safe to show to the MCP client."""

    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.status = status


class OdooAuthError(OdooError):
    pass


class OdooAccessError(OdooError):
    pass


class OdooNotFoundError(OdooError):
    pass


class OdooInvalidRequestError(OdooError):
    pass


class OdooUnavailableError(OdooError):
    pass


# Business errors (missing required field, constraint violated) are meant for users,
# and a client cannot fix a write without them. Only their first line goes out,
# truncated and with anything token-like removed; tracebacks never do.
_BUSINESS_ERRORS = frozenset({"odoo.exceptions.ValidationError", "odoo.exceptions.UserError"})
_MAX_BUSINESS_MESSAGE = 300
_TOKENISH = re.compile(r"(bearer\s+)?[A-Za-z0-9\-._~+/]{24,}=*", re.IGNORECASE)


def _business_message(body: object) -> str | None:
    if not isinstance(body, dict) or body.get("name") not in _BUSINESS_ERRORS:
        return None
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    first = message.strip().splitlines()[0]
    first = _TOKENISH.sub("[REDACTED]", first)
    return first[:_MAX_BUSINESS_MESSAGE]


_BY_NAME: dict[str, type[OdooError]] = {
    "werkzeug.exceptions.Unauthorized": OdooAuthError,
    "odoo.exceptions.AccessDenied": OdooAuthError,
    "odoo.exceptions.AccessError": OdooAccessError,
    "werkzeug.exceptions.Forbidden": OdooAccessError,
    "odoo.exceptions.MissingError": OdooNotFoundError,
    "werkzeug.exceptions.NotFound": OdooNotFoundError,
    "odoo.exceptions.ValidationError": OdooInvalidRequestError,
    "odoo.exceptions.UserError": OdooInvalidRequestError,
    "werkzeug.exceptions.UnprocessableEntity": OdooInvalidRequestError,
    "werkzeug.exceptions.BadRequest": OdooInvalidRequestError,
}

_BY_STATUS: dict[int, type[OdooError]] = {
    401: OdooAuthError,
    403: OdooAccessError,
    404: OdooNotFoundError,
    400: OdooInvalidRequestError,
    422: OdooInvalidRequestError,
}


def _message(kind: type[OdooError], model: str, method: str) -> str:
    if kind is OdooAuthError:
        return (
            "Odoo rejected the API key: it is invalid or expired. "
            "Create a new key in Odoo (Preferences > Account Security)."
        )
    if kind is OdooAccessError:
        return f"Your Odoo user has no permission for this operation on {model}."
    if kind is OdooNotFoundError:
        return f"Odoo reported not found for {model}.{method}: the records may not exist."
    if kind is OdooInvalidRequestError:
        return f"Odoo rejected the request to {model}.{method}; check the arguments."
    return "Odoo returned an unexpected error. Try again later."


class OdooClient:
    def __init__(
        self,
        base_url: str,
        database: str,
        timeout_seconds: float,
        *,
        max_response_bytes: int = 10_000_000,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._database = database
        self._timeout = {
            "connect": timeout_seconds,
            "read": timeout_seconds,
            "write": timeout_seconds,
            "pool": timeout_seconds,
        }
        self._max_response_bytes = max_response_bytes
        # Calling the transport directly (not an AsyncClient) means there is no
        # cookie jar to leak sessions between users and no redirect handling.
        self._transport = transport or httpx2.AsyncHTTPTransport(
            verify=True, http2=False, retries=0, limits=httpx2.Limits(max_connections=100)
        )

    async def aclose(self) -> None:
        await self._transport.aclose()

    async def call(self, api_key: str, model: str, method: str, payload: dict[str, Any]) -> Any:
        request = httpx2.Request(
            "POST",
            f"{self._base_url}/json/2/{model}/{method}",
            headers={
                "Authorization": f"bearer {api_key}",
                "X-Odoo-Database": self._database,
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
            content=json.dumps(payload).encode(),
            extensions={"timeout": self._timeout},
        )
        try:
            response = await self._transport.handle_async_request(request)
            try:
                body = await self._read_body(response)
            finally:
                await response.aclose()
        except httpx2.TimeoutException as exc:
            raise OdooUnavailableError(
                "Odoo did not answer in time. Try again later.", None
            ) from exc
        except httpx2.TransportError as exc:
            logger.warning("Odoo transport error: %s", type(exc).__name__)
            raise OdooUnavailableError("Odoo is not reachable right now.", None) from exc

        status = response.status_code
        if status == 200:
            try:
                return json.loads(body)
            except ValueError as exc:
                raise OdooUnavailableError(
                    _message(OdooUnavailableError, model, method), status
                ) from exc

        parsed = self._parse(body)
        kind = self._classify(status, parsed)
        logger.info(
            "Odoo call %s.%s failed with HTTP %s (%s)", model, method, status, kind.__name__
        )
        message = _message(kind, model, method)
        if kind is OdooInvalidRequestError and (detail := _business_message(parsed)):
            message = f"{message} Odoo says: {detail}"
        raise kind(message, status)

    async def _read_body(self, response: httpx2.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        stream = response.stream
        if not isinstance(stream, httpx2.AsyncByteStream):  # pragma: no cover - transport contract
            raise TypeError("transport returned a synchronous stream")
        async for chunk in stream:
            size += len(chunk)
            if size > self._max_response_bytes:
                raise OdooUnavailableError(
                    "The Odoo response is too large. Request fewer records or fields.",
                    response.status_code,
                )
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse(body: bytes) -> object:
        try:
            return json.loads(body)
        except ValueError:
            return None

    @staticmethod
    def _classify(status: int, parsed: object) -> type[OdooError]:
        name = parsed.get("name") if isinstance(parsed, dict) else None
        if isinstance(name, str) and name in _BY_NAME:
            return _BY_NAME[name]
        return _BY_STATUS.get(status, OdooUnavailableError)
