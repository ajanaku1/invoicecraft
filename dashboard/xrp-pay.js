(function () {
  'use strict';

  var API_BASE = (window.INVOICECRAFT_API || window.location.origin).replace(/\/$/, '');
  var INVOICE_API = '/api/v1/xrp/invoices/';
  var SETTLEMENT_STATES = [
    'open', 'quoted', 'awaiting_signature', 'xrpl_submitted',
    'flare_executing', 'paid', 'quote_expired', 'payment_rejected',
    'recovery_required'
  ];
  var STEP_FOR_STATE = {
    open: 0, quoted: 0, quote_expired: 0,
    awaiting_signature: 1, payment_rejected: 1,
    xrpl_submitted: 2, flare_executing: 3,
    recovery_required: 3, paid: 4
  };
  var STAMP_FOR_STATE = {
    open: 'Invoice ready', quoted: 'Quote ready', quote_expired: 'Quote expired',
    awaiting_signature: 'Awaiting signature', payment_rejected: 'Payment rejected',
    xrpl_submitted: 'XRPL submitted', flare_executing: 'Flare executing',
    recovery_required: 'Recovery required', paid: 'Paid · verified'
  };
  var currentRecord = null;
  var currentStep = 0;
  var renderedState = '';
  var pollTimer = null;
  var quoteTimer = null;

  function element(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    element(id).textContent = value == null || value === '' ? '—' : String(value);
  }

  function setHidden(id, hidden) {
    element(id).hidden = hidden;
  }

  function invoiceId() {
    var parts = window.location.pathname.split('/').filter(Boolean);
    var value = parts[0] === 'pay' ? parts[1] : '';
    return value && value.length <= 128 && !/\s/.test(value) ? value : '';
  }

  function apiPath(suffix) {
    return INVOICE_API + encodeURIComponent(invoiceId()) + (suffix || '');
  }

  function request(path, options) {
    return fetch(API_BASE + path, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.ok) return body;
        var error = new Error(body.message || 'The request could not be completed.');
        error.code = body.error || 'request_failed';
        error.status = response.status;
        throw error;
      });
    });
  }

  function invoiceDetails(record) {
    return record && typeof record.invoice === 'object' ? record.invoice : {};
  }

  function partyName(invoice, party, fallback) {
    var value = invoice[party];
    return value && value.name ? value.name : fallback;
  }

  function firstDescription(invoice) {
    var items = Array.isArray(invoice.line_items) ? invoice.line_items : [];
    return items[0] && items[0].description ? items[0].description : 'Services rendered';
  }

  function money(invoice) {
    var amount = Number(invoice.total);
    if (!Number.isFinite(amount)) return '—';
    return invoice.currency === 'USD' ? '$' + amount.toFixed(2) : amount.toFixed(2) + ' ' + invoice.currency;
  }

  function payout(invoice) {
    var amount = Number(invoice.total);
    return Number.isFinite(amount) ? amount.toFixed(2) + ' USD₮0' : '— USD₮0';
  }

  function shortValue(value) {
    if (typeof value !== 'string') return '—';
    return value.length > 22 ? value.slice(0, 12) + '…' + value.slice(-6) : value;
  }

  function formatUnits(value, suffix) {
    if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
    var formatted = (value / 1000000).toFixed(6).replace(/\.?0+$/, '');
    return formatted + ' ' + suffix;
  }

  function showPageError(title, message) {
    setText('pageErrorTitle', title);
    setText('pageErrorMessage', message);
    setHidden('pageError', false);
    element('record').setAttribute('aria-busy', 'false');
  }

  function clearPageError() {
    setHidden('pageError', true);
  }

  function setButtonBusy(button, busy, busyLabel, readyLabel) {
    button.disabled = busy;
    button.setAttribute('aria-busy', String(busy));
    button.textContent = busy ? busyLabel : readyLabel;
  }

  function showFieldError(inputId, errorId, message) {
    element(inputId).setAttribute('aria-invalid', String(Boolean(message)));
    setText(errorId, message || '');
    setHidden(errorId, !message);
  }

  function selectStep(index, moveFocus) {
    currentStep = Math.max(0, Math.min(4, index));
    document.querySelectorAll('[data-panel]').forEach(function (panel, panelIndex) {
      panel.hidden = panelIndex !== currentStep;
    });
    updateRail();
    if (moveFocus) focusStepControl();
  }

  function focusStepControl() {
    var targets = ['quoteButton', 'sourceAccount', 'transactionHash', 'refreshButton', 'xrplExplorer'];
    var target = element(targets[currentStep]);
    if (target) target.focus();
  }

  function effectiveState() {
    if (!currentRecord) return 'open';
    if (currentRecord.state === 'quoted' && quoteExpired()) return 'quote_expired';
    return SETTLEMENT_STATES.indexOf(currentRecord.state) >= 0 ? currentRecord.state : 'open';
  }

  function updateRail() {
    var progress = STEP_FOR_STATE[effectiveState()] || 0;
    document.querySelectorAll('#stepRail button').forEach(function (button, index) {
      if (index === currentStep) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
      button.dataset.complete = String(index < progress || effectiveState() === 'paid');
      button.querySelector('.rail-index').textContent = index < progress || effectiveState() === 'paid' ? '✓' : String(index + 1);
    });
  }

  function renderSummary(record) {
    var invoice = invoiceDetails(record);
    var issuer = partyName(invoice, 'issuer', 'The contractor');
    setText('invoiceNumber', invoice.invoice_number || record.id);
    setText('clientName', partyName(invoice, 'client', 'Client'));
    setText('issuerName', issuer);
    setText('issuerSummary', issuer);
    setText('invoiceDescription', firstDescription(invoice));
    setText('invoiceTotal', money(invoice));
    setText('beneficiary', shortValue(record.beneficiary));
    setText('quotePayout', payout(invoice));
    setText('receiptPayout', payout(invoice));
    document.title = (invoice.invoice_number || 'Invoice') + ' · Pay with XRP';
  }

  function quoteExpired() {
    var quote = currentRecord && currentRecord.quote;
    return Boolean(quote && typeof quote.expires_at === 'number' && quote.expires_at <= Date.now() / 1000);
  }

  function renderQuote() {
    var quote = currentRecord && currentRecord.quote;
    clearInterval(quoteTimer);
    if (!quote) return renderEmptyQuote();
    setText('quotePrice', quote.xrp_usd_price);
    setText('quoteMaximum', formatUnits(quote.maximum_fxrp_uba, 'FXRP'));
    updateQuoteClock();
    quoteTimer = window.setInterval(updateQuoteClock, 1000);
  }

  function renderEmptyQuote() {
    setText('quotePrice', 'Not quoted');
    setText('quoteMaximum', '—');
    setText('quoteExpiry', '—');
    element('quoteButton').textContent = 'Get live quote';
    setHidden('continueToSigning', true);
  }

  function updateQuoteClock() {
    if (!currentRecord || !currentRecord.quote) return;
    var remaining = Math.max(0, currentRecord.quote.expires_at - Math.floor(Date.now() / 1000));
    var minutes = String(Math.floor(remaining / 60)).padStart(2, '0');
    var seconds = String(remaining % 60).padStart(2, '0');
    setText('quoteExpiry', minutes + ':' + seconds);
    if (remaining === 0) renderQuoteExpired();
  }

  function renderQuoteExpired() {
    clearInterval(quoteTimer);
    element('quoteButton').textContent = 'Refresh live quote';
    element('quoteStatus').dataset.tone = 'error';
    element('quoteStatus').textContent = 'Quote expired. Refresh it before signing.';
    setHidden('continueToSigning', true);
    setText('statusStamp', STAMP_FOR_STATE.quote_expired);
    renderSigning();
    updateRail();
  }

  function renderQuoteReady() {
    element('quoteButton').textContent = 'Refresh live quote';
    element('quoteStatus').dataset.tone = 'success';
    element('quoteStatus').textContent = 'Live quote ready. Check the cap and expiry before signing.';
    setHidden('continueToSigning', false);
  }

  function trustedXamanLink(value) {
    try {
      var url = new URL(value);
      return url.protocol === 'https:' && url.hostname === 'xumm.app' ? url.href : '';
    } catch (_error) {
      return '';
    }
  }

  function renderSigning() {
    var signing = currentRecord && currentRecord.signing_request;
    setHidden('signingResult', !signing);
    if (!signing) return;
    if (quoteExpired()) return renderExpiredSigning();
    var link = trustedXamanLink(signing.links && signing.links.always);
    var qr = trustedXamanLink(signing.links && signing.links.qr_png);
    setText('signingTitle', link ? 'Xaman request ready' : 'Unsigned transaction ready');
    setText('signingCopy', link ? 'Open Xaman and approve the testnet payment.' : 'Xaman is unavailable; use the unsigned transaction fallback.');
    element('unsignedTransaction').textContent = JSON.stringify(signing.unsigned_transaction, null, 2);
    renderXamanLink(link, qr);
  }

  function renderExpiredSigning() {
    setText('signingTitle', 'This Xaman request expired');
    setText('signingCopy', 'Refresh the live quote before creating another payment request.');
    element('unsignedTransaction').textContent = '';
    renderXamanLink('', '');
  }

  function renderXamanLink(link, qr) {
    var anchor = element('xamanLink');
    var image = element('xamanQr');
    anchor.hidden = !link;
    if (link) anchor.href = link;
    else anchor.removeAttribute('href');
    image.hidden = !qr;
    if (qr) image.src = qr;
    else image.removeAttribute('src');
  }

  function safeExplorerLink(id, value, host) {
    try {
      var url = new URL(value);
      if (url.protocol === 'https:' && url.hostname === host) element(id).href = url.href;
    } catch (_error) {
      return;
    }
  }

  function renderReceipt(record) {
    var receipt = record.receipt;
    if (!receipt || typeof receipt !== 'object') return;
    var amount = receipt.payout && receipt.payout.amount_uba;
    setText('receiptPayout', formatUnits(amount, 'USD₮0'));
    safeExplorerLink('xrplExplorer', receipt.xrpl && receipt.xrpl.explorer_url, 'testnet.xrpl.org');
    safeExplorerLink('flareExplorer', receipt.flare && receipt.flare.explorer_url, 'coston2-explorer.flare.network');
    setText('userOpHash', receipt.fsa && receipt.fsa.user_op_hash);
    setText('fdcProof', receipt.fdc && receipt.fdc.proof_hash);
  }

  function renderExecutionMessage(needsRecovery) {
    var container = element('executionMessage');
    var heading = document.createElement('strong');
    heading.textContent = needsRecovery ? 'Settlement needs attention.' : 'Settlement is working.';
    var detail = needsRecovery
      ? ' Verify the retained evidence before following the guided recovery step.'
      : ' This page checks for new evidence automatically.';
    container.replaceChildren(heading, document.createTextNode(detail));
  }

  function renderExecution(record) {
    var recovery = record.recovery;
    var needsRecovery = record.state === 'recovery_required';
    setText('executionStatus', needsRecovery ? 'Needs guided recovery' : 'Checking evidence');
    setText('fdcStatus', record.xrpl_evidence && record.xrpl_evidence.fdc_proof_hash ? 'Evidence received' : 'Verifying');
    setHidden('recoveryPanel', !needsRecovery);
    if (recovery && recovery.guidance) setText('recoveryGuidance', recovery.guidance);
    element('executionMessage').dataset.tone = needsRecovery ? 'error' : 'loading';
    renderExecutionMessage(needsRecovery);
  }

  function stateChanged(record) {
    var state = effectiveState();
    if (state === renderedState) return false;
    renderedState = state;
    currentStep = STEP_FOR_STATE[state] || 0;
    return true;
  }

  function renderRecord(record) {
    currentRecord = record;
    clearPageError();
    renderSummary(record);
    renderQuote();
    if (record.quote && !quoteExpired()) renderQuoteReady();
    renderSigning();
    renderExecution(record);
    renderReceipt(record);
    stateChanged(record);
    setText('statusStamp', STAMP_FOR_STATE[effectiveState()] || 'Invoice ready');
    selectStep(currentStep, false);
    element('record').setAttribute('aria-busy', 'false');
    schedulePoll(record.state === 'flare_executing');
  }

  function schedulePoll(shouldPoll) {
    clearTimeout(pollTimer);
    if (!shouldPoll) return;
    pollTimer = window.setTimeout(function () { loadInvoice(true); }, 3000);
  }

  function loadInvoice(silent) {
    if (!invoiceId()) {
      showPageError('This payment link is incomplete.', 'Ask the contractor for a new InvoiceCraft link.');
      return Promise.resolve();
    }
    if (!silent) element('record').setAttribute('aria-busy', 'true');
    return request(apiPath()).then(renderRecord).catch(function (error) {
      showPageError('We could not load this invoice.', error.message);
    });
  }

  function handleQuote() {
    var button = element('quoteButton');
    setButtonBusy(button, true, 'Reading live values…', 'Refresh live quote');
    request(apiPath('/quote'), { method: 'POST' }).then(renderRecord).catch(function (error) {
      element('quoteStatus').dataset.tone = 'error';
      element('quoteStatus').textContent = error.message;
    }).finally(function () { setButtonBusy(button, false, '', currentRecord && currentRecord.quote ? 'Refresh live quote' : 'Get live quote'); });
  }

  function validSourceAccount(value) {
    return value.length >= 5 && value.length <= 64 && !/\s/.test(value);
  }

  function handleSigning() {
    var account = element('sourceAccount').value.trim();
    if (!validSourceAccount(account)) return showFieldError('sourceAccount', 'sourceAccountError', 'Enter a valid public XRP account.');
    showFieldError('sourceAccount', 'sourceAccountError', '');
    var button = element('signingButton');
    setButtonBusy(button, true, 'Preparing payment…', 'Create Xaman request');
    var options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source_account: account }) };
    request(apiPath('/signing-request'), options).then(renderRecord).catch(function (error) {
      showFieldError('sourceAccount', 'sourceAccountError', error.message);
    }).finally(function () { setButtonBusy(button, false, '', 'Create Xaman request'); });
  }

  function validTransactionHash(value) {
    return /^(?:0x)?[0-9a-fA-F]{64}$/.test(value);
  }

  function handleSubmit() {
    var hash = element('transactionHash').value.trim();
    if (!validTransactionHash(hash)) return showFieldError('transactionHash', 'transactionHashError', 'Enter the complete 64-character transaction hash.');
    showFieldError('transactionHash', 'transactionHashError', '');
    var button = element('submitButton');
    setButtonBusy(button, true, 'Verifying XRPL…', 'Verify payment and settle');
    var options = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ xrpl_transaction_hash: hash }) };
    request(apiPath('/submit'), options).then(renderRecord).catch(function (error) {
      if (currentRecord) currentRecord.state = 'payment_rejected';
      renderedState = 'payment_rejected';
      setText('statusStamp', STAMP_FOR_STATE.payment_rejected);
      updateRail();
      showFieldError('transactionHash', 'transactionHashError', error.message);
    }).finally(function () { setButtonBusy(button, false, '', 'Verify payment and settle'); });
  }

  function loadReceipt() {
    return request(apiPath('/receipt')).then(function (receipt) {
      if (currentRecord) currentRecord.receipt = receipt;
      renderReceipt({ receipt: receipt });
    }).catch(function (error) {
      showPageError('Receipt evidence is unavailable.', error.message);
    });
  }

  function bindEvents() {
    document.querySelectorAll('#stepRail button').forEach(function (button) {
      button.addEventListener('click', function () { selectStep(Number(button.dataset.step), false); });
    });
    element('quoteButton').addEventListener('click', handleQuote);
    element('continueToSigning').addEventListener('click', function () { selectStep(1, true); });
    element('signingButton').addEventListener('click', handleSigning);
    element('continueToSubmit').addEventListener('click', function () { selectStep(2, true); });
    element('backToSigning').addEventListener('click', function () { selectStep(1, true); });
    element('submitButton').addEventListener('click', handleSubmit);
    element('refreshButton').addEventListener('click', function () { loadInvoice(false); });
    element('retryButton').addEventListener('click', function () { loadInvoice(false); });
    element('printButton').addEventListener('click', function () { window.print(); });
  }

  function initialize() {
    window.InvoiceCraftXrpStates = SETTLEMENT_STATES.slice();
    bindEvents();
    loadInvoice(false).then(function () {
      if (currentRecord && currentRecord.state === 'paid' && !currentRecord.receipt) loadReceipt();
    });
  }

  initialize();
})();
