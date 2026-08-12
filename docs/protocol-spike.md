# Phase 0 protocol spike

This artifact records a completed Phase 0 Testnet checkpoint: RPC-validated XRPL Testnet
Payment `1CBE730F2C98A858DA769C654BB2CD3B2F7F5DB2165BE0AAA76A11D654F788CC`, Coston2 FDC request
`0x9552053f15c27f99ab7177243ceeb32b2b46021ea7d88f181ed8cf990050edec`, and Coston2 direct-mint
execute transaction `0x9b16227528c10af0f5ec4e46018d9f9dabe5af6235dfb8955d6932f4bf3fa1b6`. It accounts for
10.1 XRP delivered, 10 FXRP net minted, a 0.1 XRP protocol fee, and zero memo executor fee.
This Phase 0 record does not, by itself, assert the later product settlement:
no mainnet or real-funds action occurred. The later testnet deployment and
product settlement have separate evidence records under `evidence/`.

The `0xFE` custom-instruction memo is fixed at 42 bytes:

```text
0xFE | walletId (1 byte) | executorFeeUBA (uint64 big-endian) | keccak256(exact ABI PackedUserOperation bytes)
```

The builder deliberately accepts pre-encoded `PackedUserOperation` ABI bytes and preserves
them verbatim. It does not construct individual UserOp fields. A payment template contains one
uppercase `MemoData` value and never includes `DestinationTag`; it is unsigned and cannot submit
or broadcast a transaction.

Recovery classification is guided only. An explicit XRPL rejection does not deliver the payment
principal, though the XRPL network fee can still be charged. A finalized payment can remain
pending or delayed on Flare, and a reverted custom execution is directed to a verified `0xE0`
skip-memo process. Nothing in this spike moves or refunds funds automatically.

`evidence/protocol-spike.json` now contains the independently RPC-validated completed Phase 0
record. The local builder and tests remain deterministic: they construct and inspect exact public
bytes, never ingest keys, auto-connect, send, switch networks, or publish evidence. Completed
validation requires XRPL success, exact payment binding, no destination tag, Coston2 identity,
and a block-consistent successful receipt. The XRPL timestamp equals the validated transaction's
Ripple-epoch `date`; the Flare timestamp equals the execution block timestamp. Fee values are
queried at that same block.

The validator owns its read authority. It makes XRPL requests only to
`https://s.altnet.rippletest.net:51234/` and Flare requests only to
`https://coston2-api.flare.network/ext/C/rpc`; evidence may record those URLs but cannot replace
them. The XRPL endpoint must report Testnet `server_info.info.network_id == 1`, Coston2 must
report chain ID 114, and HTTP redirects are rejected. The endpoints and Coston2 chain ID are from
the official network references below; `server_info` is the official XRPL RPC identity source.

For this XRP-only flow, the completed XRPL record must be a successful native-drop `Payment` with
no `tfPartialPayment` flag (`0x00020000`). Its `meta.delivered_amount` must be a decimal drops
string equal to the requested `Amount`, and both values must bind the evidence. The verifier uses
that delivered amount for all fee math; it rejects missing, unavailable, issued-currency/object,
or mismatched values.

At that block the verifier resolves both `AssetManagerFXRP` and `MasterAccountController` from
the official Flare Contract Registry `0xaD67FE66660Fb8dFE9d6b1b4240d8650e30F6019`. The asset
manager result must equal the transaction target. It also calls
`directMintingPaymentAddress()` on that resolved asset manager and requires the returned XRPL
Core Vault address to equal the payment destination. A successful receipt is insufficient on its
own: it must include a `UserOperationExecuted(address indexed personalAccount,uint256 nonce)`
log emitted by the resolved controller, with topic 0 computed from the canonical signature,
topic 1 equal to the ABI-padded decoded `PackedUserOperation.sender`, and data equal to its
decoded nonce.

The canonical selector is `0xa7556da6`, computed locally from the current official signature:

```text
executeDirectMintingWithData(
  (bytes32[],(bytes32,bytes32,uint64,uint64,(bytes32,address),
  (uint64,uint64,string,bytes32,bytes32,bytes32,int256,int256,int256,int256,
  bool,bytes,bool,uint256,uint8))),bytes)
```

The validator decodes the proof's `RequestBody.transactionId` and the ABI `bytes _data` argument;
it then decodes the ABI `PackedUserOperation` tuple's `sender` word, which must equal the recorded
personal-account receiver. This replaces substring-only linkage and self-asserted selectors.

At the same receipt block it queries the direct-mint protocol-fee getters,
`assetMintingGranularityUBA()`, `minimumRedeemAmountUBA()`, and `getSettings()`. The current
official `AssetManagerSettings.Data` layout puts `assetDecimals` at ABI head word 11 and
`lotSizeAMG` at word 19 (zero based). It computes `lotSizeUBA = lotSizeAMG ×
assetMintingGranularityUBA`. The contract-aligned fee equation uses the actual XRPL delivered
amount, not the lot size:

```text
protocolFeeUBA = min(max(floor(deliveredAmountUBA × feeBIPS / 10,000), minimumFeeUBA), deliveredAmountUBA)
netMintedUBA = deliveredAmountUBA - protocolFeeUBA - memoExecutorFeeUBA
require netMintedUBA >= lotSizeUBA
```

`memoExecutorFeeUBA` is decoded from bytes 2–9 of the `0xFE` memo and is independent of the
standard-recipient `getDirectMintingExecutorFeeUBA()` setting. The latter is recorded only as
`standard_direct_mint_executor_fee_uba` informational evidence; it does not enter the smart
account payment calculation. Completed evidence records the delivered amount, protocol fee,
memo executor fee, net minted amount, and lot conversion. Decimal XRP display values are derived
from the live asset decimals. This avoids hardcoded 10- or 5-XRP thresholds.

Protocol sources: [Flare custom instruction](https://dev.flare.network/smart-accounts/custom-instruction),
[Flare IAssetManager reference](https://dev.flare.network/fassets/reference/IAssetManager),
[Flare direct minting](https://dev.flare.network/fassets/direct-minting),
[Flare cross-chain mint TypeScript guide](https://dev.flare.network/smart-accounts/guides/typescript-viem/cross-chain-mint-ts),
[Flare MasterAccountController reference](https://dev.flare.network/smart-accounts/reference/IMasterAccountController),
[official DirectMintingFacet source](https://github.com/flare-foundation/fassets/blob/main/contracts/assetManager/facets/DirectMintingFacet.sol),
[official SafePct source](https://github.com/flare-foundation/fassets/blob/main/contracts/utils/library/SafePct.sol),
[IXRPPayment source/reference](https://dev.flare.network/fdc/reference/IXRPPayment),
[official settings guide](https://dev.flare.network/fassets/developer-guides/fassets-settings-node),
[official AssetManagerSettings source](https://github.com/flare-foundation/fassets/blob/main/contracts/userInterfaces/data/AssetManagerSettings.sol), and
[XRPL Payment](https://xrpl.org/docs/references/protocol/transactions/types/payment),
[XRPL partial payments and delivered amount](https://xrpl.org/docs/concepts/payment-types/partial-payments),
[XRPL server_info](https://xrpl.org/docs/references/http-websocket-apis/public-api-methods/server-info-methods/server_info),
[XRPL public servers](https://xrpl.org/docs/tutorials/public-servers), and
[Flare Coston2 network configuration](https://dev.flare.network/network/overview).
