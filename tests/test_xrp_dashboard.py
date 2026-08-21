from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


ROOT = Path(__file__).resolve().parents[1]
PAY_HTML = ROOT / "dashboard" / "pay.html"
PAY_JS = ROOT / "dashboard" / "xrp-pay.js"
CREATOR_HTML = ROOT / "dashboard" / "index.html"
CREATOR_JS = ROOT / "dashboard" / "xrp-create.js"
OPERATOR_HTML = ROOT / "dashboard" / "operator.html"
OPERATOR_JS = ROOT / "dashboard" / "xrp-operator.js"
RECOVERY_OPERATOR_HTML = ROOT / "dashboard" / "recovery-operator.html"
RECOVERY_OPERATOR_JS = ROOT / "dashboard" / "recovery-operator.js"


def test_shared_payment_route_serves_the_selected_docket_shell() -> None:
    response = TestClient(app).get("/pay/example-invoice")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="settlementDocket"' in response.text
    assert '<script src="/xrp-pay.js"></script>' in response.text


def test_public_homepage_displays_the_authorized_x_account() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    footer_marker = '<footer class="site-footer"'
    assert footer_marker in response.text
    footer = response.text.split(footer_marker, maxsplit=1)[1]
    assert 'href="https://x.com/curioswhispers"' in footer
    assert "> @curioswhispers</a>" in footer


def test_payment_page_exposes_the_complete_truthful_settlement_journey() -> None:
    assert PAY_JS.is_file()
    surface = PAY_HTML.read_text() + PAY_JS.read_text()

    for state in (
        "open",
        "quoted",
        "awaiting_signature",
        "xrpl_submitted",
        "flare_executing",
        "paid",
        "quote_expired",
        "payment_rejected",
        "recovery_required",
    ):
        assert state in surface
    for disclosure in (
        "Coston2",
        "Test liquidity",
        "USD₮0",
        "No mainnet guarantee",
        "Xaman",
        "Unsigned transaction",
        "XRPL finalizes first",
        "guided recovery",
    ):
        assert disclosure.lower() in surface.lower()


def test_payment_controller_wires_every_api_boundary_and_recovery_path() -> None:
    source = PAY_JS.read_text()

    for endpoint in (
        "/api/v1/xrp/invoices/",
        "/quote",
        "/signing-request",
        "/submit",
        "/receipt",
    ):
        assert endpoint in source
    for behavior in (
        "expires_at",
        "setTimeout",
        "clearTimeout",
        "finally",
        "aria-busy",
        "Receipt evidence is unavailable.",
        "Settlement needs attention.",
        "textContent",
        "window.print",
    ):
        assert behavior in source
    assert "innerHTML" not in source


def test_expired_quote_removes_stale_xaman_approval_targets() -> None:
    source = PAY_JS.read_text()

    assert "This Xaman request expired" in source
    assert "renderExpiredSigning" in source
    assert "anchor.removeAttribute('href')" in source
    assert "image.removeAttribute('src')" in source


def test_creator_can_bind_a_flare_beneficiary_and_share_a_free_xrp_invoice() -> None:
    assert CREATOR_JS.is_file()
    surface = CREATOR_HTML.read_text() + CREATOR_JS.read_text()

    for marker in (
        'id="xrpBeneficiary"',
        'id="createXrpBtn"',
        'id="xrpSharePanel"',
        "/api/v1/xrp/invoices",
        "Idempotency-Key",
        "share_url",
        "Free to create",
        "Coston2",
        "USD₮0",
    ):
        assert marker in surface
    assert '<script src="xrp-create.js"></script>' in CREATOR_HTML.read_text()


def test_browser_operator_is_keyless_explicit_and_queue_driven() -> None:
    assert OPERATOR_HTML.is_file()
    assert OPERATOR_JS.is_file()
    surface = OPERATOR_HTML.read_text() + OPERATOR_JS.read_text()

    for marker in (
        'id="operatorToken"',
        'id="invoiceId"',
        'id="wallet"',
        'id="connectWallet"',
        'id="confirmIntent"',
        'id="sendTransaction"',
        "/operator-job",
        "X-Operator-Token",
        "eip6963:requestProvider",
        "wallet_switchEthereumChain",
        "eth_sendTransaction",
        "Coston2",
        "Never stores wallet keys",
    ):
        assert marker in surface
    assert "localStorage.setItem('operatorToken'" not in surface
    assert "privateKey" not in surface
    assert "eth_requestAccounts" in surface


def test_recovery_operator_verifies_public_intent_before_rabby_submission() -> None:
    assert RECOVERY_OPERATOR_HTML.is_file()
    assert RECOVERY_OPERATOR_JS.is_file()
    surface = RECOVERY_OPERATOR_HTML.read_text() + RECOVERY_OPERATOR_JS.read_text()

    for marker in (
        'id="intentPath"',
        'id="wallet"',
        'id="connectWallet"',
        'id="confirmIntent"',
        'id="sendTransaction"',
        "eip6963:requestProvider",
        "eth_requestAccounts",
        "wallet_switchEthereumChain",
        "web3_sha3",
        "eth_sendTransaction",
        "calldata_hash",
        "0x72",
        "Rabby",
        "Never stores wallet keys",
    ):
        assert marker in surface
    for forbidden in ("privateKey", "seedPhrase", "XRP_OPERATOR_TOKEN"):
        assert forbidden not in surface
    assert "resetVerification();\n    selected = providers.get" in RECOVERY_OPERATOR_JS.read_text()
