"""End-to-end tests of the ASGI app with a real MCP client and a mocked Odoo."""

import json
import logging
from collections.abc import Callable
from typing import Any

import anyio
import httpx2
import pytest
from mcp import MCPError
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
    "state": {"type": "selection", "string": "State", "required": False, "readonly": True},
    "company_name": {
        "type": "char",
        "string": "Company Name",
        "required": False,
        "readonly": False,
        "related": "parent_id.name",
    },
}

ROWS = {
    KEY_A: [{"id": 1, "name": "Visible to A"}],
    KEY_B: [{"id": 2, "name": "Visible to B"}, {"id": 3, "name": "Also B"}],
}


KEY_CHECK = ("res.users", "context_get")


def unauthorized() -> httpx2.Response:
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


def odoo_calls(odoo: FakeOdoo) -> list[httpx2.Request]:
    """Requests to Odoo other than the key check."""
    return [r for r in odoo.requests if tuple(r.url.path.split("/")[-2:]) != KEY_CHECK]


def key_checks(odoo: FakeOdoo) -> list[httpx2.Request]:
    return [r for r in odoo.requests if tuple(r.url.path.split("/")[-2:]) == KEY_CHECK]


class FakeOdoo:
    """Answers JSON-2 calls like Odoo would, per bearer key."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.delay = 0.0
        self.deny_ir_model = False
        # Keys that pass the key check but that Odoo rejects afterwards (revoked meanwhile).
        self.revoked: set[str] = set()
        # Status code the key check answers with instead of 200, e.g. 503.
        self.key_check_status = 200

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.delay:
            await anyio.sleep(self.delay)
        key = request.headers["authorization"].removeprefix("bearer ")
        if key not in ROWS:
            return unauthorized()
        *_, model, method = request.url.path.split("/")
        if (model, method) == KEY_CHECK:
            if self.key_check_status != 200:
                return httpx2.Response(self.key_check_status, text="unavailable")
            return httpx2.Response(200, json={"lang": "en_US", "tz": "UTC"})
        if key in self.revoked:
            return unauthorized()
        body = json.loads(request.content)
        if model == "ir.model" and self.deny_ir_model:
            return httpx2.Response(403, json={"name": "odoo.exceptions.AccessError"})
        if model == "ir.model" and method == "search_read":
            prefix = body["domain"][0][2].removesuffix("%")
            names = ["sale.order", "sale.order.line"]
            return httpx2.Response(
                200, json=[{"model": n, "name": n} for n in names if n.startswith(prefix)]
            )
        if method == "fields_get":
            return httpx2.Response(200, json=PARTNER_FIELDS)
        if method == "search_read":
            return httpx2.Response(200, json=ROWS[key][: body.get("limit", 80)])
        if method == "search_count":
            return httpx2.Response(200, json=len(ROWS[key]))
        if method == "read":
            return httpx2.Response(200, json=[r for r in ROWS[key] if r["id"] in body["ids"]])
        if method == "create":
            return httpx2.Response(200, json=[42])
        if method in ("write", "unlink"):
            return httpx2.Response(200, json=True)
        if method == "search_read" and model == "ir.model":  # pragma: no cover - handled above
            return httpx2.Response(200, json=[])
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

READ_TOOLS = {"list_models", "get_fields", "search_records", "count_records", "read_records"}
WRITE_TOOLS = {"create_record", "update_records"}


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
    assert odoo_calls(odoo) == []


# --- MCP tools -------------------------------------------------------------


async def test_lists_read_only_tools(make_app: AppFactory) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        tools = (await client.list_tools()).tools
    names = {tool.name for tool in tools}
    assert names == READ_TOOLS
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
    assert result.structured_content == {
        "models": [
            {"model": "res.partner", "access": "read"},
            {"model": "res.country", "access": "read"},
        ]
    }
    assert odoo_calls(odoo) == []


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
        "readonly": False,
    }
    body = json.loads(odoo_calls(odoo)[0].content)
    assert body["attributes"] == [
        "type",
        "string",
        "relation",
        "required",
        "readonly",
        "store",
        "related",
    ]
    assert fields["company_name"]["readonly"] is True


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
    assert odoo_calls(odoo) == []


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


async def test_v5_0_0_6_3_4_unknown_key_is_rejected_before_any_mcp_method(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    bad_key = "f" * 40
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {bad_key}"}
    async with running(make_app()) as app, http(app, headers) as client:
        response = await client.post("/mcp", json=INIT)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert "invalid or expired" in response.json()["message"]
    assert "Traceback" not in response.text
    assert bad_key not in response.text
    assert odoo_calls(odoo) == []


async def test_v5_0_0_6_3_4_list_models_needs_a_key_odoo_accepts(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        await client.call_tool("list_models", {})
    assert len(key_checks(odoo)) == 1
    assert key_checks(odoo)[0].headers["authorization"] == f"bearer {KEY_A}"


async def test_verified_key_is_checked_once_per_cache_period(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    async with running(make_app()) as app, http(app, headers) as client:
        for _ in range(3):
            assert (await client.post("/mcp", json=INIT)).status_code == 200
    assert len(key_checks(odoo)) == 1


async def test_rejected_key_is_not_checked_again(make_app: AppFactory, odoo: FakeOdoo) -> None:
    headers = {**MCP_HEADERS, "Authorization": "Bearer " + "f" * 40}
    async with running(make_app()) as app, http(app, headers) as client:
        codes = [(await client.post("/mcp", json=INIT)).status_code for _ in range(3)]
    assert codes == [401, 401, 401]
    assert len(odoo.requests) == 1


async def test_v5_0_0_2_4_1_rotating_unknown_keys_is_throttled(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    async with running(make_app(auth_attempts_per_minute=2)) as app, http(app, MCP_HEADERS) as c:
        codes = []
        for i in range(4):
            key = f"{i:x}" * 40
            response = await c.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {key}"})
            codes.append(response.status_code)
        known = await c.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {KEY_A}"})
    assert codes == [401, 401, 429, 429]
    assert len(odoo.requests) == 2
    assert known.status_code == 429


async def test_v5_0_0_2_4_1_throttle_spares_keys_already_verified(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    async with running(make_app(auth_attempts_per_minute=1)) as app, http(app, MCP_HEADERS) as c:
        first = await c.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {KEY_A}"})
        bad = await c.post("/mcp", json=INIT, headers={"Authorization": "Bearer " + "f" * 40})
        again = await c.post("/mcp", json=INIT, headers={"Authorization": f"Bearer {KEY_A}"})
    assert (first.status_code, bad.status_code, again.status_code) == (200, 429, 200)
    assert int(bad.headers["retry-after"]) >= 1


async def test_key_check_failure_is_503_and_not_cached(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    async with running(make_app()) as app, http(app, headers) as client:
        odoo.key_check_status = 503
        down = await client.post("/mcp", json=INIT)
        odoo.key_check_status = 200
        up = await client.post("/mcp", json=INIT)
    assert down.status_code == 503
    assert up.status_code == 200
    assert len(key_checks(odoo)) == 2


async def test_key_without_rights_for_the_check_still_counts_as_valid(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {KEY_A}"}
    async with running(make_app()) as app, http(app, headers) as client:
        odoo.key_check_status = 403
        response = await client.post("/mcp", json=INIT)
    assert response.status_code == 200


async def test_key_revoked_after_the_check_gives_clear_tool_error(
    make_app: AppFactory, odoo: FakeOdoo
) -> None:
    odoo.revoked.add(KEY_A)
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("count_records", {"model": "res.partner"})
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "invalid or expired" in text
    assert "Traceback" not in text
    assert KEY_A not in text


SECURITY_HEADERS = {
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


@pytest.mark.parametrize(
    ("method", "path", "auth", "status"),
    [
        ("GET", "/healthz", None, 200),
        ("POST", "/mcp", None, 401),
        ("POST", "/mcp", KEY_A, 200),
        ("GET", "/docs", KEY_A, 404),
    ],
)
async def test_v5_0_0_3_4_3_to_3_4_6_and_14_3_2_security_headers_on_every_response(
    make_app: AppFactory, method: str, path: str, auth: str | None, status: int
) -> None:
    headers = {**MCP_HEADERS, **({"Authorization": f"Bearer {auth}"} if auth else {})}
    async with running(make_app()) as app, http(app, headers) as client:
        response = await client.request(method, path, json=INIT if method == "POST" else None)
    assert response.status_code == status
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get_list(name) == [value], name


@pytest.mark.parametrize(
    "model",
    [
        "sale.x/../res.users",
        "sale.order/unlink?x=1",
        "sale.order#frag",
        "sale.order\n",
        "sale.order%2f..%2fres.users",
        "sale." + "a" * 200,
    ],
)
async def test_v5_0_0_1_2_2_wildcard_model_cannot_leave_the_url_path(
    make_app: AppFactory, odoo: FakeOdoo, model: str
) -> None:
    async with (
        running(make_app(**WRITE_SETTINGS)) as app,
        mcp_client(app, KEY_A) as client,
    ):
        result = await client.call_tool("create_record", {"model": model, "values": {"name": "x"}})
        search = await client.call_tool("search_records", {"model": model})
    assert result.is_error
    assert search.is_error
    assert odoo_calls(odoo) == []


async def test_v5_0_0_14_2_4_rejected_input_stays_out_of_the_audit_log(
    make_app: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        await client.call_tool(
            "search_records", {"model": "res.partner", "order": "jane@example.com desc"}
        )
        await client.call_tool("search_records", {"model": "sale.jane@example.com"})
    events = [r.audit for r in caplog.records if r.name == "odoo_mcp.audit"]
    rejected = [e for e in events if e.get("outcome") == "rejected"]
    assert len(rejected) == 2
    assert all(e["reason"] == "invalid_input" for e in rejected)
    assert rejected[1]["model"] is None
    assert "jane" not in json.dumps(events)


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
        async with http(app, {**MCP_HEADERS, "Authorization": f"Bearer {bad_key}"}) as c:
            await c.post("/mcp", json=INIT)
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
        e["event"] == "auth_failed"
        and e["reason"] == "key_rejected"
        and e["key_fp"] == key_fingerprint(bad_key)
        for e in events
    )


# --- write tools -----------------------------------------------------------

WRITE_SETTINGS = {
    "odoo_models": "res.partner:rw,res.country,sale.*:rw",
    "odoo_mcp_allow_write": True,
}
DELETE_SETTINGS = {
    "odoo_models": "res.partner:rwd,res.country,sale.order:rw",
    "odoo_mcp_allow_write": True,
    "odoo_mcp_allow_delete": True,
}


async def test_write_tools_absent_without_switch(make_app: AppFactory) -> None:
    async with (
        running(make_app(odoo_models="res.partner:rw")) as app,
        mcp_client(app, KEY_A) as client,
    ):
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == READ_TOOLS


async def test_write_tools_absent_without_writable_model(make_app: AppFactory) -> None:
    async with (
        running(make_app(odoo_mcp_allow_write=True)) as app,
        mcp_client(app, KEY_A) as client,
    ):
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == READ_TOOLS


async def test_every_tool_is_fully_annotated(make_app: AppFactory) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    for tool in tools.values():
        hints = tool.annotations
        assert hints is not None, tool.name
        assert hints.title, tool.name
        for value in (
            hints.read_only_hint,
            hints.destructive_hint,
            hints.idempotent_hint,
            hints.open_world_hint,
        ):
            assert value is not None, tool.name
        assert hints.read_only_hint is (tool.name in READ_TOOLS), tool.name
        assert hints.open_world_hint is False, tool.name
        assert tool.title == hints.title, tool.name
    assert tools["create_record"].annotations.destructive_hint is False  # type: ignore[union-attr]
    assert tools["update_records"].annotations.destructive_hint is True  # type: ignore[union-attr]
    assert tools["update_records"].annotations.idempotent_hint is True  # type: ignore[union-attr]
    assert tools["create_record"].annotations.idempotent_hint is False  # type: ignore[union-attr]


async def test_list_models_shows_access_and_expands_wildcards(make_app: AppFactory) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("list_models", {})
    assert result.structured_content == {
        "models": [
            {"model": "res.partner", "access": "read-write"},
            {"model": "res.country", "access": "read"},
            {"model": "sale.order", "access": "read-write"},
            {"model": "sale.order.line", "access": "read-write"},
        ]
    }


async def test_create_record(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "create_record",
            {"model": "res.partner", "values": {"name": "New", "country_id": 57}},
        )
    assert not result.is_error, result.content
    assert result.structured_content == {"id": 42}
    create = odoo.requests[-1]
    assert create.url.path == "/json/2/res.partner/create"
    assert create.headers["authorization"] == f"bearer {KEY_A}"
    assert json.loads(create.content) == {"vals_list": [{"name": "New", "country_id": 57}]}


async def test_update_records(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "update_records",
            {"model": "res.partner", "ids": [1, 1, 2], "values": {"email": "a@example.com"}},
        )
    assert not result.is_error, result.content
    assert result.structured_content == {"ids": [1, 2], "updated": 2}
    write = odoo.requests[-1]
    assert write.url.path == "/json/2/res.partner/write"
    assert json.loads(write.content) == {"ids": [1, 2], "vals": {"email": "a@example.com"}}


async def test_write_to_read_only_model_is_rejected(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "create_record", {"model": "res.country", "values": {"name": "X"}}
        )
    assert result.is_error
    assert "read-only" in result.content[0].text  # type: ignore[union-attr]
    assert odoo_calls(odoo) == []


async def test_write_of_readonly_field_is_rejected(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "update_records", {"model": "res.partner", "ids": [1], "values": {"state": "done"}}
        )
    assert result.is_error
    assert all(not r.url.path.endswith("/write") for r in odoo.requests)


async def test_update_is_capped(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "update_records",
            {"model": "res.partner", "ids": list(range(1, 102)), "values": {"name": "x"}},
        )
    assert result.is_error
    assert all(not r.url.path.endswith("/write") for r in odoo.requests)


async def test_write_audit_logs_field_names_not_values(
    make_app: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        await client.call_tool(
            "update_records",
            {"model": "res.partner", "ids": [1], "values": {"email": "secret@example.com"}},
        )
        await client.call_tool(
            "update_records",
            {"model": "res.partner", "ids": [1], "values": {"x" * 500: "y"}},
        )
        await client.call_tool("create_record", {"model": "res.partner", "values": {"name": "N"}})
    events = [r.audit for r in caplog.records if r.name == "odoo_mcp.audit"]
    writes = [e for e in events if e.get("tool") == "update_records"]
    assert writes[0]["outcome"] == "ok"
    assert writes[0]["fields"] == ["email"]
    assert writes[0]["record_ids"] == [1]
    assert writes[0]["result_count"] == 1
    assert writes[1]["outcome"] == "rejected"
    assert "fields" not in writes[1]
    create = next(e for e in events if e.get("tool") == "create_record")
    assert create["record_ids"] == [42]
    assert "secret@example.com" not in json.dumps(events)
    assert "x" * 100 not in json.dumps(events)


async def test_list_models_falls_back_to_patterns(make_app: AppFactory, odoo: FakeOdoo) -> None:
    odoo.deny_ir_model = True
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("list_models", {})
    assert result.structured_content is not None
    assert {"model": "sale.*", "access": "read-write"} in result.structured_content["models"]


async def test_write_business_error_reaches_client(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async def reject(request: httpx2.Request) -> httpx2.Response:
        odoo.requests.append(request)
        if request.url.path.endswith("/create"):
            return httpx2.Response(
                422,
                json={
                    "name": "odoo.exceptions.ValidationError",
                    "message": "The partner must have a name.",
                    "debug": "Traceback ...",
                },
            )
        return httpx2.Response(200, json=PARTNER_FIELDS)

    app = create_app(settings(**WRITE_SETTINGS), odoo_transport=httpx2.MockTransport(reject))
    async with running(app), mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "create_record", {"model": "res.partner", "values": {"email": "x@example.com"}}
        )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "The partner must have a name." in text
    assert "Traceback" not in text


async def test_business_error_text_stays_out_of_logs(
    make_app: AppFactory, caplog: pytest.LogCaptureFixture
) -> None:
    for name in ("", "mcp", "odoo_mcp"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)

    async def reject(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/create"):
            return httpx2.Response(
                422,
                json={"name": "odoo.exceptions.UserError", "message": "Customer Jane Roe blocked"},
            )
        return httpx2.Response(200, json=PARTNER_FIELDS)

    app = create_app(settings(**WRITE_SETTINGS), odoo_transport=httpx2.MockTransport(reject))
    async with running(app), mcp_client(app, KEY_A) as client:
        result = await client.call_tool(
            "create_record", {"model": "res.partner", "values": {"name": "Jane"}}
        )
        rejected = await client.call_tool(
            "search_records", {"model": "res.partner", "domain": [["secret_term", "=", 1]]}
        )
    assert "Jane Roe" in result.content[0].text  # type: ignore[union-attr]
    assert rejected.is_error
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "Jane Roe" not in log


@pytest.mark.parametrize("tool", ["create_record", "update_records", "delete_records"])
async def test_write_timeout_says_outcome_unknown(tool: str) -> None:
    async def slow(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(("/create", "/write", "/unlink")):
            raise httpx2.ReadTimeout("timed out", request=request)
        return httpx2.Response(200, json=PARTNER_FIELDS)

    app = create_app(settings(**DELETE_SETTINGS), odoo_transport=httpx2.MockTransport(slow))
    arguments: dict[str, Any] = {"model": "res.partner"}
    if tool != "delete_records":
        arguments["values"] = {"name": "N"}
    if tool != "create_record":
        arguments["ids"] = [1]
    async with running(app), mcp_client(app, KEY_A) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "unknown" in text
    assert "before retrying" in text


async def test_startup_warns_when_rw_is_switched_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    create_app(settings(odoo_models="res.partner:rw"))
    assert any("ODOO_MCP_ALLOW_WRITE" in r.getMessage() for r in caplog.records)


# --- delete ----------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"odoo_models": "res.partner:rwd", "odoo_mcp_allow_write": True},
        {"odoo_models": "res.partner:rwd", "odoo_mcp_allow_delete": True},
        {
            "odoo_models": "res.partner:rw",
            **dict.fromkeys(("odoo_mcp_allow_write", "odoo_mcp_allow_delete"), True),
        },
    ],
)
async def test_delete_tool_needs_rwd_and_both_switches(
    make_app: AppFactory, overrides: dict[str, Any]
) -> None:
    async with running(make_app(**overrides)) as app, mcp_client(app, KEY_A) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "delete_records" not in names


async def test_delete_tool_annotations(make_app: AppFactory) -> None:
    async with running(make_app(**DELETE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    hints = tools["delete_records"].annotations
    assert hints is not None
    assert hints.title == tools["delete_records"].title == "Delete records"
    assert hints.read_only_hint is False
    assert hints.destructive_hint is True
    assert hints.idempotent_hint is True
    assert hints.open_world_hint is False
    assert "cannot be undone" in (tools["delete_records"].description or "")


async def test_delete_records(
    make_app: AppFactory, odoo: FakeOdoo, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    async with running(make_app(**DELETE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("delete_records", {"model": "res.partner", "ids": [3, 4]})
    assert not result.is_error, result.content
    assert result.structured_content == {"deleted": [3, 4], "count": 2}
    unlink = odoo.requests[-1]
    assert unlink.url.path == "/json/2/res.partner/unlink"
    assert unlink.headers["authorization"] == f"bearer {KEY_A}"
    assert json.loads(unlink.content) == {"ids": [3, 4]}
    events = [r.audit for r in caplog.records if r.name == "odoo_mcp.audit"]
    delete = next(e for e in events if e.get("tool") == "delete_records")
    assert delete["record_ids"] == [3, 4]
    assert delete["outcome"] == "ok"


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"model": "sale.order", "ids": [1]}, "cannot be deleted"),
        ({"model": "res.country", "ids": [1]}, "cannot be deleted"),
        ({"model": "res.partner", "ids": list(range(1, 22))}, "at most 20"),
    ],
)
async def test_delete_is_refused(
    make_app: AppFactory, odoo: FakeOdoo, arguments: dict[str, Any], fragment: str
) -> None:
    async with running(make_app(**DELETE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.call_tool("delete_records", arguments)
    assert result.is_error
    assert fragment in result.content[0].text  # type: ignore[union-attr]
    assert all(not r.url.path.endswith("/unlink") for r in odoo.requests)


# --- resources -------------------------------------------------------------


async def test_resource_templates_are_listed(make_app: AppFactory) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        templates = (await client.list_resource_templates()).resource_templates
    by_uri = {t.uri_template: t for t in templates}
    assert set(by_uri) == {"odoo://schema/{model}", "odoo://record/{model}/{id}"}
    for template in templates:
        assert template.mime_type == "application/json"
        assert template.title
        assert template.description


async def test_schema_resource(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app(**WRITE_SETTINGS)) as app, mcp_client(app, KEY_A) as client:
        result = await client.read_resource("odoo://schema/res.partner")
    (content,) = result.contents
    assert content.mime_type == "application/json"
    data = json.loads(content.text)  # type: ignore[union-attr]
    assert data["model"] == "res.partner"
    assert data["access"] == "read-write"
    assert data["fields"]["country_id"]["relation"] == "res.country"
    assert odoo.requests[-1].headers["authorization"] == f"bearer {KEY_A}"


async def test_record_resource(make_app: AppFactory, odoo: FakeOdoo) -> None:
    async with running(make_app()) as app, mcp_client(app, KEY_B) as client:
        result = await client.read_resource("odoo://record/res.partner/3")
    (content,) = result.contents
    data = json.loads(content.text)  # type: ignore[union-attr]
    assert data == {"id": 3, "name": "Also B"}
    read = odoo.requests[-1]
    assert read.url.path == "/json/2/res.partner/read"
    assert json.loads(read.content)["ids"] == [3]


@pytest.mark.parametrize(
    ("uri", "fragment"),
    [
        ("odoo://schema/res.users", "not allowed"),
        ("odoo://record/res.users/1", "not allowed"),
        ("odoo://record/res.partner/abc", "record id"),
        ("odoo://record/res.partner/0", "record id"),
        ("odoo://record/res.partner/99", "not found"),
    ],
)
async def test_resource_errors(
    make_app: AppFactory, odoo: FakeOdoo, uri: str, fragment: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with running(make_app()) as app, mcp_client(app, KEY_A) as client:
        with pytest.raises(MCPError, match=fragment):
            await client.read_resource(uri)
    assert all("res.users" not in r.url.path for r in odoo_calls(odoo))
    sdk_lines = [r.getMessage() for r in caplog.records if r.name.startswith("mcp.")]
    assert not any(fragment in line for line in sdk_lines)
