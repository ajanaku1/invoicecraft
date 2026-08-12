# Draft report: Coston2 provider rejects `web3_sha3`

## Summary

During the Phase 0 browser signing flow, an injected EIP-1193 wallet connected
to Coston2 (`0x72`) but returned this error when the page requested
`web3_sha3`:

```text
method [web3_sha3] doesn't has corresponding handler
```

The failure prevented the browser signer from preparing the transaction through
that provider. No secret or private key was exposed to the page.

## Environment

- Browser: Google Chrome on macOS
- Provider: injected EIP-1193 wallet selected by the user
- Chain: Coston2, chain ID `0x72`
- Page: static browser signer in `scripts/coston2_signer.html`
- Date observed: 6 August 2026

## Reproduction

1. Add Coston2 to an EIP-1193 browser wallet.
2. Open the static signer page.
3. Select the wallet and connect.
4. Confirm that `eth_chainId` returns `0x72`.
5. Prepare a transaction whose helper requests `web3_sha3`.

## Expected result

The provider returns a Keccak-256 digest for the supplied hex bytes, or the
wallet documents that the method is unsupported before a signing attempt.

## Actual result

The provider reports that `web3_sha3` has no handler. The transaction flow does
not open a signing confirmation.

## Local mitigation

InvoiceCraft no longer depends on the injected provider for Keccak hashing. It
computes and verifies selectors in the application code, then rechecks the
chain, signer, destination, value, and calldata immediately before requesting a
wallet signature.

This draft has not been posted upstream.
