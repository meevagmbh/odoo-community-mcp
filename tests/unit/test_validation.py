from collections.abc import Mapping
from typing import Any

import pytest

from odoo_mcp.validation import (
    MAX_DOMAIN_TERMS,
    InputError,
    check_model,
    validate_domain,
    validate_fields,
    validate_ids,
    validate_limit,
    validate_offset,
    validate_order,
)

pytestmark = pytest.mark.anyio

ALLOWED = ("res.partner", "res.country", "sale.order")

FIELDS: dict[str, dict[str, dict[str, Any]]] = {
    "res.partner": {
        "id": {"type": "integer"},
        "name": {"type": "char"},
        "email": {"type": "char"},
        "is_company": {"type": "boolean"},
        "country_id": {"type": "many2one", "relation": "res.country"},
        "user_id": {"type": "many2one", "relation": "res.users"},
        "image_1920": {"type": "binary"},
    },
    "res.country": {
        "id": {"type": "integer"},
        "code": {"type": "char"},
        "name": {"type": "char"},
    },
}


async def fields_of(model: str) -> Mapping[str, Mapping[str, Any]]:
    return FIELDS[model]


async def check(domain: Any) -> list[Any]:
    return await validate_domain(domain, "res.partner", fields_of, ALLOWED)


# --- model allowlist -------------------------------------------------------


def test_check_model_accepts_allowlisted() -> None:
    check_model("res.partner", ALLOWED)


@pytest.mark.parametrize("model", ["res.users", "ir.config_parameter", "", "res.partner "])
def test_check_model_rejects_others(model: str) -> None:
    with pytest.raises(InputError, match="not allowed"):
        check_model(model, ALLOWED)


# --- limit / offset / ids --------------------------------------------------


def test_limit_defaults_to_50() -> None:
    assert validate_limit(None) == 50


@pytest.mark.parametrize("limit", [1, 50, 500])
def test_limit_in_range(limit: int) -> None:
    assert validate_limit(limit) == limit


@pytest.mark.parametrize("limit", [0, -1, 501, 10_000])
def test_limit_out_of_range(limit: int) -> None:
    with pytest.raises(InputError, match="limit"):
        validate_limit(limit)


def test_offset() -> None:
    assert validate_offset(None) == 0
    assert validate_offset(20) == 20
    with pytest.raises(InputError):
        validate_offset(-1)


def test_ids_valid() -> None:
    assert validate_ids([3, 1, 3]) == [3, 1]


@pytest.mark.parametrize("ids", [[], [0], [-4], [1] * 0 + list(range(1, 502))])
def test_ids_invalid(ids: list[int]) -> None:
    with pytest.raises(InputError):
        validate_ids(ids)


# --- fields ----------------------------------------------------------------


def test_fields_default_excludes_binary() -> None:
    result = validate_fields(None, FIELDS["res.partner"])
    assert "image_1920" not in result
    assert {"id", "name", "email", "country_id"} <= set(result)


def test_fields_explicit() -> None:
    assert validate_fields(["name", "email", "name"], FIELDS["res.partner"]) == ["name", "email"]


@pytest.mark.parametrize("fields", [["nope"], ["country_id.name"], ["name", "password"], []])
def test_fields_unknown_rejected(fields: list[str]) -> None:
    with pytest.raises(InputError):
        validate_fields(fields, FIELDS["res.partner"])


# --- order -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        (None, None),
        ("name", "name"),
        ("name desc", "name desc"),
        ("name DESC, id asc", "name desc, id asc"),
        ("  email   asc ", "email asc"),
    ],
)
def test_order_valid(order: str | None, expected: str | None) -> None:
    assert validate_order(order, FIELDS["res.partner"]) == expected


@pytest.mark.parametrize(
    "order",
    [
        "",
        "nope",
        "name sideways",
        "name desc nulls first",
        "name; DROP TABLE res_partner",
        "country_id.name",
        "(select 1)",
        ",".join(["name"] * 6),
    ],
)
def test_order_invalid(order: str) -> None:
    with pytest.raises(InputError, match="order"):
        validate_order(order, FIELDS["res.partner"])


# --- domain ----------------------------------------------------------------


async def test_empty_domain() -> None:
    assert await check(None) == []
    assert await check([]) == []


async def test_simple_domain() -> None:
    domain = [["name", "ilike", "deco"], ["is_company", "=", True]]
    assert await check(domain) == domain


async def test_prefix_operators() -> None:
    domain = ["|", ["name", "ilike", "a"], "!", ["email", "=", False]]
    assert await check(domain) == domain


async def test_in_operator_with_list_value() -> None:
    domain = [["id", "in", [1, 2, 3]]]
    assert await check(domain) == domain


async def test_dotted_path_through_allowlisted_relation() -> None:
    domain = [["country_id.code", "=", "DE"]]
    assert await check(domain) == domain


async def test_dotted_path_through_foreign_model_is_rejected() -> None:
    with pytest.raises(InputError, match=r"res\.users"):
        await check([["user_id.login", "=", "admin"]])


async def test_dotted_path_through_non_relational_field_is_rejected() -> None:
    with pytest.raises(InputError):
        await check([["name.foo", "=", "x"]])


@pytest.mark.parametrize(
    "domain",
    [
        "name = 1",
        {"name": "x"},
        [["nope", "=", 1]],
        [["name", "=like", "x", "extra"]],
        [["name", "regex", "x"]],
        [["country_id", "any", [["code", "=", "DE"]]]],
        [["country_id", "not any", [["code", "=", "DE"]]]],
        [["name", "=", {"$ne": 1}]],
        [["name", "in", [[1, 2]]]],
        [["name", "=", "x" * 1001]],
        [["id", "in", list(range(1001))]],
        ["|", ["name", "=", "a"]],
        ["&"],
        ["!", "!"],
        [["name", "=", "a"], "|"],
        ["xor", ["name", "=", "a"], ["name", "=", "b"]],
        [[1, "=", "a"]],
        [["", "=", "a"]],
        [["name", "=", 1.5e400]],
    ],
)
async def test_invalid_domains(domain: Any) -> None:
    with pytest.raises(InputError):
        await check(domain)


async def test_domain_term_limit() -> None:
    domain = [["name", "=", str(i)] for i in range(MAX_DOMAIN_TERMS + 1)]
    with pytest.raises(InputError, match="terms"):
        await check(domain)


async def test_tuples_are_normalised_to_lists() -> None:
    assert await check([("name", "=", "a")]) == [["name", "=", "a"]]
