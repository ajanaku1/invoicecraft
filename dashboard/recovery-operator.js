(function () {
  'use strict';

  var rpcUrl = 'https://coston2-api.flare.network/ext/C/rpc';
  var network = { chainId: '0x72', chainName: 'Flare Testnet Coston2', nativeCurrency: { name: 'Coston2 Flare', symbol: 'C2FLR', decimals: 18 }, rpcUrls: [rpcUrl], blockExplorerUrls: ['https://coston2-explorer.flare.network'] };
  var fields = ['version', 'purpose', 'chain_id', 'signer', 'to', 'value', 'data', 'calldata_hash'];
  var providers = new Map();
  var selected = null;
  var intent = null;
  var account = '';
  var verified = false;

  function node(id) { return document.getElementById(id); }
  function status(message, tone) { node('status').textContent = message; node('status').dataset.tone = tone || ''; }
  function show(id, value) { node(id).textContent = value; }
  function exactKeys(value) { return value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value).length === fields.length && fields.every(function (key) { return typeof value[key] === 'string'; }); }

  function validateIntent(value) {
    if (!exactKeys(value) || value.version !== '1' || value.chain_id !== '0x72') throw new Error('Public sign request is malformed.');
    if (!['fdc-request', 'execute-direct-mint'].includes(value.purpose)) throw new Error('Unsupported recovery purpose.');
    if (!/^0x[0-9a-f]{40}$/i.test(value.signer) || !/^0x[0-9a-f]{40}$/i.test(value.to)) throw new Error('Intent address is malformed.');
    if (!/^0x(?:0|[1-9a-f][0-9a-f]*)$/i.test(value.value) || !/^0x(?:[0-9a-f]{2})*$/i.test(value.data) || !/^0x[0-9a-f]{64}$/i.test(value.calldata_hash)) throw new Error('Intent bytes are malformed.');
    return Object.freeze(Object.assign({}, value));
  }

  function allowedPath(value) {
    if (!/^\/evidence\/recovery-run\/[a-z0-9-]+\/(?:fdc|execute)-sign-request\.json$/.test(value)) throw new Error('Intent path is outside the recovery workspace.');
    return value;
  }

  function resetVerification() {
    selected = null; account = ''; verified = false;
    node('confirmIntent').checked = false; node('confirmIntent').disabled = true;
    node('sendTransaction').disabled = true;
  }

  function renderIntent(value) {
    intent = validateIntent(value); resetVerification();
    show('intentPurpose', intent.purpose); show('intentSigner', intent.signer); show('intentTarget', intent.to);
    show('intentValue', intent.value); show('intentData', intent.data); show('intentHash', intent.calldata_hash);
    node('intentDetails').hidden = false; status('Intent loaded. Choose Rabby, then verify it.', 'success');
  }

  function loadIntent() {
    var path;
    try { path = allowedPath(node('intentPath').value.trim()); }
    catch (error) { return status(error.message, 'error'); }
    fetch(path, { cache: 'no-store', credentials: 'same-origin' }).then(function (response) {
      if (!response.ok) throw new Error('Prepared intent could not be loaded.');
      return response.json();
    }).then(renderIntent).catch(function (error) { status(error.message, 'error'); });
  }

  function addWallet(detail) {
    if (!detail || !detail.provider || typeof detail.provider.request !== 'function') return;
    var name = detail.info && detail.info.name || (detail.provider.isRabby ? 'Rabby' : 'Injected wallet');
    var id = detail.info && detail.info.uuid || 'legacy-' + providers.size;
    if (!providers.has(id)) providers.set(id, { name: name, provider: detail.provider });
    renderWallets();
  }

  function renderWallets() {
    var select = node('wallet'); select.replaceChildren();
    var first = document.createElement('option'); first.value = ''; first.textContent = providers.size ? 'Choose Rabby' : 'No wallets detected'; select.appendChild(first);
    providers.forEach(function (entry, id) { var option = document.createElement('option'); option.value = id; option.textContent = entry.name; select.appendChild(option); });
    select.disabled = !providers.size;
  }

  function switchChain(provider) {
    return provider.request({ method: 'wallet_switchEthereumChain', params: [{ chainId: '0x72' }] }).catch(function (error) {
      if (String(error && error.code) !== '4902') throw error;
      return provider.request({ method: 'wallet_addEthereumChain', params: [network] });
    });
  }

  function officialHash(data) {
    return fetch(rpcUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'web3_sha3', params: [data] }) }).then(function (response) { return response.json(); }).then(function (value) {
      if (!value || typeof value.result !== 'string') throw new Error('Official calldata hash failed.');
      return value.result;
    });
  }

  function verifyHash(provider) {
    return provider.request({ method: 'web3_sha3', params: [intent.data] }).catch(function () { return officialHash(intent.data); }).then(function (hash) {
      if (hash.toLowerCase() !== intent.calldata_hash.toLowerCase()) throw new Error('Calldata hash does not match.');
    });
  }

  function connectWallet() {
    var walletId = node('wallet').value;
    resetVerification();
    selected = providers.get(walletId) || null;
    if (!selected || !intent) return status('Load an intent and choose Rabby.', 'error');
    if (!/rabby/i.test(selected.name) && !selected.provider.isRabby) return status('Select Rabby for this authorized recovery.', 'error');
    selected.provider.request({ method: 'eth_requestAccounts' }).then(function (accounts) {
      account = accounts[0] || '';
      if (account.toLowerCase() !== intent.signer.toLowerCase()) throw new Error('Rabby account does not match the required signer.');
      return selected.provider.request({ method: 'eth_chainId' });
    }).then(function (chain) { return chain === '0x72' ? chain : switchChain(selected.provider); }).then(function () { return selected.provider.request({ method: 'eth_chainId' }); }).then(function (chain) {
      if (chain !== '0x72') throw new Error('Rabby is not on Coston2.');
      return verifyHash(selected.provider);
    }).then(function () { verified = true; node('confirmIntent').disabled = false; status('Rabby account, Coston2, and calldata hash verified.', 'success'); }).catch(function (error) { resetVerification(); status(error.message || 'Rabby verification failed.', 'error'); });
  }

  function guardKey() { return 'invoicecraft-recovery:' + intent.signer.toLowerCase() + ':' + intent.to.toLowerCase() + ':' + intent.calldata_hash.toLowerCase(); }

  function sendTransaction() {
    if (!verified || !node('confirmIntent').checked || !selected) return;
    var key = guardKey();
    if (localStorage.getItem(key)) return status('This exact intent may already be submitted. Verify it before retrying.', 'error');
    localStorage.setItem(key, 'pending'); node('sendTransaction').disabled = true;
    selected.provider.request({ method: 'eth_sendTransaction', params: [{ from: account, to: intent.to, value: intent.value, data: intent.data }] }).then(function (hash) {
      if (!/^0x[0-9a-f]{64}$/i.test(hash)) throw new Error('Rabby returned an invalid transaction hash.');
      localStorage.setItem(key, hash.toLowerCase()); show('transactionHash', hash); node('transactionHash').hidden = false;
      status('Submitted. Copy the transaction hash back to the operator.', 'success');
    }).catch(function (error) {
      if (String(error && error.code) === '4001') localStorage.removeItem(key);
      node('sendTransaction').disabled = false; status(error.message || 'Submission status is unknown. Verify before retrying.', 'error');
    });
  }

  var initial = new URLSearchParams(location.search).get('intent') || '';
  node('intentPath').value = initial;
  node('loadIntent').addEventListener('click', loadIntent);
  node('wallet').addEventListener('change', function () { node('connectWallet').disabled = !node('wallet').value || !intent; });
  node('connectWallet').addEventListener('click', connectWallet);
  node('confirmIntent').addEventListener('change', function () { node('sendTransaction').disabled = !verified || !node('confirmIntent').checked; });
  node('sendTransaction').addEventListener('click', sendTransaction);
  window.addEventListener('eip6963:announceProvider', function (event) { addWallet(event.detail); });
  window.dispatchEvent(new Event('eip6963:requestProvider'));
  (window.ethereum && (window.ethereum.providers || [window.ethereum]) || []).forEach(function (provider) { addWallet({ info: { name: provider.isRabby ? 'Rabby' : 'Injected wallet' }, provider: provider }); });
  if (initial) loadIntent();
}());
