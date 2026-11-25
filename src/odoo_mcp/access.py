"""Per-model access policy of the gateway: which models may be read or written.

This narrows what Odoo would allow; it never widens it. Odoo still checks the
caller's own rights on every call.

Entries (from `ODOO_MODELS`, comma-separated):
  `res.partner`         read (default)
  `sale.order:rw`       read and write
  `sale.order:rwd`      read, write and delete (exact model names only)
  `sale.*:rw`           every model whose name starts with `sale.`
Exact entries win over wildcards; among wildcards the longest prefix wins.
"""

import re
from collections.abc import Iterable
from enum import StrEnum

_MODEL = r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+"
_MODEL_NAME = re.compile(_MODEL)
# Odoo's ir_model.model column is a varchar; real names are far shorter.
MAX_MODEL_LENGTH = 128
_ENTRY = re.compile(
    rf"^(?P<name>{_MODEL}|[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\.\*)(?::(?P<mode>r|rw|rwd))?$"
)


def model_name_ok(model: str) -> bool:
    """True for a plain Odoo model name such as `sale.order.line`."""
    return len(model) <= MAX_MODEL_LENGTH and _MODEL_NAME.fullmatch(model) is not None


class Access(StrEnum):
    READ = "read"
    READ_WRITE = "read-write"
    READ_WRITE_DELETE = "read-write-delete"


_WRITE_LEVELS = frozenset({Access.READ_WRITE, Access.READ_WRITE_DELETE})


class ModelAccess:
    def __init__(self, exact: dict[str, Access], patterns: dict[str, Access]) -> None:
        self.exact = exact
        # Keys are prefixes including the trailing dot, e.g. "sale.".
        self.patterns = patterns
        self._ordered = sorted(patterns.items(), key=lambda item: len(item[0]), reverse=True)

    @classmethod
    def parse(
        cls, entries: Iterable[str], *, allow_write: bool, allow_delete: bool = False
    ) -> ModelAccess:
        exact: dict[str, Access] = {}
        patterns: dict[str, Access] = {}
        for entry in entries:
            match = _ENTRY.fullmatch(entry)
            if not match:
                raise ValueError(
                    f"invalid ODOO_MODELS entry {entry!r}; use 'model', 'model:r', "
                    "'model:rw', 'model:rwd' or 'prefix.*:rw'"
                )
            name, mode = match.group("name"), match.group("mode") or "r"
            if mode == "rwd" and name.endswith(".*"):
                raise ValueError(
                    f"ODOO_MODELS entry {entry!r}: delete needs an exact model name, not a wildcard"
                )
            access = _effective(mode, allow_write=allow_write, allow_delete=allow_delete)
            target, key = (patterns, name[:-1]) if name.endswith(".*") else (exact, name)
            if key in target:
                raise ValueError(f"duplicate ODOO_MODELS entry for {name!r}")
            target[key] = access
        if not exact and not patterns:
            raise ValueError("ODOO_MODELS must list at least one model")
        return cls(exact, patterns)

    def access(self, model: str) -> Access | None:
        # Client-supplied names end up in the Odoo URL path. Anything that is not a
        # plain model name ("sale.x/../res.users") must never match a wildcard.
        if not model_name_ok(model):
            return None
        if model in self.exact:
            return self.exact[model]
        for prefix, access in self._ordered:
            if model.startswith(prefix):
                return access
        return None

    def __contains__(self, model: object) -> bool:
        return isinstance(model, str) and self.access(model) is not None

    def can_write(self, model: str) -> bool:
        return self.access(model) in _WRITE_LEVELS

    def can_delete(self, model: str) -> bool:
        return self.access(model) is Access.READ_WRITE_DELETE

    @property
    def any_writable(self) -> bool:
        return any(a in _WRITE_LEVELS for a in (*self.exact.values(), *self.patterns.values()))

    @property
    def any_deletable(self) -> bool:
        return Access.READ_WRITE_DELETE in self.exact.values()


def _effective(mode: str, *, allow_write: bool, allow_delete: bool) -> Access:
    """Configured mode capped by the master switches."""
    if mode == "rwd" and allow_write and allow_delete:
        return Access.READ_WRITE_DELETE
    if mode in ("rw", "rwd") and allow_write:
        return Access.READ_WRITE
    return Access.READ
