(function () {
  'use strict';

  var API_BASE = (window.INVOICECRAFT_API || window.location.origin).replace(/\/$/, '');
  var createButton = document.getElementById('createXrpBtn');
  var beneficiaryInput = document.getElementById('xrpBeneficiary');
  var errorMessage = document.getElementById('xrpCreateError');
  var pendingFingerprint = '';
  var pendingKey = '';

  function value(id) {
    var field = document.getElementById(id);
    return field ? field.value.trim() : '';
  }

  function party(fields) {
    var result = {};
    Object.keys(fields).forEach(function (name) {
      var fieldValue = value(fields[name]);
      if (fieldValue) result[name] = fieldValue;
    });
    return result;
  }

  function requestBody() {
    var body = {
      description: value('desc'),
      beneficiary: beneficiaryInput.value.trim(),
      currency: (value('currency') || 'USD').toUpperCase(),
      tax_rate: value('taxRate') || '0'
    };
    var issuer = party({ name: 'issuerName', email: 'issuerEmail', address: 'issuerAddress' });
    var client = party({ name: 'billName', email: 'billEmail', address: 'billAddress' });
    if (Object.keys(issuer).length) body.issuer = issuer;
    if (Object.keys(client).length) body.client = client;
    return body;
  }

  function validBeneficiary(address) {
    return /^0x[0-9a-fA-F]{40}$/.test(address) && !/^0x0{40}$/.test(address);
  }

  function showError(message) {
    beneficiaryInput.setAttribute('aria-invalid', String(Boolean(message)));
    errorMessage.textContent = message || '';
    errorMessage.hidden = !message;
  }

  function idempotencyKey(body) {
    var fingerprint = JSON.stringify(body);
    if (pendingKey && fingerprint === pendingFingerprint) return pendingKey;
    pendingFingerprint = fingerprint;
    pendingKey = window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : 'xrp-' + Date.now() + '-' + Math.random().toString(16).slice(2);
    return pendingKey;
  }

  function createInvoice(body) {
    return fetch(API_BASE + '/api/v1/xrp/invoices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey(body) },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().then(function (result) {
        if (response.ok) return result;
        throw new Error(result.message || 'The XRP invoice could not be created.');
      });
    });
  }

  function shareUrl(path) {
    return new URL(path, window.location.origin).href;
  }

  function renderShare(record) {
    var url = shareUrl(record.share_url);
    var shareLink = document.getElementById('xrpShareLink');
    var openLink = document.getElementById('openXrpLink');
    shareLink.href = url;
    shareLink.textContent = url;
    openLink.href = url;
    document.getElementById('xrpSharePanel').hidden = false;
    pendingFingerprint = '';
    pendingKey = '';
  }

  function validate(body) {
    if (body.description.length < 10) return 'Describe at least ten characters of work first.';
    if (!body.issuer || !body.issuer.name) return 'Add your business name so the invoice identifies its issuer.';
    if (!validBeneficiary(body.beneficiary)) return 'Enter a non-zero 20-byte Flare payout address.';
    return '';
  }

  function setBusy(busy) {
    createButton.disabled = busy;
    createButton.setAttribute('aria-busy', String(busy));
    createButton.textContent = busy ? 'Creating persistent link…' : 'Create free XRP invoice';
  }

  function handleCreate() {
    var body = requestBody();
    var validationError = validate(body);
    showError(validationError);
    if (validationError) return;
    setBusy(true);
    createInvoice(body).then(renderShare).catch(function (error) {
      showError(error.message);
    }).finally(function () { setBusy(false); });
  }

  function copyLink() {
    var url = document.getElementById('xrpShareLink').href;
    if (!navigator.clipboard) return showError('Copy is unavailable; select the link above instead.');
    navigator.clipboard.writeText(url).then(function () {
      document.getElementById('copyXrpLink').textContent = 'Copied';
    }).catch(function () { showError('Copy failed; select the link above instead.'); });
  }

  createButton.addEventListener('click', handleCreate);
  document.getElementById('copyXrpLink').addEventListener('click', copyLink);
})();
