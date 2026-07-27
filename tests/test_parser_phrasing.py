"""Loosely-worded descriptions the heuristic parser used to mangle.

These all worked via the LLM parser; the cases here pin the no-API-key
fallback so a provider outage doesn't silently drop billable line items.
"""

from decimal import Decimal

import pytest

from app.invoice import _extract_client, _extract_line_items, compute_totals


def subtotal_of(text: str) -> Decimal:
    total, _, _ = compute_totals(_extract_line_items(text), Decimal("0"))
    return total


def test_words_between_hours_and_rate():
    items = _extract_line_items("40 hours of design and front-end build at 75/hr")
    assert len(items) == 1
    assert items[0].quantity == 40
    assert items[0].amount == "3000.00"


def test_and_inside_an_item_does_not_split_it():
    items = _extract_line_items("hosting and DNS setup 200")
    assert len(items) == 1
    assert items[0].description == "Hosting and DNS setup"
    assert items[0].amount == "200.00"


def test_rate_written_per_hour_long_form():
    items = _extract_line_items("12 hours at $95 per hour")
    assert len(items) == 1
    assert items[0].amount == "1140.00"


def test_every_item_is_captured_in_a_loose_description():
    text = (
        "Website redesign for FinFlow Ltd, 40 hours of design and front-end "
        "build at 75/hr, CMS integration 1200, hosting and DNS setup 200, "
        "plus 6 hours of team training at 90/hr"
    )
    items = _extract_line_items(text)
    assert len(items) == 4
    assert subtotal_of(text) == Decimal("4940.00")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Website redesign for FinFlow Ltd — 40 hours at 75/hr", "FinFlow Ltd"),
        ("Website redesign for FinFlow Ltd, 40 hours at 75/hr", "FinFlow Ltd"),
        ("Brand refresh for Northwind Coffee - 24 hours at 95/hr", "Northwind Coffee"),
        ("Consulting for Beta LLC 10 hours at 120/hr", "Beta LLC"),
    ],
)
def test_client_name_stops_before_the_work(text, expected):
    name, _ = _extract_client(text)
    assert name == expected


def test_line_item_label_drops_the_trailing_client_clause():
    items = _extract_line_items("Website redesign for FinFlow Ltd, 40 hours at 75/hr")
    assert items[0].description == "Website redesign"


def test_unpriced_text_still_falls_back_to_one_default_item():
    items = _extract_line_items("Design a beautiful brand identity and a logo")
    assert len(items) == 1
    assert items[0].amount == "500.00"


def test_label_comes_from_the_words_between_hours_and_rate():
    items = _extract_line_items("6 hours of team training at 90/hr")
    assert items[0].description == "Team training"
    assert items[0].amount == "540.00"


def test_an_email_never_becomes_a_line_item_label():
    items = _extract_line_items("ap@finflow.com, 40 hours at 75/hr")
    assert "@" not in items[0].description


@pytest.mark.parametrize(
    "text",
    [
        "Redesign for FinFlow Ltd (ap@finflow.com), 40 hours at 75/hr",
        "Redesign for FinFlow Ltd [billing], 40 hours at 75/hr",
    ],
)
def test_client_name_stops_at_a_bracket(text):
    name, _ = _extract_client(text)
    assert name == "FinFlow Ltd"
