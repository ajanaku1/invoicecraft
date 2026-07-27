/**
 * Browser-side x402 client for the OKX Payment SDK's `exact` EVM scheme.
 *
 * The server (okxweb3-app-x402) answers an unpaid request with 402 and the
 * payment requirements base64-encoded in the PAYMENT-REQUIRED header. This
 * module signs an EIP-3009 TransferWithAuthorization over those requirements
 * and packs it into the PAYMENT-SIGNATURE header the SDK expects on the retry.
 *
 * Structures mirror x402/mechanisms/evm/{types,eip712}.py and
 * x402/schemas/payments.py, so the server's decoder validates the result.
 */
(function (root) {
  'use strict';

  var PAYMENT_REQUIRED_HEADER = 'payment-required';
  var PAYMENT_SIGNATURE_HEADER = 'PAYMENT-SIGNATURE';
  // Clock-skew allowance and default lifetime, matching the SDK's defaults.
  var VALIDITY_BUFFER = 600;
  var DEFAULT_VALIDITY_PERIOD = 3600;

  var EIP712_DOMAIN_TYPE = [
    { name: 'name', type: 'string' },
    { name: 'version', type: 'string' },
    { name: 'chainId', type: 'uint256' },
    { name: 'verifyingContract', type: 'address' }
  ];

  var TRANSFER_WITH_AUTHORIZATION_TYPE = [
    { name: 'from', type: 'address' },
    { name: 'to', type: 'address' },
    { name: 'value', type: 'uint256' },
    { name: 'validAfter', type: 'uint256' },
    { name: 'validBefore', type: 'uint256' },
    { name: 'nonce', type: 'bytes32' }
  ];

  function base64EncodeUtf8(text) {
    var bytes = new TextEncoder().encode(text);
    var binary = '';
    for (var i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }

  function base64DecodeUtf8(value) {
    var binary = atob(value);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TextDecoder().decode(bytes);
  }

  /** Decode the PAYMENT-REQUIRED header into the x402 requirements object. */
  function decodeRequirements(headerValue) {
    if (!headerValue) return null;
    try {
      return JSON.parse(base64DecodeUtf8(headerValue));
    } catch (e) {
      return null;
    }
  }

  function randomNonce() {
    var bytes = new Uint8Array(32);
    crypto.getRandomValues(bytes);
    var hex = '';
    for (var i = 0; i < bytes.length; i++) {
      hex += ('0' + bytes[i].toString(16)).slice(-2);
    }
    return '0x' + hex;
  }

  function chainIdOf(network) {
    // CAIP-2, e.g. "eip155:196" -> 196.
    var parts = String(network || '').split(':');
    return parseInt(parts[1], 10);
  }

  /** Build the EIP-3009 authorization the payer is asked to sign. */
  function buildAuthorization(accept, from, now) {
    var issuedAt = typeof now === 'number' ? now : Math.floor(Date.now() / 1000);
    var lifetime = accept.maxTimeoutSeconds || DEFAULT_VALIDITY_PERIOD;
    return {
      from: from,
      to: accept.payTo,
      value: accept.amount,
      validAfter: String(issuedAt - VALIDITY_BUFFER),
      validBefore: String(issuedAt + lifetime),
      nonce: randomNonce()
    };
  }

  /** EIP-712 typed data for eth_signTypedData_v4. */
  function buildTypedData(accept, authorization) {
    var extra = accept.extra || {};
    return {
      types: {
        EIP712Domain: EIP712_DOMAIN_TYPE,
        TransferWithAuthorization: TRANSFER_WITH_AUTHORIZATION_TYPE
      },
      domain: {
        name: extra.name,
        version: extra.version || '1',
        chainId: chainIdOf(accept.network),
        verifyingContract: accept.asset
      },
      primaryType: 'TransferWithAuthorization',
      message: authorization
    };
  }

  /** Pack the signed authorization into a PAYMENT-SIGNATURE header value. */
  function buildPaymentHeader(requirements, accept, authorization, signature) {
    var payload = {
      x402Version: requirements.x402Version || 2,
      payload: { authorization: authorization, signature: signature },
      accepted: accept
    };
    if (requirements.resource) payload.resource = requirements.resource;
    return base64EncodeUtf8(JSON.stringify(payload));
  }

  /**
   * Sign the requirements with an EIP-1193 provider and return the header value.
   * `provider` must already be connected and on the right chain.
   */
  function signPayment(provider, requirements, account) {
    var accept = requirements.accepts && requirements.accepts[0];
    if (!accept) return Promise.reject(new Error('No payment option offered'));
    if (accept.scheme !== 'exact') {
      return Promise.reject(new Error('Unsupported payment scheme: ' + accept.scheme));
    }
    if (!(accept.extra && accept.extra.name)) {
      return Promise.reject(new Error('Payment requirements are missing the token name'));
    }

    var authorization = buildAuthorization(accept, account);
    var typedData = buildTypedData(accept, authorization);

    return provider
      .request({
        method: 'eth_signTypedData_v4',
        params: [account, JSON.stringify(typedData)]
      })
      .then(function (signature) {
        return buildPaymentHeader(requirements, accept, authorization, signature);
      });
  }

  root.X402Client = {
    PAYMENT_REQUIRED_HEADER: PAYMENT_REQUIRED_HEADER,
    PAYMENT_SIGNATURE_HEADER: PAYMENT_SIGNATURE_HEADER,
    decodeRequirements: decodeRequirements,
    buildAuthorization: buildAuthorization,
    buildTypedData: buildTypedData,
    buildPaymentHeader: buildPaymentHeader,
    chainIdOf: chainIdOf,
    signPayment: signPayment
  };
})(typeof window !== 'undefined' ? window : globalThis);
