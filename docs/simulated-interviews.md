# Simulated InvoiceCraft interviews

> **Demo material — not user research.** Every participant and response below
> is fictional. These transcripts are scripted product-review scenarios for a
> demo and must not be cited as interviews, validation, testimonials, or
> hackathon acceptance evidence.

## Scenario 1 — independent product designer

**Profile:** A freelancer who invoices overseas clients and normally receives
bank transfers.

**How do clients pay you now?**

Usually Wise or a bank transfer. The payment itself is fine; the annoying part
is matching a payment to the right invoice when the reference is missing.

**What would you need to see before accepting XRP for a USD invoice?**

I would want the XRP amount locked for a few minutes, a clear expiry, and the
exact amount I will receive. I should not have to work out whether a network
fee changes what lands in my wallet.

**What is unclear in the demo?**

“FDC finality” sounds like infrastructure language. I understand “XRP payment
confirmed” much faster. The two transaction hashes are useful, but I would
hide them behind a receipt-details section.

**What should happen during a delay?**

Show that the XRP is safe and say which step is still running. Give me one
reference number I can send to support instead of asking me to copy several
hashes.

**Scenario takeaway:** Use customer-facing status labels and group technical
proof under expandable receipt details.

## Scenario 2 — small software agency owner

**Profile:** Runs a three-person agency and sometimes accepts stablecoins from
repeat clients.

**How do clients pay you now?**

Mostly ACH and USDC. Crypto is quick when both sides already know what they are
doing, but a first-time client always asks which network to use.

**Would exact stablecoin settlement be useful?**

Yes. I quote projects in dollars and pay contractors in dollars, so receiving
XRP would create another treasury decision. Exact settlement means the invoice
can close without someone adjusting it by a few cents.

**What makes you hesitate?**

The test adapter label is honest, but I would need a plain explanation of who
provides the stablecoin in a real deployment. I would also want to know what
happens if that source runs out halfway through the process.

**What would stop you using it?**

If I cannot export a receipt with the invoice number, paid amount, payer
transaction, settlement transaction, and timestamps, accounting will reject
it.

**Scenario takeaway:** Keep the adapter limitation visible and make the
dual-ledger receipt exportable and invoice-linked.

## Scenario 3 — video editor paid by international clients

**Profile:** A solo contractor who has received crypto twice but does not use
it regularly.

**Tell me about your last crypto payment.**

The client sent a token with the right name on the wrong network. I eventually
recovered it, but the experience made me nervous about wallet prompts.

**What would you check before paying?**

The amount, the network, and who receives it. I would not recognize a contract
address, so showing only the address would not reassure me. The wallet should
say Coston2 testnet very clearly before I approve anything.

**How does the current flow feel?**

Two wallet approvals after the XRP payment surprised me. The page needs to say
up front that the operator will make two testnet transactions, otherwise the
second prompt feels like the first one failed.

**What recovery action would you expect?**

A button that checks again. If I need help, give me a copyable status summary
that does not include anything secret.

**Scenario takeaway:** Set expectations for the exact number of wallet actions
before the flow begins and provide a safe, copyable recovery summary.

## Scenario 4 — bookkeeping consultant

**Profile:** Helps small creative businesses reconcile invoices and contractor
payments.

**What proof matters to you?**

“Paid” is not enough. I need the invoice currency, the quoted exchange rate,
what the client sent, what the contractor received, fees, and both timestamps.
Those values should remain fixed after settlement.

**What is clear in the receipt?**

Separating the XRP payment from the USD₮0 payout is helpful. I would add a
short sentence explaining that they are two records for one invoice, not two
charges.

**Would you use explorer links?**

Only when something goes wrong. Keep them, but do not make them the primary
proof for a nontechnical user. A downloadable receipt is more useful day to
day.

**What would stop you approving the product?**

If the displayed dollar amount can be edited after the client pays, or if the
receipt does not preserve the quote used at payment time.

**Scenario takeaway:** Treat settlement values as immutable receipt data and
explain why two ledger records appear.

## Scenario 5 — developer who invoices in crypto

**Profile:** A technical freelancer comfortable with browser wallets and block
explorers.

**What do you look for before signing?**

Chain ID, target, value, and calldata. I like that the operator screen exposes
those fields, but the page should warn me if the connected account differs
from the expected signer.

**What makes the design trustworthy?**

Keeping private keys in Rabby and Xaman is the right boundary. The app should
never ask me to paste a seed phrase or private key, even in a recovery flow.

**What failure worries you most?**

The XRP payment becoming final while the Coston2 step stalls. I would want a
state machine that distinguishes waiting, retryable, and manual review. A
generic error would make me wonder whether retrying could pay twice.

**What would you change first?**

Show transaction simulations before each approval and disable the approval
button if the prepared bytes change. That makes the operator’s confirmation
meaningful.

**Scenario takeaway:** Preserve signer and transaction-byte checks, show
simulation results, and make recovery states explicit and replay-safe.

## How these scenarios may be used

Use them to rehearse the demo, test whether the interface answers predictable
questions, or draft a future research plan. If real interviews are conducted,
store those notes separately using [the interview guide](interview-guide.md)
and never replace a participant’s words with text from this file.
