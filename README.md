# InvoiceCraft

AI invoice generator. Describe the job, pay 0.50 USDT via x402, get a PDF.

## How it works

1. **POST a job description** to `/api/v1/invoice` → receive an x402 payment challenge (HTTP 402) with a `challenge_id` and the receiver address.
2. **Pay 0.50 USDT** on X Layer (chain `eip155:196`). Use any wallet that supports the chain — the memo field must contain the `challenge_id`.
3. **Re-POST** with `description`, `payment_tx_hash`, and `challenge_id` → receive invoice JSON + base64-encoded PDF.

## Quick start

```bash
git clone <repo> && cd InvoiceCraft
pip install -r requirements.txt

export ASP_WALLET=0xYourWallet
export INVOICE_ISSUER_NAME="Your Name"
export INVOICE_ISSUER_EMAIL="you@example.com"

uvicorn app.main:app --reload
```

- Open `dashboard/index.html` for the demo UI.
- Or POST directly to `/api/v1/invoice`.

## API

### `POST /api/v1/invoice`

**Request** (first call — no payment):
```json
{ "description": "Build a React dashboard with 3 pages and API integration" }
```

**402 Response** (payment required):
```json
{
  "error": "payment_required",
  "message": "Pay 0.50 USDT on X Layer to generate invoice",
  "payment": {
    "amount": "0.50",
    "token": "USDT",
    "chain": "eip155:196",
    "receiver": "0x...",
    "challenge_id": "abc123...",
    "memo": "abc123..."
  }
}
```

**Request** (second call — after payment):
```json
{
  "description": "Build a React dashboard with 3 pages and API integration",
  "payment_tx_hash": "0x...",
  "challenge_id": "abc123..."
}
```

**200 Response**:
```json
{
  "invoice": {
    "issuer": { "name": "...", "address": "0x...", "email": "..." },
    "client": { "name": "...", "email": "..." },
    "line_items": [
      { "description": "...", "quantity": 1, "unit_price": "500.00", "amount": "500.00" }
    ],
    "invoice_number": "INV-20260726-001",
    "subtotal": "500.00",
    "tax_rate": "0.08",
    "tax_amount": "40.00",
    "total": "540.00",
    "currency": "USD",
    "due_date": "2026-08-25",
    "status": "paid",
    "notes": "Payment: 0x... on X Layer"
  },
  "pdf": "<base64>"
}
```

**Validation**: description must be 10–2000 characters. `challenge_id` is required when `payment_tx_hash` is provided.

## Architecture

- **FastAPI** backend with a single endpoint.
- **ReportLab** generates PDFs from invoice data.
- **x402 middleware** issues payment challenges and verifies on-chain payments.
- No database, no auth, no subscriptions — stateless challenge store (in-memory, TTL 15 min).

| File | Role |
|---|---|
| `app/main.py` | FastAPI app, routes, request handling |
| `app/invoice.py` | Description parsing, invoice creation, number generation |
| `app/pdf_engine.py` | ReportLab PDF layout and rendering |
| `app/tax.py` | Configurable tax rate (env `TAX_RATE`, default 8%) |
| `app/x402.py` | Payment challenge generation and verification |
| `app/models.py` | Pydantic request/response models |

## Sponsor integrations

- **OKX.AI A2MCP** — InvoiceCraft is registered as an Agent Service Provider, discoverable through OKX.AI's agent directory and task marketplace.
- **x402 (OKX Payment SDK)** — Pay-per-call billing. Each invoice costs 0.50 USDT, collected before PDF generation.
- **X Layer (eip155:196)** — All settlements happen on X Layer using the native bridge.
- **OKX Agentic Wallet** — The `ASP_WALLET` environment variable identifies the payment receiver. Users pay from any wallet that supports X Layer USDT transfers.

## Project structure

```
InvoiceCraft/
├── app/             # FastAPI application
├── tests/           # pytest tests
├── dashboard/       # Demo dashboard (open index.html)
├── proposals/       # UI design proposals
└── Dockerfile       # Production container
```

## License

MIT
