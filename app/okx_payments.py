"""Official OKX Onchain OS Payment SDK integration.

Gates POST /api/v1/invoice with the OKX x402 seller SDK: unpaid callers get a
standard 402 with the PAYMENT-REQUIRED header, the Agentic Wallet retries with
PAYMENT-SIG, and the SDK verifies and settles through the OKX facilitator.

Requires OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE (Onchain OS dev portal)
plus ASP_WALLET. When any of those are missing the middleware is not installed
and the service falls back to the built-in x402 challenge in app.main, so local
development, tests, and the demo keep working without OKX credentials.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

PROTECTED_ROUTE = "POST /api/v1/invoice"


def _credentials() -> dict[str, str]:
    return {
        "api_key": os.getenv("OKX_API_KEY", ""),
        "secret_key": os.getenv("OKX_SECRET_KEY", ""),
        "passphrase": os.getenv("OKX_PASSPHRASE", ""),
    }


def payments_enabled() -> bool:
    """True when every credential needed by the OKX SDK is configured."""
    return all(_credentials().values()) and bool(os.getenv("ASP_WALLET", ""))


def _network() -> str:
    return os.getenv("PAYMENT_CHAIN", "eip155:196")


def _build_server(facilitator: Any) -> Any:
    """Build an x402 resource server, or None if the facilitator is unusable."""
    from x402.http import OKXAuthConfig, OKXFacilitatorClient, OKXFacilitatorConfig
    from x402.mechanisms.evm.exact.server import ExactEvmScheme
    from x402.server import x402ResourceServer

    if facilitator is None:
        facilitator = OKXFacilitatorClient(
            OKXFacilitatorConfig(
                auth=OKXAuthConfig(**_credentials()),
                base_url=os.getenv("OKX_BASE_URL", "https://web3.okx.com"),
                sync_settle=True,
            )
        )

    server = x402ResourceServer(facilitator)
    server.register(_network(), ExactEvmScheme())

    # Handshake at boot rather than 500-ing the first caller: the facilitator
    # rejects a bad API key here, and we keep serving the fallback 402.
    try:
        server.initialize()
    except Exception:
        logger.exception("OKX facilitator handshake failed; payment middleware disabled")
        return None
    return server


def _build_routes() -> dict[str, Any]:
    """The single payment-protected route, priced in USD for the facilitator."""
    from x402.http import PaymentOption
    from x402.http.types import RouteConfig

    return {
        PROTECTED_ROUTE: RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    price=f"${os.getenv('INVOICE_PRICE_USDT', '0.50')}",
                    network=_network(),
                    pay_to=os.getenv("ASP_WALLET", ""),
                    max_timeout_seconds=300,
                )
            ],
            description="InvoiceCraft AI invoice generation",
            mime_type="application/json",
        )
    }


def _resilient_middleware(base: type) -> type:
    """Subclass the SDK middleware so facilitator failures answer 402, not 500.

    Falling back can only ever return "payment required", never free access.
    """

    class ResilientPaymentMiddleware(base):  # type: ignore[misc, valid-type]
        def __init__(self, app: Any, fallback: Callable | None = None, **kwargs: Any):
            super().__init__(app, **kwargs)
            self._fallback = fallback

        async def dispatch(self, request: Any, call_next: Callable) -> Any:
            try:
                return await super().dispatch(request, call_next)
            except Exception:
                if self._fallback is None:
                    raise
                logger.exception("OKX payment middleware failed; serving fallback 402")
                return self._fallback(request)

    return ResilientPaymentMiddleware


def install_payment_middleware(
    app: Any, fallback: Callable | None = None, facilitator: Any = None
) -> bool:
    """Install the OKX payment middleware on `app`.

    `fallback` is an optional `(request) -> Response` used to answer with the
    built-in 402 if the facilitator errors mid-request. `facilitator` overrides
    the OKX facilitator client (tests only).

    Returns True when the middleware was installed, False when credentials are
    missing, unusable, or the SDK is not available in this environment.
    """
    if facilitator is None and not payments_enabled():
        logger.info("OKX Payment SDK not configured; using fallback x402 challenge")
        return False

    try:
        from x402.http.middleware.fastapi import PaymentMiddlewareASGI
    except ImportError:
        logger.warning("okxweb3-app-x402 is not installed; payment middleware disabled")
        return False

    server = _build_server(facilitator)
    if server is None:
        return False

    app.add_middleware(
        _resilient_middleware(PaymentMiddlewareASGI),
        routes=_build_routes(),
        server=server,
        fallback=fallback,
    )
    logger.info("OKX Payment SDK middleware installed on %s", PROTECTED_ROUTE)
    return True
