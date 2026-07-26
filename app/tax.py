from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

DEFAULT_TAX_RATE = Decimal("0.08")


def get_tax_rate() -> Decimal:
    raw = os.environ.get("TAX_RATE")
    if raw is None:
        return DEFAULT_TAX_RATE
    try:
        rate = Decimal(str(raw))
        if rate >= Decimal("0") and rate <= Decimal("1"):
            return rate
    except Exception:
        pass
    return DEFAULT_TAX_RATE


def calculate_tax(amount: Decimal, rate: Optional[Decimal] = None) -> Decimal:
    r = rate if rate is not None else get_tax_rate()
    return (amount * r).quantize(Decimal("0.01"))
