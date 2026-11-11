"""RazorpayX payouts (India): contacts, fund accounts (vpa, bank_account) and payouts.

Registry name `razorpay` for payouts (alias `razorpayx`). Plain HTTPS via `requests`.

Docs this follows (razorpay.com/docs, RazorpayX API):
  * Contact:      POST /v1/contacts {name (3-50 chars), type, reference_id (<=40), notes}; the same
                  name/email/contact/type/reference_id returns the existing contact. `type` is one of
                  vendor|customer|employee|self (no new types via the API); drivers are `vendor`.
  * Fund account: POST /v1/fund_accounts {contact_id, account_type: vpa|bank_account,
                  vpa: {address} | bank_account: {name, ifsc, account_number}}.
  * Payout:       POST /v1/payouts {account_number (the RazorpayX account to DEBIT, differs between
                  test and live), fund_account_id, amount (paise, min 100), currency INR, mode
                  (UPI for a vpa; IMPS/NEFT/RTGS for a bank, uppercase), purpose, queue_if_low_balance,
                  reference_id (<=40), narration (<=30, a-z A-Z 0-9 space), notes} with the header
                  `X-Payout-Idempotency`. IP allowlisting applies in LIVE mode only
                  (x/dashboard/allowlist-ip).
  * Statuses:     queued, pending, processing, processed, reversed, cancelled, failed, rejected.

OUTCOME RULES (same as the Cashfree path): a timeout / dropped connection / 5xx on the payout
create is `PayoutDispatchUnknown` (the payout may exist), never "failed". A 4xx is a definite
rejection (None). Contact / fund-account failures happen before any payout is requested, so they
are definite "no money moved" too.
"""

import logging
import re
from typing import Any, Dict, Optional

from django.conf import settings

from integrations import razorpay_http as rzp
from servers.payments.payment_gateways.base_gateway import (
    BasePaymentGateway, PayoutDispatchUnknown,
)

logger = logging.getLogger(__name__)

#: Razorpay payout status -> (our bucket). 'completed' pays out, 'failed' refunds the wallet,
#: 'reversed' takes the same refund path, 'inflight' is a no-op that records the status.
STATUS_BUCKET = {
    'queued': 'inflight', 'pending': 'inflight', 'processing': 'inflight',
    'processed': 'completed',
    'failed': 'failed', 'rejected': 'failed', 'cancelled': 'failed',
    'reversed': 'reversed',
}
#: Statuses on the create response that mean the payout definitely does not exist / will not pay.
DEFINITE_NO = ('failed', 'rejected', 'cancelled')


def key_pair():
    return (getattr(settings, 'RAZORPAYX_KEY_ID', '') or getattr(settings, 'RAZORPAY_KEY_ID', '') or '',
            getattr(settings, 'RAZORPAYX_KEY_SECRET', '') or getattr(settings, 'RAZORPAY_KEY_SECRET', '') or '')


def account_number():
    return getattr(settings, 'RAZORPAYX_ACCOUNT_NUMBER', '') or ''


def webhook_secret():
    return getattr(settings, 'RAZORPAYX_WEBHOOK_SECRET', '') or ''


def _clean_name(name, minimum=3, maximum=50):
    """A contact / account-holder name inside Razorpay's character rules."""
    text = re.sub(r"[^A-Za-z0-9 '\-_/().]", '', str(name or '')).strip()
    text = re.sub(r'[^A-Za-z0-9)]+$', '', text) if text.endswith(('-', '_', '/', '(', "'")) else text
    text = text[:maximum].strip()
    return text if len(text) >= minimum else 'SaaradhiGo Driver'


def _narration(text):
    return re.sub(r'[^A-Za-z0-9 ]', '', str(text or 'SaaradhiGo payout'))[:30].strip() or 'SaaradhiGo'


class RazorpayXGateway(BasePaymentGateway):
    """RazorpayX payout provider. Falsy (and sends nothing) while keys / account are missing."""

    CURRENCIES = ('INR',)
    PROVIDER = 'razorpay'

    def get_name(self) -> str:
        return 'razorpay'

    def supported_currencies(self):
        return self.CURRENCIES

    def not_configured_reason(self, kind='payouts'):
        key_id, secret = key_pair()
        refusal = rzp.key_refusal(key_id)
        if refusal:
            return f'razorpayx: {refusal}'
        if not secret:
            return 'razorpayx: key secret is not set'
        if not account_number():
            return 'razorpayx: RAZORPAYX_ACCOUNT_NUMBER is not set'
        return None

    def __bool__(self):
        return self.not_configured_reason() is None

    @property
    def available(self):
        return bool(self)

    def _call(self, method, path, **kw):
        key_id, secret = key_pair()
        return rzp.request(method, path, key_id, secret, **kw)

    # -- webhook (payout events) ----------------------------------------------------------

    def verify_webhook_signature(self, body: bytes, signature: str,
                                 timestamp: Optional[str] = None) -> bool:
        secret = webhook_secret()
        if not secret:
            logger.error('razorpayx webhook rejected: RAZORPAYX_WEBHOOK_SECRET is not set')
            return False
        raw = body if isinstance(body, (bytes, bytearray)) else str(body).encode('utf-8')
        return rzp.signatures_match(rzp.hmac_hex(secret, bytes(raw)), signature)

    # -- contacts and fund accounts -------------------------------------------------------

    def ensure_contact(self, name, reference_id, contact_id=None):
        """The RazorpayX contact for a driver: reuse `contact_id`, else create (Razorpay
        returns the existing one for identical details). None on any failure (no payout yet)."""
        if contact_id:
            return contact_id
        payload = {'name': _clean_name(name), 'type': 'vendor',
                   'reference_id': str(reference_id or '')[:40] or None}
        payload = {k: v for k, v in payload.items() if v}
        try:
            code, data = self._call('POST', '/contacts', json=payload)
        except rzp.RazorpayTransportError as exc:
            logger.error('razorpayx create contact transport failure: %s', exc)
            return None
        if code not in (200, 201) or not data.get('id'):
            logger.error('razorpayx create contact failed: HTTP %s %s', code, rzp.error_text(data))
            return None
        return data['id']

    def ensure_fund_account(self, contact_id, account_type, details, fund_account_id=None):
        """A fund account on a contact (`vpa` or `bank_account`), reusing `fund_account_id`."""
        if fund_account_id:
            return fund_account_id
        payload = {'contact_id': contact_id, 'account_type': account_type, account_type: details}
        try:
            code, data = self._call('POST', '/fund_accounts', json=payload)
        except rzp.RazorpayTransportError as exc:
            logger.error('razorpayx create fund account transport failure: %s', exc)
            return None
        if code not in (200, 201) or not data.get('id'):
            logger.error('razorpayx create fund account failed: HTTP %s %s', code, rzp.error_text(data))
            return None
        return data['id']

    # -- payouts --------------------------------------------------------------------------

    def _create_payout(self, fund_account_id, amount_minor, mode, purpose, reference_id, narration):
        """POST /payouts with X-Payout-Idempotency = our reference. Raises PayoutDispatchUnknown
        when the outcome is unknown; returns the payout dict, or None for a definite rejection."""
        reference = str(reference_id)[:40]
        payload = {
            'account_number': account_number(), 'fund_account_id': fund_account_id,
            'amount': amount_minor, 'currency': 'INR', 'mode': mode,
            'purpose': purpose if purpose in ('refund', 'cashback', 'payout', 'salary',
                                               'utility bill', 'vendor bill') else 'payout',
            'queue_if_low_balance': True, 'reference_id': reference,
            'narration': _narration(narration),
        }
        try:
            code, data = self._call('POST', '/payouts', json=payload,
                                    headers={'X-Payout-Idempotency': reference})
        except rzp.RazorpayTransportError as exc:
            logger.error('razorpayx payout outcome UNKNOWN for %s: %s', reference, exc)
            raise PayoutDispatchUnknown(
                f'payout {reference}: request issued, outcome unknown ({exc})') from exc
        if code >= 500:
            logger.error('razorpayx payout outcome UNKNOWN for %s: HTTP %s', reference, code)
            raise PayoutDispatchUnknown(f'payout {reference}: provider returned {code}')
        if code not in (200, 201) or not data.get('id'):
            logger.error('razorpayx payout rejected for %s: HTTP %s %s',
                         reference, code, rzp.error_text(data))
            return None
        if str(data.get('status')) in DEFINITE_NO:
            logger.error('razorpayx payout %s came back %s', reference, data.get('status'))
            return None
        return data

    def _result(self, data, reference, contact_id, fund_account_id):
        return {
            # Our stable reference, like the Cashfree adapter: it is what WithdrawalRequest
            # stores and what the webhook returns as the payout's reference_id.
            'payout_id': reference,
            'status': str(data.get('status') or 'processing'),
            'gateway_reference': data.get('id'),
            'contact_id': contact_id,
            'fund_account_id': fund_account_id,
            'provider': 'razorpay',
            'raw': {k: data.get(k) for k in ('id', 'status', 'mode', 'amount', 'reference_id')},
        }

    def create_upi_payout(self, upi_id, amount_minor, purpose='payout', currency='INR',
                          reference_id=None, name=None, contact_id=None, fund_account_id=None,
                          contact_reference=None, **_) -> Optional[Dict[str, Any]]:
        """UPI payout: contact -> vpa fund account -> payout (mode UPI)."""
        if not self or not upi_id or not reference_id:
            logger.error('razorpayx create_upi_payout refused (configured=%s)', bool(self))
            return None
        if (str(currency).upper() != 'INR' or not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool) or amount_minor < 100):
            logger.error('razorpayx create_upi_payout: need INR and at least 100 paise, got %r %r',
                         currency, amount_minor)
            return None
        contact = self.ensure_contact(name, contact_reference or reference_id, contact_id)
        if not contact:
            return None
        fund = self.ensure_fund_account(contact, 'vpa', {'address': upi_id}, fund_account_id)
        if not fund:
            return None
        data = self._create_payout(fund, amount_minor, 'UPI', purpose, reference_id,
                                   'SaaradhiGo payout')
        return self._result(data, str(reference_id)[:40], contact, fund) if data else None

    def create_bank_payout(self, account_number_value, ifsc, amount_minor, purpose='payout',
                           currency='INR', reference_id=None, name=None, mode='IMPS',
                           contact_id=None, fund_account_id=None, contact_reference=None,
                           **_) -> Optional[Dict[str, Any]]:
        """Bank payout: contact -> bank_account fund account -> payout (IMPS by default)."""
        if not self or not account_number_value or not ifsc or not reference_id:
            return None
        if (str(currency).upper() != 'INR' or not isinstance(amount_minor, int)
                or isinstance(amount_minor, bool) or amount_minor < 100
                or mode not in ('IMPS', 'NEFT', 'RTGS')):
            return None
        contact = self.ensure_contact(name, contact_reference or reference_id, contact_id)
        if not contact:
            return None
        fund = self.ensure_fund_account(
            contact, 'bank_account',
            {'name': _clean_name(name, 3, 120), 'ifsc': str(ifsc).upper(),
             'account_number': str(account_number_value)}, fund_account_id)
        if not fund:
            return None
        data = self._create_payout(fund, amount_minor, mode, purpose, reference_id,
                                   'SaaradhiGo payout')
        return self._result(data, str(reference_id)[:40], contact, fund) if data else None

    def get_payout_status(self, payout_ref):
        """Status of a payout by Razorpay id (`pout_...`) or by our reference. None if unknown."""
        if not self or not payout_ref:
            return None
        try:
            if str(payout_ref).startswith('pout_'):
                code, data = self._call('GET', f'/payouts/{payout_ref}')
                items = [data] if code == 200 and data.get('id') else []
            else:
                code, data = self._call('GET', '/payouts', params={
                    'account_number': account_number(), 'reference_id': str(payout_ref)[:40]})
                items = data.get('items') or [] if code == 200 else []
        except rzp.RazorpayTransportError:
            return None
        if not items:
            return None
        item = items[0]
        raw = str(item.get('status') or '')
        return {'gateway': 'razorpay', 'payout_id': item.get('id'), 'status': raw.upper(),
                'bucket': STATUS_BUCKET.get(raw), 'utr': item.get('utr'),
                'reference_id': item.get('reference_id'),
                'failure_reason': (item.get('status_details') or {}).get('description')}

    # -- payments methods this adapter does not provide ----------------------------------

    def create_order(self, amount_minor, trip_id=None, currency='INR', **_):
        return None

    def verify_payment_signature(self, order_id, *a, **k) -> bool:
        return False

    def create_refund(self, payment_id, amount_minor=None, currency='INR', **_):
        return None

    def get_order_status(self, order_id):
        return None
