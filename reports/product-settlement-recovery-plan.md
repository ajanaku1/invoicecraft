# Product settlement recovery plan

Status: read-only plan; no recovery transaction is authorized or submitted.

## Decision

Do not submit the prepared `0xFE` user operation. Its product settlement
deadline has expired, so `executeDirectMintingWithData` would revert atomically.
The safe protocol path is a guided `0xE0` skip-memo recovery, followed by a
separately authorized product settlement attempt with a fresh quote, deadline,
nonce, and XRPL signature.

This recovery returns value as FXRP on Coston2. It does not refund native XRP
to the XRPL wallet and it does not complete the InvoiceCraft USD₮0 payout.

## Stuck payment

- XRPL transaction:
  `C2688B1BF367AE4E1D8A095A093E6AE00C7D7EB701FBD960EBBE54B7C60D6165`
- Validated XRPL timestamp: `2026-08-11T12:25:22Z`
- Gross payment: `10.1 XRP`
- Personal account: `0x8107f6a07c3be8ab606bb3718c4316c954aca580`
- Committed UserOp nonce: `1`
- Product settlement deadline: `2026-08-11T12:39:02Z`
- Full attempt evidence: `evidence/product-settlement-attempt.json`

Around Coston2 block `0x205997f` (`2026-08-11T13:02:02Z`), read-only
RPC calls showed:

- `MasterAccountController`:
  `0x434936d47503353f06750db1a444dbdc5f0ad37c`
- `AssetManagerFXRP`:
  `0xc1ca88b937d0b528842f95d5731ffb586f4fbdfa`
- `isTransactionIdUsed(stuckTxId) == false`
- `getNonce(personalAccount) == 1`
- `getExecutor(personalAccount) == address(0)`
- `getPaymentProofValidityDurationSeconds() == 86400`
- `AssetManagerFXRP.getSettings().attestationWindowSeconds == 86400`
- No direct-mint delay was recorded for the stuck transaction.

The controller and AssetManager both report a 24-hour proof window. This gives
an outer bound of
`2026-08-12T12:25:22Z` for accepting the original payment proof. Treat this as
a hard upper bound, not an operating target: FDC request finalization and both
mint executions need time before it. Re-read every live value immediately
before any authorized recovery.

The personal account currently holds `10,000,000` UBA of FXRP, but the stuck
transaction ID is unused and its nonce is unchanged. Therefore that balance is
not evidence that this payment was minted.

## Why `0xE0` applies

Flare's recovery guide explicitly covers both cases where
`executeDirectMintingWithData` reverted and where the executor never submitted
the FDC proof. Recovery is allowed only while the original transaction remains
unused. The `0xE0` memo has the fixed layout:

`[0xE0 | walletId(1B) | executorFeeUBA(8B) | targetTxId(32B)]`

Finalizing that recovery payment emits `IgnoreMemoSet`. The original payment
can then be finalized with its original proof and original packed UserOp bytes;
the controller mints FXRP while skipping the expired settlement call.

Protocol references:

- [Flare: Recover Stuck Mint Transaction](https://dev.flare.network/smart-accounts/guides/typescript-viem/recover-stuck-mint-transaction-ts)
- [Flare: Custom Instruction failure and recovery](https://dev.flare.network/smart-accounts/custom-instruction#recovery-after-a-failed-mint)
- [Flare: AssetManager direct-mint reference](https://dev.flare.network/fassets/reference/IAssetManager#execute-direct-minting)
- [Flare: MasterAccountController reference](https://dev.flare.network/smart-accounts/reference/IMasterAccountController)
- [Flare Coston2 FXRP deployment parameters](https://github.com/flare-foundation/fassets/blob/main/deployment/config/coston2/f-testxrp.json)

## Exact authorized recovery sequence

Recovery is not one transaction. Because no FDC request was submitted for the
original payment, a future authorization must explicitly cover all five
mutations below:

1. Xaman signs one new XRPL Payment to the live Core Vault with the `0xE0`
   memo targeting the stuck transaction ID. It must have a positive net mint;
   current parameters and Flare's official recovery helper imply `1.2 XRP`
   gross for `1 XRP` net: `0.1 XRP` minting fee plus `0.1 XRP` standard
   executor fee. The application must recompute the exact amount and fees at
   authorization.
2. Rabby signs the Coston2 FDC attestation request for the recovery payment.
3. Rabby signs `executeDirectMintingWithData(recoveryProof, 0x)` to finalize the
   recovery payment and verify `IgnoreMemoSet` for the stuck transaction ID.
4. Rabby signs the Coston2 FDC attestation request for the original payment.
5. Rabby signs `executeDirectMintingWithData(originalProof, originalUserOp)`.
   The skip flag must suppress `UserOperationExecuted`; the receipt must instead
   prove the original transaction was minted to the personal account.

Stop immediately if any preflight changes: the original transaction becomes
used, the nonce changes unexpectedly, the Core Vault or registry bindings
change, the proof window is too short, live fees cannot be reproduced, or a
wallet simulation reports a revert.

## Wallet and secret boundary

- Xaman signs XRPL transactions on the user's device.
- Rabby signs every Coston2 transaction in the browser.
- The application may retain public transaction artifacts and the already
  committed packed UserOp, but it must never request, store, log, or derive a
  wallet seed or private key.
- Do not use the official sample's `XRPL_SEED` environment-variable approach in
  InvoiceCraft.
- Rotate `XRP_OPERATOR_TOKEN` before restarting the local operator service,
  because the previous browser automation session exposed that local bearer
  token in tool output. It was not a wallet key.

## Evidence required before recovery is accepted

- The new XRPL recovery transaction is validated with exact destination,
  amount, and `0xE0` memo bytes.
- Both FDC request transactions and both execution transactions have successful
  Coston2 receipts.
- The recovery execution emits `IgnoreMemoSet` for the exact personal account
  and original XRPL transaction ID.
- The original transaction changes from unused to used exactly once.
- The retry receipt does not contain `UserOperationExecuted` for nonce `1`.
- The personal-account FXRP balance delta matches the two net mints and fees.
- No InvoiceCraft `InvoiceSettled`, adapter liquidity, or beneficiary USD₮0
  transfer is claimed from recovery.

After recovery, a new product smoke remains necessary. It needs its own explicit
authorization and fresh end-to-end evidence; recovery cannot be relabeled as the
failed invoice settlement.
