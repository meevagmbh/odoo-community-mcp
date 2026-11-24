"""End-to-end tests of the ASGI app with a real MCP client and a mocked Odoo."""

import json
import logging
from collections.abc import Callable
from typing import Any

import anyio
import httpx2
import pytest
from mcp_support import http, mcp_client, running
from starlette.applications import Starlette

from odoo_mcp.app import create_app
from odoo_mcp.auth import key_fingerprint
from odoo_mcp.config import Settings

pytestmark = pytest.mark.anyio

KEY_A = "a1" * 20
KEY_B = "b2" * 20

PARTNER_FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "string": "ID", "required": False},
    "name": {"type": "char", "string": "Name", "required": True},
    "email": {"type": "char", "string": "Email", "required": False},
    "country_id": {
        "type": "many2one",
        "string": "Country",
        "relation": "res.country",
        "required": False,
    },
    "image_1920": {"type": "binary", "string": "Image", "required": False},
    "user_id": {
        "type": "many2one",
        "string": "Salesperson",
        "relation": "res.users",
        "required": False,
    },
}

ROWS = {
    KEY_A: [{"id": 1, "name": "Visible to A"}],
    KEY_B: [{"id": 2, "name": "Visible to B"}, {"id": 3, "name": "Also B"}],
}


class FakeOdoo:
    """Answers JSON-2 calls like Odoo would, per bearer key."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.delay = 0.0

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.delay:
            await anyio.sleep(self.delay)
        key = request.headers["authorization"].removeprefix("bearer ")
        if key not in ROWS:
            return httpx2.Response(
                401,
                json={
                    "name": "werkzeug.exceptions.Unauthorized",
                    "message": "Invalid apikey",
                    "arguments": ["Invalid apikey", 401],
                    "context": {},
                    "debug": "Traceback (most recent call last): secret internals",
                },
            )
        *_, _model, method = request.url.path.split("/")
        body = json.loads(request.content)
        if method == "fields_get":
            return httpx2.Response(200, json=PARTNER_FIELDS)
        if method == "search_read":
            return httpx2.Response(200, json=ROWS[key][: body.get("limit", 80)])
        if method == "search_count":
            return httpx2.Response(200, json=len(ROWS[key]))
        if method == "read":
            return httpx2.Response(200, json=[r for r in ROWS[key] if r["id"] in body["ids"]])
        return httpx2.Response(404, json={"name": "werkzeug.exceptions.NotFound"})


def settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "odoo_url": "https://odoo.example.com",
        "odoo_db": "exampledb",
        "odoo_models": "res.partner,res.country",
        **overrides,
    }
    return Settings.model_validate(values)


@pytest.fixture
def odoo() -> FakeOdoo:
    return FakeOdoo()


AppFactory = Callable[..., Starlette]


@pytest.fixture
def make_app(odoo: FakeOdoo) -> AppFactory:
    def factory(**overrides: Any) -> Starlette:
        return create_app(settings(**overrides), odoo_transport=httpx2.MockTransport(odoo.handler))

    return factory


INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


# --- HTTP layer ------------------------------------------------------------


async def test_healthz_needs_no_auth_and_reveals_nothing(make_app: AppFactory) -> None:
    async with running(make_app()) as app, http(app) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "server" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic dXNlcjpwYXNz"}, {"Authorization": "Bearer short"}],
)
async def test_mcp_without_valid_bearer_is_401(
    make_app: AppFactory, odoo: FakeOdoo, headers: dict[str, str]
) -> None:
    async with running(make_app()) as app, http(app, {**MCP_HEADERS, **headers}) as client:
        response = await client.post("/mcp", json=INIT)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert response.headers["content-type"].startswith("application/json")
    assert odoo.requests == []


async def test_unknown_path_is_404_even_with_bearer(make_app: AppFactory) -> None:
    async with running(make_app()) as app, http(app, {"Authorization": f"Bearer {KEY_A}"}) as c:
        response = await c.get("/docs")
    assert response.status_code == 404


async def test_rate_limit_per_key(make_app: AppFactory) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    async with running(make_app(rate_limit_per_minute=2)) as app, http(app, headers) as client:
        codes = [(await client.post("/mcp", json=INIT)).status_code for _ in range(3)]
        other = await client.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {KEY_B}"})
    assert codes[:2] == [200, 200]
    assert codes[2] == 429
    assert other.status_code == 200


async def test_rate_limit_response_has_retry_after(make_app: AppFactory) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    async with running(make_app(rate_limit_per_minute=1)) as app, http(app, headers) as client:
        await client.post("/mcp", json=INIT)
        response = await client.post("/mcp", json=INIT)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


async def test_browser_origin_is_rejected_unless_allowed(make_app: AppFactory) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}", "Origin": "https://evil.test"}
    async with running(make_app()) as app, http(app, headers) as client:
        rejected = await client.post("/mcp", json=INIT)
    async with (
        running(make_app(mcp_allowed_origins="https://evil.test")) as app,
        http(app, headers) as client,
    ):
        allowed = await client.post("/mcp", json=INIT)
    assert rejected.status_code == 403
    assert allowed.status_code == 200


async def test_host_allowlist(make_app: AppFactory) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    async with (
        running(make_app(mcp_allowed_hosts="mcp.example.com")) as app,
        http(app, headers) as client,
    ):
        wrong = await client.post("/mcp", json=INIT)
        right = await client.post("/mcp", json=INIT, headers={"Host": "mcp.example.com"})
    assert wrong.status_code == 421
    assert right.status_code == 200


async def test_oversized_body_is_rejected(make_app: AppFactory, odoo: FakeOdoo) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    big = {**INIT, "params": {**INIT["params"], "padding": "x" * 300_000}}  # type: ignore[dict-item]
    async with running(make_app()) as app, http(app, headers) as client:
        response = await client.post("/mcp", json=big)
    assert response.status_code == 413
    assert odoo.requests == []


# --- MCP tools -------------------------------------------------------------


async def test_lists_read_only_tools(make_app: AppFactory) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        tools = (await client.list_tools()).tools
    names = {tool.name for tool in tools}
    assert names == {"list_models", "get_fields", "search_records", "count_records", "read_records"}
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
    search = next(tool for tool in tools if tool.name == "search_records")
    assert search.description is not None
    assert "ilike" in search.description


async def test_list_models_returns_allowlist(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("list_models", {})
    assert not result.is_error
    assert result.structured_content == {"models": ["res.partner", "res.country"]}
    assert odoo.requests == []


async def test_get_fields_returns_reduced_metadata(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("get_fields", {"model": "res.partner"})
    assert not result.is_error
    assert result.structured_content is not None
    fields = result.structured_content["fields"]
    assert fields["country_id"] == {
        "type": "many2one",
        "label": "Country",
        "relation": "res.country",
        "required": False,
    }
    body = json.loads(odoo.requests[0].content)
    assert body["attributes"] == ["type", "string", "relation", "required", "store"]


async def test_search_records_passes_through_callers_key(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "search_records",
            {
                "model": "res.partner",
                "domain": [["name", "ilike", "visible"]],
                "fields": ["name"],
                "limit": 10,
                "order": "name desc",
            },
        )
    assert not result.is_error, result.content
    assert result.structured_content == {"records": ROWS[KEY_A], "count": 1}
    search = odoo.requests[-1]
    assert search.url.path == "/json/2/res.partner/search_read"
    assert search.headers["authorization"] == f"bearer {KEY_A}"
    assert json.loads(search.content) == {
        "domain": [["name", "ilike", "visible"]],
        "fields": ["name"],
        "limit": 10,
        "offset": 0,
        "order": "name desc",
    }


async def test_foreign_many2one_values_are_reduced_to_ids(
    make_app: AppFactory, odoo: FakeOdoo, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = {"id": 1, "name": "A", "user_id": [2, "Mitchell Admin"], "country_id": [57, "Germany"]}
    monkeypatch.setitem(ROWS, KEY_A, [row])
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "search_records", {"model": "res.partner", "fields": ["user_id", "country_id"]}
        )
    assert result.structured_content is not None
    (record,) = result.structured_content["records"]
    assert record["user_id"] == 2
    assert record["country_id"] == [57, "Germany"]


async def test_concurrent_users_only_see_their_own_data(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    odoo.delay = 0.05
    results: dict[str, Any] = {}

    async def run(key: str) -> None:
        async with mcp_client(app, key) as client:
            result = await client.call_tool("search_records", {"model": "res.partner"})
            results[key] = result.structured_content

    async with running(make_app()) as app, anyio.create_task_group() as tg:
        for _ in range(3):
            tg.start_soon(run, KEY_A)
            tg.start_soon(run, KEY_B)

    assert results[KEY_A]["records"] == ROWS[KEY_A]
    assert results[KEY_B]["records"] == ROWS[KEY_B]
    for request in odoo.requests:
        assert request.headers["authorization"] in {f"bearer {KEY_A}", f"bearer {KEY_B}"}


async def test_count_and_read(make_app: AppFactory) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_B) as client:
        count = await client.call_tool("count_records", {"model": "res.partner"})
        read = await client.call_tool(
            "read_records", {"model": "res.partner", "ids": [3], "fields": ["name"]}
        )
    assert count.structured_content == {"count": 2}
    assert read.structured_content == {"records": [{"id": 3, "name": "Also B"}], "count": 1}


async def test_model_outside_allowlist_is_rejected_without_calling_odoo(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("search_records", {"model": "res.users"})
    assert result.is_error
    assert "not allowed" in result.content[0].text  # type: ignore[union-attr]
    assert odoo.requests == []


async def test_invalid_domain_is_a_tool_error(make_app: AppFactory) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "search_records", {"model": "res.partner", "domain": [["password", "=", "x"]]}
        )
    assert result.is_error
    assert "password" in result.content[0].text  # type: ignore[union-attr]


async def test_client_context_is_never_forwarded(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        await client.call_tool("search_records", {"model": "res.partner", "context": {"a": 1}})
    for request in odoo.requests:
        assert "context" not in json.loads(request.content)


async def test_invalid_key_gives_clear_tool_error_without_traceback(make_app: AppFactory) -> None:
    bad_key = "f" * 40
    async with running(make_app()) as app, mcp_client(app, bad_key) as client:
        result = await client.call_tool("count_records", {"model": "res.partner"})
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "invalid or expired" in text
    assert "Traceback" not in text
    assert bad_key not in text


async def test_keys_never_appear_in_logs(
    make_app: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    bad_key = "e" * 40
    for name in ("", "mcp", "httpx2", "httpcore2", "uvicorn", "starlette", "odoo_mcp"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    async with running(make_app()) as app:
        async with mcp_client(app, KEY_A) as client:
            await client.call_tool("search_records", {"model": "res.partner"})
            await client.call_tool("search_records", {"model": "res.users"})
        async with mcp_client(app, bad_key) as client:
            await client.call_tool("count_records", {"model": "res.partner"})
        async with http(app, {**MCP_HEADERS, "Authorization": "Bearer " + "d" * 40}) as c:
            await c.post("/mcp", json=INIT)

    full_log = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for key in (KEY_A, bad_key):
        assert key not in full_log
        assert key[:16] not in full_log

    audit = [r for r in caplog.records if r.name == "odoo_mcp.audit"]
    events = [r.audit for r in audit]
    success = next(e for e in events if e["event"] == "tool_call" and e["outcome"] == "ok")
    assert success["key_fp"] == key_fingerprint(KEY_A)
    assert success["tool"] == "search_records"
    assert success["model"] == "res.partner"
    assert success["result_count"] == 1
    assert success["odoo_status"] == 200
    assert any(e["outcome"] == "rejected" and e["model"] == "res.users" for e in events)
    assert any(
        e["outcome"] == "odoo_error"
        and e["odoo_status"] == 401
        and e["key_fp"] == key_fingerprint(bad_key)
        for e in events
    )
