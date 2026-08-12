from app.xrp.repository import XrpInvoiceRepository


def test_xrp_invoice_persists_across_repository_instances() -> None:
    first = XrpInvoiceRepository()
    document = {"id": "xrp_inv_1", "state": "open", "beneficiary": "0x" + "11" * 20}

    assert first.create("xrp_inv_1", document) is True
    assert XrpInvoiceRepository().get("xrp_inv_1") == document


def test_duplicate_create_is_idempotent_and_never_overwrites() -> None:
    repository = XrpInvoiceRepository()
    original = {"id": "xrp_inv_1", "state": "open"}

    assert repository.create("xrp_inv_1", original) is True
    assert repository.create("xrp_inv_1", {"id": "xrp_inv_1", "state": "paid"}) is False
    assert repository.get("xrp_inv_1") == original


def test_replace_requires_an_existing_invoice() -> None:
    repository = XrpInvoiceRepository()

    assert repository.replace("missing", {"id": "missing"}) is False
    assert repository.create("xrp_inv_1", {"id": "xrp_inv_1", "state": "open"}) is True
    assert repository.replace("xrp_inv_1", {"id": "xrp_inv_1", "state": "quoted"}) is True
    assert repository.get("xrp_inv_1") == {"id": "xrp_inv_1", "state": "quoted"}
