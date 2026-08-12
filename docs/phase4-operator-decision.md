# Phase 4 operator decision

## Chosen workflow

InvoiceCraftXRP needs an operator to submit the Coston2 FDC request and
`executeDirectMintingWithData` transaction after the client signs the XRPL
Payment. The application must not store a Coston2 private key, and the
XRP-native payer is not expected to own an EVM wallet.

The recommended testnet workflow is a browser-operated queue:

1. The public API verifies the finalized XRPL Payment and stores a pending
   operator job.
2. A local operator page prepares exact Coston2 transaction bytes and displays
   every target, value, calldata hash, and purpose.
3. The operator explicitly connects the previously selected Chrome wallet and
   approves each Coston2 transaction.
4. The API records only public hashes and progresses the invoice through
   bounded RPC validation.

This preserves the approved architecture: one XRP payment from the payer, no
private key in the app, no automatic recovery, and no new runtime dependency.
The operator queue and operator-only API boundary are implemented and covered
by API, browser, and live-adapter tests.

## Rejected defaults

- A server-held Coston2 key conflicts with the no-private-key constraint.
- Asking the XRP payer to connect an EVM wallet changes the product journey.
- Treating the Phase 0 direct-mint receipt as a product settlement would be
  false; it contains no product-contract payout.
- Assuming a public executor will perform the custom zero-fee UserOp is not an
  evidence-backed operating model.

## Deployment record

Read-only Coston2 RPC checks at block `0x2027844` verified:

- FXRP: `0x0b6a3645c240605887a5532109323a3e12273dc7`, `FTestXRP`, 6 decimals.
- Faucet USD₮0 test: `0xc1a5b41512496b80903d1f32d6dea3a73212e71f`, `USD₮0`, 6 decimals.
- Standard deterministic deployment proxy:
  `0x4e59b44847b379578588920ca78fbf26c0b4956c` has deployed bytecode.

`InvoiceSettlementDeployment` bootstrapped the adapter, settlement contract,
and one-time authorization in one deployment transaction. The deployed
addresses and RPC checks are recorded in `evidence/product-deployment.json`.

## Recorded settlement

The adapter received 10 USD₮0 test in the recorded funding transaction. The
authorized product smoke then paid exactly 5 USD₮0 to the beneficiary through
the labelled test adapter.

The deployment, funding, and payout transactions are bound in
`evidence/product-deployment.json` and
`evidence/product-settlement-smoke.json`. The product smoke is intentionally a
5 USD₮0 testnet settlement; it is not represented as a $75 payout.

## Operational boundary

Any future transaction requires fresh authorization and must stop for visible
wallet approval at each step. The application must not store private keys.
