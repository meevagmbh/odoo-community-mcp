"""Read-only MCP tools. Each call runs with the caller's own Odoo API key."""

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from odoo_mcp.auth import extract_bearer, key_digest, key_fingerprint
from odoo_mcp.config import Settings
from odoo_mcp.logging_setup import audit
from odoo_mcp.odoo_client import OdooClient, OdooError
from odoo_mcp.validation import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    FieldMap,
    InputError,
    check_model,
    validate_domain,
    validate_fields,
    validate_ids,
    validate_limit,
    validate_offset,
    validate_order,
)

FIELD_ATTRIBUTES = ["type", "string", "relation", "required"]
FIELDS_CACHE_SECONDS = 60.0
FIELDS_CACHE_SIZE = 2_000

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)

DOMAIN_HELP = """
`domain` is an Odoo search domain: a JSON list of conditions in prefix (Polish) notation.
- A condition is `[field, operator, value]`, e.g. `["name", "ilike", "acme"]`.
- Conditions in sequence are AND-ed:
  `[["is_company", "=", true], ["country_id.code", "=", "DE"]]`.
- OR needs a prefix `"|"` before its two operands:
  `["|", ["email", "ilike", "@acme.com"], ["name", "ilike", "acme"]]`.
- NOT is a prefix `"!"`: `["!", ["active", "=", false]]`.
- Operators: = != > >= < <= =? like, not like, ilike, not ilike, =like, =ilike,
  in, not in, child_of, parent_of.
- `ilike` is a case-insensitive substring match.
- `in` takes a list: `["state", "in", ["sale", "done"]]`.
- Dates are strings: `["date_order", ">=", "2026-01-01"]`.
- Datetimes are UTC strings: `"2026-01-01 00:00:00"`.
- Empty relations compare with false: `["user_id", "=", false]`.
- Follow relations with dots, e.g. `partner_id.name`; every model on the path must be allowed.
- Use get_fields first to learn the exact field names and types of a model.
""".strip()


class FieldInfo(BaseModel):
    type: str
    label: str
    relation: str | None = None
    required: bool = False


class ModelsResult(BaseModel):
    models: list[str]


class FieldsResult(BaseModel):
    fields: dict[str, FieldInfo]


class RecordsResult(BaseModel):
    records: list[dict[str, Any]]
    count: int = Field(description="Number of records in this result")


class CountResult(BaseModel):
    count: int


Model = Annotated[str, Field(description="Technical Odoo model name, e.g. res.partner")]
Domain = Annotated[
    list[Any] | None,
    Field(description="Odoo search domain (see tool description). Omit or [] for all records."),
]
Fields = Annotated[
    list[str] | None,
    Field(description="Field names to return. Omit for all non-binary fields."),
]


class _FieldsCache:
    """Short-lived fields_get cache per (key, model): field visibility is per user."""

    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], tuple[float, FieldMap]] = OrderedDict()

    def get(self, digest: str, model: str) -> FieldMap | None:
        entry = self._entries.get((digest, model))
        if entry is None or entry[0] < time.monotonic():
            return None
        return entry[1]

    def put(self, digest: str, model: str, fields: FieldMap) -> None:
        self._entries[(digest, model)] = (time.monotonic() + FIELDS_CACHE_SECONDS, fields)
        self._entries.move_to_end((digest, model))
        while len(self._entries) > FIELDS_CACHE_SIZE:
            self._entries.popitem(last=False)


def _caller_key(ctx: Context) -> str:
    headers = ctx.headers
    key = extract_bearer(headers.get("authorization") if headers is not None else None)
    if key is None:
        # The HTTP layer already answers 401; this is the fail-closed backstop.
        raise ToolError("Missing or malformed Authorization: Bearer <Odoo API key> header.")
    return key


def register_tools(server: MCPServer, settings: Settings, odoo: OdooClient) -> None:
    allowed = settings.odoo_models
    cache = _FieldsCache()

    async def fields_of(key: str, model: str) -> FieldMap:
        check_model(model, allowed)
        digest = key_digest(key)
        cached = cache.get(digest, model)
        if cached is not None:
            return cached
        result = await odoo.call(key, model, "fields_get", {"attributes": FIELD_ATTRIBUTES})
        if not isinstance(result, dict):
            raise OdooError("Odoo returned an unexpected fields_get result.", 200)
        cache.put(digest, model, result)
        return result

    async def run[T](
        ctx: Context, tool: str, model: str | None, action: Callable[[str], Awaitable[T]]
    ) -> T:
        key = _caller_key(ctx)
        fingerprint = key_fingerprint(key)
        started = time.perf_counter()

        def log(outcome: str, **fields: Any) -> None:
            audit(
                "tool_call",
                key_fp=fingerprint,
                tool=tool,
                model=model,
                outcome=outcome,
                duration_ms=round((time.perf_counter() - started) * 1000),
                **fields,
            )

        try:
            if model is not None:
                check_model(model, allowed)
            result = await action(key)
        except InputError as exc:
            log("rejected", reason=str(exc))
            raise ToolError(str(exc)) from None
        except OdooError as exc:
            log("odoo_error", odoo_status=exc.status, error=type(exc).__name__)
            raise ToolError(str(exc)) from None
        count = result.count if isinstance(result, RecordsResult | CountResult) else None
        log("ok", odoo_status=200 if model is not None else None, result_count=count)
        return result

    @server.tool(annotations=READ_ONLY)
    async def list_models(ctx: Context) -> ModelsResult:
        """List the Odoo models this gateway allows. Only these can be queried."""

        async def action(_key: str) -> ModelsResult:
            return ModelsResult(models=list(allowed))

        return await run(ctx, "list_models", None, action)

    @server.tool(annotations=READ_ONLY)
    async def get_fields(model: Model, ctx: Context) -> FieldsResult:
        """Describe the fields of an Odoo model: technical name, type, label, related
        model (for many2one/one2many/many2many) and whether it is required.

        Call this before searching so that domains, field lists and order use exact names."""

        async def action(key: str) -> FieldsResult:
            raw = await fields_of(key, model)
            return FieldsResult(
                fields={
                    name: FieldInfo(
                        type=str(meta.get("type", "")),
                        label=str(meta.get("string", "")),
                        relation=meta.get("relation") or None,
                        required=bool(meta.get("required", False)),
                    )
                    for name, meta in raw.items()
                }
            )

        return await run(ctx, "get_fields", model, action)

    @server.tool(annotations=READ_ONLY, description=_search_description())
    async def search_records(
        model: Model,
        ctx: Context,
        domain: Domain = None,
        fields: Fields = None,
        limit: Annotated[
            int, Field(ge=1, le=MAX_LIMIT, description=f"Max records (1-{MAX_LIMIT})")
        ] = DEFAULT_LIMIT,
        offset: Annotated[int, Field(ge=0, description="Records to skip, for paging")] = 0,
        order: Annotated[
            str | None,
            Field(description="Sort, e.g. 'name asc' or 'date_order desc, id desc'"),
        ] = None,
    ) -> RecordsResult:
        async def action(key: str) -> RecordsResult:
            available = await fields_of(key, model)
            payload: dict[str, Any] = {
                "domain": await validate_domain(
                    domain, model, lambda m: fields_of(key, m), allowed
                ),
                "fields": validate_fields(fields, available),
                "limit": validate_limit(limit),
                "offset": validate_offset(offset),
            }
            if (sort := validate_order(order, available)) is not None:
                payload["order"] = sort
            records = await odoo.call(key, model, "search_read", payload)
            return _records(records)

        return await run(ctx, "search_records", model, action)

    @server.tool(annotations=READ_ONLY)
    async def count_records(model: Model, ctx: Context, domain: Domain = None) -> CountResult:
        """Count the records of an Odoo model that match a domain (same syntax as
        search_records). Use it to size a result before fetching it."""

        async def action(key: str) -> CountResult:
            payload = {
                "domain": await validate_domain(domain, model, lambda m: fields_of(key, m), allowed)
            }
            count = await odoo.call(key, model, "search_count", payload)
            if isinstance(count, bool) or not isinstance(count, int):
                raise OdooError("Odoo returned an unexpected search_count result.", 200)
            return CountResult(count=count)

        return await run(ctx, "count_records", model, action)

    @server.tool(annotations=READ_ONLY)
    async def read_records(
        model: Model,
        ids: Annotated[list[int], Field(description="Record ids, e.g. [7, 42] (max 500)")],
        ctx: Context,
        fields: Fields = None,
    ) -> RecordsResult:
        """Read specific records of an Odoo model by id. Ids the user may not see
        or that do not exist cause an error."""

        async def action(key: str) -> RecordsResult:
            available = await fields_of(key, model)
            payload = {"ids": validate_ids(ids), "fields": validate_fields(fields, available)}
            return _records(await odoo.call(key, model, "read", payload))

        return await run(ctx, "read_records", model, action)


def _records(result: object) -> RecordsResult:
    if not isinstance(result, list) or not all(isinstance(r, dict) for r in result):
        raise OdooError("Odoo returned an unexpected result.", 200)
    return RecordsResult(records=result, count=len(result))


def _search_description() -> str:
    return (
        "Search records of an Odoo model and return the requested fields.\n\n"
        f"{DOMAIN_HELP}\n\n"
        "Results are paged with `limit` (default 50, max 500) and `offset`. "
        "many2one fields come back as [id, display_name]. "
        "Example: model='res.partner', domain=[['is_company', '=', true], "
        "['name', 'ilike', 'acme']], fields=['name', 'email'], order='name asc'."
    )
