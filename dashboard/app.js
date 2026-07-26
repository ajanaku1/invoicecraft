(function () {
  'use strict';

  var STATE = { INPUT: 'input', PAYMENT: 'payment', INVOICE: 'invoice' };
  var currentState = STATE.INPUT;

  var app = document.querySelector('.app');
  var generateBtn = document.getElementById('generateBtn');
  var payBtn = document.getElementById('payBtn');
  var downloadBtn = document.getElementById('downloadBtn');
  var textarea = document.getElementById('desc');
  var challengeIdEl = document.getElementById('challengeId');
  var toast = document.getElementById('toast');

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

  var toastTimer = null;

  function randHex(len) {
    var h = '0123456789abcdef';
    var s = '';
    for (var i = 0; i < len; i++) s += h[Math.floor(Math.random() * 16)];
    return s;
  }

  function formatCurrency(n) {
    return '$' + n.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function showToast(message) {
    if (toastTimer) clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.add('show');
    toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 3000);
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
          '<span class="lightning" aria-hidden="true">⚡</span> Simulate Payment';
        break;
      case STATE.PAYMENT:
        generateBtn.textContent = 'Generating...';
        generateBtn.disabled = true;
        downloadBtn.hidden = true;
        textarea.disabled = true;
        challengeIdEl.textContent =
          '0x' + randHex(8) + '...' + randHex(4);
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

  function parseDescription(text) {
    text = text.trim();
    var name = 'Client';
    var email = 'client@example.com';

    if (/Acme Corp/i.test(text)) {
      name = 'Acme Corp';
    } else {
      var m = text.match(/([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,2})/);
      if (m) name = m[1];
    }

    var emailMatch = text.match(
      /([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})/
    );
    if (emailMatch) email = emailMatch[1];

    var summary = text.length > 80 ? text.slice(0, 80) + '...' : text;
    return { clientName: name, clientEmail: email, summary: summary };
  }

  function generateInvoiceNumber() {
    var now = new Date();
    var y = now.getFullYear();
    var m = String(now.getMonth() + 1).padStart(2, '0');
    var d = String(now.getDate()).padStart(2, '0');
    var n = String(Math.floor(Math.random() * 900) + 100);
    return 'INV-' + y + m + d + '-' + n;
  }

  function populateInvoice(parsed) {
    var num = generateInvoiceNumber();
    invNumber.textContent = num;

    var now = new Date();
    var due = new Date(now);
    due.setDate(due.getDate() + 15);
    var fmt = function (d) {
      return d.toLocaleDateString('en-US', {
        month: 'long', day: 'numeric', year: 'numeric'
      });
    };
    invDate.textContent = 'Issued: ' + fmt(now);
    paymentTerms.innerHTML =
      'Net 15 &middot; Due: ' + fmt(due) +
      '<br><span style="font-size:11px;color:var(--muted);">x402 micropayment enabled</span>';

    clientNameEl.textContent = parsed.clientName;
    clientEmailEl.textContent = parsed.clientEmail;

    var amount = 500;
    var taxRate = 0.08;
    var tax = amount * taxRate;
    var total = amount + tax;

    invoiceItems.innerHTML = '';
    var tr = document.createElement('tr');
    tr.innerHTML =
      '<td class="desc">' + parsed.summary + '</td>' +
      '<td class="qty amt">1</td>' +
      '<td class="price amt">' + formatCurrency(amount) + '</td>' +
      '<td class="total amt">' + formatCurrency(amount) + '</td>';
    invoiceItems.appendChild(tr);

    subtotalEl.textContent = formatCurrency(amount);
    taxEl.textContent = formatCurrency(tax);
    totalDue.textContent = formatCurrency(total);
    footerAddress.textContent =
      '0x' + randHex(6) + '...' + randHex(4);
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
    footerAddress.textContent = '0x...';
  }

  function handleGenerate() {
    if (currentState === STATE.INPUT) {
      var text = textarea.value.trim();
      if (!text) {
        showToast('Please describe the work to generate an invoice.');
        textarea.focus();
        return;
      }
      setState(STATE.PAYMENT);
    } else if (currentState === STATE.INVOICE) {
      resetInvoice();
      setState(STATE.INPUT);
    }
  }

  function handlePay() {
    if (currentState !== STATE.PAYMENT) return;
    if (payBtn.disabled) return;

    payBtn.disabled = true;
    payBtn.classList.add('loading');
    payBtn.innerHTML = '';

    setTimeout(function () {
      payBtn.classList.remove('loading');
      payBtn.classList.add('success');
      payBtn.innerHTML = '&#10003; Payment Successful';
      showToast('Invoice unlocked — payment simulated');

      var parsed = parseDescription(textarea.value.trim());
      populateInvoice(parsed);

      setTimeout(function () {
        setState(STATE.INVOICE);
        setTimeout(function () {
          payBtn.className = 'btn-pay';
          payBtn.disabled = false;
          payBtn.innerHTML =
            '<span class="lightning" aria-hidden="true">⚡</span> Simulate Payment';
        }, 400);
      }, 700);
    }, 1600);
  }

  function handleDownload() {
    showToast('PDF download would start here. (Mock)');
  }

  generateBtn.addEventListener('click', handleGenerate);
  payBtn.addEventListener('click', handlePay);
  downloadBtn.addEventListener('click', handleDownload);

  setState(STATE.INPUT);
})();
