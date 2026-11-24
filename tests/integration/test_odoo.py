"""Integration tests against a real Odoo 19+ (run via scripts/integration.sh)."""

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


async def test_default_fields_and_foreign_relations(app: Any, seed: dict[str, Any]) -> None:
    async with running(app), mcp_client(app, seed["keys"]["alice"]) as client:
        default = await client.call_tool(
            "search_records", {"model": "res.partner", "domain": [["ref", "=", "mcp-shared"]]}
        )
        by_id = await client.call_tool(
            "count_records", {"model": "res.partner", "domain": [["user_id", "=", False]]}
        )
        by_name = await client.call_tool(
            "count_records", {"model": "res.partner", "domain": [["user_id", "ilike", "admin"]]}
        )
    assert not default.is_error, default.content
    assert default.structured_content is not None
    (record,) = default.structured_content["records"]
    assert record["name"] == "MCP Shared Partner"
    assert "image_1920" not in record
    assert "child_ids" not in record
    assert not by_id.is_error
    assert by_name.is_error
    assert "res.users" in by_name.content[0].text  # type: ignore[union-attr]


@pytest.fixture
def write_app(seed: dict[str, Any]) -> Any:
    settings = Settings.model_validate(
        {
            "odoo_url": os.environ["ODOO_IT_URL"],
            "odoo_db": "mcptest",
            "odoo_models": "res.partner:rw,res.partner.category:rw,res.country,res.country.*",
            "odoo_mcp_allow_write": True,
            "odoo_allow_insecure_http": True,
        }
    )
    return create_app(settings)


async def test_create_update_and_link(write_app: Any, seed: dict[str, Any]) -> None:
    async with running(write_app), mcp_client(write_app, seed["keys"]["alice"]) as client:
        tag = await client.call_tool(
            "create_record", {"model": "res.partner.category", "values": {"name": "MCP Tag"}}
        )
        assert not tag.is_error, tag.content
        assert tag.structured_content is not None
        tag_id = tag.structured_content["id"]

        created = await client.call_tool(
            "create_record",
            {"model": "res.partner", "values": {"name": "MCP Created", "ref": "mcp-created"}},
        )
        assert not created.is_error, created.content
        assert created.structured_content is not None
        partner_id = created.structured_content["id"]

        updated = await client.call_tool(
            "update_records",
            {
                "model": "res.partner",
                "ids": [partner_id],
                "values": {"email": "created@example.com", "category_id": [[4, tag_id]]},
            },
        )
        assert not updated.is_error, updated.content

        read = await client.call_tool(
            "read_records",
            {"model": "res.partner", "ids": [partner_id], "fields": ["email", "category_id"]},
        )
    assert read.structured_content is not None
    (record,) = read.structured_content["records"]
    assert record["email"] == "created@example.com"
    assert record["category_id"] == [tag_id]


async def test_odoo_still_enforces_rights_on_writes(write_app: Any, seed: dict[str, Any]) -> None:
    async with running(write_app), mcp_client(write_app, seed["keys"]["bob"]) as client:
        result = await client.call_tool(
            "update_records",
            {"model": "res.partner", "ids": [seed["shared_id"]], "values": {"email": "x@x.de"}},
        )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "permission" in text
    assert "Traceback" not in text


async def test_read_only_model_cannot_be_written(write_app: Any, seed: dict[str, Any]) -> None:
    async with running(write_app), mcp_client(write_app, seed["keys"]["alice"]) as client:
        result = await client.call_tool(
            "create_record", {"model": "res.country", "values": {"name": "Atlantis"}}
        )
    assert result.is_error
    assert "read-only" in result.content[0].text  # type: ignore[union-attr]


async def test_business_errors_are_explained(write_app: Any, seed: dict[str, Any]) -> None:
    async with running(write_app), mcp_client(write_app, seed["keys"]["alice"]) as client:
        result = await client.call_tool(
            "create_record", {"model": "res.partner", "values": {"email": "no-name@example.com"}}
        )
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    assert "Odoo says:" in text
    assert "Traceback" not in text
    assert len(text) < 600


async def test_wildcard_shown_as_pattern_without_ir_model_access(
    write_app: Any, seed: dict[str, Any]
) -> None:
    # Odoo 19+ lets only "Access Rights" managers read ir.model, so for a normal user the
    # wildcard stays a pattern; concrete names under it still work.
    async with running(write_app), mcp_client(write_app, seed["keys"]["alice"]) as client:
        listed = await client.call_tool("list_models", {})
        states = await client.call_tool(
            "count_records",
            {"model": "res.country.state", "domain": [["country_id.code", "=", "DE"]]},
        )
    assert listed.structured_content is not None
    models = {m["model"]: m["access"] for m in listed.structured_content["models"]}
    assert models == {
        "res.partner": "read-write",
        "res.partner.category": "read-write",
        "res.country": "read",
        "res.country.*": "read",
    }
    assert not states.is_error, states.content
    assert states.structured_content is not None
    assert states.structured_content["count"] > 0
