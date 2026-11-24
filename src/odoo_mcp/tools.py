"""MCP tools. Each call runs with the caller's own Odoo API key."""

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Container
from typing import Annotated, Any, Protocol

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from odoo_mcp.access import Access, ModelAccess
from odoo_mcp.auth import extract_bearer, key_digest, key_fingerprint
from odoo_mcp.config import Settings
from odoo_mcp.logging_setup import audit
from odoo_mcp.odoo_client import OdooClient, OdooError
from odoo_mcp.validation import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_WRITE_FIELDS,
    FieldMap,
    InputError,
    check_model,
    validate_domain,
    validate_fields,
    validate_ids,
    validate_limit,
    validate_offset,
    validate_order,
    validate_values,
)

FIELD_ATTRIBUTES = ["type", "string", "relation", "required", "readonly", "store"]
MAX_UPDATE_IDS = 100
MAX_LISTED_MODELS = 200
FIELDS_CACHE_SECONDS = 60.0
FIELDS_CACHE_SIZE = 2_000


def _hints(title: str, *, read_only: bool, destructive: bool, idempotent: bool) -> ToolAnnotations:
    """Every tool states all hints explicitly; clients must not have to guess defaults."""
    return ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=True,  # talks to an external system (Odoo)
    )


def _read(title: str) -> ToolAnnotations:
    return _hints(title, read_only=True, destructive=False, idempotent=True)


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
- Relations to models that are not allowed can only be compared by id:
  `["user_id", "=", 7]`, `["user_id", "in", [7, 8]]`, `["user_id", "=", false]`.
  Their values come back as plain ids.
- Use get_fields first to learn the exact field names and types of a model.
""".strip()


class FieldInfo(BaseModel):
    type: str
    label: str
    relation: str | None = None
    required: bool = False
    readonly: bool = False


class ModelEntry(BaseModel):
    model: str
    access: Access


class ModelsResult(BaseModel):
    models: list[ModelEntry]


class CreateResult(BaseModel):
    id: int = Field(description="Id of the new record")


class UpdateResult(BaseModel):
    ids: list[int]
    updated: int = Field(description="Number of records written")


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


class _Runner(Protocol):
    async def __call__[T](
        self,
        ctx: Context,
        tool: str,
        model: str | None,
        action: Callable[[str], Awaitable[T]],
        **extra: Any,
    ) -> T: ...


def _count(result: object) -> int | None:
    if isinstance(result, RecordsResult | CountResult):
        return result.count
    if isinstance(result, UpdateResult):
        return result.updated
    if isinstance(result, CreateResult):
        return 1
    return None


def register_tools(server: MCPServer, settings: Settings, odoo: OdooClient) -> None:
    allowed = settings.model_access()
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
        ctx: Context,
        tool: str,
        model: str | None,
        action: Callable[[str], Awaitable[T]],
        **extra: Any,
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
                **extra,
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
        log("ok", odoo_status=200 if model is not None else None, result_count=_count(result))
        return result

    async def expand(key: str, prefix: str) -> list[str] | None:
        """Concrete model names behind a wildcard, as far as the caller may see ir.model."""
        payload = {
            "domain": [["model", "=like", f"{prefix}%"], ["transient", "=", False]],
            "fields": ["model"],
            "order": "model",
            "limit": MAX_LISTED_MODELS,
        }
        try:
            rows = await odoo.call(key, "ir.model", "search_read", payload)
        except OdooError:
            return None
        if not isinstance(rows, list):
            return None
        return [r["model"] for r in rows if isinstance(r, dict) and isinstance(r.get("model"), str)]

    @server.tool(annotations=_read("List allowed models"))
    async def list_models(ctx: Context) -> ModelsResult:
        """List the Odoo models this gateway allows and whether each is read-only
        ("read") or writable ("read-write"). Only these models can be used."""

        async def action(key: str) -> ModelsResult:
            entries = [ModelEntry(model=m, access=a) for m, a in allowed.exact.items()]
            seen = set(allowed.exact)
            for prefix, pattern_access in allowed.patterns.items():
                names = await expand(key, prefix)
                if names is None:
                    entries.append(ModelEntry(model=f"{prefix}*", access=pattern_access))
                    continue
                for name in names:
                    access = allowed.access(name)
                    if name not in seen and access is not None:
                        seen.add(name)
                        entries.append(ModelEntry(model=name, access=access))
            return ModelsResult(models=entries)

        return await run(ctx, "list_models", None, action)

    @server.tool(annotations=_read("Describe model fields"))
    async def get_fields(model: Model, ctx: Context) -> FieldsResult:
        """Describe the fields of an Odoo model: technical name, type, label, related
        model (for many2one/one2many/many2many), whether it is required and whether it
        is read-only (read-only fields cannot be written).

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
                        readonly=bool(meta.get("readonly", False)),
                    )
                    for name, meta in raw.items()
                }
            )

        return await run(ctx, "get_fields", model, action)

    @server.tool(annotations=_read("Search records"), description=_search_description())
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
            if (sort := validate_order(order, available, allowed)) is not None:
                payload["order"] = sort
            records = await odoo.call(key, model, "search_read", payload)
            return _records(records, available, allowed)

        return await run(ctx, "search_records", model, action)

    @server.tool(annotations=_read("Count records"))
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

    @server.tool(annotations=_read("Read records by id"))
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
            return _records(await odoo.call(key, model, "read", payload), available, allowed)

        return await run(ctx, "read_records", model, action)

    if allowed.any_writable:
        _register_write_tools(server, odoo, allowed, fields_of, run)


Values = Annotated[
    dict[str, Any],
    Field(
        description="Field values, e.g. {'name': 'Acme', 'partner_id': 7}. See tool description."
    ),
]

WRITE_HELP = f"""
`values` maps field names to new values (use get_fields for names, types and read-only flags):
- char/text/html/selection: a string, or false to clear. Selection takes the technical key.
- integer, float, monetary: a number. boolean: true or false.
- date: "YYYY-MM-DD"; datetime: "YYYY-MM-DD HH:MM:SS" in UTC; false clears.
- many2one: the related record id, e.g. {{"partner_id": 7}}; false clears.
- many2many: a list of commands: [4, id] adds a link, [3, id] removes a link,
  [6, 0, [id, ...]] replaces all links. Other commands are refused.
- one2many, binary, properties and read-only fields cannot be written.
At most {MAX_WRITE_FIELDS} fields per call. Odoo checks the user's rights and business rules;
there is no tool to delete records.
""".strip()


def _register_write_tools(
    server: MCPServer,
    odoo: OdooClient,
    allowed: ModelAccess,
    fields_of: Callable[[str, str], Awaitable[FieldMap]],
    run: _Runner,
) -> None:
    def check_writable(model: str) -> None:
        if not allowed.can_write(model):
            raise InputError(
                f"Model {model} is read-only on this server. "
                "Use list_models to see which models are writable."
            )

    @server.tool(
        annotations=_hints("Create record", read_only=False, destructive=False, idempotent=False),
        description=(
            "Create one record in an Odoo model that this server allows writing "
            "(list_models shows 'read-write'). Returns the new record id. Calling it twice "
            f"creates two records.\n\n{WRITE_HELP}\n\n"
            "Example: model='res.partner', values={'name': 'Acme GmbH', 'is_company': true, "
            "'country_id': 57}."
        ),
    )
    async def create_record(model: Model, values: Values, ctx: Context) -> CreateResult:
        async def action(key: str) -> CreateResult:
            check_writable(model)
            vals = validate_values(values, await fields_of(key, model))
            result = await odoo.call(key, model, "create", {"vals_list": [vals]})
            if not (
                isinstance(result, list)
                and len(result) == 1
                and isinstance(result[0], int)
                and not isinstance(result[0], bool)
            ):
                raise OdooError("Odoo returned an unexpected create result.", 200)
            return CreateResult(id=result[0])

        return await run(ctx, "create_record", model, action, fields=sorted(values))

    @server.tool(
        annotations=_hints("Update records", read_only=False, destructive=True, idempotent=True),
        description=(
            "Write the same field values to existing records of an Odoo model that this "
            f"server allows writing. Overwrites the current values. At most {MAX_UPDATE_IDS} "
            f"ids per call.\n\n{WRITE_HELP}\n\n"
            "Example: model='sale.order', ids=[12], values={'client_order_ref': 'PO-4711'}."
        ),
    )
    async def update_records(
        model: Model,
        ids: Annotated[
            list[int], Field(description=f"Record ids to update (max {MAX_UPDATE_IDS})")
        ],
        values: Values,
        ctx: Context,
    ) -> UpdateResult:
        async def action(key: str) -> UpdateResult:
            check_writable(model)
            unique = validate_ids(ids)
            if len(unique) > MAX_UPDATE_IDS:
                raise InputError(f"update_records accepts at most {MAX_UPDATE_IDS} ids per call")
            vals = validate_values(values, await fields_of(key, model))
            result = await odoo.call(key, model, "write", {"ids": unique, "vals": vals})
            if result is not True:
                raise OdooError("Odoo returned an unexpected write result.", 200)
            return UpdateResult(ids=unique, updated=len(unique))

        return await run(ctx, "update_records", model, action, fields=sorted(values))


def _records(result: object, available: FieldMap, allowed: Container[str]) -> RecordsResult:
    if not isinstance(result, list) or not all(isinstance(r, dict) for r in result):
        raise OdooError("Odoo returned an unexpected result.", 200)
    # many2one values are [id, display_name]; the name belongs to the related model,
    # so it is only shown when that model is allowlisted.
    foreign = {
        name
        for name, meta in available.items()
        if meta.get("type") == "many2one" and meta.get("relation") not in allowed
    }
    for record in result:
        for name in foreign & record.keys():
            value = record[name]
            if isinstance(value, list) and value:
                record[name] = value[0]
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
