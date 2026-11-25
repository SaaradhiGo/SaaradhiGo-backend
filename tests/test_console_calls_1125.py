"""Console calls (owner ruling 25 Nov): Support calls a trip's rider or driver through the same masked
call bridge as the apps (the fake bridge until the keys exist); audited; in the trip's call log."""
import pytest

from notifications_support import geo  # noqa: F401
from servers.admin_audit.models import AdminAuditLog
from servers.callbridge.models import CallSession
from test_operate_1125 import TRIPS, trip  # noqa: F401
from test_sos_support_zones_matrix import operator

pytestmark = pytest.mark.django_db


class TestConsoleCalls:
    def test_support_calls_the_rider_masked_and_it_is_logged(self, geo, trip):  # noqa: F811
        c, agent = operator('support_agent', geo)
        agent.phone_number = '+919800000777'
        agent.save(update_fields=['phone_number'])
        r = c.post(f'{TRIPS}/{trip.id}/call/', {'to': 'rider', 'reason': 'Lost item follow-up'}, format='json')
        assert r.status_code == 201, r.content
        call = r.json()['data']['call']
        assert call['test'] is True and call['number'] and trip.user_id.phone_number not in str(r.content)
        s = CallSession.objects.get(pk=call['session_id'])
        assert (s.from_role, s.to_role, s.staff_id) == ('staff', 'rider', agent.id)
        assert AdminAuditLog.objects.filter(action='staff_call_started', target_id=str(trip.id)).exists()
        detail = c.get(f'{TRIPS}/{trip.id}/').json()['data']
        assert detail['calls'][-1]['from_role'] == 'staff' and detail['calls'][-1]['to_role'] == 'rider'

    def test_refusals(self, geo, trip):  # noqa: F811
        c, agent = operator('support_agent', geo)
        assert c.post(f'{TRIPS}/{trip.id}/call/', {'to': 'nobody', 'reason': 'x' * 10}, format='json').status_code == 400
        agent.phone_number = ''
        agent.save(update_fields=['phone_number'])
        r = c.post(f'{TRIPS}/{trip.id}/call/', {'to': 'driver', 'reason': 'Pickup confusion'}, format='json')
        assert r.status_code == 400 and r.json()['error']['code'] == 'NO_STAFF_NUMBER'
        finance, _ = operator('finance', geo)
        assert finance.post(f'{TRIPS}/{trip.id}/call/', {'to': 'rider', 'reason': 'x' * 10},
                            format='json').status_code == 403
