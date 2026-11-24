"""Per-model access policy of the gateway: which models may be read or written.

This narrows what Odoo would allow; it never widens it. Odoo still checks the
caller's own rights on every call.

Entries (from `ODOO_MODELS`, comma-separated):
  `res.partner`         read (default)
  `sale.order:rw`       read and write
  `sale.*:rw`           every model whose name starts with `sale.`
Exact entries win over wildcards; among wildcards the longest prefix wins.
"""

import re
from collections.abc import Iterable
from enum import StrEnum

_MODEL = r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+"
_ENTRY = re.compile(
    rf"^(?P<name>{_MODEL}|[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\.\*)(?::(?P<mode>r|rw))?$"
)


class Access(StrEnum):
    READ = "read"
    READ_WRITE = "read-write"


class ModelAccess:
    def __init__(self, exact: dict[str, Access], patterns: dict[str, Access]) -> None:
        self.exact = exact
        # Keys are prefixes including the trailing dot, e.g. "sale.".
        self.patterns = patterns
        self._ordered = sorted(patterns.items(), key=lambda item: len(item[0]), reverse=True)

    @classmethod
    def parse(cls, entries: Iterable[str], *, allow_write: bool) -> ModelAccess:
        exact: dict[str, Access] = {}
        patterns: dict[str, Access] = {}
        for entry in entries:
            match = _ENTRY.fullmatch(entry)
            if not match:
                raise ValueError(
                    f"invalid ODOO_MODELS entry {entry!r}; use 'model', 'model:r', "
                    "'model:rw' or 'prefix.*:rw'"
                )
            name, mode = match.group("name"), match.group("mode") or "r"
            access = Access.READ_WRITE if mode == "rw" and allow_write else Access.READ
            target, key = (patterns, name[:-1]) if name.endswith(".*") else (exact, name)
            if key in target:
                raise ValueError(f"duplicate ODOO_MODELS entry for {name!r}")
            target[key] = access
        if not exact and not patterns:
            raise ValueError("ODOO_MODELS must list at least one model")
        return cls(exact, patterns)

    def access(self, model: str) -> Access | None:
        if model in self.exact:
            return self.exact[model]
        for prefix, access in self._ordered:
            if model.startswith(prefix):
                return access
        return None

    def __contains__(self, model: object) -> bool:
        return isinstance(model, str) and self.access(model) is not None

    def can_write(self, model: str) -> bool:
        return self.access(model) is Access.READ_WRITE

    @property
    def any_writable(self) -> bool:
        return (
            Access.READ_WRITE in self.exact.values() or Access.READ_WRITE in self.patterns.values()
        )
