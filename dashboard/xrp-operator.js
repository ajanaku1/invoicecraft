(function () {
  'use strict';

  var providers = new Map();
  var selectedProvider = null;
  var requestIntent = null;
  var connectedAccount = '';
  var verified = false;
  var sent = false;
  var recoverableHash = '';
  var contextVersion = 0;
  var rpcUrl = 'https://coston2-api.flare.network/ext/C/rpc';
  var network = { chainId: '0x72', chainName: 'Flare Testnet Coston2', nativeCurrency: { name: 'Coston2 Flare', symbol: 'C2FLR', decimals: 18 }, rpcUrls: [rpcUrl], blockExplorerUrls: ['https://coston2-explorer.flare.network'] };
  var intentFields = ['version', 'purpose', 'chain_id', 'signer', 'to', 'value', 'data', 'calldata_hash'];

  function element(id) { return document.getElementById(id); }
  function text(id, value) { element(id).textContent = value || '—'; }
  function setStatus(id, message, tone) { var node = element(id); node.textContent = message; node.dataset.tone = tone || ''; }
  function invoiceId() { return element('invoiceId').value.trim(); }
  function operatorToken() { return element('operatorToken').value; }
  function jobPath() { return '/api/v1/xrp/invoices/' + encodeURIComponent(invoiceId()) + '/operator-job'; }
  function operatorHeaders(json) { var headers = { 'X-Operator-Token': operatorToken() }; if (json) headers['Content-Type'] = 'application/json'; return headers; }

  function api(method, body) {
    return fetch(jobPath(), { method: method, headers: operatorHeaders(Boolean(body)), body: body ? JSON.stringify(body) : undefined }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (value) {
        if (response.ok) return value;
        throw new Error(value.message || 'Operator request failed.');
      });
    });
  }

  function resetVerification(message) {
    contextVersion += 1;
    connectedAccount = '';
    verified = false;
    sent = false;
    recoverableHash = '';
    element('confirmIntent').checked = false;
    element('confirmIntent').disabled = true;
    element('sendTransaction').disabled = true;
    element('sendTransaction').textContent = 'Open wallet approval';
    element('transactionHash').hidden = true;
    if (message) setStatus('walletStatus', message);
  }

  function validateIntent(value) {
    var exact = value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === intentFields.length;
    if (!exact || intentFields.some(function (key) { return typeof value[key] !== 'string'; })) throw new Error('Public sign request is malformed.');
    if (value.version !== '1' || value.chain_id !== '0x72') throw new Error('Sign request is not bound to Coston2.');
    if (!['fdc-request', 'execute-direct-mint'].includes(value.purpose)) throw new Error('Sign request purpose is unsupported.');
    if (!/^0x[0-9a-f]{40}$/i.test(value.signer) || !/^0x[0-9a-f]{40}$/i.test(value.to)) throw new Error('Sign request address is malformed.');
    if (!/^0x(?:0|[1-9a-f][0-9a-f]*)$/i.test(value.value) || !/^0x(?:[0-9a-f]{2})*$/i.test(value.data) || !/^0x[0-9a-f]{64}$/i.test(value.calldata_hash)) throw new Error('Sign request bytes are malformed.');
    return Object.freeze(Object.assign({}, value));
  }

  function renderIntent(value) {
    requestIntent = validateIntent(value);
    text('intentPurpose', requestIntent.purpose);
    text('intentSigner', requestIntent.signer);
    text('intentTarget', requestIntent.to);
    text('intentValue', requestIntent.value);
    text('intentData', requestIntent.data);
    text('intentHash', requestIntent.calldata_hash);
    element('stageStamp').textContent = requestIntent.purpose === 'fdc-request' ? 'FDC request' : 'Direct mint';
    element('walletSection').hidden = false;
    resetVerification('Choose a detected wallet, then connect to verify this exact intent.');
    recoverStoredSubmission();
  }

  function storedValue(key) {
    try { return localStorage.getItem(key) || ''; }
    catch (_error) { throw new Error('Submission guard storage is unavailable. Do not submit.'); }
  }

  function rememberValue(key, value) {
    try { localStorage.setItem(key, value); }
    catch (_error) { throw new Error('Submission guard storage is unavailable. Do not submit.'); }
  }

  function forgetValue(key) {
    try { localStorage.removeItem(key); }
    catch (_error) { throw new Error('Submission guard storage is unavailable. Verify the explorer before retrying.'); }
  }

  function recoverStoredSubmission() {
    recoverableHash = '';
    var stored = storedValue(guardKey(requestIntent));
    if (!/^0x[0-9a-f]{64}$/i.test(stored)) {
      if (stored) setStatus('walletStatus', 'Submission status is unknown. Verify the explorer before retrying.', 'error');
      return;
    }
    recoverableHash = stored;
    text('transactionHash', stored);
    element('transactionHash').hidden = false;
    element('sendTransaction').textContent = 'Verify submitted hash';
    element('sendTransaction').disabled = false;
    setStatus('walletStatus', 'A submitted hash was recovered. Verify it without sending again.', 'success');
  }

  function renderRail(stage) {
    var order = ['prepare_fdc', 'awaiting_fdc_transaction', 'prepare_execute', 'awaiting_execute_transaction', 'complete'];
    var progress = Math.max(0, order.indexOf(stage));
    document.querySelectorAll('[data-step]').forEach(function (node) {
      var anchor = order.indexOf(node.dataset.step);
      node.dataset.active = String(anchor === progress || (node.dataset.step === 'prepare_fdc' && progress === 1) || (node.dataset.step === 'prepare_execute' && progress === 3));
      node.dataset.done = String(anchor >= 0 && anchor < progress);
    });
  }

  function renderJob(job) {
    renderRail(job.stage);
    element('prepareJob').disabled = !['prepare_fdc', 'prepare_execute'].includes(job.stage);
    var waiting = ['awaiting_fdc_transaction', 'awaiting_execute_transaction'].includes(job.stage);
    if (waiting && job.sign_request) renderIntent(job.sign_request);
    if (job.stage === 'complete') {
      requestIntent = null;
      element('walletSection').hidden = true;
      setStatus('jobStatus', 'Settlement complete. Exact receipt evidence is now attached to the invoice.', 'success');
      return;
    }
    setStatus('jobStatus', waiting ? 'A public wallet intent is ready for explicit approval.' : 'Job loaded. Prepare the next public transaction.', waiting ? 'success' : '');
  }

  function loadJob() {
    if (!invoiceId() || !operatorToken()) return setStatus('jobStatus', 'Invoice ID and operator token are required.', 'error');
    element('loadJob').disabled = true;
    api('GET').then(renderJob).catch(function (error) { setStatus('jobStatus', error.message, 'error'); }).finally(function () { element('loadJob').disabled = false; });
  }

  function prepareJob() {
    element('prepareJob').disabled = true;
    api('POST').then(renderJob).catch(function (error) { setStatus('jobStatus', error.message, 'error'); element('prepareJob').disabled = false; });
  }

  function addWallet(detail) {
    if (!detail || !detail.info || !detail.provider || typeof detail.provider.request !== 'function') return;
    if (providers.has(detail.info.uuid)) return;
    providers.set(detail.info.uuid, { name: detail.info.name, provider: detail.provider });
    renderWallets();
  }

  function addLegacyWallets() {
    var values = window.ethereum && Array.isArray(window.ethereum.providers) ? window.ethereum.providers : (window.ethereum ? [window.ethereum] : []);
    values.forEach(function (provider, index) { addWallet({ info: { uuid: 'legacy-' + index, name: 'Legacy wallet ' + (index + 1) }, provider: provider }); });
  }

  function renderWallets() {
    var select = element('wallet');
    select.replaceChildren();
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = providers.size ? 'Choose a wallet' : 'No wallets detected';
    select.appendChild(placeholder);
    providers.forEach(function (entry, id) { var option = document.createElement('option'); option.value = id; option.textContent = entry.name; select.appendChild(option); });
    select.disabled = !providers.size;
    element('connectWallet').disabled = true;
  }

  function selectedWallet() {
    return providers.get(element('wallet').value) || null;
  }

  function currentContext() {
    if (!selectedProvider || !requestIntent) throw new Error('Choose a wallet and prepare a transaction first.');
    return { version: contextVersion, entry: selectedProvider, provider: selectedProvider.provider, intent: requestIntent };
  }

  function assertCurrent(context) {
    if (context.version !== contextVersion || context.entry !== selectedProvider || context.intent !== requestIntent) throw new Error('Wallet context changed. Reconnect and verify again.');
  }

  function readChain(context) { return context.provider.request({ method: 'eth_chainId' }).then(function (chain) { assertCurrent(context); return chain; }); }

  function switchCoston2(context) {
    return context.provider.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: '0x72' }] }).catch(function (error) {
      if (String(error && error.code) !== '4902') throw error;
      return context.provider.request({ method: 'wallet_addEthereumChain', params: [network] });
    }).then(function () { assertCurrent(context); return readChain(context); }).then(function (chain) { if (chain !== '0x72') throw new Error('Wallet must use Coston2 (0x72).'); });
  }

  function officialHash(context) {
    return fetch(rpcUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'web3_sha3', params: [context.intent.data] }) }).then(function (response) { return response.json(); }).then(function (value) {
      assertCurrent(context);
      if (!value || typeof value.result !== 'string') throw new Error('Official Coston2 calldata verification failed.');
      return value.result;
    });
  }

  function verifiedHash(context) {
    return context.provider.request({ method: 'web3_sha3', params: [context.intent.data] }).catch(function (error) {
      var unsupported = String(error && error.code) === '-32601' || /web3_sha3.*(not found|unsupported|handler)/i.test(String(error && error.message));
      if (!unsupported) throw error;
      return officialHash(context);
    });
  }

  function connectWallet() {
    selectedProvider = selectedWallet();
    resetVerification('Opening the selected wallet…');
    if (!selectedProvider || !requestIntent) return setStatus('walletStatus', 'Choose a wallet and prepare a transaction first.', 'error');
    var context = currentContext();
    context.provider.request({ method: 'eth_requestAccounts' }).then(function (accounts) {
      assertCurrent(context);
      if (!accounts[0] || accounts[0].toLowerCase() !== context.intent.signer.toLowerCase()) throw new Error('Selected account does not match the required signer.');
      connectedAccount = accounts[0].toLowerCase();
      return readChain(context);
    }).then(function (chain) { return chain === '0x72' ? chain : switchCoston2(context); }).then(function () { return verifiedHash(context); }).then(function (hash) {
      assertCurrent(context);
      if (hash.toLowerCase() !== context.intent.calldata_hash.toLowerCase()) throw new Error('Wallet calldata hash does not match the prepared request.');
      verified = true;
      element('confirmIntent').disabled = false;
      setStatus('walletStatus', 'Wallet, signer, Coston2 chain, and calldata hash verified.', 'success');
    }).catch(function (error) { resetVerification(); setStatus('walletStatus', error.message || 'Wallet verification failed.', 'error'); });
  }

  function guardKey(intent) { return 'invoicecraft-operator:' + intent.signer.toLowerCase() + ':' + intent.to.toLowerCase() + ':' + intent.calldata_hash.toLowerCase(); }

  function sendTransaction() {
    if (recoverableHash) {
      element('sendTransaction').disabled = true;
      return api('PATCH', { transaction_hash: recoverableHash }).then(renderJob).catch(function (error) {
        element('sendTransaction').disabled = false;
        setStatus('walletStatus', error.message || 'The submitted hash could not be verified yet.', 'error');
      });
    }
    if (!verified || !element('confirmIntent').checked || sent) return;
    var context = currentContext();
    var key = guardKey(context.intent);
    try {
      if (storedValue(key)) return setStatus('walletStatus', 'This exact intent may already be submitted. Verify the explorer before retrying.', 'error');
      rememberValue(key, 'pending');
      sent = true;
      element('sendTransaction').disabled = true;
    } catch (error) {
      setStatus('walletStatus', error.message, 'error');
      return;
    }
    context.provider.request({ method: 'eth_sendTransaction', params: [{ from: connectedAccount, to: context.intent.to, value: context.intent.value, data: context.intent.data }] }).then(function (hash) {
      assertCurrent(context);
      if (!/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error('Wallet returned an invalid transaction hash.');
      recoverableHash = hash.toLowerCase();
      rememberValue(key, recoverableHash);
      text('transactionHash', hash);
      element('transactionHash').hidden = false;
      setStatus('walletStatus', 'Wallet submitted the public transaction. Verifying it on Coston2…', 'success');
      return api('PATCH', { transaction_hash: hash });
    }).then(renderJob).catch(function (error) {
      if (String(error && error.code) === '4001') {
        try { forgetValue(key); sent = false; }
        catch (storageError) { return setStatus('walletStatus', storageError.message, 'error'); }
      }
      if (recoverableHash) {
        element('sendTransaction').textContent = 'Verify submitted hash';
        element('sendTransaction').disabled = false;
      }
      setStatus('walletStatus', error.message || 'Submission status is unknown. Verify the explorer before retrying.', 'error');
    });
  }

  element('loadJob').addEventListener('click', loadJob);
  element('prepareJob').addEventListener('click', prepareJob);
  element('wallet').addEventListener('change', function () { selectedProvider = selectedWallet(); resetVerification(selectedProvider ? 'Wallet selected. Connect to verify.' : 'Choose a wallet.'); element('connectWallet').disabled = !selectedProvider || !requestIntent; });
  element('connectWallet').addEventListener('click', connectWallet);
  element('confirmIntent').addEventListener('change', function () { element('sendTransaction').disabled = !verified || !this.checked || sent; });
  element('sendTransaction').addEventListener('click', sendTransaction);
  window.addEventListener('eip6963:announceProvider', function (event) { addWallet(event.detail); });
  window.dispatchEvent(new Event('eip6963:requestProvider'));
  addLegacyWallets();
}());
