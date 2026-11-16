"""Stripe Connect payouts (USA): transfers to a driver's Express account (13 Nov; was a stub).

The driver onboards an Express account (integrations/payouts/driver_onboarding.py, stored on
`Driver.stripe_account_id`). A payout is a Transfer from the platform balance to that account:

    POST /v1/transfers {amount (cents), currency usd, destination acct_..., metadata[reference_id],
                        transfer_group}, Idempotency-Key = our withdrawal reference

Stripe then pays the connected account out to the driver's bank on the account's schedule.
Webhooks (Connect endpoint, STRIPE_CONNECT_WEBHOOK_SECRET): transfer.created (sent),
transfer.reversed (back in the driver's balance). Test keys only outside production.
"""

import logging

from django.conf import settings

from integrations import stripe_http as stripe
from servers.payments.payment_gateways.base_gateway import BasePaymentGateway, PayoutDispatchUnknown

logger = logging.getLogger(__name__)


class StripeConnectGateway(BasePaymentGateway):
    CURRENCIES = ('USD',)

    def get_name(self):
        return 'stripe_connect'

    def supported_currencies(self):
        return self.CURRENCIES

    def not_configured_reason(self, kind='payouts'):
        refusal = stripe.key_refusal(stripe.secret_key())
        return f'stripe_connect: {refusal}' if refusal else None

    def __bool__(self):
        return self.not_configured_reason() is None

    @property
    def available(self):
        return bool(self)

    def verify_webhook_signature(self, body, signature, timestamp=None):
        secret = getattr(settings, 'STRIPE_CONNECT_WEBHOOK_SECRET', '') or ''
        if not secret:
            logger.error('stripe connect webhook rejected: STRIPE_CONNECT_WEBHOOK_SECRET is not set')
            return False
        return stripe.verify_signature(body, signature, secret)

    def create_connect_transfer(self, account_id, amount_minor, currency='USD', reference_id=None,
                                **_):
        """Transfer to the driver's connected account. None on a definite refusal; raises
        PayoutDispatchUnknown when the answer was lost (the caller must not refund)."""
        if not self or not account_id or not reference_id:
            logger.error('stripe connect transfer refused (configured=%s)', bool(self))
            return None
        if (str(currency).upper() != 'USD' or not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool) or amount_minor < 1):
            return None
        reference = str(reference_id)[:100]
        try:
            code, data = stripe.request('POST', '/transfers', {
                'amount': amount_minor, 'currency': 'usd', 'destination': account_id,
                'transfer_group': reference, 'metadata': {'reference_id': reference}},
                idempotency_key=reference)
        except stripe.StripeTransportError as exc:
            raise PayoutDispatchUnknown(f'stripe transfer outcome unknown: {exc}') from None
        if code >= 500:
            raise PayoutDispatchUnknown(f'stripe answered HTTP {code}')
        if code != 200 or not data.get('id'):
            logger.error('stripe transfer failed: HTTP %s %s', code, stripe.error_text(data))
            return None
        return {'payout_id': reference, 'status': 'reversed' if data.get('reversed') else 'processed',
                'gateway_reference': data['id'], 'provider': 'stripe_connect',
                'raw': {k: data.get(k) for k in ('id', 'amount', 'destination', 'reversed')}}

    def get_payout_status(self, payout_ref):
        if not self or not str(payout_ref or '').startswith('tr_'):
            return None
        try:
            code, data = stripe.request('GET', f'/transfers/{payout_ref}')
        except stripe.StripeTransportError:
            return None
        if code != 200 or not data.get('id'):
            return None
        reversed_ = bool(data.get('reversed'))
        return {'gateway': 'stripe_connect', 'payout_id': data['id'],
                'status': 'REVERSED' if reversed_ else 'PROCESSED',
                'bucket': 'reversed' if reversed_ else 'success',
                'reference_id': (data.get('metadata') or {}).get('reference_id')}

    # -- payment methods this adapter does not provide ------------------------------------

    def create_order(self, amount_minor, trip_id=None, currency='USD', **_):
        return None

    def verify_payment_signature(self, order_id, *a, **k):
        return False

    def create_refund(self, payment_id, amount_minor=None, currency='USD', **_):
        return None

    def get_order_status(self, order_id):
        return None

    def create_upi_payout(self, upi_id, amount_minor, purpose='payout', currency='USD', **_):
        logger.warning('create_upi_payout refused: US payouts are Connect transfers')
        return None
