"""
Cashfree Payment Gateway Implementation

Implements the BasePaymentGateway interface for Cashfree APIs.
Uses the SDK for Payment Gateway (PG) and direct REST API for Payouts (v2024-01-01).
"""

import logging
import time
import hashlib
import hmac
import json
import requests
import base64
from typing import Optional, Dict, Any
from django.conf import settings

from .base_gateway import BasePaymentGateway

logger = logging.getLogger(__name__)

# Cashfree PG SDK (v3.2.12)
try:
    import cashfree_pg
    from cashfree_pg.api_client import Cashfree
    from cashfree_pg.models.create_order_request import CreateOrderRequest
    from cashfree_pg.models.customer_details import CustomerDetails
    from cashfree_pg.models.order_meta import OrderMeta
    
    CASHFREE_PG_AVAILABLE = True
except ImportError:
    logger.warning("Cashfree PG SDK not installed. Install with: pip install cashfree-pg==3.2.12")
    CASHFREE_PG_AVAILABLE = False


class CashfreeGateway(BasePaymentGateway):
    """Cashfree payment gateway implementation."""

    # Every outbound gateway call is bounded. Without a timeout a hung
    # connection holds the worker slot until the OS times out the socket,
    # which on the payment path means a rider stuck on a spinner and one
    # fewer worker for everyone else.
    HTTP_TIMEOUT_SECONDS = 15
    
    def __init__(self):
        # PG SDK Initialization
        if CASHFREE_PG_AVAILABLE:
            Cashfree.XClientId = settings.CASHFREE_APP_ID
            Cashfree.XClientSecret = settings.CASHFREE_SECRET_KEY
            Cashfree.XEnvironment = (
                Cashfree.SANDBOX if "sandbox" in settings.CASHFREE_PG_BASE_URL.lower() 
                else Cashfree.PRODUCTION
            )
            self.pg_client = Cashfree()
        else:
            self.pg_client = None


    
    def get_name(self) -> str:
        return 'cashfree'



    def create_order(
        self,
        amount,
        trip_id=None,
        currency: str = 'INR',
        customer_id=None,
        customer_phone=None,
        customer_email=None,
        **_,  # tolerate caller-supplied extras (receipt, notes, ...)
    ) -> Optional[Dict[str, Any]]:
        """Create a Cashfree order using REST API to bypass SDK validation bugs.

        Callers from non-trip contexts (e.g. wallet top-up) pass their own
        customer_id and customer phone/email. We never hard-code a real person's
        email; the fallback is the platform's no-reply address.
        """
        try:
            ref = customer_id or (str(trip_id) if trip_id is not None else 'na')
            order_id = f"{ref}{int(time.time())}"

            env = "sandbox" if "sandbox" in settings.CASHFREE_PG_BASE_URL.lower() else "api"
            url = f"https://{env}.cashfree.com/pg/orders"

            headers = {
                "X-Client-Id": settings.CASHFREE_APP_ID,
                "X-Client-Secret": settings.CASHFREE_SECRET_KEY,
                "x-api-version": "2023-08-01",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            # Ensure notify_url has a valid protocol
            notify_url = f"{settings.BACKEND_URL}/api/v1/payments/webhook/"
            if not notify_url.startswith("http"):
                notify_url = f"http://{notify_url}"

            payload = {
                "order_amount": float(amount),
                "order_currency": currency,
                "order_id": order_id,
                "customer_details": {
                    "customer_id": str(ref),
                    "customer_phone": customer_phone or "9999999999",
                    "customer_email": customer_email or "noreply@saaradhigo.in",
                },
                "order_meta": {
                    "notify_url": notify_url,
                },
            }
            logger.debug("Cashfree create_order for trip=%s amount=%s", trip_id, amount)
            response = requests.post(
                url, headers=headers, json=payload,
                timeout=self.HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()
            
            logger.debug("Cashfree create_order response order_id=%s", data.get("order_id"))
            
            return {
                'gateway': 'cashfree',
                'gateway_order_id': order_id,
                'order_id': order_id,
                'payment_session_id': data.get('payment_session_id'),
                'order_amount': float(amount),
                'payment_link': data.get('payment_link'),
                'cf_order_id': data.get('cf_order_id'),
            }
        except Exception as e:
            logger.error(f"CashfreeGateway.create_order failed: {e}")
            if isinstance(e, requests.exceptions.RequestException) and getattr(e, 'response', None) is not None:
                logger.error(f"Cashfree API Error Response: {e.response.text}")
            return None

    def verify_payment_signature(self, order_id: str) -> bool:
        """Verify payment via order status API."""
        order_status = self.get_order_status(order_id)
        return order_status.get('payment_status') == 'SUCCESS' if order_status else False

    # Reject webhooks whose timestamp drifts more than this from server time.
    # Cashfree retries on failure, so a small window is fine and replays are blocked.
    WEBHOOK_TIMESTAMP_WINDOW_SECONDS = 5 * 60

    def verify_webhook_signature(self, body: bytes, signature: str, timestamp: Optional[str] = None) -> bool:
        """Verify Cashfree webhook signature (V2/V3) and freshness.

        Returns False — fail closed — when the webhook secret is not configured
        or the timestamp is outside the freshness window. A misconfigured
        production deployment would otherwise accept any forged "payment success"
        payload from the public internet.
        """
        secret = getattr(settings, 'CASHFREE_WEBHOOK_SECRET', None)
        if not secret:
            logger.error(
                "Cashfree webhook rejected: CASHFREE_WEBHOOK_SECRET is not configured"
            )
            return False

        if not signature:
            logger.warning("Cashfree webhook rejected: missing signature header")
            return False

        # Cashfree V3 always sends x-webhook-timestamp. Requiring it gives us a
        # replay window; treating it as optional would re-open the very hole we
        # are closing here.
        if not timestamp:
            logger.warning("Cashfree webhook rejected: missing timestamp header")
            return False

        try:
            ts_val = int(timestamp)
            # Cashfree may send seconds (10-11 digits) or milliseconds (13 digits).
            if ts_val > 10 ** 11:
                ts_val //= 1000
            event_age = int(time.time()) - ts_val
            if abs(event_age) > self.WEBHOOK_TIMESTAMP_WINDOW_SECONDS:
                logger.warning(
                    f"Cashfree webhook rejected: timestamp out of window "
                    f"(age={event_age}s, max={self.WEBHOOK_TIMESTAMP_WINDOW_SECONDS}s)"
                )
                return False
        except (TypeError, ValueError):
            logger.warning("Cashfree webhook rejected: malformed timestamp header")
            return False

        try:
            body_str = body.decode('utf-8') if isinstance(body, bytes) else str(body)
            secret_bytes = secret.encode('utf-8')

            message = timestamp + body_str
            computed = base64.b64encode(
                hmac.new(secret_bytes, message.encode('utf-8'), hashlib.sha256).digest()
            ).decode('utf-8')

            return hmac.compare_digest(computed, signature)
        except Exception as e:
            logger.error(f"Webhook signature verification failed: {e}")
            return False

    def create_refund(self, payment_id: str, amount: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Create a Cashfree refund using PG SDK.

        Note: Cashfree's PGOrderCreateRefund is keyed on the *order id*, not the
        cf_payment_id. The `payment_id` parameter name is kept for the abstract
        BasePaymentGateway interface, but callers MUST pass the Cashfree
        order_id (or the equivalent stored on `Payment.cashfree_order_id`).
        Passing a cf_payment_id will return 404 at Cashfree.

        `amount` is required. Cashfree rejects 0-amount refunds; passing None
        is a caller bug, so we fail fast rather than letting the gateway
        produce a confusing 400.
        """
        if not self.pg_client:
            return None
        if amount is None or float(amount) <= 0:
            logger.error(
                f"CashfreeGateway.create_refund: amount must be positive, got {amount} "
                f"(order_id={payment_id})"
            )
            return None
        try:
            from cashfree_pg.models.order_create_refund_request import OrderCreateRefundRequest

            refund_id = f"refund_{payment_id}_{int(time.time())}"

            refund_request = OrderCreateRefundRequest(
                refund_amount=float(amount),
                refund_id=refund_id,
                refund_note="Trip cancellation"
            )

            # payment_id here is the Cashfree order_id (see docstring).
            response = self.pg_client.PGOrderCreateRefund("2023-08-01", payment_id, refund_request)
            if response and hasattr(response, 'data'):
                return {
                    'gateway': 'cashfree',
                    'refund_id': refund_id,
                    'status': getattr(response.data, 'status', 'PENDING'),
                    'cf_refund_id': getattr(response.data, 'cf_refund_id', None),
                }
            return None
        except Exception as e:
            logger.error(f"Refund failed: {e}")
            return None



    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order status using REST API to bypass SDK validation bugs.

        Returns a dict including the authoritative order_amount and order_currency
        so callers can server-verify the amount they expected against what
        Cashfree actually recorded as paid.
        """
        try:
            env = "sandbox" if "sandbox" in settings.CASHFREE_PG_BASE_URL.lower() else "api"
            url = f"https://{env}.cashfree.com/pg/orders/{order_id}"

            headers = {
                "X-Client-Id": settings.CASHFREE_APP_ID,
                "X-Client-Secret": settings.CASHFREE_SECRET_KEY,
                "x-api-version": "2023-08-01",
                "Accept": "application/json",
            }

            response = requests.get(
                url, headers=headers, timeout=self.HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data = response.json()

            return {
                'gateway': 'cashfree',
                'order_id': order_id,
                'order_status': data.get('order_status'),
                'order_amount': data.get('order_amount'),
                'order_currency': data.get('order_currency'),
                'payment_status': 'SUCCESS' if data.get('order_status') == 'PAID' else data.get('order_status'),
            }
        except Exception as e:
            logger.error(f"Fetch order failed: {e}")
            if isinstance(e, requests.exceptions.RequestException) and getattr(e, 'response', None) is not None:
                logger.error(f"Cashfree response: {e.response.text}")
            return None

    # ---------------------------------------------------------------------
    # Payouts (Cashfree Payouts API — separate product from PG)
    # ---------------------------------------------------------------------

    _payout_token = None
    _payout_token_expires_at = 0  # epoch seconds

    def _payout_base_url(self):
        return getattr(
            settings,
            'CASHFREE_PAYOUT_BASE_URL',
            'https://payout-api.cashfree.com',
        )

    def _payout_credentials(self):
        """Return (client_id, client_secret) for Cashfree Payouts or (None, None).

        Payouts use a *different* credential pair from the PG side. If
        the platform isn't onboarded for payouts yet, both will be empty
        and the caller falls back to the failure path.
        """
        client_id = getattr(settings, 'CASHFREE_PAYOUT_APP_ID', '') or ''
        client_secret = getattr(settings, 'CASHFREE_PAYOUT_SECRET_KEY', '') or ''
        return client_id or None, client_secret or None

    def _get_payout_token(self):
        """Authorize against Cashfree Payouts and cache the token in-process.

        Cashfree Payouts tokens are valid ~30 min. We refresh ~25 min in.
        """
        now = int(time.time())
        if self._payout_token and now < self._payout_token_expires_at:
            return self._payout_token

        client_id, client_secret = self._payout_credentials()
        if not client_id or not client_secret:
            logger.error(
                "CashfreeGateway.create_upi_payout: CASHFREE_PAYOUT_APP_ID / "
                "CASHFREE_PAYOUT_SECRET_KEY are not configured; payouts will fail. "
                "Configure them in env once the platform is onboarded for Cashfree Payouts."
            )
            return None

        try:
            resp = requests.post(
                f"{self._payout_base_url()}/payout/v1/authorize",
                headers={
                    "X-Client-Id": client_id,
                    "X-Client-Secret": client_secret,
                    "Accept": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get('data', {}).get('token')
            if not token:
                logger.error(f"CashfreeGateway: payout authorize returned no token: {data}")
                return None
            type(self)._payout_token = token
            # Refresh 5 min before the 30-min expiry.
            type(self)._payout_token_expires_at = now + (25 * 60)
            return token
        except Exception as e:
            logger.error(f"CashfreeGateway: payout authorize failed: {e}")
            if isinstance(e, requests.exceptions.RequestException) and getattr(e, 'response', None) is not None:
                logger.error(f"Cashfree response: {e.response.text}")
            return None

    def create_upi_payout(
        self,
        upi_id: str,
        amount,
        purpose: str = "payout",
        currency: str = "INR",
        reference_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Create a UPI payout via Cashfree Payouts (request_transfer flavour).

        Returns a dict with at least:
            { 'payout_id': str, 'status': str, 'contact_id': str|None,
              'fund_account_id': str|None }
        or None on failure (caller marks the withdrawal failed).

        Phase-0 note: this implementation uses the Cashfree Payouts V1
        request_transfer endpoint with the in-line beneficiary form so
        we don't have to maintain a beneId. Once we onboard for V2 and
        want to reuse beneficiaries, this method should switch to
        addBeneficiary + requestTransfer with beneId.
        """
        if not upi_id:
            logger.error("create_upi_payout: upi_id is required")
            return None
        try:
            amount_val = float(amount)
        except (TypeError, ValueError):
            logger.error(f"create_upi_payout: bad amount {amount!r}")
            return None
        if amount_val <= 0:
            logger.error(f"create_upi_payout: non-positive amount {amount_val}")
            return None

        token = self._get_payout_token()
        if not token:
            return None

        transfer_id = reference_id or f"payout_{int(time.time())}"

        payload = {
            "beneId": "",  # leaving empty triggers in-line beneficiary mode
            "amount": str(round(amount_val, 2)),
            "transferId": transfer_id,
            "transferMode": "upi",
            "remarks": purpose or "Driver earnings payout",
            "beneficiaryDetails": {
                "name": (name or "Driver")[:50],
                "vpa": upi_id,
            },
        }

        try:
            resp = requests.post(
                f"{self._payout_base_url()}/payout/v1.2/requestTransfer",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=payload,
                timeout=15,
            )
            data = resp.json() if resp.content else {}
        except Exception as e:
            logger.error(f"create_upi_payout: network failure: {e}")
            return None

        if resp.status_code not in (200, 201):
            logger.error(
                f"create_upi_payout: HTTP {resp.status_code} from Cashfree: {data}"
            )
            return None

        # Cashfree returns various envelope shapes across versions. Be defensive.
        status_val = data.get('status') or (data.get('data') or {}).get('status')
        if status_val and str(status_val).upper() in ('ERROR', 'FAILED'):
            logger.error(f"create_upi_payout: gateway rejected transfer: {data}")
            return None

        body = data.get('data') or {}
        return {
            'payout_id': transfer_id,
            'status': str(status_val or 'PENDING'),
            'gateway_reference': body.get('referenceId') or body.get('reference_id'),
            'contact_id': body.get('contact_id'),
            'fund_account_id': body.get('fund_account_id'),
            'raw': data,
        }






