"""Alerts have an owner, an age in words and a next step; stale ones escalate or close (22 Nov).

Owner brief, 22 Nov: "Only cities whose launch gate is open count as live ... Cities that are not
live raise no alerts and don't colour the dashboard. Every alert has an owner, an age in human
units, and a next step. Stale alerts escalate on a rule, or auto-resolve when their cause is gone."
And: system checks go to an Engineering health page plus Sentry, never ops lists.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from servers.admin_access.models import GeoNode
from servers.command_centre import alert_policy
from servers.command_centre.models import CRITICAL, DATA_INTEGRITY, WARNING, OperatorAlert
from servers.command_centre.services import raise_alert
from test_sos_support_zones_matrix import geo, operator  # noqa: F401 - fixtures

pytestmark = pytest.mark.django_db

ALERTS = '/api/v1/command-centre/admin/alerts/'
NEEDS = '/api/v1/command-centre/admin/needs-action/'
ENGINEERING = '/api/v1/command-centre/admin/engineering/'


def _age(alert, **delta):
    OperatorAlert.objects.filter(pk=alert.pk).update(created_at=timezone.now() - timedelta(**delta))
    alert.refresh_from_db()
    return alert


def _withdrawal_alert(city):
    return raise_alert('threshold_withdrawal_pending', title=f'{city.name}: Oldest pending withdrawal 48.1 h',
                       severity=WARNING, source_type='alert_threshold', source_id=f'{city.code}:withdrawal_pending',
                       city=city, payload={'metric': 'withdrawal_pending'})


class TestHumanAge:
    @pytest.mark.parametrize('seconds,text', [
        (30, '1 min'), (12 * 60, '12 min'), (3 * 3600 + 5, '3 h'), (47 * 3600, '47 h'),
        (2 * 86400, '2 days'), (21 * 86400 + 3600, '21 days'),
    ])
    def test_words(self, seconds, text):
        assert alert_policy.human_age(seconds) == text


class TestNotLiveCities:
    def test_a_city_that_is_not_live_raises_no_alert_of_any_kind(self, geo):
        GeoNode.objects.filter(code='US-TX-AUS').update(status='draft')
        geo['US-TX-AUS'].refresh_from_db()
        assert raise_alert('safety_sos_escalated', title='SOS #6 not acknowledged', severity=CRITICAL,
                           source_type='safety_incident', source_id='6', city=geo['US-TX-AUS']) is None
        assert raise_alert('fraud.flag_high', title='flag', city=geo['US-TX-AUS']) is None

    def test_alerts_left_open_in_a_city_that_stopped_being_live_are_closed(self, geo):
        city = geo['IN-TG-HYD']
        alert = _withdrawal_alert(city)
        GeoNode.objects.filter(pk=city.pk).update(status='draft')

        counts = alert_policy.sweep()

        alert.refresh_from_db()
        assert counts['closed'] == 1
        assert alert.acknowledged_at is not None and 'not live' in alert.acknowledged_note


class TestEscalation:
    def test_a_warning_nobody_took_for_a_day_becomes_critical(self, geo):
        alert = _age(_withdrawal_alert(geo['IN-TG-HYD']), hours=25)

        alert_policy.sweep()

        alert.refresh_from_db()
        assert alert.severity == CRITICAL
        assert 'after 24 h' in alert.payload['escalated']
        assert not alert.payload.get('escalated_to')

    def test_after_three_days_it_is_escalated_to_the_super_admin(self, geo):
        alert = _age(_withdrawal_alert(geo['IN-TG-HYD']), days=5)

        alert_policy.sweep()

        alert.refresh_from_db()
        assert alert.payload['escalated_to'] == 'Super Admin'

    def test_a_fresh_alert_is_left_alone(self, geo):
        alert = _withdrawal_alert(geo['IN-TG-HYD'])
        alert_policy.sweep()
        alert.refresh_from_db()
        assert alert.severity == WARNING and 'escalated' not in alert.payload


class TestWhatTheConsoleShows:
    def test_every_alert_has_an_owner_an_age_and_a_next_step(self, geo):
        _age(_withdrawal_alert(geo['IN-TG-HYD']), days=21)
        sa, _ = operator('super_admin', geo)

        row = sa.get(f'{ALERTS}?open=1').json()['data']['results'][0]

        assert row['owner'] == 'Finance'
        assert row['age_text'] == '21 days'
        assert row['next_step']['text'] == 'Approve or reject the oldest waiting withdrawal'
        assert row['next_step']['link'] == '/withdrawals?scope=IN-TG-HYD'

    def test_needs_action_rows_carry_the_owner_and_the_next_step(self, geo):
        _withdrawal_alert(geo['IN-TG-HYD'])
        sa, _ = operator('super_admin', geo)

        items = [i for i in sa.get(NEEDS).json()['data']['items'] if i['kind'] == 'alert']

        assert items and items[0]['owner']['label'] == 'Finance'
        assert 'withdrawal' in items[0]['detail']
        assert items[0]['link'].startswith('/withdrawals')


class TestEngineeringIsNotOps:
    def _integrity(self):
        return raise_alert(DATA_INTEGRITY, title='Data integrity: 2 to check', source_type='integrity_scan',
                           source_id='integrity_scan')

    def test_system_checks_never_reach_ops_lists_even_for_the_super_admin(self, geo):
        self._integrity()
        sa, _ = operator('super_admin', geo)

        assert DATA_INTEGRITY not in {r['kind'] for r in sa.get(f'{ALERTS}?open=1').json()['data']['results']}
        assert not [i for i in sa.get(NEEDS).json()['data']['items'] if 'integrity' in i['title'].lower()]

    def test_they_are_on_the_engineering_page_with_an_owner(self, geo):
        self._integrity()
        sa, _ = operator('super_admin', geo)

        data = sa.get(ENGINEERING).json()['data']

        assert [a['kind'] for a in data['alerts']] == [DATA_INTEGRITY]
        assert data['alerts'][0]['owner'] == 'Engineering'
        assert data['audit_chain']['verifies'] in (True, False)

    def test_city_ops_cannot_open_the_engineering_page(self, geo):
        ops, _ = operator('city_ops_manager', geo)
        assert ops.get(ENGINEERING).status_code == 403

    def test_the_integrity_scan_keeps_one_alert_and_closes_it_when_clean(self):
        from servers.platform_ops.integrity import _alert

        dirty = {'driver_without_wallet': {'found': 1, 'fixed': 0}, 'total_found': 1, 'total_fixed': 0}
        first = _alert(dirty)
        second = _alert(dirty)
        assert first.pk == second.pk

        _alert({'total_found': 0, 'total_fixed': 0})

        first.refresh_from_db()
        assert first.acknowledged_at is not None


class TestDriverWallet:
    def test_a_new_driver_has_a_wallet_in_their_market_currency(self, geo):
        from django.contrib.auth import get_user_model

        from servers.driver.models import Driver
        from servers.rider.models import Wallet

        user = get_user_model().objects.create_user(phone_number='+12085550142', username='+12085550142',
                                                    role='driver')
        Driver.objects.get_or_create(user_id=user)
        wallet = Wallet.objects.get(user_id=user, scope=Wallet.SCOPE_DRIVER)
        assert wallet.currency == 'USD'

    def test_an_empty_wallet_follows_the_home_city(self, geo):
        from django.contrib.auth import get_user_model

        from servers.driver.models import Driver
        from servers.rider.models import Wallet

        user = get_user_model().objects.create_user(phone_number='+919800000142', username='+919800000142',
                                                    role='driver')
        driver, _ = Driver.objects.get_or_create(user_id=user)
        assert Wallet.objects.get(user_id=user, scope=Wallet.SCOPE_DRIVER).currency == 'INR'

        driver.home_city = geo['US-TX-AUS']
        driver.save()

        assert Wallet.objects.get(user_id=user, scope=Wallet.SCOPE_DRIVER).currency == 'USD'
