"""Positive validation of everything a client may send towards Odoo.

Odoo enforces access rights itself. This layer narrows the surface further:
only allowlisted models, known fields, a small domain grammar and bounded sizes.
"""

import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
DEFAULT_EXCLUDED_TYPES = frozenset({"binary"})

Scalar = str | int | float | bool | None


class InputError(ValueError):
    """Client input that violates the gateway's rules. Safe to show to the client."""


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def check_model(model: str, allowed: Sequence[str]) -> None:
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
            if meta.get("type") not in DEFAULT_EXCLUDED_TYPES
        ]
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


def validate_order(order: str | None, available: FieldMap) -> str | None:
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


async def _check_path(
    path: str, model: str, fields_of: FieldsLookup, allowed: Sequence[str]
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
        if index == len(segments) - 1:
            return
        relation = meta.get("relation")
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
    allowed: Sequence[str],
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
        await _check_path(field, model, fields_of, allowed)
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
