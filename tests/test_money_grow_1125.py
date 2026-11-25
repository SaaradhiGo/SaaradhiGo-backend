"""25 Nov (owner), console capabilities, Money and Grow: rerun reconciliation for a day, preview who
qualifies for a promotion, and send a message template to yourself."""

from datetime import timedelta

import pytest
from django.utils import timezone

from notifications_support import geo, make_driver_user, make_rider_user, make_template  # noqa: F401
from servers.admin_audit.models import AdminAuditLog
from test_sos_support_zones_matrix import operator

pytestmark = pytest.mark.django_db


class TestReconciliationRerun:
    URL = '/api/v1/payments/admin/reconciliation/rerun/'

    def test_finance_reruns_a_finished_day_and_it_is_audited(self, geo):
        c, _ = operator('finance', geo, 'IN')
        day = (timezone.now() - timedelta(days=2)).date().isoformat()
        r = c.post(self.URL, {'country': 'IN', 'date': day, 'reason': 'Late webhook fixed'}, format='json')
        assert r.status_code == 200, r.content
        assert r.json()['data']['runs']
        assert AdminAuditLog.objects.filter(action='finance_reconciliation_rerun', target_id=f'IN:{day}').exists()

    def test_refusals(self, geo):
        c, _ = operator('finance', geo, 'IN')
        from zoneinfo import ZoneInfo

        # India's own date: the server refuses today in the country's time zone (it was UTC's here).
        today = timezone.now().astimezone(ZoneInfo('Asia/Kolkata')).date().isoformat()
        assert c.post(self.URL, {'country': 'IN', 'date': today, 'reason': 'Late webhook'}, format='json').status_code == 400
        assert c.post(self.URL, {'country': 'US', 'date': '2026-11-01', 'reason': 'Late webhook'},
                      format='json').status_code == 403
        s, _ = operator('support_agent', geo)
        assert s.post(self.URL, {'country': 'IN', 'date': '2026-11-01', 'reason': 'Late webhook'},
                      format='json').status_code == 403


class TestWhoQualifies:
    def _campaign(self, geo, kind, config):
        from servers.promotions.models import Campaign

        return Campaign.objects.create(kind=kind, name='Test', country='IN', geo_node=geo['IN-TG-HYD'],
                                       currency='INR', config=config)

    def test_new_riders_only_counts_riders_with_no_trip(self, geo):
        make_rider_user(geo)
        make_rider_user(geo)
        make_rider_user(geo, node='IN-AP') if 'IN-AP' in geo else None
        camp = self._campaign(geo, 'rider_promo', {'new_riders_only': True})
        c, _ = operator('marketing', geo)
        r = c.get(f'/api/v1/promotions/admin/campaigns/{camp.id}/audience/')
        assert r.status_code == 200, r.content
        data = r.json()['data']
        assert data['who'] == 'riders' and data['count'] == 2 and data['rules']

    def test_an_incentive_counts_approved_drivers_in_the_place(self, geo):
        make_driver_user(geo)
        make_driver_user(geo, approved=False)
        camp = self._campaign(geo, 'driver_incentive', {})
        c, _ = operator('marketing', geo)
        data = c.get(f'/api/v1/promotions/admin/campaigns/{camp.id}/audience/').json()['data']
        assert data == {'who': 'drivers', 'count': 1, 'place': geo['IN-TG-HYD'].name, 'rules': []}


class TestTestSend:
    def test_sends_to_me_only_and_is_audited(self, geo):
        from servers.notifications.models import NotificationMessage

        template = make_template(geo, key='promo.test', channel='in_app', status='draft')
        c, me = operator('marketing', geo, 'IN')
        r = c.post(f'/api/v1/notifications/admin/templates/{template.id}/test-send/', {}, format='json')
        assert r.status_code == 201, r.content
        msg = NotificationMessage.objects.get(pk=r.json()['data']['message_id'])
        assert msg.recipient_user_id == me.id and msg.source == 'test_send' and '<thing>' in msg.body
        assert AdminAuditLog.objects.filter(action='notification_template_test_sent').exists()
