"""A fake payout provider for QA (9 Nov), like the CallBridge fake: no money moves.

Used ONLY when `PAYOUT_FAKE_ALLOWED` is on (never in production) AND the driver's UPI ID is one of
RazorpayX's published test-mode addresses: `success@razorpay` is accepted and paid, and
`failure@razorpay` is refused. The payout is "processing" when created; a moment later
`payments.fake_payout_settle` delivers the "processed" event through the same handler a real
RazorpayX webhook goes through, so the withdrawal goes Processing -> Paid with the real ledger rows.
"""

import logging
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)

SUCCESS_VPA = 'success@razorpay'
FAILURE_VPA = 'failure@razorpay'
TEST_VPAS = (SUCCESS_VPA, FAILURE_VPA)


def allowed_for(upi_id):
    return bool(getattr(settings, 'PAYOUT_FAKE_ALLOWED', False)) and \
        str(upi_id or '').strip().lower() in TEST_VPAS


class FakePayoutGateway:
    PROVIDER = 'razorpay'   # webhook handling and ledger rows are RazorpayX's

    def get_name(self):
        return 'razorpay'

    def __bool__(self):
        return bool(getattr(settings, 'PAYOUT_FAKE_ALLOWED', False))

    def create_upi_payout(self, upi_id, amount_minor, purpose='payout', currency='INR',
                          reference_id=None, **_):
        if not self or str(upi_id).lower() not in TEST_VPAS or not reference_id:
            return None
        if str(upi_id).lower() == FAILURE_VPA:
            logger.info('fake payout refused (failure VPA) ref=%s', reference_id)
            return None
        gateway_ref = f'pout_fake_{uuid.uuid4().hex[:14]}'
        try:
            from servers.payments.tasks import fake_payout_settle

            fake_payout_settle.apply_async((str(reference_id)[:40], gateway_ref), countdown=5)
        except Exception:  # noqa: BLE001 - settle by hand: the withdrawal stays processing
            logger.warning('fake payout: could not schedule settle for %s', reference_id, exc_info=True)
        return {'payout_id': str(reference_id)[:40], 'status': 'processing', 'gateway_reference': gateway_ref,
                'contact_id': 'cont_fake', 'fund_account_id': 'fa_fake', 'provider': 'razorpay',
                'raw': {'id': gateway_ref, 'status': 'processing', 'mode': 'UPI', 'amount': amount_minor}}

    def get_payout_status(self, payout_ref):
        return None


def settle(reference_id, gateway_ref):
    """The 'processed' event, through the real payout webhook handler."""
    from servers.payments import views

    return views._handle_cashfree_payout_webhook({
        'event': 'TRANSFER_SUCCESS', 'transfer_id': reference_id, 'cf_transfer_id': gateway_ref,
        'status': 'processed', 'failureReason': ''}, FakePayoutGateway())
