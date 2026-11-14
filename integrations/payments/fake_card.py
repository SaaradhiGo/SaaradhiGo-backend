"""A fake card processor for QA (14 Nov, USA), like the fake payout provider: no money moves.

Used ONLY when `CARD_FAKE_ALLOWED` is on and never in production (`IS_PRODUCTION`), so the
Idaho journey (hold at booking, capture at trip end, a tip) can run on QA before Stripe test keys
are set. Every hold is authorised at once; capture and tips succeed. The web says plainly that
no card is charged. With Stripe keys configured, Stripe is used instead.
"""
import uuid

from django.conf import settings


def allowed():
    return bool(getattr(settings, 'CARD_FAKE_ALLOWED', False)) and not getattr(settings, 'IS_PRODUCTION', True)


class FakeCardGateway:
    def get_name(self):
        return 'fake_card'

    def __bool__(self):
        return allowed()

    def create_customer(self, user):
        return f'cus_fake_{user.id}'

    def create_hold(self, amount_minor, *, currency='USD', customer_id=None, reference=None, metadata=None):
        return {'id': f'pi_fake_{uuid.uuid4().hex[:16]}', 'client_secret': '', 'status': 'requires_capture',
                'amount': amount_minor}

    def get_intent(self, intent_id):
        return {'id': intent_id, 'status': 'requires_capture', 'payment_method': 'pm_fake_visa_4242'}

    def capture_hold(self, intent_id, amount_minor, reference=None):
        return {'id': intent_id, 'status': 'succeeded', 'latest_charge': f'ch_fake_{uuid.uuid4().hex[:12]}',
                'payment_method': 'pm_fake_visa_4242', 'amount_received': amount_minor}

    def release_hold(self, intent_id):
        return True

    def charge_saved(self, customer_id, payment_method_id, amount_minor, *, currency='USD', reference=None,
                     metadata=None):
        return {'id': f'pi_fake_tip_{uuid.uuid4().hex[:12]}', 'status': 'succeeded'}
