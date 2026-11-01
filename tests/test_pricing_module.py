"""Module 8: Pricing and fares. The layered, versioned commission model, fare cards
through maker-checker, and the module-7 carry-overs.

WHAT THESE PROVE (owner rulings, AUDIT 27.36 / 27.43 / 27.44)
-------------------------------------------------------------
  * COMMISSION IS ONE SOURCE. A country default, a city override and a vehicle-class
    override; the most specific active rule wins; with none in force the settlement
    RAISES (no silent default). Nothing reads the old rate-card column, the
    PlatformSettings key or the environment setting (a source-tree guard).
  * NEVER RETROACTIVE. A trip snapshots percent, rule id and rule version at booking;
    a rule changed afterwards does not reach it. A trip with no snapshot resolves the
    rule effective at its requested_at.
  * NOTICE. A rule that raises the rate cannot bite sooner than notice_days; a
    decrease may bite at once; a past date is moved to the moment of approval.
  * CAP. Refused above max_percent when one is set; allowed (and said so) when not.
  * KIND. Only percentage; flat_fee and subscription are "not supported yet".
  * MAKER-CHECKER. Rules, the country config and every fare-card change need a
    DIFFERENT admin; the maker alone cannot activate.
  * FEE RULE. The commission a driver is shown is the one the settlement applied.
  * READINESS. A city cannot go live without a commission rule and a fare card for
    every vehicle class.
  * RULING COMMAND. apply_commission_ruling is audited and idempotent.
  * SCOPE. A City Ops Manager in Hyderabad gets 404 on an Austin rule.
  * CARRY-OVERS: the weekly run skips drivers with no payout handle and tells
    finance; a USD approval is held awaiting_rail with no gateway call; a zone needs
    a country.

Fixture passwords are not used: tokens are minted directly.
"""

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from conftest import seed_actor
from finance_world import (
    client_for, make_driver, make_rider, make_trip, operator, settle, zone,
)
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import EXECUTED, REJECTED, REQUESTED, ApprovalRequest
from servers.driver.models import VehicleType, WithdrawalRequest
from servers.payments.models import PayoutRun, TripSettlement
from servers.pricing import commission as resolver
from servers.pricing.commission_models import (
    ACTIVE, ARCHIVED, DRAFT, PENDING_APPROVAL, CommissionCountryConfig, CommissionRule, DriverNotice,
)
from servers.pricing.models import RateCard, ServiceZone
from test_finance_actions import (
    approve_request, body_of, checker, error_code, get, post, swept,
)

pytestmark = [pytest.mark.django_db, pytest.mark.no_default_commission]

BASE = '/api/v1/pricing/admin'
APPROVALS = '/api/v1/approvals/admin/requests'
LONG_AGO = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)
REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------

@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
    return {n.code: n for n in GeoNode.objects.all()}


@pytest.fixture
def world(geo):
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    auto, _ = VehicleType.objects.get_or_create(type='auto')
    hyd = zone('IN-TG-HYD', geo['IN-TG-HYD'], country='IN')
    # The migrations seed Hyderabad with four rate cards; these tests build their own.
    RateCard.objects.filter(zone=hyd).delete()
    aus = zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
               timezone_name='America/Chicago')
    return SimpleNamespace(geo=geo, sedan=sedan, auto=auto, hyd=hyd, aus=aus,
                           super=operator('super_admin', geo),
                           ops=operator('city_ops_manager', geo, 'IN-TG-HYD'))


def make_rule(country='IN', percent='15.00', *, node=None, vt=None, status=ACTIVE, version=1,
              start=LONG_AGO, end=None, reason='test'):
    return CommissionRule.objects.create(
        country=country, geo_node=node, vehicle_type=vt, percent=Decimal(percent), status=status,
        version=version, effective_from=start, effective_to=end, reason=reason)


def draft(client, **body):
    body.setdefault('country', 'IN')
    body.setdefault('percent', '14')
    body.setdefault('reason', 'Quarterly review of the rate')
    return post(client, f'{BASE}/commission/rules/', body)


def submit(client, rule_id, expect=202):
    return post(client, f'{BASE}/commission/rules/{rule_id}/submit/',
                {'reason': 'Please approve'}, expect=expect)


def activate(world, rule_id):
    """Submit as the super admin and have a second super admin approve."""
    maker, _ = world.super
    r = submit(maker, rule_id)
    request_id = body_of(r)['approval_request']['id']
    chk, _ = checker()
    assert approve_request(chk, request_id).status_code == 200
    return request_id


# ===========================================================================
# THE LAYERS: most specific wins
# ===========================================================================

class TestMostSpecificWins:
    def test_country_then_city_then_vehicle_then_both(self, world):
        hyd = world.geo['IN-TG-HYD']
        make_rule('IN', '15.00', node=world.geo['IN'])
        make_rule('IN', '13.00', node=hyd)
        make_rule('IN', '11.00', vt=world.sedan)
        make_rule('IN', '9.00', node=hyd, vt=world.sedan)
        r = resolver.resolve
        assert r('IN', None, None).percent == Decimal('15.00')
        assert r('IN', hyd.id, world.auto.id).percent == Decimal('13.00')        # city beats country
        assert r('IN', None, world.sedan.id).percent == Decimal('11.00')          # class beats country
        assert r('IN', hyd.id, world.sedan.id).percent == Decimal('9.00')         # both beats all
        other = world.geo['US-TX-AUS']
        assert r('IN', other.id, world.sedan.id).percent == Decimal('11.00')      # the city rule is not theirs

    def test_a_rule_not_yet_in_force_or_already_closed_is_ignored(self, world):
        now = timezone.now()
        make_rule('IN', '15.00')
        make_rule('IN', '20.00', node=world.geo['IN-TG-HYD'], start=now + timedelta(days=3))
        make_rule('IN', '5.00', vt=world.sedan, end=now - timedelta(days=1))
        hyd = world.geo['IN-TG-HYD']
        assert resolver.resolve('IN', hyd.id, world.sedan.id, now).percent == Decimal('15.00')
        assert resolver.resolve('IN', hyd.id, world.sedan.id, now + timedelta(days=4)).percent == Decimal('20.00')

    def test_a_draft_or_pending_rule_never_applies(self, world):
        make_rule('IN', '15.00')
        make_rule('IN', '1.00', node=world.geo['IN-TG-HYD'], status=DRAFT)
        make_rule('IN', '2.00', node=world.geo['IN-TG-HYD'], status=PENDING_APPROVAL, version=2)
        assert resolver.resolve('IN', world.geo['IN-TG-HYD'].id, None).percent == Decimal('15.00')

    def test_the_matrix_endpoint_shows_the_effective_rate_and_its_source(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        make_rule('IN', '12.00', node=world.geo['IN-TG-HYD'], vt=world.sedan)
        maker, _ = world.super
        data = get(maker, f'{BASE}/commission/matrix/?country=IN')
        cells = {(r['city'], r['vehicle_type_name']): r for r in data['rows']}
        sedan = cells[('IN-TG-HYD', 'sedan')]
        auto = cells[('IN-TG-HYD', 'auto')]
        assert (sedan['percent'], sedan['source']) == ('12.00', 'city + vehicle-class override')
        assert (auto['percent'], auto['source']) == ('15.00', 'country default')
        assert sedan['version'] == 1 and sedan['effective_from']
        assert data['country_defaults'][0]['rule']['percent'] == '15.00'
        assert [k['supported'] for k in data['kinds']] == [True, False, False]

    def test_a_cell_with_no_rule_says_so(self, world):
        maker, _ = world.super
        data = get(maker, f'{BASE}/commission/matrix/?country=IN')
        assert all(r['percent'] is None and r['source'] is None for r in data['rows'])


# ===========================================================================
# NEVER RETROACTIVE: the snapshot
# ===========================================================================

class TestSnapshotAtBooking:
    def test_booking_writes_percent_rule_id_and_version(self, world):
        rule = make_rule('IN', '15.00', node=world.geo['IN'])
        snap = resolver.snapshot_for_booking(world.hyd, world.sedan.id)
        assert snap == {'commission_percent_snapshot': Decimal('15.00'),
                        'commission_rule_id': rule.id, 'commission_rule_version': 1}

    def test_no_rule_means_no_snapshot_and_a_logged_error_not_a_blocked_booking(self, world):
        assert resolver.snapshot_for_booking(world.hyd, world.sedan.id) == {}

    def test_the_consumer_passes_the_snapshot_to_the_trip(self):
        source = (REPO / 'servers' / 'consumers.py').read_text(encoding='utf-8')
        assert 'snapshot_for_booking(' in source and '**commission_snapshot' in source

    def test_a_rule_changed_after_booking_does_not_reach_the_settlement(self, world):
        old = make_rule('IN', '15.00', node=world.geo['IN'])
        driver = make_driver(world.geo, 'IN-TG-HYD', 'S1', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('S1', '91'), driver, fare='1000.00')
        Trip_snap = resolver.snapshot_for_booking(world.hyd, world.sedan.id)
        for k, v in Trip_snap.items():
            setattr(trip, k, v)
        trip.save(update_fields=list(Trip_snap))
        # An operator changes the rate AFTER the trip was booked.
        CommissionRule.objects.filter(pk=old.pk).update(effective_to=timezone.now(), status=ARCHIVED)
        make_rule('IN', '30.00', node=world.geo['IN'], version=2, start=timezone.now() - timedelta(seconds=1))
        settle(trip)
        s = TripSettlement.objects.get(trip=trip)
        assert s.commission_percent == Decimal('15.00')
        assert s.commission_amount_minor == 15_000 and s.driver_net_minor == 85_000

    def test_a_trip_booked_before_the_module_resolves_the_rule_at_its_requested_at(self, world):
        from servers.ride.models import Trip

        long_ago = timezone.now() - timedelta(days=10)
        make_rule('IN', '15.00', node=world.geo['IN'], end=timezone.now() - timedelta(days=2))
        make_rule('IN', '25.00', node=world.geo['IN'], version=2, start=timezone.now() - timedelta(days=2))
        driver = make_driver(world.geo, 'IN-TG-HYD', 'S2', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('S2', '91'), driver, fare='1000.00')
        Trip.objects.filter(pk=trip.pk).update(requested_at=long_ago)
        trip.refresh_from_db()
        assert trip.commission_percent_snapshot is None
        decision = resolver.commission_for_trip(trip)
        assert decision.percent == Decimal('15.00') and decision.source == 'rule_at_requested_at'
        settle(trip)
        assert TripSettlement.objects.get(trip=trip).commission_percent == Decimal('15.00')

    def test_with_no_rule_the_settlement_raises_and_writes_nothing(self, world):
        driver = make_driver(world.geo, 'IN-TG-HYD', 'S3', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('S3', '91'), driver, fare='1000.00')
        with pytest.raises(resolver.CommissionNotConfigured) as caught:
            settle(trip)
        assert caught.value.code == 'COMMISSION_NOT_CONFIGURED' and 'IN' in str(caught.value)
        assert not TripSettlement.objects.filter(trip=trip).exists()


# ===========================================================================
# NOTICE for increases, immediate effect for decreases
# ===========================================================================

class TestNotice:
    def test_an_increase_cannot_start_sooner_than_the_notice_period(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        soon = (timezone.now() + timedelta(days=2)).isoformat()
        r = draft(maker, percent='18', effective_from=soon)
        assert r.status_code == 400 and error_code(r) == 'NOTICE_TOO_SHORT'

    def test_an_increase_with_no_date_defaults_to_the_earliest_allowed(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        data = body_of(draft(maker, percent='18', expect=201))
        eff = datetime.fromisoformat(data['effective_from'])
        assert eff >= timezone.now() + timedelta(days=6, hours=23)

    def test_an_approved_increase_bites_after_the_notice_and_queues_a_driver_notice(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='18', expect=201))
        before = timezone.now()
        activate(world, rule['id'])
        row = CommissionRule.objects.get(pk=rule['id'])
        assert row.status == ACTIVE and row.effective_from >= before + timedelta(days=6, hours=23)
        # not yet in force: trips today still pay 15
        assert resolver.resolve('IN', None, None).percent == Decimal('15.00')
        assert resolver.resolve('IN', None, None, timezone.now() + timedelta(days=8)).percent == Decimal('18.00')
        notice = DriverNotice.objects.get(rule=row)
        assert (notice.old_percent, notice.new_percent, notice.status) == (
            Decimal('15.00'), Decimal('18.00'), 'queued')
        # the predecessor stays in force until the new one starts
        old = CommissionRule.objects.get(version=1)
        assert old.effective_to == row.effective_from

    def test_a_decrease_takes_effect_immediately_with_no_driver_notice(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        activate(world, rule['id'])
        assert resolver.resolve('IN', None, None).percent == Decimal('12.00')
        assert not DriverNotice.objects.exists()

    def test_a_date_in_the_past_is_moved_to_the_moment_of_approval(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'], start=LONG_AGO)
        maker, _ = world.super
        past = (timezone.now() - timedelta(days=30)).isoformat()
        rule = body_of(draft(maker, percent='12', effective_from=past, expect=201))
        activate(world, rule['id'])
        row = CommissionRule.objects.get(pk=rule['id'])
        assert row.effective_from > timezone.now() - timedelta(minutes=5) and row.effective_from_adjusted

    def test_the_notice_period_is_per_country_and_a_zero_means_none(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        CommissionCountryConfig.objects.create(country='IN', notice_days=0)
        maker, _ = world.super
        rule = body_of(draft(maker, percent='18', expect=201))
        assert datetime.fromisoformat(rule['effective_from']) < timezone.now() + timedelta(minutes=5)

    def test_the_driver_notice_is_handed_to_the_outbox_and_stays_queued_until_drained(self, world):
        from servers.pricing.driver_notices import deliver

        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        activate(world, body_of(draft(maker, percent='18', expect=201))['id'])
        notice = DriverNotice.objects.get()
        assert deliver(notice) is False
        notice.refresh_from_db()
        assert notice.status == 'queued' and notice.sent_at is None


# ===========================================================================
# CAP and KIND
# ===========================================================================

class TestCapAndKind:
    def test_with_no_cap_set_any_rate_may_be_drafted_and_the_console_says_so(self, world):
        maker, _ = world.super
        make_rule('IN', '15.00', node=world.geo['IN'])
        body_of(draft(maker, percent='45', expect=201))
        cfg = get(maker, f'{BASE}/commission/config/')['results']
        india = next(c for c in cfg if c['country'] == 'IN')
        assert india['max_percent'] is None and india['cap_status'] == 'cap not confirmed (legal)'

    def test_with_a_cap_set_a_rate_above_it_is_refused(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        CommissionCountryConfig.objects.create(country='IN', max_percent=Decimal('20.00'))
        maker, _ = world.super
        r = draft(maker, percent='21')
        assert r.status_code == 400 and error_code(r) == 'ABOVE_CAP'
        body_of(draft(maker, percent='20', expect=201))

    def test_a_cap_lowered_after_submission_blocks_the_activation(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        request_id = body_of(submit(maker, rule['id']))['approval_request']['id']
        CommissionCountryConfig.objects.create(country='IN', max_percent=Decimal('10.00'))
        chk, _ = checker()
        approve_request(chk, request_id)
        request = ApprovalRequest.objects.get(pk=request_id)
        assert request.status == 'failed' and request.error_code == 'ABOVE_CAP'
        assert CommissionRule.objects.get(pk=rule['id']).status == PENDING_APPROVAL

    def test_only_percentage_is_implemented(self, world):
        maker, _ = world.super
        for kind in ('flat_fee', 'subscription'):
            r = draft(maker, kind=kind, percent='5')
            assert r.status_code == 400 and error_code(r) == 'KIND_NOT_SUPPORTED'
            assert 'not supported yet' in r.json()['error']['message']
        r = draft(maker, kind='lottery')
        assert r.status_code == 400 and error_code(r) == 'UNKNOWN_KIND'
        assert not CommissionRule.objects.exists()

    def test_the_rate_and_the_reason_are_validated(self, world):
        maker, _ = world.super
        assert error_code(draft(maker, percent='101')) == 'INVALID_PERCENT'
        assert error_code(draft(maker, percent='abc')) == 'INVALID_PERCENT'
        assert error_code(draft(maker, reason='')) == 'REASON_REQUIRED'
        assert error_code(draft(maker, country='ZZ')) == 'UNKNOWN_COUNTRY'

    def test_a_cap_change_is_itself_a_request_a_checker_decides(self, world):
        maker, _ = world.super
        r = swept(maker.put(f'{BASE}/commission/config/IN/', {
            'max_percent': '22', 'notice_days': 5, 'reason': 'Legal confirmed the state cap'}, format='json'))
        assert r.status_code == 202
        assert not CommissionCountryConfig.objects.filter(country='IN', max_percent__isnull=False).exists()
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        cfg = CommissionCountryConfig.objects.get(country='IN')
        assert (cfg.max_percent, cfg.notice_days, cfg.version) == (Decimal('22.00'), 5, 1)


# ===========================================================================
# MAKER-CHECKER on rules and fare cards
# ===========================================================================

class TestMakerChecker:
    def test_the_maker_alone_cannot_activate_a_rule(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        assert rule['status'] == 'draft'
        r = submit(maker, rule['id'])
        request_id = body_of(r)['approval_request']['id']
        assert body_of(r)['state'] == 'pending_approval'
        assert CommissionRule.objects.get(pk=rule['id']).status == PENDING_APPROVAL
        own = approve_request(maker, request_id)
        assert own.status_code == 403
        assert CommissionRule.objects.get(pk=rule['id']).status == PENDING_APPROVAL
        assert resolver.resolve('IN', None, None).percent == Decimal('15.00')

    def test_a_different_checker_activates_it_and_it_is_audited(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        activate(world, rule['id'])
        row = CommissionRule.objects.get(pk=rule['id'])
        assert row.status == ACTIVE and row.version == 2
        assert CommissionRule.objects.get(version=1).status == ARCHIVED
        actions = set(AdminAuditLog.objects.values_list('action', flat=True))
        assert {'commission_rule_drafted', 'commission_rule_submitted', 'commission_rule_activated',
                'approval_requested', 'approval_executed'} <= actions

    def test_a_rejection_returns_the_rule_to_a_draft_to_edit_and_resubmit(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        request_id = body_of(submit(maker, rule['id']))['approval_request']['id']
        chk, _ = checker()
        r = swept(chk.post(f'{APPROVALS}/{request_id}/reject/', {'reason': 'Not now'}, format='json'))
        assert r.status_code == 200
        row = CommissionRule.objects.get(pk=rule['id'])
        assert row.status == DRAFT
        assert ApprovalRequest.objects.get(pk=request_id).status == REJECTED
        r = swept(maker.patch(f'{BASE}/commission/rules/{rule["id"]}/', {'percent': '13'}, format='json'))
        assert r.status_code == 200
        submit(maker, rule['id'])

    def test_only_a_draft_can_be_edited_discarded_or_submitted(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        activate(world, rule['id'])
        for call in (lambda: maker.patch(f'{BASE}/commission/rules/{rule["id"]}/', {'percent': '1'}, format='json'),
                     lambda: maker.delete(f'{BASE}/commission/rules/{rule["id"]}/'),
                     lambda: maker.post(f'{BASE}/commission/rules/{rule["id"]}/submit/',
                                        {'reason': 'again'}, format='json')):
            r = swept(call())
            assert r.status_code == 409 and error_code(r) == 'NOT_A_DRAFT'

    def test_one_open_draft_per_scope_and_a_draft_can_be_discarded(self, world):
        maker, _ = world.super
        first = body_of(draft(maker, percent='12', expect=201))
        r = draft(maker, percent='13')
        assert r.status_code == 409 and error_code(r) == 'DRAFT_EXISTS'
        assert swept(maker.delete(f'{BASE}/commission/rules/{first["id"]}/')).status_code == 200
        draft(maker, percent='13', expect=201)
        assert 'commission_rule_discarded' in AdminAuditLog.objects.values_list('action', flat=True)

    def test_the_preview_and_matrix_gets_are_read_only(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        maker, _ = world.super
        before = AdminAuditLog.objects.count()
        get(maker, f'{BASE}/commission/matrix/')
        get(maker, f'{BASE}/commission/rules/')
        assert AdminAuditLog.objects.count() == before


class TestFareCardsThroughMakerChecker:
    def card_body(self, world, **over):
        body = {'zone': world.hyd.id, 'vehicle_type': world.sedan.id, 'base_fare': '30.00',
                'per_km_fare': '12.00', 'per_min_fare': '2.00', 'min_fare': '50.00',
                'reason': 'New city card'}
        body.update(over)
        return body

    def seed_card(self, world, **over):
        values = {'base_fare': '30.00', 'per_km_fare': '12.00', 'per_min_fare': '2.00',
                  'min_fare': '50.00', 'night_surge_multiplier': '1.25', 'surge_cap_multiplier': '1.50'}
        values.update(over)
        return RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                       **{k: Decimal(v) for k, v in values.items()})

    def test_create_is_a_request_and_writes_no_card_until_approved(self, world):
        maker, _ = world.super
        r = post(maker, f'{BASE}/rate-cards/', self.card_body(world), expect=202)
        assert body_of(r)['state'] == 'pending_approval'
        assert not RateCard.objects.filter(zone=world.hyd).exists()
        chk, _ = checker()
        assert approve_request(chk, body_of(r)['approval_request']['id']).status_code == 200
        card = RateCard.objects.get(zone=world.hyd)
        assert (card.version, card.base_fare, card.is_active) == (1, Decimal('30.00'), True)

    def test_the_maker_alone_cannot_approve_a_fare_card(self, world):
        maker, _ = world.super
        r = post(maker, f'{BASE}/rate-cards/', self.card_body(world), expect=202)
        assert approve_request(maker, body_of(r)['approval_request']['id']).status_code == 403
        assert not RateCard.objects.filter(zone=world.hyd).exists()

    def test_changing_a_surge_cap_or_night_multiplier_is_a_new_version_request(self, world):
        card = self.seed_card(world)
        maker, _ = world.super
        r = swept(maker.patch(f'{BASE}/rate-cards/{card.id}/', {
            'surge_cap_multiplier': '1.40', 'night_surge_multiplier': '1.10', 'reason': 'MVA review'},
            format='json'))
        assert r.status_code == 202
        card.refresh_from_db()
        assert card.surge_cap_multiplier == Decimal('1.50') and RateCard.objects.filter(zone=world.hyd).count() == 1
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        cards = list(RateCard.objects.filter(zone=world.hyd).order_by('version'))
        assert [c.version for c in cards] == [1, 2]
        assert cards[1].surge_cap_multiplier == Decimal('1.40') and cards[1].base_fare == Decimal('30.00')
        assert cards[0].effective_to is not None      # closed, never edited
        assert cards[0].surge_cap_multiplier == Decimal('1.50')

    def test_retire_and_delete_are_requests_and_a_card_is_never_deleted(self, world):
        card = self.seed_card(world)
        maker, _ = world.super
        r = swept(maker.patch(f'{BASE}/rate-cards/{card.id}/', {'is_active': False, 'reason': 'Wrong class'},
                              format='json'))
        assert r.status_code == 202
        card.refresh_from_db()
        assert card.is_active
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        card.refresh_from_db()
        assert card.is_active is False and card.effective_to is not None
        other = self.seed_card(world, base_fare='31.00')
        r = swept(maker.delete(f'{BASE}/rate-cards/{other.id}/', {'reason': 'Retire it'}, format='json'))
        assert r.status_code == 202 and RateCard.objects.filter(pk=other.pk).exists()

    def test_a_notes_only_edit_stays_direct_and_is_audited(self, world):
        card = self.seed_card(world)
        maker, _ = world.super
        r = swept(maker.patch(f'{BASE}/rate-cards/{card.id}/', {'notes': 'checked with ops'}, format='json'))
        assert r.status_code == 200
        card.refresh_from_db()
        assert card.notes == 'checked with ops'
        assert AdminAuditLog.objects.filter(action='rate_card_updated').exists()

    def test_a_reason_is_required_and_a_card_in_another_country_is_a_404(self, world):
        maker, _ = world.super
        r = post(maker, f'{BASE}/rate-cards/', self.card_body(world, reason=''))
        assert r.status_code == 400 and error_code(r) == 'REASON_REQUIRED'
        ops, _ = world.ops
        r = ops.post(f'{BASE}/rate-cards/', self.card_body(world, zone=world.aus.id), format='json')
        assert r.status_code == 404

    def test_a_card_that_changed_since_the_request_is_refused_not_applied(self, world):
        card = self.seed_card(world)
        maker, _ = world.super
        r = swept(maker.patch(f'{BASE}/rate-cards/{card.id}/', {'base_fare': '35.00', 'reason': 'Review'},
                              format='json'))
        card.new_version(effective_from=timezone.now(), base_fare=Decimal('33.00'))
        chk, _ = checker()
        approve_request(chk, body_of(r)['approval_request']['id'])
        request = ApprovalRequest.objects.get(pk=body_of(r)['approval_request']['id'])
        assert request.status == 'failed' and request.error_code == 'CARD_CHANGED'

    def test_a_change_must_be_built_on_the_newest_version(self, world):
        card = self.seed_card(world)
        later = card.new_version(effective_from=timezone.now() + timedelta(days=5),
                                 per_km_fare=Decimal('13.00'))
        maker, _ = world.super
        r = swept(maker.patch(f'{BASE}/rate-cards/{card.id}/', {'base_fare': '35.00', 'reason': 'Review'},
                              format='json'))
        assert r.status_code == 400 and error_code(r) == 'NEWER_VERSION_EXISTS'
        assert 'v2' in r.json()['error']['message'] and 'scheduled' in r.json()['error']['message']
        # built on the newest one it goes through
        r = swept(maker.patch(f'{BASE}/rate-cards/{later.id}/', {'base_fare': '35.00', 'reason': 'Review'},
                              format='json'))
        assert r.status_code == 202

    def test_the_preview_compares_with_the_card_a_rider_pays_today(self, world):
        card = self.seed_card(world)
        card.new_version(effective_from=timezone.now() + timedelta(days=5), per_km_fare=Decimal('13.00'))
        maker, _ = world.super
        r = post(maker, f'{BASE}/fare-cards/preview/', {
            'zone': world.hyd.id, 'vehicle_type': world.sedan.id, 'values': {}}, expect=200)
        row = next(x for x in body_of(r)['preview']['rows']
                   if x['distance_km'] == 8 and x['period'] == 'off_peak')
        # draft = the scheduled v2 (13/km), old = the live v1 (12/km): 8 km is 8.00 more
        assert row['change_minor'] == 800

    def test_the_rate_card_api_no_longer_exposes_the_commission_column(self, world):
        card = self.seed_card(world)
        maker, _ = world.super
        data = get(maker, f'{BASE}/rate-cards/{card.id}/') if False else swept(
            maker.get(f'{BASE}/rate-cards/{card.id}/')).json()
        flat = str(data)
        assert 'commission' not in flat

    def test_the_overview_lists_the_live_card_and_its_history_per_class(self, world):
        self.seed_card(world)
        maker, _ = world.super
        data = get(maker, f'{BASE}/fare-cards/overview/?zone={world.hyd.id}')
        sedan = next(c for c in data['zones'][0]['classes'] if c['vehicle_type_name'] == 'sedan')
        auto = next(c for c in data['zones'][0]['classes'] if c['vehicle_type_name'] == 'auto')
        assert sedan['live']['version'] == 1 and len(sedan['history']) == 1
        assert auto['live'] is None


class TestRiderFarePreview:
    def values(self, **over):
        v = {'base_fare': Decimal('30.00'), 'per_km_fare': Decimal('12.00'),
             'per_min_fare': Decimal('2.00'), 'min_fare': Decimal('50.00'),
             'night_surge_multiplier': Decimal('1.25'), 'night_surge_start_hour': 23,
             'night_surge_end_hour': 5, 'surge_cap_multiplier': Decimal('1.50'),
             'gst_percent': Decimal('5.000')}
        v.update(over)
        return v

    def test_it_equals_quote_fare_off_peak_and_at_night(self, world):
        from servers.pricing import fare_card_workflow as fc
        from servers.pricing.services import quote_fare

        v = self.values()
        RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                **{k: val for k, val in v.items()})
        from shapely.geometry import shape

        # finance_world draws Austin over Hyderabad's coordinates, so take a point that is
        # Hyderabad and not Austin.
        point = shape(world.hyd.polygon_geojson).difference(
            shape(world.aus.polygon_geojson)).representative_point()
        lat, lon = point.y, point.x
        for km in (3, 8, 15):
            minutes = km * fc.MINUTES_PER_KM
            for night, hour in ((False, 12), (True, 1)):
                # 12:00 and 01:00 Hyderabad time are off-peak and night
                at = datetime(2026, 11, 3, hour, 0, tzinfo=dt_timezone(timedelta(hours=5, minutes=30)))
                with mock.patch('servers.pricing.services.compute_surge_multiplier',
                                return_value=Decimal('1.00')):
                    quoted = quote_fare(km, minutes, 'sedan', pickup_lat=lat, pickup_lon=lon, at=at)
                preview = fc.quote_from_values(world.hyd, v, km, minutes, night=night)
                assert quoted['source'] == 'db'
                assert preview['total'] == quoted['total_fare'], (km, night)

    def test_the_preview_endpoint_shows_old_against_new_in_the_zone_currency(self, world):
        RateCard.objects.create(zone=world.hyd, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                **self.values())
        maker, _ = world.super
        r = post(maker, f'{BASE}/fare-cards/preview/', {
            'zone': world.hyd.id, 'vehicle_type': world.sedan.id, 'values': {'per_km_fare': '14.00'}},
            expect=200)
        preview = body_of(r)['preview']
        assert preview['currency'] == 'INR'
        assert {(row['distance_km'], row['period']) for row in preview['rows']} == {
            (km, p) for km in (3, 8, 15) for p in ('off_peak', 'night')}
        row = next(x for x in preview['rows'] if x['distance_km'] == 8 and x['period'] == 'off_peak')
        # 8 km at 12 -> 14 per km is 16 INR more: 1600 minor units
        assert row['change_minor'] == 1600 and row['new_minor'] - row['old_minor'] == 1600
        night = next(x for x in preview['rows'] if x['distance_km'] == 8 and x['period'] == 'night')
        assert night['night_applied'] is True and night['old_minor'] > row['old_minor']

    def test_a_us_zone_previews_in_dollars_with_additive_tax(self, world):
        RateCard.objects.create(zone=world.aus, vehicle_type=world.sedan, effective_from=LONG_AGO,
                                **self.values(base_fare=Decimal('3.00'), per_km_fare=Decimal('1.00'),
                                              per_min_fare=Decimal('0.20'), min_fare=Decimal('5.00'),
                                              gst_percent=Decimal('8.250')))
        maker, _ = world.super
        r = post(maker, f'{BASE}/fare-cards/preview/', {
            'zone': world.aus.id, 'vehicle_type': world.sedan.id, 'values': {}}, expect=200)
        preview = body_of(r)['preview']
        assert preview['currency'] == 'USD'
        three = next(x for x in preview['rows'] if x['distance_km'] == 3 and x['period'] == 'off_peak')
        # 3.00 + 3 km + 9 min * 0.20 = 7.80, tax 8.25% = 0.64, total 8.44
        assert three['new_minor'] == 844 and three['change_minor'] == 0

    def test_a_zone_out_of_scope_is_a_404(self, world):
        ops, _ = world.ops
        r = ops.post(f'{BASE}/fare-cards/preview/', {'zone': world.aus.id, 'vehicle_type': world.sedan.id},
                     format='json')
        assert r.status_code == 404


# ===========================================================================
# THE DRIVER-EARNINGS PREVIEW
# ===========================================================================

class TestEarningsPreview:
    def settled(self, world, tag, fare, *, vt=None, zone_obj=None, calling='91'):
        zone_obj = zone_obj or world.hyd
        driver = make_driver(world.geo, 'IN-TG-HYD', tag, currency='INR', calling=calling)
        trip = make_trip(zone_obj, make_rider(tag, calling), driver, fare=fare)
        if vt is not None:
            trip.requested_vehicle_type = vt
            trip.save(update_fields=['requested_vehicle_type'])
        settle(trip)
        return driver, trip

    def test_trips_drivers_and_the_average_and_total_change(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        d1, _ = self.settled(world, 'P1', '1000.00')
        d2, _ = self.settled(world, 'P2', '2000.00')
        self.settled(world, 'P3', '500.00')
        # d1 has a second trip too
        make_trip(world.hyd, make_rider('P1b', '91'), d1, fare='1000.00')
        for t in (d1,):
            settle(d1.trips.order_by('-id').first())
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        data = body_of(swept(maker.get(f'{BASE}/commission/rules/{rule["id"]}/preview/')))
        inr = data['by_currency']['INR']
        # 4 trips, fares 1000+2000+500+1000 = 4500; 3 points less commission = 135.00
        assert inr['trips_affected'] == 4 and inr['drivers_affected'] == 3
        assert inr['total_change_minor'] == 13_500 and inr['average_change_minor'] == 3_375
        assert data['window_days'] == 30 and data['increase'] is False

    def test_an_increase_shows_drivers_keeping_less(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        self.settled(world, 'P4', '1000.00')
        maker, _ = world.super
        rule = body_of(draft(maker, percent='20', expect=201))
        data = body_of(swept(maker.get(f'{BASE}/commission/rules/{rule["id"]}/preview/')))
        assert data['by_currency']['INR']['total_change_minor'] == -5_000 and data['increase'] is True

    def test_trips_where_a_more_specific_rule_governs_are_not_affected(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        make_rule('IN', '10.00', vt=world.sedan)                     # sedans have their own rate
        self.settled(world, 'P5', '1000.00', vt=world.sedan)
        self.settled(world, 'P6', '1000.00', vt=world.auto)
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        inr = body_of(swept(maker.get(f'{BASE}/commission/rules/{rule["id"]}/preview/')))['by_currency']['INR']
        assert inr['trips_affected'] == 1                          # only the auto trip

    def test_a_trip_older_than_thirty_days_and_another_countrys_trips_are_left_out(self, world):
        from servers.ride.models import Trip

        make_rule('IN', '15.00', node=world.geo['IN'])
        make_rule('US', '15.00', node=world.geo['US'])
        _, old = self.settled(world, 'P7', '1000.00')
        Trip.objects.filter(pk=old.pk).update(completed_at=timezone.now() - timedelta(days=45))
        us_driver = make_driver(world.geo, 'US-TX-AUS', 'P8', currency='USD', calling='1')
        settle(make_trip(world.aus, make_rider('P8', '1'), us_driver, fare='30.00'))
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        data = body_of(swept(maker.get(f'{BASE}/commission/rules/{rule["id"]}/preview/')))
        assert data['by_currency'] == {}

    def test_currencies_are_never_added_together(self, world):
        make_rule('IN', '15.00')
        make_rule('US', '15.00')
        self.settled(world, 'P9', '1000.00')
        us_driver = make_driver(world.geo, 'US-TX-AUS', 'P10', currency='USD', calling='1')
        settle(make_trip(world.aus, make_rider('P10', '1'), us_driver, fare='30.00'))
        in_rule = make_rule('IN', '12.00', status=DRAFT, version=2)
        draft_global = CommissionRule.objects.get(pk=in_rule.pk)
        data = resolver.earnings_preview(draft_global)
        assert list(data['by_currency']) == ['INR']


# ===========================================================================
# NOTHING READS THE DEPRECATED SOURCES
# ===========================================================================

SOURCE_DIRS = ('servers', 'base')
#: Files that may NAME the deprecated sources: the model that keeps the column and its
#: serializer module. A DOCSTRING or COMMENT naming them is fine anywhere: the guard
#: reads the syntax tree, not the text.
# `platform_ops/settings_editor.py` NAMES the key to refuse it: it is the deprecated-key registry of
# the Platform Settings editor (read-only, pointing at /pricing). It never reads the value.
MAY_NAME = {'servers/pricing/models.py', 'servers/pricing/serializers.py',
            'servers/platform_ops/settings_editor.py'}


def python_trees():
    import ast

    for top in SOURCE_DIRS:
        for path in (REPO / top).rglob('*.py'):
            rel = path.relative_to(REPO).as_posix()
            if '/migrations/' in rel or '/tests/' in rel or rel.endswith('_test.py'):
                continue
            yield rel, ast.parse(path.read_text(encoding='utf-8'))


def docstring_nodes(tree):
    import ast

    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, 'body', [])
            if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


class TestDeprecatedSourcesAreNotRead:
    def test_no_code_reads_platform_commission_percent(self):
        import ast

        hits = []
        for rel, tree in python_trees():
            docs = docstring_nodes(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name):
                    name = node.id
                elif isinstance(node, ast.Attribute):
                    name = node.attr
                elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and id(node) not in docs):
                    name = node.value
                else:
                    continue
                if name == 'PLATFORM_COMMISSION_PERCENT' and rel not in MAY_NAME:
                    hits.append((rel, getattr(node, 'lineno', 0)))
        assert hits == []

    def test_no_code_reads_a_rate_cards_commission_percent(self):
        import ast

        def is_card(expr):
            """A name or call that is plainly a rate card."""
            if isinstance(expr, ast.Name):
                return 'card' in expr.id.lower() or expr.id == 'rc'
            if isinstance(expr, ast.Call):
                fn = expr.func
                called = fn.id if isinstance(fn, ast.Name) else getattr(fn, 'attr', '')
                return called in ('rate_card_for_trip', 'get_active_rate_card', 'current_card')
            if isinstance(expr, ast.Attribute):
                return 'card' in expr.attr.lower()
            return False

        hits = []
        for rel, tree in python_trees():
            if rel in MAY_NAME:
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == 'commission_percent'
                        and isinstance(node.ctx, ast.Load) and is_card(node.value)):
                    hits.append((rel, node.lineno))
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == 'getattr' and len(node.args) >= 2
                        and is_card(node.args[0]) and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == 'commission_percent'):
                    hits.append((rel, node.lineno))
        assert hits == []

    def test_the_settings_module_no_longer_defines_the_setting(self, settings):
        assert not hasattr(settings, 'PLATFORM_COMMISSION_PERCENT')

    def test_the_old_sources_cannot_change_a_settlement(self, world):
        from servers.pricing.models import PlatformSettings

        make_rule('IN', '15.00', node=world.geo['IN'])
        card = RateCard.objects.create(
            zone=world.hyd, vehicle_type=world.sedan, base_fare=30, per_km_fare=12, per_min_fare=2,
            min_fare=50, effective_from=LONG_AGO, commission_percent=Decimal('33.00'))
        PlatformSettings.objects.create(key='PLATFORM_COMMISSION_PERCENT', value='44',
                                        setting_type='decimal')
        with mock.patch.object(type(card), 'commission_percent', Decimal('33.00'), create=True):
            driver = make_driver(world.geo, 'IN-TG-HYD', 'D1', currency='INR', calling='91')
            trip = make_trip(world.hyd, make_rider('D1', '91'), driver, fare='1000.00')
            settle(trip)
        assert TripSettlement.objects.get(trip=trip).commission_percent == Decimal('15.00')
        assert 'commission_percent' in {f.name for f in RateCard._meta.fields}     # column kept
        assert 'DEPRECATED' in RateCard._meta.get_field('commission_percent').help_text

    def test_the_hard_coded_eighteen_is_gone(self):
        import ast

        tree = ast.parse((REPO / 'servers' / 'pricing' / 'services.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'commission_percent_for_trip')
        numbers = [c.value for c in ast.walk(fn) if isinstance(c, ast.Constant)
                   and isinstance(c.value, (int, float, str)) and c is not fn.body[0].value]
        assert not any(str(v) in ('18', '18.0', '18.00') for v in numbers)


# ===========================================================================
# DISPLAY == SETTLED (the fee rule, AUDIT 27.43)
# ===========================================================================

class TestDisplayEqualsSettled:
    def test_the_driver_earnings_endpoints_show_the_snapshot_percent_the_settlement_used(self, world):
        old = make_rule('IN', '12.00', node=world.geo['IN'])
        driver = make_driver(world.geo, 'IN-TG-HYD', 'F1', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('F1', '91'), driver, fare='1000.00')
        for k, v in resolver.snapshot_for_booking(world.hyd, None).items():
            setattr(trip, k, v)
        trip.save()
        # The rate moves AFTER booking; the driver must still be shown 12.
        CommissionRule.objects.filter(pk=old.pk).update(effective_to=timezone.now(), status=ARCHIVED)
        make_rule('IN', '18.00', node=world.geo['IN'], version=2, start=timezone.now())
        settle(trip)
        settled = TripSettlement.objects.get(trip=trip)
        client = client_for(driver.user_id)
        rows = client.get('/api/v1/driver/earnings/').json()['data']['results']
        summary = client.get('/api/v1/driver/earnings/summary/').json()['data']
        assert settled.commission_percent == Decimal('12.00')
        assert Decimal(rows[0]['commission_percent']) == settled.commission_percent
        assert Decimal(summary['commission_percent']) == settled.commission_percent
        assert Decimal(rows[0]['commission']) == settled.commission_amount
        assert summary['commission_percent_mixed'] is False

    def test_trips_settled_at_different_rates_are_flagged_mixed_and_each_row_is_exact(self, world):
        driver = make_driver(world.geo, 'IN-TG-HYD', 'F2', currency='INR', calling='91')
        first = make_rule('IN', '12.00', node=world.geo['IN'])
        t1 = make_trip(world.hyd, make_rider('F2a', '91'), driver, fare='1000.00')
        for k, v in resolver.snapshot_for_booking(world.hyd, None).items():
            setattr(t1, k, v)
        t1.save()
        CommissionRule.objects.filter(pk=first.pk).update(effective_to=timezone.now(), status=ARCHIVED)
        make_rule('IN', '20.00', node=world.geo['IN'], version=2, start=timezone.now())
        t2 = make_trip(world.hyd, make_rider('F2b', '91'), driver, fare='1000.00')
        for k, v in resolver.snapshot_for_booking(world.hyd, None).items():
            setattr(t2, k, v)
        t2.save()
        settle(t1)
        settle(t2)
        client = client_for(driver.user_id)
        rows = client.get('/api/v1/driver/earnings/').json()['data']['results']
        assert sorted(Decimal(r['commission_percent']) for r in rows) == [Decimal('12.00'), Decimal('20.00')]
        summary = client.get('/api/v1/driver/earnings/summary/').json()['data']
        assert summary['commission_percent_mixed'] is True

    def test_the_admin_settlement_view_and_the_ledger_read_the_same_stored_percent(self, world):
        make_rule('IN', '17.00', node=world.geo['IN'])
        driver = make_driver(world.geo, 'IN-TG-HYD', 'F3', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('F3', '91'), driver, fare='1000.00')
        settle(trip)
        s = TripSettlement.objects.get(trip=trip)
        from servers.payments.admin_finance import settlement_row

        row = settlement_row(s) if callable(settlement_row) else {}
        assert str(row.get('commission_percent')) == str(s.commission_percent)


# ===========================================================================
# READINESS
# ===========================================================================

class TestReadiness:
    def items(self, node):
        from servers.admin_access.readiness import city_readiness

        return {i['key']: i for i in city_readiness(node)}

    def test_a_city_with_no_rule_and_no_card_fails_both_new_items(self, world):
        items = self.items(world.geo['IN-TG-HYD'])
        assert items['commission']['ok'] is False and 'No active commission rule' in items['commission']['detail']
        assert items['fare_card']['ok'] is False
        assert {'safety_coverage', 'commission', 'fare_card'} <= set(items)

    def test_a_country_rule_alone_satisfies_commission_but_not_the_fare_card(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        items = self.items(world.geo['IN-TG-HYD'])
        assert items['commission']['ok'] is True and items['fare_card']['ok'] is False

    def cards_for(self, world, classes):
        for vt in classes:
            RateCard.objects.create(zone=world.hyd, vehicle_type=vt, base_fare=30, per_km_fare=12,
                                    per_min_fare=2, min_fare=50, effective_from=LONG_AGO)

    def test_every_vehicle_class_needs_a_card_in_the_citys_zone(self, world):
        classes = list(VehicleType.objects.order_by('type'))
        assert len(classes) >= 2
        self.cards_for(world, classes[:-1])
        items = self.items(world.geo['IN-TG-HYD'])
        assert items['fare_card']['ok'] is False and classes[-1].type in items['fare_card']['detail']
        self.cards_for(world, classes[-1:])
        assert self.items(world.geo['IN-TG-HYD'])['fare_card']['ok'] is True

    def test_a_retired_card_no_longer_counts(self, world):
        self.cards_for(world, VehicleType.objects.all())
        assert self.items(world.geo['IN-TG-HYD'])['fare_card']['ok'] is True
        RateCard.objects.filter(vehicle_type=world.auto).update(is_active=False)
        assert self.items(world.geo['IN-TG-HYD'])['fare_card']['ok'] is False

    def test_a_city_with_no_active_zone_has_no_fare_cards(self, world):
        ServiceZone.objects.filter(pk=world.hyd.pk).update(is_active=False)
        items = self.items(world.geo['IN-TG-HYD'])
        assert items['fare_card']['ok'] is False and 'no active service zone' in items['fare_card']['detail']

    def test_a_city_with_a_rule_and_every_card_is_ready_on_both_new_items(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        self.cards_for(world, VehicleType.objects.all())
        items = self.items(world.geo['IN-TG-HYD'])
        assert items['commission']['ok'] is True and items['fare_card']['ok'] is True

    def test_a_city_cannot_go_live_without_them(self, world):
        from servers.admin_access.readiness import CityNotReady, set_city_status

        node = world.geo['IN-TG-HYD']
        GeoNode.objects.filter(pk=node.pk).update(status='paused')
        node.refresh_from_db()
        _, actor = world.super
        with pytest.raises(CityNotReady) as caught:
            set_city_status(node, 'active', actor, 'Launch')
        keys = {i['key'] for i in caught.value.items}
        assert {'commission', 'fare_card'} <= keys


# ===========================================================================
# THE RULING COMMAND
# ===========================================================================

class TestRulingCommand:
    def run(self):
        out = StringIO()
        call_command('apply_commission_ruling', actor_email=seed_actor().email, stdout=out)
        return out.getvalue()

    def test_it_creates_an_active_country_rule_for_india_and_the_us(self, world):
        self.run()
        rules = CommissionRule.objects.order_by('country')
        assert [r.country for r in rules] == ['IN', 'US']
        for r in rules:
            assert (r.percent, r.status, r.kind, r.vehicle_type_id) == (
                Decimal('15.00'), ACTIVE, 'percentage', None)
            assert r.geo_node.kind == 'country' and r.reason == (
                'Owner ruling, AUDIT 27.44: 15% default, pre-launch, no live drivers')
            assert r.applies_at(timezone.now())
        assert not DriverNotice.objects.exists()
        assert resolver.resolve('IN', world.geo['IN-TG-HYD'].id, world.sedan.id).percent == Decimal('15.00')
        assert resolver.resolve('US', world.geo['US-TX-AUS'].id, None).percent == Decimal('15.00')

    def test_it_starts_at_the_earliest_trip_so_an_unsettled_older_trip_can_still_settle(self, world):
        from servers.ride.models import Trip

        driver = make_driver(world.geo, 'IN-TG-HYD', 'RC1', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('RC1', '91'), driver, fare='1000.00')
        Trip.objects.filter(pk=trip.pk).update(requested_at=timezone.now() - timedelta(days=9))
        trip.refresh_from_db()
        with pytest.raises(resolver.CommissionNotConfigured):
            settle(trip)                              # no rule yet
        self.run()
        rule = CommissionRule.objects.get(country='IN')
        assert rule.effective_from <= trip.requested_at       # covers the old trip
        settle(trip)
        assert TripSettlement.objects.get(trip=trip).commission_percent == Decimal('15.00')

    def test_it_creates_no_city_or_vehicle_override(self, world):
        self.run()
        assert not CommissionRule.objects.filter(vehicle_type__isnull=False).exists()
        assert not CommissionRule.objects.filter(geo_node__kind='city').exists()

    def test_it_is_audited_by_the_named_operator_and_idempotent(self, world):
        self.run()
        audited = AdminAuditLog.objects.filter(action__in=['commission_rule_activated',
                                                           'commission_ruling_applied'])
        assert audited.count() == 3
        assert all(seed_actor().email in a.actor_email or 'apply_commission_ruling' in a.actor_label
                   for a in audited)
        before = (CommissionRule.objects.count(), AdminAuditLog.objects.count())
        out = self.run()
        assert 'Nothing to change' in out
        assert (CommissionRule.objects.count(), AdminAuditLog.objects.count()) == before

    def test_it_leaves_a_rule_a_person_set_since(self, world):
        make_rule('IN', '13.00', node=world.geo['IN'])
        self.run()
        assert CommissionRule.objects.filter(country='IN').count() == 1
        assert CommissionRule.objects.get(country='IN').percent == Decimal('13.00')
        assert CommissionRule.objects.filter(country='US').count() == 1

    def test_it_refuses_without_an_actor(self, world):
        with pytest.raises(CommandError):
            call_command('apply_commission_ruling', stdout=StringIO())
        assert not CommissionRule.objects.exists()

    def test_it_writes_the_config_rows_without_a_cap(self, world):
        self.run()
        cfg = {c.country: c for c in CommissionCountryConfig.objects.all()}
        assert set(cfg) == {'IN', 'US'} and all(c.max_percent is None and c.notice_days == 7
                                                for c in cfg.values())


# ===========================================================================
# SCOPE, CAPABILITY AND AUDIT
# ===========================================================================

class TestScopeAndCapability:
    def test_city_ops_in_hyderabad_gets_404_on_an_austin_rule(self, world):
        austin = make_rule('US', '15.00', node=world.geo['US-TX-AUS'], reason='austin only')
        hyd = make_rule('IN', '14.00', node=world.geo['IN-TG-HYD'])
        ops, _ = world.ops
        for path in (f'{BASE}/commission/rules/{austin.id}/', f'{BASE}/commission/rules/{austin.id}/preview/'):
            assert swept(ops.get(path)).status_code == 404
        assert swept(ops.get(f'{BASE}/commission/rules/{hyd.id}/')).status_code == 200
        listed = {r['id'] for r in get(ops, f'{BASE}/commission/rules/')['results']}
        assert listed == {hyd.id}
        assert swept(ops.patch(f'{BASE}/commission/rules/{austin.id}/', {'percent': '1'},
                               format='json')).status_code == 404
        assert swept(ops.post(f'{BASE}/commission/rules/{austin.id}/submit/', {}, format='json')
                     ).status_code == 404

    def test_the_matrix_and_config_show_only_the_callers_scope(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        make_rule('US', '15.00', node=world.geo['US'])
        ops, _ = world.ops
        data = get(ops, f'{BASE}/commission/matrix/')
        assert {r['city'] for r in data['rows']} == {'IN-TG-HYD'}
        assert {r['country'] for r in data['rows']} == {'IN'}

    def test_city_ops_can_draft_a_hyderabad_override_but_not_a_country_or_austin_rule(self, world):
        make_rule('IN', '15.00', node=world.geo['IN'])
        ops, _ = world.ops
        r = ops.post(f'{BASE}/commission/rules/', {
            'country': 'IN', 'city': 'IN-TG-HYD', 'percent': '14', 'reason': 'Hyderabad review'},
            format='json')
        assert r.status_code == 201
        # the whole country is above their scope: answered as not found
        r = ops.post(f'{BASE}/commission/rules/', {
            'country': 'IN', 'percent': '14', 'reason': 'Whole country'}, format='json')
        assert r.status_code == 404
        r = ops.post(f'{BASE}/commission/rules/', {
            'country': 'US', 'city': 'US-TX-AUS', 'percent': '14', 'reason': 'Austin'}, format='json')
        assert r.status_code == 404

    def test_a_checker_without_pricing_write_cannot_approve(self, world, geo):
        make_rule('IN', '15.00', node=geo['IN'])
        maker, _ = world.super
        rule = body_of(draft(maker, percent='12', expect=201))
        request_id = body_of(submit(maker, rule['id']))['approval_request']['id']
        # Finance holds approval.check? no: a Support Agent holds neither
        agent, _ = operator('support_agent', geo)
        assert swept(agent.post(f'{APPROVALS}/{request_id}/approve/', {'reason': 'ok'},
                                format='json')).status_code in (403, 404)
        assert ApprovalRequest.objects.get(pk=request_id).status == REQUESTED

    def test_no_capability_is_403_and_an_unauthenticated_caller_is_401(self, world, geo):
        agent, _ = operator('support_agent', geo)
        assert swept(agent.get(f'{BASE}/commission/matrix/')).status_code == 403
        assert swept(agent.post(f'{BASE}/commission/rules/', {'country': 'IN'}, format='json')
                     ).status_code == 403
        assert swept(agent.get(f'{BASE}/fare-cards/overview/')).status_code == 403
        from rest_framework.test import APIClient

        assert APIClient().get(f'{BASE}/commission/matrix/').status_code in (401, 403)

    def test_reading_needs_zone_read_and_drafting_needs_pricing_write(self, world, geo):
        analyst, _ = operator('analyst', geo)
        assert swept(analyst.get(f'{BASE}/commission/matrix/')).status_code == 200
        r = analyst.post(f'{BASE}/commission/rules/', {'country': 'IN', 'percent': '12',
                                                        'reason': 'Analyst tries'}, format='json')
        assert r.status_code == 403
        finance, _ = operator('finance', geo)
        assert swept(finance.get(f'{BASE}/commission/matrix/')).status_code == 200
        assert finance.post(f'{BASE}/fare-cards/preview/', {}, format='json').status_code in (404, 400)

    def test_city_ops_now_holds_pricing_write_and_nobody_else_gained_it(self):
        from servers.admin_access.roles import ROLE_TEMPLATES

        holders = {t['code'] for t in ROLE_TEMPLATES if 'pricing.write' in t['permissions']}
        assert holders == {'super_admin', 'city_ops_manager'}


# ===========================================================================
# CARRY-OVERS FROM MODULE 7
# ===========================================================================

class TestWeeklyRunSkipsDriversWithNoHandle:
    @pytest.fixture
    def india_weekly(self, world, geo):
        from servers.payments.models import PayoutCountryConfig

        PayoutCountryConfig.objects.filter(country='IN').delete()
        PayoutCountryConfig.objects.create(
            country='IN', currency='INR', fee_percent=Decimal('0'), minimum_withdrawal_minor=100,
            payout_schedule='weekly', payout_weekday=0, is_active=True, version=1)
        make_rule('IN', '15.00', node=geo['IN'])
        return world

    def funded(self, world, tag, *, handle):
        from servers.driver.models import DriverUPIContact
        from servers.rider.models import Wallet

        driver = make_driver(world.geo, 'IN-TG-HYD', tag, currency='INR', calling='91')
        Wallet.objects.filter(user_id=driver.user_id, scope='driver').update(balance_minor=100_000)
        if handle:
            DriverUPIContact.objects.create(driver=driver, upi_id=f'{tag.lower()}@okaxis', is_active=True)
        return driver

    MONDAY = datetime(2026, 11, 2, 6, 0, tzinfo=dt_timezone.utc)      # Monday morning in Hyderabad

    def test_a_driver_with_no_handle_is_skipped_listed_and_finance_is_told(self, india_weekly):
        from servers.payments.weekly_payout import run_weekly_payouts
        from servers.support.models import OperatorNotification

        paid = self.funded(india_weekly, 'W1', handle=True)
        skipped = self.funded(india_weekly, 'W2', handle=False)
        _, finance_user = operator('finance', india_weekly.geo)
        _, ops_user = india_weekly.ops
        result = run_weekly_payouts(self.MONDAY)[0]
        assert result['ran'] is True and result['withdrawals'] == 1
        assert result['skipped_no_handle'] == [skipped.id]
        assert WithdrawalRequest.objects.filter(origin='scheduled').get().driver_id == paid.id
        run = PayoutRun.objects.get()
        assert run.skipped == [{'driver_id': skipped.id, 'reason': 'no_payout_handle'}]
        # the skipped driver's money is untouched
        from servers.rider.models import Wallet
        assert Wallet.objects.get(user_id=skipped.user_id, scope='driver').balance_minor == 100_000
        # a finance holder in scope got an inbox item; a City Ops Manager did not (no finance.payout)
        mine = OperatorNotification.objects.filter(recipient=finance_user, kind='payout_run_skipped')
        assert mine.count() == 1 and str(skipped.id) in mine.get().text
        assert not OperatorNotification.objects.filter(recipient=ops_user).exists()
        assert AdminAuditLog.objects.get(action='payout_run_created').after['skipped_no_handle'] == 1

    def test_a_bank_account_is_a_handle_and_an_inactive_upi_contact_is_not(self, india_weekly):
        from servers.driver.models import DriverBankAccount, DriverUPIContact
        from servers.payments.weekly_payout import has_payout_handle

        bank = self.funded(india_weekly, 'W3', handle=False)
        DriverBankAccount.objects.create(driver=bank, bank_name='HDFC', account_number='1234567890',
                                         ifsc_code='HDFC0000001')
        inactive = self.funded(india_weekly, 'W4', handle=False)
        DriverUPIContact.objects.create(driver=inactive, upi_id='x@okaxis', is_active=False)
        assert has_payout_handle(bank) is True and has_payout_handle(inactive) is False

    def test_a_run_where_everybody_has_a_handle_skips_nobody_and_sends_no_inbox_item(self, india_weekly):
        from servers.payments.weekly_payout import run_weekly_payouts
        from servers.support.models import OperatorNotification

        self.funded(india_weekly, 'W5', handle=True)
        operator('finance', india_weekly.geo)
        result = run_weekly_payouts(self.MONDAY)[0]
        assert result['skipped_no_handle'] == [] and PayoutRun.objects.get().skipped == []
        assert not OperatorNotification.objects.filter(kind='payout_run_skipped').exists()

    def test_the_console_payout_settings_carry_the_skipped_list(self, india_weekly):
        from servers.payments.weekly_payout import run_weekly_payouts

        skipped = self.funded(india_weekly, 'W6', handle=False)
        run_weekly_payouts(self.MONDAY)
        fin, _ = operator('finance', india_weekly.geo)
        data = get(fin, '/api/v1/payments/admin/payout-config/')
        india = next(c for c in data['countries'] if c['country'] == 'IN')
        assert india['recent_runs'][0]['skipped_count'] == 1
        assert india['recent_runs'][0]['skipped'][0]['driver_id'] == skipped.id


class TestSkippedDriverNoticeAndOverview(TestWeeklyRunSkipsDriversWithNoHandle):
    def test_a_skipped_driver_gets_one_queued_notice_and_a_rerun_adds_none(self, india_weekly):
        from servers.payments.models import DriverPayoutNotice
        from servers.payments.weekly_payout import _queue_driver_notices, run_weekly_payouts

        skipped = self.funded(india_weekly, 'N1', handle=False)
        self.funded(india_weekly, 'N2', handle=True)
        run_weekly_payouts(self.MONDAY)
        notice = DriverPayoutNotice.objects.get()
        assert (notice.driver_id, notice.status, notice.kind) == (skipped.id, 'queued', 'add_payout_details')
        assert notice.sent_at is None             # no active template in this world: nothing to send
        run_weekly_payouts(self.MONDAY)                    # same week again: nothing new
        _queue_driver_notices(PayoutRun.objects.get(), [skipped])
        assert DriverPayoutNotice.objects.count() == 1

    def test_the_finance_overview_counts_skipped_drivers_in_the_viewers_scope_only(self, india_weekly):
        from servers.payments.weekly_payout import run_weekly_payouts

        a = self.funded(india_weekly, 'N3', handle=False)
        run_weekly_payouts(self.MONDAY)
        us_driver = make_driver(india_weekly.geo, 'US-TX-AUS', 'N4', currency='USD', calling='1')
        PayoutRun.objects.create(country='US', currency='USD', week_start=self.MONDAY.date(),
                                 skipped=[{'driver_id': us_driver.id, 'reason': 'no_payout_handle'}])
        rng = '?from=2026-10-01&to=2026-11-30'
        hyd_fin, _ = operator('finance', india_weekly.geo)
        data = get(hyd_fin, f'/api/v1/payments/admin/overview/{rng}')
        assert data['skipped_in_latest_run'] == [
            {'country': 'IN', 'week_start': '2026-11-02', 'count': 1}]
        sup, _ = india_weekly.super
        both = get(sup, f'/api/v1/payments/admin/overview/{rng}')['skipped_in_latest_run']
        assert [(r['country'], r['count']) for r in both] == [('IN', 1), ('US', 1)]
        assert 'driver_id' not in str(data['skipped_in_latest_run'])


class TestNoPayoutRailHoldsTheApproval:
    def us_withdrawal(self, world, *, amount=5_000):
        us_driver = make_driver(world.geo, 'US-TX-AUS', 'R1', currency='USD', calling='1')
        return WithdrawalRequest.objects.create(
            driver=us_driver, amount_minor=amount, currency='USD', status='pending')

    def test_a_us_approval_is_held_awaiting_rail_with_no_gateway_call(self, world, geo):
        w = self.us_withdrawal(world)
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        with mock.patch('servers.driver.services.initiate_upi_payout') as upi, \
                mock.patch('servers.payments.payment_gateways.factory.get_payment_gateway_for_payouts') as gw:
            r = post(us_finance, f'/api/v1/payments/admin/payouts/{w.id}/approve/', expect=200)
        w.refresh_from_db()
        assert (w.status, w.payout_status) == ('approved', 'awaiting_rail')
        assert upi.call_count == 0 and gw.call_count == 0
        assert w.failure_reason in (None, '') and not w.payout_reference_id
        assert w.failure_count == 0
        request = ApprovalRequest.objects.get(action_key='finance.payout')
        assert request.status == EXECUTED and request.result['payout_held_awaiting_rail_ids'] == [w.id]
        assert request.result['payout_trigger_failed_ids'] == []

    def test_a_held_payout_is_not_refunded_or_failed(self, world, geo):
        from servers.rider.models import Wallet

        w = self.us_withdrawal(world)
        before = Wallet.objects.get(user_id=w.driver.user_id, scope='driver').balance_minor
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        post(us_finance, f'/api/v1/payments/admin/payouts/{w.id}/approve/', expect=200)
        assert Wallet.objects.get(user_id=w.driver.user_id, scope='driver').balance_minor == before
        w.refresh_from_db()
        assert w.status != 'failed' and w.status != 'rejected'

    def test_the_console_row_and_list_say_nothing_is_sent(self, world, geo):
        w = self.us_withdrawal(world)
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        post(us_finance, f'/api/v1/payments/admin/payouts/{w.id}/approve/', expect=200)
        listed = get(us_finance, '/api/v1/payments/admin/payouts/')
        row = next(r for r in listed['results'] if r['id'] == w.id)
        assert row['awaiting_rail'] is True
        assert row['rail_note'] == 'No payout rail for USD yet: approved payouts wait here; nothing is sent.'
        assert listed['rail_notices'] == [{'currency': 'USD', 'note': row['rail_note']}]

    def test_india_still_goes_to_the_gateway(self, world, geo):
        from servers.payments import payout_rails

        assert payout_rails.has_rail('INR') and not payout_rails.has_rail('USD')
        assert payout_rails.has_rail('inr')

    def test_the_release_hook_sends_held_payouts_once_a_rail_exists(self, world, geo):
        from servers.payments import payout_rails

        w = self.us_withdrawal(world)
        us_finance, _ = operator('finance', geo, scope_code='US-TX-AUS')
        post(us_finance, f'/api/v1/payments/admin/payouts/{w.id}/approve/', expect=200)
        assert payout_rails.release_awaiting_rail() == []                  # no rail yet: still held
        w.refresh_from_db()
        assert w.payout_status == 'awaiting_rail'
        with mock.patch.dict(payout_rails.RAILS, {'USD': 'test_rail'}), \
                mock.patch('servers.driver.services.trigger_payout_creation', return_value=True) as sent:
            assert payout_rails.release_awaiting_rail() == [w.id]
        assert sent.call_count == 1 and sent.call_args[0][0].id == w.id

    def test_the_release_task_is_registered_and_scheduled(self):
        from django.conf import settings

        from servers.payments import tasks

        assert tasks.release_awaiting_rail.name == 'payments.release_awaiting_rail'
        assert settings.CELERY_BEAT_SCHEDULE['payments-release-awaiting-rail']['task'] == (
            'payments.release_awaiting_rail')


class TestZoneNeedsACountry:
    def test_the_model_refuses_a_zone_with_no_country(self):
        from django.core.exceptions import ValidationError

        with pytest.raises(ValidationError) as caught:
            ServiceZone.objects.create(
                code='NOWHERE', name='Nowhere', zone_type='city', is_active=True,
                polygon_geojson={'type': 'Polygon', 'coordinates': [[[0, 0], [0, 1], [1, 1], [0, 0]]]})
        assert 'country' in caught.value.message_dict

    def test_the_model_field_has_no_default(self):
        field = ServiceZone._meta.get_field('country')
        assert not field.has_default()

    def test_the_admin_api_requires_a_country(self, world):
        maker, _ = world.super
        polygon = {'type': 'Polygon', 'coordinates': [[[78.0, 17.0], [78.1, 17.0], [78.1, 17.1],
                                                       [78.0, 17.1], [78.0, 17.0]]]}
        body = {'code': 'IN-X-NEW', 'name': 'New', 'zone_type': 'city', 'polygon_geojson': polygon,
                'priority': 10, 'geo_node': 'IN-TG-HYD', 'reason': 'A new zone'}
        r = swept(maker.post(f'{BASE}/zones/', body, format='json'))
        assert r.status_code == 400 and 'country' in str(r.json())
        r = swept(maker.post(f'{BASE}/zones/', {**body, 'country': 'IN'}, format='json'))
        assert r.status_code == 202                              # a request since module 9
        approve_request(checker()[0], r.json()['data']['approval_request']['id'])
        assert ServiceZone.objects.get(code='IN-X-NEW').country == 'IN'

    def test_the_migration_changes_only_the_default(self):
        from importlib import import_module

        migration = import_module('servers.pricing.migrations.0015_commission_rules')
        altered = [op for op in migration.Migration.operations
                   if op.__class__.__name__ == 'AlterField' and op.model_name == 'servicezone']
        assert len(altered) == 1 and altered[0].name == 'country'
        assert altered[0].field.max_length == 2 and not altered[0].field.has_default()


# ===========================================================================
# STRUCTURE
# ===========================================================================

class TestStructure:
    def test_pricing_change_has_exactly_one_executor_and_the_kinds_it_handles(self):
        from servers.approvals import registry

        assert registry.has_executor('pricing.change')
        from servers.pricing import commission_workflow as flow

        assert flow.PAYLOAD_RULE == 'commission_rule' and flow.PAYLOAD_CONFIG == 'commission_country_config'

    def test_every_new_admin_get_is_in_the_pii_sweep_inventory(self):
        from test_pii_masking_everywhere import INVENTORY

        for route in ('api/v1/pricing/admin/commission/matrix/', 'api/v1/pricing/admin/commission/rules/',
                      'api/v1/pricing/admin/commission/rules/<int:rule_id>/',
                      'api/v1/pricing/admin/commission/rules/<int:rule_id>/preview/',
                      'api/v1/pricing/admin/commission/config/', 'api/v1/pricing/admin/fare-cards/overview/'):
            assert route in INVENTORY, route

    def test_the_commission_rule_is_scoped_by_its_node(self):
        from servers.admin_access.scoping import scope_path_for

        assert scope_path_for(CommissionRule) == 'geo_node__path'

    def test_an_unknown_pricing_change_is_refused_by_the_executor(self):
        from servers.approvals.registry import ExecutionRefused
        from servers.pricing.commission_workflow import execute_pricing_change

        with pytest.raises(ExecutionRefused):
            execute_pricing_change(SimpleNamespace(payload={'kind': 'nonsense'}))


class TestACompletedTripIsNeverStrandedByAMissingRule:
    def test_settle_or_defer_leaves_the_trip_unsettled_and_writes_nothing(self, world):
        from servers.driver.utils import settle_or_defer

        driver = make_driver(world.geo, 'IN-TG-HYD', 'X1', currency='INR', calling='91')
        trip = make_trip(world.hyd, make_rider('X1', '91'), driver, fare='1000.00')
        assert settle_or_defer(trip) is False
        assert not TripSettlement.objects.filter(trip=trip).exists()
        make_rule('IN', '15.00', node=world.geo['IN'])
        assert settle_or_defer(trip) is True                      # once a rule exists it settles
        assert TripSettlement.objects.filter(trip=trip).count() == 1
        assert settle_or_defer(trip) is True                      # and stays idempotent
        assert TripSettlement.objects.filter(trip=trip).count() == 1
