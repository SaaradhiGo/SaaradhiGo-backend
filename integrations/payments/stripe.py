"""Stripe payments (USA): card payments through PaymentIntents (13 Nov; was a stub).

  * Order:   POST /v1/payment_intents {amount (cents), currency usd, payment_method_types[]=card,
             metadata[trip_id]}, Idempotency-Key = our receipt. The client confirms it with
             Stripe.js using `client_secret`; nothing card-shaped ever reaches this server.
  * Proof:   Stripe has no client-side signature. `verify_payment_signature` re-reads the intent
             and is True only when Stripe says `succeeded` (money moves on Stripe's word only).
  * Refunds: POST /v1/refunds {payment_intent, amount?}, Idempotency-Key = the refund reference.
  * Webhook: STRIPE_WEBHOOK_SECRET; see integrations/stripe_http.py and
             servers/payments/stripe_webhooks.py.
Settings: STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET (test keys on QA).
"""

import logging

from django.conf import settings

from base.money import to_major_string
from integrations import stripe_http as stripe
from servers.payments.payment_gateways.base_gateway import BasePaymentGateway

logger = logging.getLogger(__name__)


def _positive_minor(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class StripeGateway(BasePaymentGateway):
    CURRENCIES = ('USD',)

    def get_name(self):
        return 'stripe'

    def supported_currencies(self):
        return self.CURRENCIES

    def not_configured_reason(self, kind='payments'):
        refusal = stripe.key_refusal(stripe.secret_key())
        return f'stripe: {refusal}' if refusal else None

    def __bool__(self):
        return self.not_configured_reason() is None

    @property
    def available(self):
        return bool(self)

    def create_order(self, amount_minor, trip_id=None, currency='USD', customer_id=None,
                     receipt=None, notes=None, **_):
        if not self or str(currency).upper() not in self.CURRENCIES or not _positive_minor(amount_minor):
            logger.error('stripe create_order refused (configured=%s currency=%s)', bool(self), currency)
            return None
        ref = receipt or (f'trip_{trip_id}' if trip_id is not None else None)
        metadata = {k: str(v) for k, v in (('trip_id', trip_id), ('customer_id', customer_id),
                                           ('reference', ref)) if v is not None}
        metadata.update({k: str(v) for k, v in (notes or {}).items()})
        try:
            code, data = stripe.request('POST', '/payment_intents', {
                'amount': amount_minor, 'currency': 'usd', 'payment_method_types': ['card'],
                'metadata': metadata}, idempotency_key=f'pi_{ref}' if ref else None)
        except stripe.StripeTransportError as exc:
            logger.error('stripe create_order transport failure: %s', exc)
            return None
        if code != 200 or not data.get('id'):
            logger.error('stripe create_order failed: HTTP %s %s', code, stripe.error_text(data))
            return None
        return {
            'gateway': 'stripe', 'gateway_order_id': data['id'], 'order_id': data['id'],
            'id': data['id'], 'client_secret': data.get('client_secret'),
            'publishable_key': getattr(settings, 'STRIPE_PUBLISHABLE_KEY', '') or '',
            'amount': data.get('amount', amount_minor), 'currency': 'USD',
            'order_amount': to_major_string(amount_minor, 'USD'), 'receipt': ref,
            'status': data.get('status'),
        }

    def _intent(self, intent_id):
        try:
            code, data = stripe.request('GET', f'/payment_intents/{intent_id}')
        except stripe.StripeTransportError:
            return None
        return data if code == 200 and data.get('id') else None

    def get_order_status(self, order_id):
        if not self or not order_id:
            return None
        data = self._intent(order_id)
        if data is None:
            return None
        paid = data.get('status') == 'succeeded'
        amount = data.get('amount_received') if paid else data.get('amount')
        return {
            'gateway': 'stripe', 'order_id': order_id,
            'order_status': 'PAID' if paid else str(data.get('status') or '').upper(),
            'order_amount': to_major_string(amount, 'USD') if isinstance(amount, int) else None,
            'order_amount_minor': data.get('amount'), 'amount_paid_minor': data.get('amount_received'),
            'order_currency': 'USD',
            'payment_status': 'SUCCESS' if paid else str(data.get('status') or '').upper(),
            'payment_id': data.get('latest_charge') or data['id'],
        }

    def verify_payment_signature(self, order_id, payment_id=None, signature=None):
        """No client signature exists for Stripe: True only when Stripe says the intent succeeded."""
        if not self or not order_id:
            return False
        data = self._intent(order_id)
        return bool(data) and data.get('status') == 'succeeded'

    def verify_webhook_signature(self, body, signature, timestamp=None):
        secret = getattr(settings, 'STRIPE_WEBHOOK_SECRET', '') or ''
        if not secret:
            logger.error('stripe webhook rejected: STRIPE_WEBHOOK_SECRET is not set')
            return False
        return stripe.verify_signature(body, signature, secret)

    def create_refund(self, payment_id, amount_minor=None, currency='USD', refund_reference=None):
        if not self or not payment_id:
            return None
        if amount_minor is not None and not _positive_minor(amount_minor):
            return None
        reference = refund_reference or (f'rf_{payment_id}' if amount_minor is None
                                         else f'rf_{payment_id}_{amount_minor}')
        key = 'payment_intent' if str(payment_id).startswith('pi_') else 'charge'
        payload = {key: payment_id, 'metadata': {'reference': reference}}
        if amount_minor is not None:
            payload['amount'] = amount_minor
        try:
            code, data = stripe.request('POST', '/refunds', payload, idempotency_key=reference)
        except stripe.StripeTransportError as exc:
            logger.error('stripe create_refund transport failure: %s', exc)
            return None
        if code != 200 or not data.get('id') or data.get('status') == 'failed':
            logger.error('stripe create_refund failed: HTTP %s %s', code, stripe.error_text(data))
            return None
        return {'gateway': 'stripe', 'refund_id': reference, 'stripe_refund_id': data['id'],
                'status': str(data.get('status') or 'pending').upper(), 'amount': data.get('amount')}

    def create_upi_payout(self, upi_id, amount_minor, purpose='payout', currency='USD', **_):
        logger.warning('create_upi_payout refused: stripe payments adapter does not pay out')
        return None
