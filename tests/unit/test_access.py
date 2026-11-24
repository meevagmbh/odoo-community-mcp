import pytest

from odoo_mcp.access import Access, ModelAccess


def make(spec: str, *, allow_write: bool = True) -> ModelAccess:
    return ModelAccess.parse([part.strip() for part in spec.split(",")], allow_write=allow_write)


def test_default_access_is_read() -> None:
    access = make("res.partner")
    assert access.access("res.partner") is Access.READ
    assert "res.partner" in access
    assert not access.can_write("res.partner")


def test_explicit_modes() -> None:
    access = make("res.partner:r, sale.order:rw")
    assert access.access("res.partner") is Access.READ
    assert access.access("sale.order") is Access.READ_WRITE
    assert access.can_write("sale.order")


def test_unlisted_model_has_no_access() -> None:
    access = make("res.partner")
    assert access.access("res.users") is None
    assert "res.users" not in access
    assert not access.can_write("res.users")


def test_wildcard_covers_an_app() -> None:
    access = make("sale.*:rw, account.*:r")
    assert access.access("sale.order") is Access.READ_WRITE
    assert access.access("sale.order.line") is Access.READ_WRITE
    assert access.access("account.move") is Access.READ
    assert access.access("sale") is None
    assert access.access("saleorder.x") is None


def test_exact_entry_beats_wildcard_and_longest_wildcard_wins() -> None:
    access = make("sale.*:rw, sale.report:r, sale.order.*:r")
    assert access.access("sale.report") is Access.READ
    assert access.access("sale.order.line") is Access.READ
    assert access.access("sale.order") is Access.READ_WRITE


def test_kill_switch_downgrades_writes() -> None:
    access = make("sale.order:rw, sale.*:rw", allow_write=False)
    assert access.access("sale.order") is Access.READ
    assert not access.can_write("sale.order.line")
    assert not access.any_writable


def test_any_writable() -> None:
    assert make("res.partner, sale.order:rw").any_writable
    assert not make("res.partner").any_writable


def test_entries_for_listing() -> None:
    access = make("res.partner, sale.*:rw")
    assert access.exact == {"res.partner": Access.READ}
    assert access.patterns == {"sale.": Access.READ_WRITE}


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "res.partner:x",
        "res.partner:",
        "*",
        "*:r",
        "sale*",
        "sale.*.line",
        "Res.Partner",
        "a:r:w",
        "res.partner, res.partner:rw",
    ],
)
def test_invalid_specs(spec: str) -> None:
    with pytest.raises(ValueError, match="ODOO_MODELS"):
        make(spec)
