from typing import Any

import pytest

from odoo_mcp.validation import MAX_WRITE_FIELDS, InputError, validate_values

FIELDS: dict[str, dict[str, Any]] = {
    "id": {"type": "integer", "readonly": True},
    "name": {"type": "char", "readonly": False},
    "note": {"type": "text"},
    "description": {"type": "html"},
    "amount": {"type": "monetary"},
    "qty": {"type": "float"},
    "sequence": {"type": "integer"},
    "active": {"type": "boolean"},
    "date_order": {"type": "date"},
    "commitment": {"type": "datetime"},
    "state": {"type": "selection", "readonly": True},
    "priority": {"type": "selection"},
    "partner_id": {"type": "many2one", "relation": "res.partner"},
    "tag_ids": {"type": "many2many", "relation": "crm.tag"},
    "order_line": {"type": "one2many", "relation": "sale.order.line"},
    "image": {"type": "binary"},
    "props": {"type": "properties"},
    "display_name": {"type": "char", "store": False, "readonly": True},
    "company_name": {"type": "char", "related": "company_id.name", "readonly": False},
}


def test_valid_values_pass_through() -> None:
    values = {
        "name": "Acme",
        "note": "multi\nline",
        "description": False,
        "amount": 12.5,
        "qty": 3,
        "sequence": 10,
        "active": True,
        "date_order": "2026-11-25",
        "commitment": "2026-11-25 14:30:00",
        "priority": "1",
        "partner_id": 7,
        "tag_ids": [[4, 3], [3, 2], [6, 0, [1, 2]]],
    }
    assert validate_values(values, FIELDS) == values


def test_relations_can_be_cleared() -> None:
    assert validate_values({"partner_id": False}, FIELDS) == {"partner_id": False}


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ({}, "at least one"),
        ({"nope": 1}, "Unknown field"),
        ({"id": 5}, "read-only"),
        ({"state": "sale"}, "read-only"),
        ({"display_name": "x"}, "read-only"),
        ({"company_name": "x"}, "related"),
        ({"order_line": [[0, 0, {"name": "x"}]]}, "one2many"),
        ({"image": "aGVsbG8="}, "binary"),
        ({"props": {}}, "properties"),
        ({"name": 5}, "name"),
        ({"name": "x" * 100_001}, "name"),
        ({"amount": "12"}, "amount"),
        ({"amount": float("nan")}, "amount"),
        ({"sequence": 1.5}, "sequence"),
        ({"sequence": True}, "sequence"),
        ({"active": "yes"}, "active"),
        ({"date_order": "25.11.2026"}, "date_order"),
        ({"date_order": "2026-13-01"}, "date_order"),
        ({"commitment": "2026-11-25T14:30:00Z"}, "commitment"),
        ({"partner_id": "Acme"}, "partner_id"),
        ({"partner_id": 0}, "partner_id"),
        ({"partner_id": True}, "partner_id"),
        ({"tag_ids": [1, 2]}, "tag_ids"),
        ({"tag_ids": [[2, 5]]}, "tag_ids"),
        ({"tag_ids": [[0, 0, {"name": "x"}]]}, "tag_ids"),
        ({"tag_ids": [[1, 5, {"name": "x"}]]}, "tag_ids"),
        ({"tag_ids": [[5]]}, "tag_ids"),
        ({"tag_ids": [[6, 0, ["a"]]]}, "tag_ids"),
        ({"tag_ids": "4,3"}, "tag_ids"),
    ],
)
def test_invalid_values(values: dict[str, Any], fragment: str) -> None:
    with pytest.raises(InputError, match=fragment):
        validate_values(values, FIELDS)


def test_field_limit() -> None:
    fields = {f"x_{i}": {"type": "char"} for i in range(MAX_WRITE_FIELDS + 1)}
    with pytest.raises(InputError, match="at most"):
        validate_values(dict.fromkeys(fields, "v"), fields)


def test_values_must_be_an_object() -> None:
    with pytest.raises(InputError):
        validate_values([("name", "x")], FIELDS)  # type: ignore[arg-type]
