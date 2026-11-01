"""Razorpay payments (India): orders, UPI / cards / wallets through Checkout, capture, refunds.

Plain HTTPS via `requests` (no SDK). Amounts cross the interface as integer minor units and
Razorpay takes paise natively, so nothing is converted on the way out; the only major-unit value
is the `order_amount` string that `get_order_status` returns so the shared webhook / reconcile
code (written for Cashfree's major-unit number) compares like for like.

Docs this follows (razorpay.com/docs):
  * Orders:   POST /v1/orders {amount, currency, receipt, notes}; status created|attempted|paid.
  * Checkout signature: HMAC-SHA256("<order_id>|<payment_id>", key_secret), hex, compared with
    `razorpay_signature` (payments / payment-gateway integration, "verify payment signature").
  * Capture:  POST /v1/payments/{id}/capture {amount, currency}; only for a payment that is
    `authorized`. With auto capture on (the account setting) payments arrive `captured`.
  * Refunds:  POST /v1/payments/{id}/refund {amount?, speed: "optimum", receipt, notes}. No
    idempotency header exists: `receipt` is the key (a repeat gives 400 "Duplicate receipt"), so
    the refund reference goes in `receipt` and a duplicate is resolved by fetching that refund.
  * Webhooks: X-Razorpay-Signature = HMAC-SHA256 hex of the RAW body under the webhook secret;
    X-Razorpay-Event-Id uniquely identifies an event (webhooks / validate-test).
"""

import logging
import time
from typing import Any, Dict, Optional

from django.conf import settings

from base.money import to_major_string
from integrations import razorpay_http as rzp
from servers.payments.payment_gateways.base_gateway import BasePaymentGateway

logger = logging.getLogger(__name__)


def _positive_minor(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def key_pair():
    return (getattr(settings, 'RAZORPAY_KEY_ID', '') or '',
            getattr(settings, 'RAZORPAY_KEY_SECRET', '') or '')


def webhook_secret():
    return getattr(settings, 'RAZORPAY_WEBHOOK_SECRET', '') or ''


class RazorpayGateway(BasePaymentGateway):
    """Razorpay payment gateway. Falsy (and sends nothing) while keys are absent or refused."""

    CURRENCIES = ('INR',)

    def get_name(self) -> str:
        return 'razorpay'

    def supported_currencies(self):
        return self.CURRENCIES

    # -- configuration -------------------------------------------------------------------

    def not_configured_reason(self, kind='payments'):
        key_id, secret = key_pair()
        refusal = rzp.key_refusal(key_id)
        if refusal:
            return f'razorpay: {refusal}'
        if not secret:
            return 'razorpay: key secret is not set'
        return None

    def __bool__(self):
        return self.not_configured_reason() is None

    @property
    def available(self):
        return bool(self)

    def _call(self, method, path, **kw):
        key_id, secret = key_pair()
        return rzp.request(method, path, key_id, secret, **kw)

    # -- orders --------------------------------------------------------------------------

    def create_order(self, amount_minor, trip_id=None, currency: str = 'INR', customer_id=None,
                     customer_phone=None, customer_email=None, receipt=None, notes=None,
                     **_) -> Optional[Dict[str, Any]]:
        """Create a Razorpay order (auto capture is the account default).

        `receipt` is OUR reference (<= 40 chars); `notes` carry the trip / customer ids. Returns
        the keys the Cashfree adapter returns plus what Razorpay Checkout needs: `id`,
        `key_id`, `amount` (paise) and `currency`.
        """
        if not self or str(currency).upper() not in self.CURRENCIES or not _positive_minor(amount_minor):
            logger.error('razorpay create_order refused (configured=%s currency=%s amount=%r)',
                         bool(self), currency, amount_minor)
            return None
        ref = receipt or (f'trip_{trip_id}' if trip_id is not None
                          else f'user_{customer_id or "na"}_{int(time.time())}')
        payload = {
            'amount': amount_minor, 'currency': 'INR', 'receipt': str(ref)[:40],
            'notes': dict({k: str(v) for k, v in
                           (('trip_id', trip_id), ('customer_id', customer_id)) if v is not None},
                          **(notes or {})),
        }
        try:
            code, data = self._call('POST', '/orders', json=payload)
        except rzp.RazorpayTransportError as exc:
            logger.error('razorpay create_order transport failure: %s', exc)
            return None
        if code != 200 or not data.get('id'):
            logger.error('razorpay create_order failed: HTTP %s %s', code, rzp.error_text(data))
            return None
        return {
            'gateway': 'razorpay',
            'gateway_order_id': data['id'],
            'order_id': data['id'],
            'id': data['id'],
            'key_id': key_pair()[0],
            'amount': data.get('amount', amount_minor),
            'currency': data.get('currency', 'INR'),
            'order_amount': to_major_string(amount_minor, 'INR'),
            'receipt': data.get('receipt'),
            'status': data.get('status'),
        }

    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Order state in the Cashfree-compatible shape the shared handlers read.

        `order_status` is 'PAID' only when Razorpay says the order is `paid` (a captured payment
        covering the amount); `order_amount` is major units as a string.
        """
        if not self or not order_id:
            return None
        try:
            code, data = self._call('GET', f'/orders/{order_id}')
        except rzp.RazorpayTransportError as exc:
            logger.error('razorpay get_order_status transport failure: %s', exc)
            return None
        if code != 200 or not data.get('id'):
            logger.error('razorpay get_order_status failed: HTTP %s %s', code, rzp.error_text(data))
            return None
        paid = data.get('status') == 'paid'
        currency = data.get('currency') or 'INR'
        amount = data.get('amount')
        return {
            'gateway': 'razorpay',
            'order_id': order_id,
            'order_status': 'PAID' if paid else str(data.get('status') or '').upper(),
            'order_amount': to_major_string(amount, currency) if isinstance(amount, int) else None,
            'order_amount_minor': amount,
            'amount_paid_minor': data.get('amount_paid'),
            'order_currency': currency,
            'payment_status': 'SUCCESS' if paid else str(data.get('status') or '').upper(),
        }

    # -- signatures ----------------------------------------------------------------------

    def verify_payment_signature(self, order_id: str, payment_id: Optional[str] = None,
                                 signature: Optional[str] = None) -> bool:
        """Checkout callback check: HMAC-SHA256(order_id|payment_id, key secret), constant time.

        Without a payment id and signature this is False: Razorpay has no "trust the order id"
        path, and a signature is the proof the rider's checkout completed.
        """
        _, secret = key_pair()
        if not (secret and order_id and payment_id and signature):
            return False
        return rzp.signatures_match(rzp.hmac_hex(secret, f'{order_id}|{payment_id}'), signature)

    def verify_webhook_signature(self, body: bytes, signature: str,
                                 timestamp: Optional[str] = None) -> bool:
        """HMAC-SHA256 hex of the RAW body under the webhook secret. Fails closed."""
        secret = webhook_secret()
        if not secret:
            logger.error('razorpay webhook rejected: RAZORPAY_WEBHOOK_SECRET is not set')
            return False
        raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
        return rzp.signatures_match(rzp.hmac_hex(secret, bytes(raw)), signature)

    # -- capture -------------------------------------------------------------------------

    def get_payment(self, payment_id):
        try:
            code, data = self._call('GET', f'/payments/{payment_id}')
        except rzp.RazorpayTransportError:
            return None
        return data if code == 200 and data.get('id') else None

    def ensure_captured(self, payment_id, amount_minor=None, currency='INR'):
        """Capture a payment that is only `authorized`; a captured one is left alone.

        Returns the payment status after the call ('captured', 'authorized', ...) or None when
        it cannot be determined. Capture needs the exact authorized amount and the currency.
        """
        if not self or not payment_id:
            return None
        payment = self.get_payment(payment_id)
        if not payment:
            return None
        if payment.get('status') != 'authorized':
            return payment.get('status')
        amount = payment.get('amount') if _positive_minor(payment.get('amount')) else amount_minor
        try:
            code, data = self._call('POST', f'/payments/{payment_id}/capture',
                                    json={'amount': amount, 'currency': payment.get('currency') or currency})
        except rzp.RazorpayTransportError:
            return 'authorized'
        if code == 200:
            return data.get('status') or 'captured'
        logger.error('razorpay capture failed: HTTP %s %s', code, rzp.error_text(data))
        return 'authorized'

    def payment_id_for_order(self, order_id):
        """The captured payment id of an order, or None (used to refund by order id)."""
        try:
            code, data = self._call('GET', f'/orders/{order_id}/payments')
        except rzp.RazorpayTransportError:
            return None
        if code != 200:
            return None
        for item in data.get('items') or []:
            if item.get('status') in ('captured', 'refunded'):
                return item.get('id')
        return None

    # -- refunds -------------------------------------------------------------------------

    def create_refund(self, payment_id: str, amount_minor: Optional[int] = None,
                      currency: str = 'INR', refund_reference: Optional[str] = None
                      ) -> Optional[Dict[str, Any]]:
        """Full (amount omitted) or partial refund of a captured payment.

        `payment_id` is the Razorpay payment id (`pay_...`); an `order_...` id is resolved to
        its captured payment. Idempotent through `receipt`: a repeat of the same reference
        returns the refund it already created instead of a second one.
        """
        if not self or not payment_id:
            return None
        if amount_minor is not None and not _positive_minor(amount_minor):
            logger.error('razorpay create_refund: amount_minor must be a positive int, got %r',
                         amount_minor)
            return None
        if str(payment_id).startswith('order_'):
            resolved = self.payment_id_for_order(payment_id)
            if not resolved:
                logger.error('razorpay create_refund: no captured payment for the order')
                return None
            payment_id = resolved
        reference = (refund_reference
                     or (f'rf_{payment_id}' if amount_minor is None else f'rf_{payment_id}_{amount_minor}'))[:40]
        payload = {'speed': 'optimum', 'receipt': reference, 'notes': {'reference': reference}}
        if amount_minor is not None:
            payload['amount'] = amount_minor
        try:
            code, data = self._call('POST', f'/payments/{payment_id}/refund', json=payload)
            if code == 400 and 'duplicate receipt' in str(data).lower():
                data = self._existing_refund(payment_id, reference) or {}
                code = 200 if data else 400
        except rzp.RazorpayTransportError as exc:
            logger.error('razorpay create_refund transport failure: %s', exc)
            return None
        if code != 200 or not data.get('id') or data.get('status') == 'failed':
            logger.error('razorpay create_refund failed: HTTP %s %s', code, rzp.error_text(data))
            return None
        return {
            'gateway': 'razorpay',
            'refund_id': reference,
            'rzp_refund_id': data['id'],
            'status': str(data.get('status') or 'pending').upper(),
            'amount': data.get('amount'),
        }

    def _existing_refund(self, payment_id, reference):
        try:
            code, data = self._call('GET', f'/payments/{payment_id}/refunds')
        except rzp.RazorpayTransportError:
            return None
        for item in (data.get('items') or []) if code == 200 else []:
            if item.get('receipt') == reference:
                return item
        return None

    # -- payouts are not this adapter's job (RazorpayX: integrations/payouts/razorpayx.py) --

    def create_upi_payout(self, upi_id, amount_minor, purpose='payout', currency='INR',
                          reference_id=None, name=None, **_):
        logger.warning('create_upi_payout refused: razorpay payments adapter does not pay out')
        return None
