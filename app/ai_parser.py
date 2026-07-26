from __future__ import annotations

import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx

from app.models import InvoiceLineItem

logger = logging.getLogger(__name__)

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

_SYSTEM = (
    "You are an invoicing assistant. Convert a freelancer's plain-language job "
    "description into structured invoice data. Infer reasonable line items, "
    "quantities, and unit prices from the text. When an hourly rate and hours "
    "are given, use them. When a flat amount is given, use it. When no price is "
    "stated, estimate a fair market rate for the described work. Respond with "
    "ONLY a JSON object, no prose."
)

_SCHEMA_HINT = (
    'Return JSON of the form: {"client_name": string, "client_email": string, '
    '"line_items": [{"description": string, "quantity": number, '
    '"unit_price": number}]}. Omit client_email if none is present.'
)


def _money(value) -> str:
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return "0.00"


def _extract_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _normalize(payload: dict) -> Optional[dict]:
    raw_items = payload.get("line_items") or []
    line_items: list[InvoiceLineItem] = []
    for raw in raw_items:
        try:
            qty = int(Decimal(str(raw.get("quantity", 1))))
            qty = max(qty, 1)
            unit = Decimal(str(raw.get("unit_price", 0)))
            amount = (unit * qty).quantize(Decimal("0.01"))
            description = str(raw.get("description", "")).strip()
        except (InvalidOperation, ValueError, TypeError):
            continue
        if not description or amount <= 0:
            continue
        line_items.append(
            InvoiceLineItem(
                description=description,
                quantity=qty,
                unit_price=_money(unit),
                amount=_money(amount),
            )
        )
    if not line_items:
        return None
    client_name = str(payload.get("client_name") or "Client").strip() or "Client"
    client_email = str(payload.get("client_email") or "client@example.com").strip()
    return {
        "client_name": client_name,
        "client_email": client_email,
        "line_items": line_items,
    }


def parse_with_ai(description: str) -> Optional[dict]:
    """Parse a job description into structured invoice data using an LLM.

    Returns None (so the caller falls back to the heuristic parser) when no API
    key is configured or the call fails for any reason. Network I/O is blocking;
    callers on the async path run this via a threadpool.
    """
    api_key = os.getenv("LLM_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = os.getenv("LLM_MODEL", "claude-sonnet-5")
    timeout = float(os.getenv("LLM_TIMEOUT", "30"))
    try:
        resp = httpx.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1024,
                "system": _SYSTEM,
                "messages": [
                    {
                        "role": "user",
                        "content": f"{_SCHEMA_HINT}\n\nJob description:\n{description}",
                    }
                ],
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["content"][0]["text"]
    except Exception:
        logger.warning("AI parse failed; falling back to heuristic parser", exc_info=True)
        return None

    payload = _extract_json(text)
    if not payload:
        return None
    return _normalize(payload)
