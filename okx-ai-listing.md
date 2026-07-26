# InvoiceCraft — AI Invoice Generator

**Tagline:** Turn natural language job descriptions into professional PDF invoices — paid per invoice via x402

**Short description:**
InvoiceCraft takes a plain English job description and returns a formatted PDF invoice. Built for freelancers who would rather work than mess with invoice templates.

**Full description:**

Most freelancers I know spend 10-15 minutes per invoice. Finding the right template, filling in line items, formatting, exporting. That time adds up, and it's never billable.

InvoiceCraft fixes one specific thing: you describe the work in natural language, the AI parses it into structured line items, and you get back a clean PDF invoice. No dashboard, no login, no account creation. Send a POST, get a PDF.

Payment is 0.50 USDT per invoice via x402, settled on X Layer. No subscription, no monthly commitment. You pay when you invoice. If you send ten invoices a month, you spend five dollars. If you send zero, you spend zero.

It is for solo devs, freelancers, and small agencies who send invoices in bursts. Anyone who has ever opened Google Docs at midnight to format an invoice.

**How it works:**

1. POST your job description (e.g. "website redesign, 40 hours at 75/hr, plus hosting setup 200")
2. Pay the 0.50 USDT x402 challenge
3. Receive structured invoice JSON and a downloadable PDF

**Pricing:** 0.50 USDT/invoice settled on X Layer

**Endpoint:** POST /api/v1/invoice

**Tags:** Invoice, PDF, x402, USDT, X Layer, OKX.AI, Freelance
