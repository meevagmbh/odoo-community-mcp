"""Positive validation of everything a client may send towards Odoo.

Odoo enforces access rights itself. This layer narrows the surface further:
only allowlisted models, known fields, a small domain grammar and bounded sizes.
"""

import datetime
import math
import re
from collections.abc import Awaitable, Callable, Container, Mapping, Sequence
from typing import Any

FieldMap = Mapping[str, Mapping[str, Any]]
FieldsLookup = Callable[[str], Awaitable[FieldMap]]

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
MAX_OFFSET = 1_000_000
MAX_IDS = 500
MAX_FIELDS = 100
MAX_ORDER_PARTS = 5
MAX_DOMAIN_TERMS = 30
MAX_PATH_HOPS = 3
MAX_STRING_LENGTH = 1000
MAX_LIST_VALUES = 1000

_FIELD_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_ORDER_PART = re.compile(r"^([a-z_][a-z0-9_]{0,62})(?:\s+(asc|desc))?$", re.IGNORECASE)

LOGICAL_OPERATORS = {"&": 2, "|": 2, "!": 1}
TERM_OPERATORS = frozenset(
    {
        "=", "!=", ">", ">=", "<", "<=", "=?",
        "like", "not like", "ilike", "not ilike", "=like", "=ilike",
        "in", "not in", "child_of", "parent_of",
    }
)  # fmt: skip
RELATIONAL_TYPES = frozenset({"many2one", "one2many", "many2many"})
# Default field list: cheap, stored, scalar-ish fields only.
DEFAULT_EXCLUDED_TYPES = frozenset(
    {"binary", "one2many", "many2many", "html", "properties", "properties_definition"}
)
# Relations to non-allowlisted models may only be compared by id: a string value
# would make Odoo search names in the foreign model, bypassing the allowlist.
ID_ONLY_OPERATORS = frozenset({"=", "!=", "in", "not in"})

Scalar = str | int | float | bool | None

# Writes: only plain field types; x2many writes only as link commands on many2many.
MAX_WRITE_FIELDS = 100
MAX_WRITE_STRING = 100_000
TEXT_TYPES = frozenset({"char", "text", "html", "selection"})
WRITABLE_TYPES = TEXT_TYPES | {
    "integer", "float", "monetary", "boolean", "date", "datetime", "many2one", "many2many"
}  # fmt: skip
# Command.link (4, id), Command.unlink (3, id), Command.set (6, 0, ids). Never create,
# update, delete or clear-and-delete commands: those change or remove other records.
M2M_COMMANDS = frozenset({3, 4, 6})


class InputError(ValueError):
    """Client input that violates the gateway's rules. Safe to show to the client."""


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def check_model(model: str, allowed: Container[str]) -> None:
    if model not in allowed:
        raise InputError(
            f"Model {_short(model)} is not allowed. Use list_models to see the allowed models."
        )


def validate_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not 1 <= limit <= MAX_LIMIT:
        raise InputError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def validate_offset(offset: int | None) -> int:
    if offset is None:
        return 0
    if not 0 <= offset <= MAX_OFFSET:
        raise InputError(f"offset must be between 0 and {MAX_OFFSET}")
    return offset


def validate_ids(ids: Sequence[int]) -> list[int]:
    unique = list(dict.fromkeys(ids))
    if not unique:
        raise InputError("ids must contain at least one record id")
    if len(unique) > MAX_IDS:
        raise InputError(f"ids may contain at most {MAX_IDS} record ids")
    if any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in unique):
        raise InputError("ids must be positive integers")
    return unique


def validate_fields(fields: Sequence[str] | None, available: FieldMap) -> list[str]:
    """Return the field list to request; default: every non-binary field."""
    if fields is None:
        return [
            name
            for name, meta in available.items()
            if meta.get("type") not in DEFAULT_EXCLUDED_TYPES and meta.get("store", True)
        ][:MAX_FIELDS]
    unique = list(dict.fromkeys(fields))
    if not unique:
        raise InputError("fields must not be empty; omit it to get the default fields")
    if len(unique) > MAX_FIELDS:
        raise InputError(f"fields may contain at most {MAX_FIELDS} names")
    unknown = [name for name in unique if name not in available]
    if unknown:
        raise InputError(
            f"Unknown field(s) {_short(unknown)}. Use get_fields to see the available fields."
        )
    return unique


def validate_order(order: str | None, available: FieldMap, allowed: Container[str]) -> str | None:
    if order is None:
        return None
    parts = [part.strip() for part in order.split(",")]
    if not 1 <= len(parts) <= MAX_ORDER_PARTS:
        raise InputError(f"order may name between 1 and {MAX_ORDER_PARTS} fields")
    normalised = []
    for part in parts:
        match = _ORDER_PART.fullmatch(part)
        if not match or match.group(1) not in available:
            raise InputError(
                f"Invalid order {_short(part)}: use 'field', 'field asc' or 'field desc' "
                "with fields of this model, separated by commas"
            )
        field, direction = match.group(1), match.group(2)
        relation = available[field].get("relation")
        if available[field].get("type") in RELATIONAL_TYPES and relation not in allowed:
            # Odoo would sort by the related record's name, which belongs to another model.
            raise InputError(
                f"Cannot order by {field}: it points to model {relation}, which is not allowed"
            )
        normalised.append(f"{field} {direction.lower()}" if direction else field)
    return ", ".join(normalised)


def _check_value(value: object) -> None:
    if isinstance(value, list | tuple):
        if len(value) > MAX_LIST_VALUES:
            raise InputError(f"Domain value lists may contain at most {MAX_LIST_VALUES} items")
        for item in value:
            if isinstance(item, list | tuple):
                raise InputError("Domain values must be scalars or flat lists of scalars")
            _check_value(item)
        return
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InputError("Domain values must be finite numbers")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_LENGTH:
            raise InputError(f"Domain strings may be at most {MAX_STRING_LENGTH} characters")
        return
    raise InputError("Domain values must be strings, numbers, booleans, null or lists of those")


def _is_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_foreign_relation(path: str, relation: str, operator: str, value: object) -> None:
    ids_ok = (
        _is_id(value)
        or value is False
        or (isinstance(value, list | tuple) and all(_is_id(v) for v in value))
    )
    if operator not in ID_ONLY_OPERATORS or not ids_ok:
        raise InputError(
            f"Field {_short(path)} points to model {relation}, which is not allowed: "
            "compare it by record id only (=, !=, in, not in with ids or false)"
        )


async def _check_path(
    path: str,
    model: str,
    fields_of: FieldsLookup,
    allowed: Container[str],
    operator: str,
    value: object,
) -> None:
    segments = path.split(".")
    if len(segments) > MAX_PATH_HOPS + 1 or not all(_FIELD_NAME.fullmatch(s) for s in segments):
        raise InputError(f"Invalid field path {_short(path)} in domain")
    current = model
    for index, segment in enumerate(segments):
        meta = (await fields_of(current)).get(segment)
        if meta is None:
            raise InputError(
                f"Unknown field {_short(segment)} on {current} in domain. "
                "Use get_fields to see the available fields."
            )
        relation = meta.get("relation")
        if index == len(segments) - 1:
            if meta.get("type") in RELATIONAL_TYPES and relation not in allowed:
                _check_foreign_relation(path, str(relation), operator, value)
            return
        if meta.get("type") not in RELATIONAL_TYPES or not isinstance(relation, str):
            raise InputError(f"Field {current}.{segment} is not relational; cannot follow it")
        if relation not in allowed:
            raise InputError(
                f"Domain path {_short(path)} crosses into model {relation}, which is not allowed"
            )
        current = relation


async def validate_domain(
    domain: object,
    model: str,
    fields_of: FieldsLookup,
    allowed: Container[str],
) -> list[Any]:
    """Validate an Odoo domain in prefix notation and return it as plain lists."""
    if domain is None:
        return []
    if not isinstance(domain, list | tuple):
        raise InputError("domain must be a list, e.g. [['name', 'ilike', 'acme']]")

    terms = sum(1 for element in domain if isinstance(element, list | tuple))
    if terms > MAX_DOMAIN_TERMS:
        raise InputError(f"domain may contain at most {MAX_DOMAIN_TERMS} terms")

    _check_arity(domain)
    result: list[Any] = []
    for element in domain:
        if isinstance(element, str):
            result.append(element)
            continue
        if not isinstance(element, list | tuple) or len(element) != 3:
            raise InputError(
                f"Invalid domain term {_short(element)}; expected [field, operator, value]"
            )
        field, operator, value = element
        if not isinstance(field, str) or not field:
            raise InputError("Domain field names must be non-empty strings")
        if operator not in TERM_OPERATORS:
            raise InputError(
                f"Unsupported domain operator {_short(operator)}; "
                f"allowed: {', '.join(sorted(TERM_OPERATORS))}"
            )
        _check_value(value)
        await _check_path(field, model, fields_of, allowed, operator, value)
        result.append([field, operator, list(value) if isinstance(value, tuple) else value])
    return result


def _check_arity(domain: Sequence[object]) -> None:
    """Check prefix notation: every logical operator has enough operands.

    Evaluated right to left; top-level expressions are joined by an implicit '&'.
    """
    operands = 0
    for element in reversed(domain):
        if isinstance(element, str):
            arity = LOGICAL_OPERATORS.get(element)
            if arity is None:
                raise InputError(f"Invalid domain operator {_short(element)}; use '&', '|' or '!'")
            if operands < arity:
                raise InputError(
                    f"domain is incomplete: operator {element!r} is missing an operand"
                )
            operands -= arity - 1
        else:
            operands += 1


def _is_positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_m2m(value: object) -> bool:
    if not isinstance(value, list) or len(value) > MAX_LIST_VALUES:
        return False
    for command in value:
        if not isinstance(command, list) or not command or command[0] not in M2M_COMMANDS:
            return False
        code = command[0]
        if code in (3, 4) and not (len(command) == 2 and _is_positive_id(command[1])):
            return False
        if code == 6 and not (
            len(command) == 3
            and command[1] == 0
            and isinstance(command[2], list)
            and len(command[2]) <= MAX_LIST_VALUES
            and all(_is_positive_id(i) for i in command[2])
        ):
            return False
    return True


def _check_date(value: str, fmt: str) -> bool:
    try:
        datetime.datetime.strptime(value, fmt)  # Odoo dates are naive UTC
    except ValueError:
        return False
    return True


def _value_ok(kind: str, value: object) -> bool:
    if kind in TEXT_TYPES:
        return value is False or (isinstance(value, str) and len(value) <= MAX_WRITE_STRING)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind in ("float", "monetary"):
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "date":
        return value is False or (isinstance(value, str) and _check_date(value, "%Y-%m-%d"))
    if kind == "datetime":
        return value is False or (
            isinstance(value, str) and _check_date(value, "%Y-%m-%d %H:%M:%S")
        )
    if kind == "many2one":
        return value is False or _is_positive_id(value)
    if kind == "many2many":
        return _check_m2m(value)
    return False  # pragma: no cover - WRITABLE_TYPES guards this


_EXPECTED = {
    "integer": "an integer",
    "float": "a number",
    "monetary": "a number",
    "boolean": "true or false",
    "date": "a date 'YYYY-MM-DD' or false",
    "datetime": "a UTC datetime 'YYYY-MM-DD HH:MM:SS' or false",
    "many2one": "a record id or false",
    "many2many": "a list of commands: [4, id] link, [3, id] unlink, [6, 0, [ids]] replace",
}


def validate_values(values: Mapping[str, object], available: FieldMap) -> dict[str, Any]:
    """Validate field values for create/write against the model's fields."""
    if not isinstance(values, Mapping) or not values:
        raise InputError("values must be an object with at least one field")
    if len(values) > MAX_WRITE_FIELDS:
        raise InputError(f"values may set at most {MAX_WRITE_FIELDS} fields")
    result: dict[str, Any] = {}
    for name, value in values.items():
        meta = available.get(name)
        if meta is None:
            raise InputError(
                f"Unknown field {_short(name)}. Use get_fields to see the available fields."
            )
        kind = str(meta.get("type"))
        if meta.get("readonly") or name == "id":
            raise InputError(f"Field {name} is read-only and cannot be set")
        if meta.get("related"):
            # A related field writes into the record it points to, i.e. possibly into
            # a model outside the allowlist.
            raise InputError(
                f"Field {name} is a related field ({meta['related']}); "
                "write the source record instead"
            )
        if kind not in WRITABLE_TYPES:
            raise InputError(f"Field {name} has type {kind}, which this server does not write")
        if not _value_ok(kind, value):
            expected = _EXPECTED.get(kind, "a string (max 100000 characters) or false")
            raise InputError(f"Field {name} expects {expected}")
        result[name] = value
    return result
