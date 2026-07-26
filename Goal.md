# InvoiceCraft — Goal

An AI-powered invoice generator Agent Service Provider for OKX.AI that turns natural language job descriptions into professional PDF invoices with x402 micropayments on X Layer.

## Why

Freelancers and small sellers spend 2-3 hours per invoice: formatting, calculating taxes, tracking payment status, and chasing late payers. Existing tools (FreshBooks, Wave) require account creation, subscriptions, and manual data entry. InvoiceCraft solves this with a single API call — describe the job in plain language, get a ready-to-send PDF invoice.

## What ships (scope)

- **A2MCP endpoint**: Single POST endpoint that accepts natural language job descriptions and returns structured invoice data + PDF
- **x402 payment**: 0.50 USDT per invoice, settled instantly on X Layer via OKX Payment SDK
- **PDF generation**: Professional invoices with line items, tax calculation, payment link, due dates
- **Demo dashboard**: Simple web UI for testing the API and previewing generated invoices
- **OKX.AI listing**: Live ASP on the marketplace, passing internal review
- **90-second demo**: X post with #OKXAI walkthrough

## Out of scope

- User accounts or authentication
- Payment gateway integrations (Stripe, PayPal)
- Multi-currency support (USD only for MVP)
- Database persistence (stateless endpoint)
- Invoice tracking / reminders / CRM features
- Mobile app

## Definition of done

1. ASP listed and live on OKX.AI marketplace (passes internal review)
2. A2MCP endpoint returns valid invoice data + PDF for natural language input
3. x402 payment middleware working (0.50 USDT/invoice on X Layer)
4. Demo dashboard deployed and accessible via HTTPS
5. 90-second demo posted on X with #OKXAI
6. Google form submitted before Jul 27, 23:59 UTC

## Constraints / deadlines

- **Stack**: Python 3.11+, FastAPI, WeasyPrint or ReportLab, OKX Payment SDK
- **Hackathon deadline**: Jul 27, 2026 23:59 UTC
- **Payment**: USDT on X Layer (CAIP-2: eip155:196)
- **Service type**: A2MCP (pay-per-call)
- **Deployment**: HTTPS endpoint required for OKX.AI registration
