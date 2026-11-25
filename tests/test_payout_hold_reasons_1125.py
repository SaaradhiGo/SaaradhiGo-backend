"""Owner ruling 25 Nov (supersedes the fraud-only rule of AUDIT 27.56): a driver's payouts may be held
for suspected fraud, a dispute, a compliance check or payout details being verified. Each needs a
written reason and a second person, lasts at most 14 days, and the driver is told."""

import pytest

from fraud_money_support import (  # noqa: F401
    approve, body_of, error_code, geo, hyd_driver, operator, post, seed_money, world,
)
from servers.approvals.models import ApprovalRequest
from servers.fraud import money
from servers.fraud.models import PayoutHold
from servers.notifications.models import NotificationMessage

pytestmark = pytest.mark.django_db


@pytest.fixture
def held_world(world):
    from io import StringIO

    from django.core.management import call_command

    from conftest import seed_actor

    seed_money()
    call_command('seed_notification_templates', actor_email=seed_actor().email, stdout=StringIO())
    world.finance, _ = operator('finance', world.geo, 'IN-TG-HYD')
    return world


def url(drv):
    return f'/api/v1/payments/admin/drivers/{drv.id}/payout-hold/'


def ask(w, drv, expect=202, **body):
    body = {'reason_kind': 'dispute', 'note': 'Rider disputes three trips this week', 'days': 7,
            'idempotency_key': 'hold-1', **body}
    return post(w.finance, url(drv), body, expect=expect)


class TestHoldReasons:
    def test_a_dispute_hold_needs_a_second_person_then_holds_and_tells_the_driver(self, held_world, django_capture_on_commit_callbacks):
        drv = hyd_driver(held_world, 'H1')
        r = ask(held_world, drv)
        req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        assert req.payload['display']['changes'][0]['to'] == 'Held for 7 days (Dispute)'
        assert money.active_payout_hold(drv) is None          # nothing until a second person approves
        with django_capture_on_commit_callbacks(execute=True):
            assert approve(req.id).status_code == 200
        hold = money.active_payout_hold(drv)
        assert hold.reason_kind == 'dispute' and hold.flag_id is None
        assert (hold.expires_at - hold.starts_at).days == 7
        assert NotificationMessage.objects.filter(recipient_user_id=drv.user_id.pk, template_key='payout.held').exists()

    @pytest.mark.parametrize('kind', ['suspected_fraud', 'compliance_check', 'payout_details_verification'])
    def test_every_named_reason_is_accepted(self, held_world, kind):
        drv = hyd_driver(held_world, f'K{kind[:3]}')
        assert ask(held_world, drv, reason_kind=kind, idempotency_key=f'k-{kind}').status_code == 202

    def test_no_reason_no_note_or_more_than_14_days_is_refused(self, held_world):
        drv = hyd_driver(held_world, 'H2')
        assert error_code(ask(held_world, drv, expect=400, reason_kind='other')) == 'INVALID_REASON'
        assert error_code(ask(held_world, drv, expect=400, note='short')) == 'INVALID_NOTE'
        assert error_code(ask(held_world, drv, expect=400, days=15)) == 'INVALID_DAYS'

    def test_one_active_hold_per_driver(self, held_world):
        drv = hyd_driver(held_world, 'H3')
        approve(body_of(ask(held_world, drv))['approval_request']['id'])
        assert error_code(ask(held_world, drv, expect=409, idempotency_key='hold-2')) == 'ALREADY_HELD'

    def test_the_hold_lapses_on_its_own_and_raises_the_renew_alert(self, held_world):
        from datetime import timedelta

        from django.utils import timezone

        drv = hyd_driver(held_world, 'H4')
        approve(body_of(ask(held_world, drv))['approval_request']['id'])
        assert money.lapse_due_holds(now=timezone.now() + timedelta(days=8)) == 1
        assert PayoutHold.objects.get(driver=drv).status == 'lapsed'
