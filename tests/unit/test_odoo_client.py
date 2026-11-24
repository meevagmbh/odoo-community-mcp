import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from odoo_mcp.odoo_client import (
    OdooAccessError,
    OdooAuthError,
    OdooClient,
    OdooError,
    OdooInvalidRequestError,
    OdooNotFoundError,
    OdooUnavailableError,
)

pytestmark = pytest.mark.anyio

KEY = "0123456789abcdef0123456789abcdef01234567"
TRACEBACK = 'Traceback (most recent call last):\n  File "/opt/odoo/http.py", line 1\nSECRET-SQL'


def odoo_error(status: int, name: str, message: str = "boom") -> httpx2.Response:
    return httpx2.Response(
        status,
        json={
            "name": name,
            "message": message,
            "arguments": [message, status],
            "context": {},
            "debug": TRACEBACK,
        },
    )


def make_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> OdooClient:
    return OdooClient(
        base_url="https://odoo.example.com",
        database="exampledb",
        timeout_seconds=5,
        transport=httpx2.MockTransport(handler),
    )


async def test_call_posts_to_json2_with_passthrough_bearer() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=[{"id": 1, "name": "Deco Addict"}])

    client = make_client(handler)
    result = await client.call(KEY, "res.partner", "search_read", {"domain": [], "limit": 5})

    assert result == [{"id": 1, "name": "Deco Addict"}]
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://odoo.example.com/json/2/res.partner/search_read"
    assert request.headers["authorization"] == f"bearer {KEY}"
    assert request.headers["x-odoo-database"] == "exampledb"
    assert request.headers["content-type"].startswith("application/json")
    assert "odoo-community-mcp" in request.headers["user-agent"]
    assert json.loads(request.content) == {"domain": [], "limit": 5}


async def test_each_call_uses_only_its_own_key_and_no_cookies() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200, json=1, headers={"set-cookie": "session_id=user-a-session; Path=/"}
        )

    client = make_client(handler)
    await client.call("a" * 40, "res.partner", "search_count", {"domain": []})
    await client.call("b" * 40, "res.partner", "search_count", {"domain": []})

    assert seen[0].headers["authorization"] == "bearer " + "a" * 40
    assert seen[1].headers["authorization"] == "bearer " + "b" * 40
    assert "cookie" not in seen[1].headers


async def test_redirects_are_not_followed() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(302, headers={"location": "https://evil.example.net/steal"})

    client = make_client(handler)
    with pytest.raises(OdooUnavailableError):
        await client.call(KEY, "res.partner", "search_read", {})
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("response", "error_type", "fragment"),
    [
        (
            odoo_error(401, "werkzeug.exceptions.Unauthorized", "Invalid apikey"),
            OdooAuthError,
            "invalid or expired",
        ),
        (
            odoo_error(403, "odoo.exceptions.AccessError", "You are not allowed"),
            OdooAccessError,
            "res.partner",
        ),
        (odoo_error(404, "werkzeug.exceptions.NotFound"), OdooNotFoundError, "not found"),
        (
            odoo_error(422, "werkzeug.exceptions.UnprocessableEntity"),
            OdooInvalidRequestError,
            "rejected",
        ),
        (odoo_error(200 + 300, "builtins.KeyError"), OdooUnavailableError, "unexpected"),
        (
            odoo_error(503, "werkzeug.exceptions.ServiceUnavailable"),
            OdooUnavailableError,
            "unexpected",
        ),
        (httpx2.Response(502, text="<html>Bad Gateway</html>"), OdooUnavailableError, "unexpected"),
    ],
)
async def test_error_mapping_never_leaks_odoo_details(
    response: httpx2.Response, error_type: type[OdooError], fragment: str
) -> None:
    client = make_client(lambda _request: response)
    with pytest.raises(error_type) as exc_info:
        await client.call(KEY, "res.partner", "search_read", {})

    message = str(exc_info.value)
    assert fragment in message
    for secret in (KEY, "Traceback", "SECRET-SQL", "boom", "Invalid apikey", "not allowed"):
        assert secret not in message
    assert exc_info.value.status == response.status_code


@pytest.mark.parametrize(
    ("name", "error_type"),
    [
        ("odoo.exceptions.AccessError", OdooAccessError),
        ("odoo.exceptions.AccessDenied", OdooAuthError),
        ("odoo.exceptions.MissingError", OdooNotFoundError),
        ("odoo.exceptions.ValidationError", OdooInvalidRequestError),
        ("odoo.exceptions.UserError", OdooInvalidRequestError),
    ],
)
async def test_odoo_exception_names_are_mapped(name: str, error_type: type[OdooError]) -> None:
    client = make_client(lambda _request: odoo_error(400, name))
    with pytest.raises(error_type):
        await client.call(KEY, "res.partner", "read", {"ids": [1]})


async def test_timeout_is_mapped() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    with pytest.raises(OdooUnavailableError, match="did not answer"):
        await make_client(handler).call(KEY, "res.partner", "search_read", {})


async def test_connection_error_is_mapped() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(OdooUnavailableError, match="not reachable"):
        await make_client(handler).call(KEY, "res.partner", "search_read", {})


async def test_invalid_json_body_is_mapped() -> None:
    client = make_client(lambda _request: httpx2.Response(200, text="<html>login</html>"))
    with pytest.raises(OdooUnavailableError):
        await client.call(KEY, "res.partner", "search_read", {})


async def test_oversized_response_is_rejected() -> None:
    big: Any = [{"id": i, "name": "x" * 1000} for i in range(200)]
    client = OdooClient(
        base_url="https://odoo.example.com",
        database="exampledb",
        timeout_seconds=5,
        max_response_bytes=10_000,
        transport=httpx2.MockTransport(lambda _request: httpx2.Response(200, json=big)),
    )
    with pytest.raises(OdooUnavailableError, match="too large"):
        await client.call(KEY, "res.partner", "search_read", {})


@pytest.mark.parametrize("name", ["odoo.exceptions.ValidationError", "odoo.exceptions.UserError"])
async def test_business_error_message_is_passed_on_sanitised(name: str) -> None:
    message = "Missing required fields: Customer\nTraceback (most recent call last): x" + "y" * 600
    client = make_client(lambda _request: odoo_error(422, name, message))
    with pytest.raises(OdooInvalidRequestError) as exc_info:
        await client.call(KEY, "sale.order", "create", {})
    text = str(exc_info.value)
    assert "Missing required fields: Customer" in text
    assert "Traceback" not in text
    assert "SECRET-SQL" not in text
    assert len(text) < 400


async def test_business_error_message_redacts_bearer_tokens() -> None:
    message = f"Invalid header bearer {KEY}"
    client = make_client(lambda _request: odoo_error(422, "odoo.exceptions.UserError", message))
    with pytest.raises(OdooInvalidRequestError) as exc_info:
        await client.call(KEY, "sale.order", "write", {})
    assert KEY not in str(exc_info.value)
