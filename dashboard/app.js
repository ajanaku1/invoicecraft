(function () {
  'use strict';

  // API base: explicit override wins, else same-origin when served over HTTP,
  // else local dev server. Set window.INVOICECRAFT_API for a deployed backend.
  var API_BASE =
    (window.INVOICECRAFT_API ||
      (location.origin && location.origin.indexOf('http') === 0
        ? location.origin
        : 'http://localhost:8000')).replace(/\/$/, '');

  var STATE = { INPUT: 'input', PAYMENT: 'payment', INVOICE: 'invoice' };
  var currentState = STATE.INPUT;

  var app = document.querySelector('.app');
  var generateBtn = document.getElementById('generateBtn');
  var payBtn = document.getElementById('payBtn');
  var downloadBtn = document.getElementById('downloadBtn');
  var textarea = document.getElementById('desc');
  var challengeIdEl = document.getElementById('challengeId');
  var toast = document.getElementById('toast');
  var logoInput = document.getElementById('logoInput');
  var logoPreview = document.getElementById('logoPreview');
  var logoChip = document.getElementById('logoChip');
  var logoName = document.getElementById('logoName');
  var attachLogoBtn = document.getElementById('attachLogoBtn');
  var removeLogoBtn = document.getElementById('removeLogoBtn');
  var previewLogo = document.getElementById('previewLogo');
  var previewIssuerName = document.getElementById('previewIssuerName');
  var previewIssuerLines = document.getElementById('previewIssuerLines');

  var invNumber = document.getElementById('invNumber');
  var invDate = document.getElementById('invDate');
  var clientNameEl = document.getElementById('clientName');
  var clientEmailEl = document.getElementById('clientEmail');
  var paymentTerms = document.getElementById('paymentTerms');
  var invoiceItems = document.getElementById('invoiceItems');
  var subtotalEl = document.getElementById('subtotal');
  var taxEl = document.getElementById('tax');
  var totalDue = document.getElementById('totalDue');
  var footerAddress = document.getElementById('footerAddress');
  var taxLabelEl = document.querySelector('.total-row.tax .label');
  var connectBtn = document.getElementById('connectBtn');
  var walletChip = document.getElementById('walletChip');
  var walletAddr = document.getElementById('walletAddr');
  var payNote = document.getElementById('payNote');

  var toastTimer = null;
  // Live payment/invoice context returned by the backend.
  var session = { payment: null, invoice: null, pdf: null, requirements: null, logo: null };
  var wallet = { account: null };

  function getProvider() {
    return window.okxwallet || window.ethereum || null;
  }

  var CURRENCY_SYMBOLS = {
    USD: '$', EUR: '\u20ac', GBP: '\u00a3', JPY: '\u00a5', CNY: '\u00a5',
    NGN: '\u20a6', INR: '\u20b9', CAD: 'C$', AUD: 'A$', BRL: 'R$', SGD: 'S$'
  };

  // Symbol for the invoice on screen; falls back to the code (e.g. "SEK 12.00").
  function currencySymbol() {
    var code = (session.invoice && session.invoice.currency) || 'USD';
    return CURRENCY_SYMBOLS[code] || code + ' ';
  }

  function fmtMoney(str) {
    var symbol = currencySymbol();
    var n = parseFloat(str);
    if (isNaN(n)) return symbol + '0.00';
    return symbol + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function shortHex(str) {
    if (!str) return '—';
    return str.length > 16 ? str.slice(0, 10) + '…' + str.slice(-4) : str;
  }

  function randTxHash() {
    var h = '0123456789abcdef';
    var s = '0x';
    for (var i = 0; i < 64; i++) s += h[Math.floor(Math.random() * 16)];
    return s;
  }

  function showToast(message) {
    if (toastTimer) clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add('show');
    toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 3200);
  }

  function setState(state) {
    currentState = state;
    app.dataset.state = state;

    switch (state) {
      case STATE.INPUT:
        generateBtn.textContent = 'Generate Invoice';
        generateBtn.disabled = false;
        downloadBtn.hidden = true;
        textarea.disabled = false;
        payBtn.disabled = false;
        payBtn.className = 'btn-pay';
        payBtn.innerHTML =
          '<span class="lightning" aria-hidden="true">⚡</span> Pay 0.50 USDT';
        break;
      case STATE.PAYMENT:
        generateBtn.textContent = 'Awaiting payment…';
        generateBtn.disabled = true;
        downloadBtn.hidden = true;
        textarea.disabled = true;
        setTimeout(function () { payBtn.focus(); }, 100);
        break;
      case STATE.INVOICE:
        generateBtn.textContent = 'Generate New Invoice';
        generateBtn.disabled = false;
        downloadBtn.hidden = false;
        textarea.disabled = false;
        setTimeout(function () { downloadBtn.focus(); }, 100);
        break;
    }
  }

  // The letterhead is the issuer's, not the product's.
  function renderIssuer(issuer) {
    previewIssuerName.textContent = issuer.name;
    previewIssuerLines.textContent =
      [issuer.address, issuer.email].filter(Boolean).join(' · ');
    if (session.logo) {
      previewLogo.src = session.logo;
      previewLogo.hidden = false;
    } else {
      previewLogo.hidden = true;
    }
  }

  function renderInvoice(invoice) {
    renderIssuer(invoice.issuer);
    invNumber.textContent = invoice.invoice_number;

    var now = new Date();
    var fmtDate = function (d) {
      return d.toLocaleDateString('en-US', {
        month: 'long', day: 'numeric', year: 'numeric'
      });
    };
    invDate.textContent = 'Issued: ' + fmtDate(now);
    paymentTerms.innerHTML =
      'Due: ' + invoice.due_date +
      '<br><span style="font-size:11px;color:var(--muted);">' +
      'Status: ' + (invoice.status || '').toUpperCase() +
      ' · x402 settled</span>';

    clientNameEl.textContent = invoice.client.name;
    clientEmailEl.textContent = invoice.client.email;

    invoiceItems.innerHTML = '';
    invoice.line_items.forEach(function (item) {
      var tr = document.createElement('tr');
      var descCell = document.createElement('td');
      descCell.className = 'desc';
      descCell.textContent = item.description;
      var qtyCell = document.createElement('td');
      qtyCell.className = 'qty amt';
      qtyCell.textContent = item.quantity;
      var priceCell = document.createElement('td');
      priceCell.className = 'price amt';
      priceCell.textContent = fmtMoney(item.unit_price);
      var amtCell = document.createElement('td');
      amtCell.className = 'total amt';
      amtCell.textContent = fmtMoney(item.amount);
      tr.appendChild(descCell);
      tr.appendChild(qtyCell);
      tr.appendChild(priceCell);
      tr.appendChild(amtCell);
      invoiceItems.appendChild(tr);
    });

    var taxPct = Math.round(parseFloat(invoice.tax_rate) * 100);
    if (taxLabelEl) taxLabelEl.textContent = 'Tax (' + taxPct + '%)';
    subtotalEl.textContent = fmtMoney(invoice.subtotal);
    taxEl.textContent = fmtMoney(invoice.tax_amount);
    totalDue.textContent = fmtMoney(invoice.total);
    footerAddress.textContent = invoice.issuer.address || invoice.issuer.email || '';
  }

  function resetInvoice() {
    invNumber.textContent = '—';
    invDate.textContent = '—';
    clientNameEl.textContent = '—';
    clientEmailEl.textContent = '—';
    paymentTerms.textContent = '—';
    invoiceItems.innerHTML =
      '<tr><td class="desc" colspan="4" style="text-align:center;color:#c8c0b4;padding:28px 0;font-size:13px;">' +
      'Describe your project and generate an invoice</td></tr>';
    subtotalEl.textContent = '$0.00';
    taxEl.textContent = '$0.00';
    totalDue.textContent = '$0.00';
    footerAddress.textContent = '\u2014';
    session.payment = null;
    session.invoice = null;
    session.pdf = null;
    session.requirements = null;
  }

  // ── Wallet (EIP-1193) ──────────────────────────────────────────────

  function updateWalletUI() {
    if (wallet.account) {
      walletChip.hidden = false;
      walletAddr.textContent = shortHex(wallet.account);
      connectBtn.classList.add('connected');
      connectBtn.textContent = 'Wallet Connected';
    } else {
      walletChip.hidden = true;
      connectBtn.classList.remove('connected');
      connectBtn.textContent = 'Connect Wallet';
    }
  }

  function ensureChain(provider, chainIdHex) {
    return provider
      .request({ method: 'wallet_switchEthereumChain', params: [{ chainId: chainIdHex }] })
      .catch(function (err) {
        if (err && err.code === 4902) {
          var rpc = session.payment && session.payment.rpc;
          return provider.request({
            method: 'wallet_addEthereumChain',
            params: [{
              chainId: chainIdHex,
              chainName: 'X Layer',
              nativeCurrency: { name: 'OKB', symbol: 'OKB', decimals: 18 },
              rpcUrls: rpc ? [rpc] : ['https://rpc.xlayer.tech'],
              blockExplorerUrls: ['https://www.oklink.com/xlayer']
            }]
          });
        }
        throw err;
      });
  }

  function connectWallet() {
    var provider = getProvider();
    if (!provider) {
      showToast('No Web3 wallet found. Install OKX Wallet or MetaMask.');
      return;
    }
    connectBtn.disabled = true;
    connectBtn.textContent = 'Connecting…';
    provider.request({ method: 'eth_requestAccounts' })
      .then(function (accounts) {
        wallet.account = accounts && accounts[0];
        updateWalletUI();
        showToast('Wallet connected: ' + shortHex(wallet.account));
      })
      .catch(function () {
        showToast('Wallet connection was rejected.');
        updateWalletUI();
      })
      .then(function () { connectBtn.disabled = false; });
  }

  function pad32(hex) {
    hex = hex.replace(/^0x/, '').toLowerCase();
    while (hex.length < 64) hex = '0' + hex;
    return hex;
  }

  // Build ERC-20 transfer(payTo, amount) calldata and send it. amountBase is
  // already in token base units (from the x402 requirements).
  function payWithWallet(payment) {
    var provider = getProvider();
    var chainId = payment.chainIdHex || '0xc4';
    var units = BigInt(payment.amountBase);
    var data = '0xa9059cbb' + pad32(payment.payTo) + pad32(units.toString(16));
    return ensureChain(provider, chainId).then(function () {
      return provider.request({
        method: 'eth_sendTransaction',
        params: [{ from: wallet.account, to: payment.asset, data: data }]
      });
    });
  }

  // The OKX Payment SDK returns the x402 requirements base64-encoded in the
  // PAYMENT-REQUIRED header with an empty body; the fallback challenge repeats
  // them in the body. Read the header first, then fall back to the body.
  function readPaymentRequirements(res, data) {
    var header = res.headers.get(X402Client.PAYMENT_REQUIRED_HEADER);
    return X402Client.decodeRequirements(header) || data;
  }

  // Details the user filled in, omitting blanks so the server keeps its own
  // fallbacks (and the parser keeps its guesses) for anything left empty.
  function invoiceBody(description) {
    var body = { description: description };
    var parties = {
      issuer: { name: 'issuerName', email: 'issuerEmail', address: 'issuerAddress' },
      client: { name: 'billName', email: 'billEmail', address: 'billAddress' }
    };
    Object.keys(parties).forEach(function (party) {
      var fields = parties[party];
      var filled = {};
      Object.keys(fields).forEach(function (key) {
        var value = fieldValue(fields[key]);
        if (value) filled[key] = value;
      });
      if (Object.keys(filled).length) body[party] = filled;
    });

    var currency = fieldValue('currency');
    if (currency) body.currency = currency.toUpperCase();
    var taxRate = fieldValue('taxRate');
    if (taxRate) body.tax_rate = taxRate;
    if (session.logo) body.logo = session.logo;
    return body;
  }

  function fieldValue(id) {
    var el = document.getElementById(id);
    return el ? el.value.trim() : '';
  }

  function requestChallenge(description) {
    return fetch(API_BASE + '/api/v1/invoice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ description: description })
    }).then(function (res) {
      return res.json().then(function (data) {
        return { status: res.status, data: readPaymentRequirements(res, data) };
      });
    });
  }

  // OKX Payment SDK flow: sign the advertised requirements and replay the same
  // request with the PAYMENT-SIGNATURE header. The SDK verifies and settles it
  // through the facilitator, so no tx hash is submitted.
  function requestInvoiceSigned(description, paymentHeader) {
    var headers = { 'Content-Type': 'application/json' };
    headers[X402Client.PAYMENT_SIGNATURE_HEADER] = paymentHeader;
    return fetch(API_BASE + '/api/v1/invoice', {
      method: 'POST',
      headers: headers,
      body: JSON.stringify(invoiceBody(description))
    }).then(function (res) {
      return res.json().then(function (data) {
        return { status: res.status, data: data };
      });
    });
  }

  function payWithSignature() {
    var provider = getProvider();
    var requirements = session.requirements;
    return ensureChain(provider, session.payment.chainIdHex)
      .then(function () {
        return X402Client.signPayment(provider, requirements, wallet.account);
      })
      .then(function (paymentHeader) {
        showToast('Authorization signed — settling through OKX…');
        return requestInvoiceSigned(textarea.value.trim(), paymentHeader);
      });
  }

  function requestInvoice(description, txHash) {
    return fetch(API_BASE + '/api/v1/invoice', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(
        Object.assign(invoiceBody(description), { payment_tx_hash: txHash })
      )
    }).then(function (res) {
      return res.json().then(function (data) {
        return { status: res.status, data: data };
      });
    });
  }

  function handleGenerate() {
    if (currentState === STATE.INVOICE) {
      resetInvoice();
      setState(STATE.INPUT);
      return;
    }
    if (currentState !== STATE.INPUT) return;

    var text = textarea.value.trim();
    if (!text) {
      showToast('Please describe the work to generate an invoice.');
      textarea.focus();
      return;
    }

    generateBtn.disabled = true;
    generateBtn.textContent = 'Requesting…';

    requestChallenge(text).then(function (res) {
      var accept = res.data && res.data.accepts && res.data.accepts[0];
      if (res.status === 402 && accept) {
        session.requirements = res.data;
        var decimals = 6;
        session.payment = {
          payTo: accept.payTo,
          asset: accept.asset,
          amountBase: accept.amount,
          amountDisplay: (parseInt(accept.amount, 10) / Math.pow(10, decimals)).toFixed(2),
          network: accept.network,
          chainIdHex: '0xc4',
          rpc: 'https://rpc.xlayer.tech'
        };
        challengeIdEl.textContent = shortHex(accept.payTo);
        var canPayOnChain = !!accept.asset;
        if (!canPayOnChain) {
          payNote.textContent = 'Demo mode: payment is simulated (no asset configured).';
        } else if (usesPaymentSdk) {
          payNote.textContent =
            'Connect a wallet to authorize ' + session.payment.amountDisplay +
            ' USD₮0 on X Layer — OKX settles it for you, no gas.';
        } else {
          payNote.textContent =
            'Connect a wallet to pay ' + session.payment.amountDisplay + ' USD₮0 on X Layer.';
        }
        setState(STATE.PAYMENT);
      } else if (res.status === 400) {
        showToast(res.data.message || 'Invalid description.');
        setState(STATE.INPUT);
      } else if (res.status === 500) {
        showToast(res.data.message || 'Server not configured (ASP_WALLET).');
        setState(STATE.INPUT);
      } else {
        showToast('Unexpected response (' + res.status + ').');
        setState(STATE.INPUT);
      }
    }).catch(function () {
      showToast('Cannot reach the API at ' + API_BASE + '.');
      setState(STATE.INPUT);
    });
  }

  function resetPayButton() {
    payBtn.disabled = false;
    payBtn.className = 'btn-pay';
    payBtn.innerHTML =
      '<span class="lightning" aria-hidden="true">⚡</span> Pay 0.50 USDT';
  }

  function renderInvoiceResponse(res) {
    payBtn.classList.remove('loading');
    if (res.status === 200 && res.data.invoice) {
      session.invoice = res.data.invoice;
      session.pdf = res.data.pdf;
      renderInvoice(res.data.invoice);
      payBtn.classList.add('success');
      payBtn.innerHTML = '&#10003; Payment Settled';
      showToast('Invoice generated — payment settled on X Layer');
      loadStats();
      setTimeout(function () {
        setState(STATE.INVOICE);
        setTimeout(resetPayButton, 400);
      }, 700);
    } else {
      resetPayButton();
      var msg = (res.data && res.data.message) || 'Payment not verified.';
      showToast(msg + ' Send 0.50 USDT on X Layer, then retry.');
    }
  }

  function handlePaymentError() {
    payBtn.classList.remove('loading');
    resetPayButton();
    showToast('Cannot reach the API at ' + API_BASE + '.');
  }

  function submitPayment(txHash) {
    requestInvoice(textarea.value.trim(), txHash)
      .then(renderInvoiceResponse)
      .catch(handlePaymentError);
  }

  function handlePay() {
    if (currentState !== STATE.PAYMENT || payBtn.disabled || !session.payment) return;

    var payment = session.payment;
    var canPayOnChain = !!payment.asset;

    payBtn.disabled = true;
    payBtn.classList.add('loading');
    payBtn.innerHTML = '';

    if (canPayOnChain) {
      // Real payment: require a connected wallet.
      if (!wallet.account) {
        payBtn.classList.remove('loading');
        resetPayButton();
        showToast('Connect a wallet first to pay on-chain.');
        return;
      }

      if (usesPaymentSdk) {
        // x402: sign an EIP-3009 authorization; OKX pulls the funds on settle.
        payWithSignature()
          .then(renderInvoiceResponse)
          .catch(function (err) {
            payBtn.classList.remove('loading');
            resetPayButton();
            var reason = (err && err.message) ? err.message : 'signature rejected';
            showToast('Payment failed: ' + reason);
          });
        return;
      }

      // Fallback: send USDT on X Layer and submit the tx hash for verification.
      payWithWallet(payment)
        .then(function (txHash) {
          showToast('Payment sent: ' + shortHex(txHash) + ' — verifying…');
          submitPayment(txHash);
        })
        .catch(function (err) {
          payBtn.classList.remove('loading');
          resetPayButton();
          var reason = (err && err.message) ? err.message : 'transaction rejected';
          showToast('Payment failed: ' + reason);
        });
    } else {
      // Demo mode: backend runs mock verification, so a well-formed hash unlocks
      // the real invoice + PDF without moving funds.
      submitPayment(randTxHash());
    }
  }

  function handleDownload() {
    if (!session.pdf) {
      showToast('Generate an invoice first.');
      return;
    }
    try {
      var binary = atob(session.pdf);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      var blob = new Blob([bytes], { type: 'application/pdf' });
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download =
        (session.invoice ? session.invoice.invoice_number : 'invoice') + '.pdf';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      showToast('PDF downloaded.');
    } catch (e) {
      showToast('Could not build the PDF file.');
    }
  }

  // Which payment flow the backend is running: the official OKX Payment SDK
  // (sign an EIP-3009 authorization, retry with PAYMENT-SIGNATURE) or the
  // fallback challenge (transfer USDT, submit the tx hash).
  var usesPaymentSdk = false;

  function loadPaymentMode() {
    fetch(API_BASE + '/health')
      .then(function (r) { return r.json(); })
      .then(function (h) {
        usesPaymentSdk = !!h && h.payment_sdk === 'okxweb3-app-x402';
      })
      .catch(function () {});
  }
  loadPaymentMode();

  var MAX_LOGO_BYTES = 2 * 1024 * 1024;

  function clearLogo() {
    session.logo = null;
    logoInput.value = '';
    logoChip.hidden = true;
    attachLogoBtn.hidden = false;
    previewLogo.hidden = true;
  }

  function handleLogoChange() {
    var file = logoInput.files && logoInput.files[0];
    if (!file) return clearLogo();

    if (file.size > MAX_LOGO_BYTES) {
      showToast('That logo is over 2 MB — pick a smaller file.');
      logoInput.value = '';
      return;
    }
    var reader = new FileReader();
    reader.onload = function () {
      session.logo = reader.result;
      logoPreview.src = reader.result;
      logoName.textContent = file.name;
      logoChip.hidden = false;
      attachLogoBtn.hidden = true;
      showToast('Logo attached — it will brand your invoice.');
    };
    reader.onerror = function () { showToast('Could not read that image file.'); };
    reader.readAsDataURL(file);
  }

  function loadStats() {
    fetch(API_BASE + '/stats')
      .then(function (r) { return r.json(); })
      .then(function (s) {
        if (!s || typeof s.invoices_generated === 'undefined') return;
        document.getElementById('statInvoices').textContent = s.invoices_generated;
        document.getElementById('statCollected').textContent = s.usdt_collected;
        document.getElementById('statsLine').hidden = false;
      })
      .catch(function () {});
  }
  loadStats();

  generateBtn.addEventListener('click', handleGenerate);
  connectBtn.addEventListener('click', connectWallet);
  payBtn.addEventListener('click', handlePay);

  var _p = getProvider();
  if (_p && _p.on) {
    _p.on('accountsChanged', function (accounts) {
      wallet.account = accounts && accounts[0] || null;
      updateWalletUI();
    });
  }
  downloadBtn.addEventListener('click', handleDownload);
  logoInput.addEventListener('change', handleLogoChange);
  attachLogoBtn.addEventListener('click', function () { logoInput.click(); });
  removeLogoBtn.addEventListener('click', clearLogo);

  setState(STATE.INPUT);
})();
