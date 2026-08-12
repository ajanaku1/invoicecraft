#!/usr/bin/env bash
# InvoiceCraftXRP — executable done predicates.
#
# On a fresh scaffold this file starts red. In this in-progress build Phase 0 is
# now green while later phases remain red for substantive missing behavior.
# Never weaken a predicate to manufacture a PASS. Live checks use bounded RPC
# validators; this wrapper invokes their evidence contracts without mutation.

set -uo pipefail
cd "$(dirname "$0")"

FILTER="${1:-}"
pass=0
fail=0
executed=0

case "$FILTER" in
  ""|phase-0|phase-1|phase-2|phase-3|phase-4) ;;
  *) printf 'usage: %s [phase-0|phase-1|phase-2|phase-3|phase-4]\n' "$0" >&2; exit 2 ;;
esac

check() {
  local tag="$1" description="$2"
  shift 2
  if [ -n "$FILTER" ] && [ "$tag" != "$FILTER" ]; then return 0; fi
  executed=$((executed + 1))
  if "$@" >/dev/null 2>&1; then
    printf '  PASS  [%s] %s\n' "$tag" "$description"
    pass=$((pass + 1))
  else
    printf '  FAIL  [%s] %s\n' "$tag" "$description"
    fail=$((fail + 1))
  fi
}

checksh() { check "$1" "$2" sh -c "$3"; }

pytest_files() {
  PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider "$@"
}

forge_isolated() {
  local scratch list_output result
  test -d contracts && test -f contracts/foundry.toml && test -d contracts/test || return 1
  scratch="$(mktemp -d)" || return 1
  if ! list_output="$(FOUNDRY_CACHE_PATH="$scratch/cache" FOUNDRY_OUT="$scratch/out" forge test --root contracts --list)"; then
    rm -rf "$scratch" || return 1
    return 1
  fi
  if ! printf '%s\n' "$list_output" | rg -q '(^|[[:space:].:])test[A-Za-z0-9_]*([[:space:]]*\(|[[:space:]]*$)'; then
    rm -rf "$scratch" || return 1
    return 1
  fi
  if FOUNDRY_CACHE_PATH="$scratch/cache" FOUNDRY_OUT="$scratch/out" forge test --root contracts; then
    result=0
  else
    result=$?
  fi
  rm -rf "$scratch" || return 1
  return "$result"
}

verify_live_evidence() {
  local evidence="$1" output
  test -s "$evidence" && test -f scripts/verify_live_evidence.py || return 1
  output="$(python3 scripts/verify_live_evidence.py --timeout-seconds 15 "$evidence")" || return 1
  test "$output" = RPC_EVIDENCE_VALID
}

verify_acceptance_record() {
  local output selected
  test -s evidence/acceptance-record.json && test -f scripts/verify_acceptance_record.py || return 1
  selected="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["selected_ui"]["proposal"])' evidence/acceptance-record.json)" || return 1
  case "$selected" in option-1.html|option-2.html|option-3.html) ;; *) return 1 ;; esac
  output="$(python3 scripts/verify_acceptance_record.py evidence/acceptance-record.json --selected-proposal "$selected")" || return 1
  test "$output" = ACCEPTANCE_RECORD_VALID
}

verify_product_settlement() {
  local output
  test -s evidence/product-settlement-smoke.json && test -f scripts/verify_product_settlement.py || return 1
  output="$(python3 scripts/verify_product_settlement.py --timeout-seconds 15 evidence/product-settlement-smoke.json)" || return 1
  test "$output" = PRODUCT_SETTLEMENT_VALID
}

verify_selected_ui() {
  test -s dashboard/logo.png && test -s dashboard/pay.html && test -s dashboard/xrp-pay.js || return 1
  test -s docs/images/xrp-creator.png && test -s docs/images/xrp-pay.png && test -s docs/images/xrp-receipt.png || return 1
  test -s evidence/acceptance-record.json || return 1
  python3 -c '
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    ui = json.load(source)["selected_ui"]
widths = {
    item["width"]
    for item in ui["viewports"]
    if item.get("result") == "passed"
    and item.get("horizontal_overflow") is False
}
approved = ui.get("status") == "approved"
has_desktop = any(width >= 1024 for width in widths)
has_mobile = any(width <= 420 for width in widths)
raise SystemExit(0 if approved and has_desktop and has_mobile else 1)
' evidence/acceptance-record.json
}

verify_no_secrets() {
  local output
  test -d app && test -d contracts && test -d dashboard && test -d tests && test -d scripts && test -d evidence && test -d docs && test -d reports || return 1
  test -f README.md && test -f requirements.txt && test -f Dockerfile && test -f render.yaml && test -f .env.example && test -f LICENSE || return 1
  test -f scripts/verify_no_secrets.py || return 1
  output="$(python3 scripts/verify_no_secrets.py app contracts dashboard tests scripts evidence docs reports README.md requirements.txt Dockerfile render.yaml .env.example LICENSE)" || return 1
  test "$output" = NO_SECRETS_VALID
}

echo '== InvoiceCraftXRP verify =='

checksh phase-0 'protocol-spike artifacts exist' \
  'test -f tests/test_fsa_instruction.py && test -f tests/test_recovery_state.py && test -f tests/test_phase0_live.py && test -s tests/fixtures/protocol-spike-pending.json && test -f scripts/verify_live_evidence.py && test -s evidence/protocol-spike.json'
check phase-0 'protocol-spike Python tests pass' \
  pytest_files tests/test_fsa_instruction.py tests/test_recovery_state.py tests/test_phase0_live.py
check phase-0 'protocol-spike evidence validates by RPC' \
  verify_live_evidence evidence/protocol-spike.json

checksh phase-1 'XRP domain/API tests exist' \
  'test -f tests/test_xrp_store.py && test -f tests/test_xrp_quote.py && test -f tests/test_xrp_api.py && test -f tests/test_xaman.py'
check phase-1 'XRP domain/API tests pass' \
  pytest_files tests/test_xrp_store.py tests/test_xrp_quote.py tests/test_xrp_api.py tests/test_xaman.py

checksh phase-2 'contract and executor artifacts exist' \
  'test -f contracts/src/InvoiceSettlement.sol && test -f contracts/src/TestLiquidityAdapter.sol && test -d contracts/test && test -f tests/test_xrp_executor.py && test -f tests/test_xrp_receipt.py'
check phase-2 'isolated Foundry tests pass' forge_isolated
check phase-2 'executor and receipt tests pass' \
  pytest_files tests/test_xrp_executor.py tests/test_xrp_receipt.py

check phase-3 'selected UI implementation and reviewed screenshots validate' verify_selected_ui
checksh phase-3 'pay-page implementation exists' \
  'test -f dashboard/pay.html && test -f dashboard/xrp-pay.js && test -f tests/test_xrp_dashboard.py'
check phase-3 'dashboard behavior test passes' pytest_files tests/test_xrp_dashboard.py
checksh phase-3 'pay page discloses settlement states' \
  'test -f dashboard/pay.html && test -f dashboard/xrp-pay.js && rg -qi "Coston2" dashboard/pay.html dashboard/xrp-pay.js && rg -qi "Test liquidity" dashboard/pay.html dashboard/xrp-pay.js && rg -q "USD₮0" dashboard/pay.html dashboard/xrp-pay.js && rg -qi "quote" dashboard/pay.html dashboard/xrp-pay.js && rg -qi "progress" dashboard/pay.html dashboard/xrp-pay.js && rg -qi "recovery" dashboard/pay.html dashboard/xrp-pay.js'

checksh phase-4 'all XRP test artifacts and live smoke exist' \
  'test -f tests/test_fsa_instruction.py && test -f tests/test_recovery_state.py && test -f tests/test_xrp_store.py && test -f tests/test_xrp_quote.py && test -f tests/test_xrp_api.py && test -f tests/test_xaman.py && test -f tests/test_xrp_executor.py && test -f tests/test_xrp_receipt.py && test -f tests/test_xrp_dashboard.py && test -s evidence/coston2-smoke.json && test -f scripts/verify_live_evidence.py'
checksh phase-4 'full nonmutating suite includes legacy tests' \
  'test -f tests/test_invoice.py && test -f tests/test_endpoint.py && test -f tests/test_x402.py && test -f tests/test_xrp_store.py && test -f tests/test_xrp_executor.py && PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -p no:cacheprovider --ignore=tests/test_verify_proposals.py tests'
check phase-4 'isolated full Foundry suite passes' forge_isolated
check phase-4 'Coston2 smoke evidence validates by RPC' \
  verify_live_evidence evidence/coston2-smoke.json
check phase-4 'exact product settlement validates by Coston2 RPC' \
  verify_product_settlement
check phase-4 'acceptance record validates all manual evidence' verify_acceptance_record
checksh phase-4 'README has disclosure and judge-run instructions' \
  'test -f README.md && rg -qi "before.*new|new.*before" README.md && rg -q "XRP→FXRP→USD₮0" README.md && rg -qi "setup" README.md && rg -qi "test" README.md && rg -qi "demo" README.md && rg -qi "test-liquidity|test liquidity" README.md'
check phase-4 'scoped source tree is secret-free' verify_no_secrets
checksh phase-4 'documentation makes no forbidden settlement claims' \
  'command -v rg >/dev/null 2>&1 && test -f README.md && test -d app && test -d contracts && test -d dashboard && ! rg -n -i "cross-chain atomic|real Coston2 liquidity|guaranteed mainnet" README.md dashboard app contracts'
checksh phase-4 'diff has no whitespace errors' \
  'test -s evidence/coston2-smoke.json && git diff --check'

echo
if [ "$executed" -eq 0 ]; then
  printf '  FAIL  [filter] no checks matched "%s"; executed 0 predicates\n' "$FILTER"
  fail=$((fail + 1))
fi
printf 'passed %d, failed %d\n' "$pass" "$fail"

cat <<'MANUAL'

manual:
  [ ] Before Phase 3 integration, visually inspect all three isolated directions
      at representative desktop and mobile sizes and confirm the recorded choice.
  [ ] Before final completion, confirm Xaman device/deep-link behavior, recovery
      wording, explorer links, two-minute demo pacing,
      and before/new disclosure in the acceptance record.
MANUAL

[ "$fail" -eq 0 ] && [ "$pass" -gt 0 ]
