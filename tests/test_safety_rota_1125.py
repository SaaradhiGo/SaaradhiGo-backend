"""The hourly safety rota (owner ruling 25 Nov): a week of hour blocks per city, changed through the
Edit pattern with a second person; SOS paging calls the person on shift; a live city with nobody on
shift this hour raises one cover alert, cleared when someone is on shift again."""
from datetime import datetime, timezone as dt_tz

import pytest

from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.approvals.models import ApprovalRequest
from servers.command_centre.models import OperatorAlert
from servers.safety import rota
from servers.safety.models import OnCallShiftPlan
from test_finance_actions import approve_request, body_of, checker, error_code, post
from test_pricing_module import geo, operator  # noqa: F401

pytestmark = pytest.mark.django_db
API = '/api/v1/safety/admin/rota/IN-TG-HYD/'
# Wednesday 25 Nov 2026, 10:30 in Hyderabad (05:00 UTC).
WED_1030 = datetime(2026, 11, 25, 5, 0, tzinfo=dt_tz.utc)


@pytest.fixture
def city(geo):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    officers = []
    for i in (1, 2):
        u = User.objects.create_user(phone_number=f'+9198700000{i:02d}', username=f'officer{i}',
                                     email=f'officer{i}@saaradhigo.test', full_name=f'Officer {i}',
                                     role='admin', is_staff=True)
        AdminAssignment.objects.create(admin=u, role=AdminRole.objects.get(code='safety_officer'),
                                       scope=geo['IN-TG-HYD'])
        officers.append(u)
    GeoNode.objects.filter(code='IN-TG-HYD').update(status='active')
    maker, _ = operator('super_admin', geo)
    return maker, officers, geo['IN-TG-HYD']


def day_shift(a, b, start=9, end=17, day=2):
    return {'weekday': day, 'start': start, 'end': end, 'primary': a.pk, 'backup': b.pk}


class TestRota:
    def test_preview_then_approval_puts_it_in_force(self, city):
        maker, (a, b), node = city
        r = post(maker, f'{API}preview/', {'shifts': [day_shift(a, b)]}, expect=200)
        shown = body_of(r)
        assert shown['changes'] == [{'label': 'Wednesday', 'from': 'nobody', 'to': '09–17 Officer 1'}]
        assert shown['impact']['rows'][2] == ['Wednesday', '0 of 24', '8 of 24']
        r = post(maker, API, {'shifts': [day_shift(a, b)], 'reason': 'Weekday cover'}, expect=202)
        assert not OnCallShiftPlan.objects.exists()       # nothing until a second person approves
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        plan = OnCallShiftPlan.objects.get(city=node, status='active')
        assert plan.version == 1 and len(plan.shifts) == 1

    def test_only_safety_staff_overlaps_refused(self, city, geo):
        maker, (a, b), _ = city
        stranger, _ = operator('finance', geo)
        r = post(maker, f'{API}preview/', {'shifts': [{'weekday': 0, 'start': 0, 'end': 8, 'primary': 999999}]})
        assert r.status_code == 400 and error_code(r) == 'NOT_ELIGIBLE'
        r = post(maker, f'{API}preview/', {'shifts': [day_shift(a, b), day_shift(b, a, start=16, end=20)]})
        assert r.status_code == 400 and error_code(r) == 'OVERLAP'

    def test_paging_calls_the_person_on_shift_and_falls_back_after_hours(self, city):
        _, (a, b), node = city
        OnCallShiftPlan.objects.create(city=node, shifts=[day_shift(a, b)], status='active')
        assert rota.contact_for(node, 'primary', WED_1030) == ('Officer 1', a.phone_number)
        assert rota.contact_for(node, 'backup', WED_1030) == ('Officer 2', b.phone_number)
        night = WED_1030.replace(hour=16)                  # 21:30 in Hyderabad: no shift
        assert rota.contact_for(node, 'primary', night) == ('', '')

    def test_a_gap_hour_raises_one_cover_alert_and_clears(self, city):
        _, (a, b), node = city
        OnCallShiftPlan.objects.create(city=node, shifts=[day_shift(a, b)], status='active')
        night = WED_1030.replace(hour=16)
        assert rota.sweep_gaps(night) == {'raised': 1, 'cleared': 0}
        assert rota.sweep_gaps(night) == {'raised': 0, 'cleared': 0}
        assert OperatorAlert.objects.filter(kind='safety_rota_gap', acknowledged_at__isnull=True).count() == 1
        assert rota.sweep_gaps(WED_1030)['cleared'] == 1

    def test_the_request_carries_the_display_and_is_always_checked(self, city):
        maker, (a, b), _ = city
        r = post(maker, API, {'shifts': [day_shift(a, b)], 'reason': 'Weekday cover'}, expect=202)
        req = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        assert req.status == 'requested' and req.payload['display']['changes']
