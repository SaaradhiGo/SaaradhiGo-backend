"""25 Nov (owner): counts agree with their noun at 0, 1 and many; the all-class fee reads as words."""

import pytest

from base.words import agree, plural
from servers.approvals.describe import describe
from test_command_centre import world  # noqa: F401 - fixture
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures


@pytest.mark.parametrize('n,one,many,want', [
    (0, 'day', None, '0 days'), (1, 'day', None, '1 day'), (2, 'day', None, '2 days'),
    (1, 'entry', 'entries', '1 entry'), (3, 'entry', 'entries', '3 entries'), (1500, 'trip', None, '1,500 trips'),
    ('1', 'hour', None, '1 hour'),
])
def test_plural(n, one, many, want):
    assert plural(n, one, many) == want


@pytest.mark.parametrize('text,want', [
    ('Your licence expires in 1 days, on 3 Dec.', 'Your licence expires in 1 day, on 3 Dec.'),
    ('1 days left: upload it.', '1 day left: upload it.'),
    ('Expires in 7 days.', 'Expires in 7 days.'),
    ('Expires in 0 days.', 'Expires in 0 days.'),
    ('In 21 days and 11 trips.', 'In 21 days and 11 trips.'),
    ('₹1.1 trips', '₹1.1 trips'),
])
def test_agree_fixes_only_a_count_of_one(text, want):
    assert agree(text) == want


@pytest.mark.django_db
class TestFeeWords:
    def _words(self, amounts, plan_type='per_trip_fee'):
        from servers.pricing.fee_plan_models import DriverFeePlan

        plan = DriverFeePlan.objects.create(country='US', scope_code='US', version=98, currency='USD',
                                            plan_type=plan_type, amounts=amounts)
        row = type('Row', (), {'payload': {'kind': 'driver_fee_plan', 'plan_id': plan.id}})()
        return describe(row, 'Change')

    def test_one_amount_for_every_class_reads_for_any_class(self):
        words = self._words({'*': {'per_trip_minor': 0}})
        assert 'Every class' not in words
        assert words.endswith('for any class') or 'for any class;' in words

    def test_a_class_and_the_rest_reads_every_other_class(self):
        words = self._words({'sedan': {'per_trip_minor': 100}, '*': {'per_trip_minor': 0}})
        assert words.index('Sedan') < words.index('for every other class')


@pytest.mark.django_db
class TestCityChips:
    def test_labels_follow_the_count(self, geo, world):
        from servers.admin_access.models import GeoNode

        GeoNode.objects.filter(code='IN-TG-HYD').update(status='active')
        sa, _ = operator('super_admin', geo)
        data = sa.get('/api/v1/command-centre/admin/today/').json()['data']
        city = next(c for c in data['cities'] if c['code'] == 'IN-TG-HYD')
        words = {'approvals': ('approval waiting for you', 'approvals waiting for you'),
                 'stuck': ('stuck trip', 'stuck trips'),
                 'kyc': ('driver waiting for KYC', 'drivers waiting for KYC')}
        for a in city['actions']:
            if a['key'] in words:
                one, many = words[a['key']]
                assert a['label'] == (one if a['count'] == 1 else many), a
