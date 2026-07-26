# InvoiceCraft — 90-Second Demo Script

**Format:** Timestamp / Narration / On-Screen Action

---

## 0:00 — 0:10 | Hook

**Narration:**  
Describe a job in plain English. Get a PDF invoice back. No account, no subscription, no monthly fee.

**Screen:**  
Full-screen shot of the InvoiceCraft dashboard on load. Clean input panel on the left, empty invoice preview on the right. Pause for a beat so the viewer sees the layout.

---

## 0:10 — 0:25 | Generate

**Narration:**  
You type something like "Web development for Acme Corp — homepage redesign and CMS integration." Hit Generate.

**Screen:**  
Cursor types into the textarea: "Website redesign for Acme Corp including responsive homepage, CMS integration, and performance optimization." Click the Generate Invoice button. The UI shifts — the payment card slides in below the button, the invoice preview dims slightly.

---

## 0:25 — 0:40 | Payment Challenge

**Narration:**  
A 402 payment challenge appears. It wants 0.50 USDT on X Layer via the x402 protocol. One click to pay. The invoice stays locked until then.

**Screen:**  
Zoom into the payment card. It shows Amount: 0.50 USDT, Network: X Layer, a Challenge ID hex string. The Simulate Payment button is highlighted. The invoice preview on the right is still empty with placeholder data.

---

## 0:40 — 0:55 | Invoice Unlocked

**Narration:**  
Click simulate. A brief loading spinner, then the invoice fills in. Line items, client info, subtotal, tax, total due. All parsed from that one sentence you typed.

**Screen:**  
Click the Simulate Payment button. It shows a spinner for a moment, then a checkmark and "Payment Successful." The invoice preview populates: Acme Corp as client, a line item row for the website description, subtotal $500.00, tax $40.00, total $540.00. The x402 badge in the footer lights up.

---

## 0:55 — 1:10 | Download & Payment Badge

**Narration:**  
Hit Download PDF to save the invoice. The x402 badge in the footer confirms the payment settled on X Layer. Each invoice is a single micropayment — no subscriptions, no forgotten renewals.

**Screen:**  
Cursor clicks the Download PDF button in the top right of the preview panel. Toast notification confirms the download. Camera lingers on the footer: the payment address, the x402 badge with the lightning bolt icon.

---

## 1:10 — 1:25 | API Demo

**Narration:**  
The same flow works from the terminal. POST a description to the API. Get a 402 challenge back with the payment details. Pay it. Retry with the transaction hash. Get your PDF.

**Screen:**  
Split screen or terminal overlay. A curl command appears:

```
curl -X POST https://api.invoicecraft.ai/api/v1/invoice \
  -H "Content-Type: application/json" \
  -d '{"description": "Web development for Acme Corp"}'
```

Response shows the 402 with payment fields. Then a second curl with the payment_tx_hash. Response shows 200 with invoice JSON and the base64 PDF field. Scroll down to show the PDF field.

---

## 1:25 — 1:30 | Close

**Narration:**  
InvoiceCraft. Live on OKX.AI. Pay per invoice, not per month.

**Screen:**  
Full logo lockup centered. URL: invoicecraft.ai and "Available on OKX.AI" with the OKX logo. Fade to black.
