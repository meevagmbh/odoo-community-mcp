"""Integration tests against a real Odoo 20 (run via scripts/integration.sh)."""

import json
import os
from typing import Any

import anyio
import pytest
from mcp_support import mcp_client, running

from odoo_mcp.app import create_app
from odoo_mcp.config import Settings

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(scope="module")
def seed() -> dict[str, Any]:
    raw = os.environ.get("ODOO_IT_SEED")
    if not raw:
        pytest.skip("ODOO_IT_SEED not set; run scripts/integration.sh")
    result: dict[str, Any] = json.loads(raw)
    return result


@pytest.fixture
def app(seed: dict[str, Any]) -> Any:
    settings = Settings.model_validate(
        {
            "odoo_url": os.environ["ODOO_IT_URL"],
            "odoo_db": "mcptest",
            "odoo_models": "res.partner,res.country",
            "odoo_allow_insecure_http": True,
        }
    )
    return create_app(settings)


async def names(app: Any, key: str) -> set[str]:
    async with mcp_client(app, key) as client:
        result = await client.call_tool(
            "search_records",
            {"model": "res.partner", "domain": [["ref", "like", "mcp-"]], "fields": ["name"]},
        )
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return {record["name"] for record in result.structured_content["records"]}


async def test_each_user_sees_only_records_odoo_allows(app: Any, seed: dict[str, Any]) -> None:
    async with running(app):
        alice = await names(app, seed["keys"]["alice"])
        bob = await names(app, seed["keys"]["bob"])
    assert alice == {"MCP Shared Partner", "MCP Alice Only"}
    assert bob == {"MCP Shared Partner"}


async def test_concurrent_users_on_one_endpoint(app: Any, seed: dict[str, Any]) -> None:
    results: dict[str, list[set[str]]] = {"alice": [], "bob": []}

    async def run(user: str) -> None:
        results[user].append(await names(app, seed["keys"][user]))

    async with running(app), anyio.create_task_group() as tg:
        for _ in range(5):
            tg.start_soon(run, "alice")
            tg.start_soon(run, "bob")

    assert all(r == {"MCP Shared Partner", "MCP Alice Only"} for r in results["alice"])
    assert all(r == {"MCP Shared Partner"} for r in results["bob"])


async def test_read_of_forbidden_record_is_a_clean_error(app: Any, seed: dict[str, Any]) -> None:
    async with running(app), mcp_client(app, seed["keys"]["bob"]) as client:
        result = await client.call_tool(
            "read_records", {"model": "res.partner", "ids": [seed["secret_id"]], "fields": ["name"]}
        )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "permission" in text or "not found" in text
    assert "Traceback" not in text


async def test_invalid_key(app: Any) -> None:
    async with running(app), mcp_client(app, "0" * 40) as client:
        result = await client.call_tool("count_records", {"model": "res.partner"})
    assert result.is_error
    assert "invalid or expired" in result.content[0].text  # type: ignore[union-attr]


async def test_fields_count_and_relation_path(app: Any, seed: dict[str, Any]) -> None:
    async with running(app), mcp_client(app, seed["keys"]["alice"]) as client:
        fields = await client.call_tool("get_fields", {"model": "res.partner"})
        count = await client.call_tool(
            "count_records", {"model": "res.partner", "domain": [["country_id.code", "=", "DE"]]}
        )
        ordered = await client.call_tool(
            "search_records",
            {
                "model": "res.partner",
                "domain": ["|", ["ref", "=", "mcp-shared"], ["ref", "=", "mcp-alice-only"]],
                "fields": ["name", "country_id"],
                "order": "name desc",
            },
        )
    assert fields.structured_content is not None
    assert fields.structured_content["fields"]["country_id"]["relation"] == "res.country"
    assert count.structured_content is not None
    assert count.structured_content["count"] >= 1
    assert ordered.structured_content is not None
    records = ordered.structured_content["records"]
    assert [r["name"] for r in records] == ["MCP Shared Partner", "MCP Alice Only"]
    assert records[0]["country_id"][1] == "Germany"


async def test_model_outside_allowlist(app: Any, seed: dict[str, Any]) -> None:
    async with running(app), mcp_client(app, seed["keys"]["alice"]) as client:
        result = await client.call_tool("search_records", {"model": "res.users"})
    assert result.is_error
    assert "not allowed" in result.content[0].text  # type: ignore[union-attr]
