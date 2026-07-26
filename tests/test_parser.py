from decimal import Decimal

from app.invoice import _extract_line_items, compute_totals, parse_description


def test_hours_and_rate():
    items = _extract_line_items("40 hours at 75/hr")
    assert len(items) == 1
    assert items[0].quantity == 40
    assert items[0].unit_price == "75.00"
    assert items[0].amount == "3000.00"


def test_trailing_flat_amount():
    items = _extract_line_items("hosting setup 200")
    assert len(items) == 1
    assert items[0].amount == "200.00"
    assert "hosting setup" in items[0].description.lower()


def test_multiple_priced_segments():
    items = _extract_line_items(
        "website redesign, 40 hours at 75/hr, plus hosting setup 200"
    )
    assert len(items) >= 2
    subtotal, _, _ = compute_totals(items, Decimal("0"))
    assert subtotal == Decimal("3200.00")


def test_no_price_falls_back_to_single_default():
    items = _extract_line_items("Design a beautiful brand identity")
    assert len(items) == 1
    assert items[0].amount == "500.00"


def test_parse_description_uses_heuristic_without_api_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    name, email, items = parse_description(
        "Consulting for Beta LLC, 10 hours at 120/hr, contact ops@beta.io"
    )
    assert name == "Beta LLC"
    assert email == "ops@beta.io"
    assert items[0].amount == "1200.00"
