"""Rider scope (multi-anchor), per-capability rider actions, block sign-out.

WHAT THESE PROVE
----------------
  * a rider is anchored at every city ridden in within the window, plus a
    home city, else the phone's country (+91 IN, +1 US, unknown prefix:
    unrestricted callers only); a caller sees a rider when ANY anchor is in
    scope; a trip-less rider is findable by every `rider.read` holder
    scoped anywhere in their country;
  * one rule serves the list, the page, search and reveal: for every role,
    every rider search returns opens with 200 (never a 404);
  * the role templates carry the owner's action split, and each rider write
    route needs its own capability;
  * a block signs the rider out at once, or at trip end when a trip is under
    way, once; SOS works for a blocked rider; ride requests stay refused.
"""

from datetime import timedelta

import pytest
from django.test import override_settings
from django.utils import timezone

from servers.admin_access.models import AdminRole
from servers.admin_access.roles import ROLE_TEMPLATES, SUPER_ADMIN, validate_templates
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider, RiderBlock
from servers.rider.scope import riders_visible_to
from test_riders_module import (  # noqa: F401 - fixtures and helpers
    LIST, _trip, ids, url,
)
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _client_for, _rider, _zone, geo, operator,
)

pytestmark = pytest.mark.django_db

SEARCH = '/api/v1/search/admin/riders/?q=Zed'
REVEAL = '/api/v1/admin-access/reveal/'
AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}


@pytest.fixture
def zones(geo):
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON,
                currency='USD')
    return {'hyd': hyd, 'aus': aus}


@pytest.fixture
def riders(geo, zones):
    now = timezone.now()
    r = {}
    r['hyd'] = _rider('+919876500001', 'Zed Hyd')
    _trip(r['hyd'], zones['hyd'], when=now - timedelta(days=5))
    r['aus'] = _rider('+15125500002', 'Zed Aus')
    _trip(r['aus'], zones['aus'], when=now - timedelta(days=5))
    r['both'] = _rider('+919876500003', 'Zed Both')
    _trip(r['both'], zones['hyd'], when=now - timedelta(days=200))
    _trip(r['both'], zones['aus'], when=now - timedelta(days=3))
    r['stale'] = _rider('+919876500004', 'Zed Stale')  # only trip is 400 days old
    _trip(r['stale'], zones['aus'], when=now - timedelta(days=400))
    r['new_in'] = _rider('+919876500005', 'Zed Newin')  # no trips, +91
    r['new_us'] = _rider('+15125500006', 'Zed Newus')  # no trips, +1
    r['nowhere'] = _rider('+447700900007', 'Zed Nowhere')  # no trips, unknown prefix
    return r


def visible(client_and_user, riders):
    client, _ = client_and_user
    return {k for k, u in riders.items() if u.id in set(ids(client.get(LIST)))}


class TestMultiAnchorScope:
    def test_hyderabad_sees_every_hyderabad_anchor_and_indian_newcomers(self, geo, riders):
        # `both` rode in Hyderabad 200 days ago (inside 12 months) and in
        # Austin 3 days ago: Hyderabad still sees them. `stale`'s only trip
        # is out of the window, so they fall back to the country (IN).
        got = visible(operator('city_ops_manager', geo), riders)
        assert got == {'hyd', 'both', 'stale', 'new_in'}

    def test_austin_sees_austin_anchors_and_us_newcomers(self, geo, riders):
        got = visible(operator('city_ops_manager', geo, 'US-TX-AUS'), riders)
        assert got == {'aus', 'both', 'new_us'}

    def test_country_scope_and_global(self, geo, riders):
        assert visible(operator('support_agent', geo, 'IN'), riders) == {
            'hyd', 'both', 'stale', 'new_in'}
        everyone = visible(operator('super_admin', geo), riders)
        assert everyone == set(riders)  # including the unknown-prefix rider

    def test_unknown_prefix_is_global_only(self, geo, riders):
        for role, scope in (('support_agent', 'IN-TG-HYD'), ('support_agent', 'IN'),
                            ('support_agent', 'US-TX-AUS')):
            client, _ = operator(role, geo, scope)
            assert client.get(url(riders['nowhere'])).status_code == 404

    def test_the_window_is_a_setting(self, geo, riders):
        client = operator('city_ops_manager', geo, 'US-TX-AUS')
        with override_settings(RIDER_SCOPE_TRIP_MONTHS=18):
            # `stale` rode in Austin 400 days ago: inside 18 months.
            assert 'stale' in visible(client, riders)
        assert 'stale' not in visible(client, riders)

    def test_a_home_city_is_an_anchor(self, geo, riders):
        Rider.objects.filter(user_id=riders['hyd']).update(home_city=geo['US-TX-AUS'])
        hyd = operator('city_ops_manager', geo)
        aus = operator('city_ops_manager', geo, 'US-TX-AUS')
        assert 'hyd' in visible(hyd, riders) and 'hyd' in visible(aus, riders)
        # A trip-less rider with a home city is anchored THERE, not at the country.
        Rider.objects.filter(user_id=riders['new_in']).update(home_city=geo['US-TX-AUS'])
        assert 'new_in' not in visible(hyd, riders) and 'new_in' in visible(aus, riders)

    def test_scope_narrowing_applies_on_top(self, geo, riders):
        admin = operator('super_admin', geo)[0]
        got = set(ids(admin.get(LIST + '?scope=US-TX-AUS')))
        assert got == {riders[k].id for k in ('aus', 'both', 'new_us')}

    def test_the_detail_names_the_anchors(self, geo, riders):
        client, _ = operator('city_ops_manager', geo)
        data = client.get(url(riders['both'])).json()['data']
        assert {a['code'] for a in data['anchors']} == {'IN-TG-HYD', 'US-TX-AUS'}
        data = client.get(url(riders['new_in'])).json()['data']
        assert [a['code'] for a in data['anchors']] == ['IN']
        assert data['city_source'] == 'country'

    def test_the_trip_anchor_is_one_indexed_exists(self, geo, riders):
        qs = riders_visible_to(operator('city_ops_manager', geo)[1])
        sql = str(qs.query)
        assert 'EXISTS' in sql and 'requested_at' in sql


    @pytest.mark.postgres
    def test_postgresql_probes_trips_by_index(self, geo, riders):
        """The trip anchor is a correlated EXISTS: it must be answerable from
        the trip table's indexes rather than a scan per rider."""
        from django.db import connection

        qs = riders_visible_to(operator('city_ops_manager', geo)[1])
        with connection.cursor() as cursor:
            cursor.execute('SET enable_seqscan = off')
            try:
                sql, params = qs.query.sql_with_params()
                cursor.execute('EXPLAIN ' + sql, params)
                plan = ' '.join(row[0] for row in cursor.fetchall())
            finally:
                cursor.execute('RESET enable_seqscan')
        assert 'Seq Scan on ride_trip' not in plan, plan


ROLES = [t['code'] for t in ROLE_TEMPLATES]


class TestOneRuleEverywhere:
    @pytest.mark.parametrize('scope', ['IN-TG-HYD', 'IN', 'US-TX-AUS', 'US'])
    @pytest.mark.parametrize('role', [r for r in ROLES if r != SUPER_ADMIN])
    def test_every_rider_search_returns_opens(self, geo, riders, role, scope):
        client, _ = operator(role, geo, scope)
        search = client.get(SEARCH)
        if search.status_code == 403:  # a role without rider.read
            assert client.get(LIST).status_code == 403
            return
        found = [row['user']['id'] for row in search.json()['data']['results']]
        listed = set(ids(client.get(LIST)))
        assert listed, (role, scope)  # every scope holds at least one rider
        for user_id in found:
            assert client.get(f'{LIST}{user_id}/').status_code == 200, (role, scope, user_id)
            assert user_id in listed
        # And nothing the list shows is missing from search.
        assert set(found) == listed

    def test_super_admin_search_opens_everyone(self, geo, riders):
        client, _ = operator('super_admin', geo)
        found = [row['user']['id'] for row in client.get(SEARCH).json()['data']['results']]
        assert len(found) == len(riders)
        assert all(client.get(f'{LIST}{i}/').status_code == 200 for i in found)

    def test_search_hides_what_the_page_would_404(self, geo, riders):
        client, _ = operator('support_agent', geo)
        found = {row['user']['id'] for row in client.get(SEARCH).json()['data']['results']}
        assert riders['hyd'].id in found and riders['new_in'].id in found
        assert riders['aus'].id not in found and riders['nowhere'].id not in found
        assert client.get(url(riders['aus'])).status_code == 404

    def _reveal(self, client, user):
        return client.post(REVEAL, {'target_type': 'rider', 'target_id': user.id,
                                    'fields': ['phone_number'], 'reason': 'support call'},
                           format='json')

    def test_reveal_follows_the_same_scope(self, geo, riders):
        client, _ = operator('safety_officer', geo)  # holds pii.view at Hyderabad
        assert self._reveal(client, riders['hyd']).status_code == 200
        assert self._reveal(client, riders['both']).status_code == 200
        assert self._reveal(client, riders['new_in']).status_code == 200  # country anchor
        assert self._reveal(client, riders['aus']).status_code == 404
        assert self._reveal(client, riders['nowhere']).status_code == 404
        user_target = client.post(REVEAL, {'target_type': 'user', 'target_id': riders['aus'].id,
                                           'fields': ['phone_number']}, format='json')
        assert user_target.status_code == 404

    def test_reveal_needs_pii_view_at_an_anchor(self, geo, riders):
        client, _ = operator('support_agent', geo)  # no pii.view at all
        assert self._reveal(client, riders['hyd']).status_code == 403


RIDER_ACTIONS = ('rider.note', 'rider.flag', 'rider.signout', 'rider.block')
EXPECTED = {
    'support_agent': {'rider.note', 'rider.flag', 'rider.signout'},
    'safety_officer': set(RIDER_ACTIONS),
    'city_ops_manager': set(RIDER_ACTIONS),
    'finance': {'rider.note'},
    'kyc_verifier': set(),
    'marketing': set(),
    'analyst': set(),
}


class TestRoleTemplates:
    def test_the_templates_are_valid(self):
        assert validate_templates() == []

    @pytest.mark.parametrize('code', sorted(EXPECTED))
    def test_the_action_split(self, code):
        template = next(t for t in ROLE_TEMPLATES if t['code'] == code)
        assert set(template['permissions']) & set(RIDER_ACTIONS) == EXPECTED[code]
        assert 'rider.write' not in template['permissions']

    def test_super_admin_holds_everything(self):
        template = next(t for t in ROLE_TEMPLATES if t['code'] == SUPER_ADMIN)
        assert set(RIDER_ACTIONS) <= set(template['permissions'])
        holders = {t['code'] for t in ROLE_TEMPLATES if 'rider.write' in t['permissions']}
        assert holders == {SUPER_ADMIN}


ROUTES = {
    'rider.note': ('note/', {'body': 'called'}),
    'rider.flag': ('flag/', {'reason': 'fraud_suspicion'}),
    'rider.signout': ('sign-out/', {'reason': 'lost phone'}),
    'rider.block': ('block/', {'reason_code': 'fraud', 'note': 'x'}),
}


class TestEachRouteHasItsOwnCapability:
    @pytest.mark.parametrize('role', sorted(EXPECTED))
    def test_the_matrix(self, geo, riders, role):
        client, _ = operator(role, geo)
        rider = riders['hyd']
        for capability, (suffix, body) in ROUTES.items():
            response = client.post(url(rider, suffix), body, format='json')
            if capability in EXPECTED[role]:
                assert response.status_code in (200, 201), (role, capability, response.content)
            else:
                assert response.status_code == 403, (role, capability)

    def test_unblock_needs_block_and_unflag_needs_flag(self, geo, riders):
        support, _ = operator('support_agent', geo)
        assert support.post(url(riders['hyd'], 'unblock/'), {'note': 'x'},
                            format='json').status_code == 403
        assert support.post(url(riders['hyd'], 'unflag/'), {},
                            format='json').status_code == 200
        finance, _ = operator('finance', geo)
        assert finance.post(url(riders['hyd'], 'unflag/'), {},
                            format='json').status_code == 403

    def test_the_page_offers_only_what_the_caller_may_do(self, geo, riders):
        for role in ('support_agent', 'finance', 'city_ops_manager', 'analyst'):
            client, _ = operator(role, geo)
            actions = client.get(url(riders['hyd'])).json()['data']['actions']
            held = EXPECTED[role]
            assert actions['note'] == ('rider.note' in held)
            assert actions['flag'] == ('rider.flag' in held)
            assert actions['sign_out'] == ('rider.signout' in held)
            assert actions['block'] == ('rider.block' in held)

    def test_a_country_scoped_support_agent_acts_on_a_newcomer(self, geo, riders):
        client, _ = operator('support_agent', geo)
        assert client.post(url(riders['new_in'], 'note/'), {'body': 'cannot book'},
                           format='json').status_code == 201

    def test_the_action_needs_the_capability_at_the_riders_anchor(self, geo, riders):
        # Note in Hyderabad, read-only in Austin: `both` has both anchors
        # (the Hyderabad one holds), `aus` has only the Austin one.
        role, _ = AdminRole.objects.get_or_create(
            code='split', defaults={'name': 'Split', 'permissions': ['rider.read', 'rider.note']})
        read_only, _ = AdminRole.objects.get_or_create(
            code='readonly_r', defaults={'name': 'RO', 'permissions': ['rider.read']})
        client, user = operator('analyst', geo, 'IN-TG-HYD')
        from servers.admin_access.models import AdminAssignment
        AdminAssignment.objects.filter(admin=user).delete()
        AdminAssignment.objects.create(admin=user, role=role, scope=geo['IN-TG-HYD'])
        AdminAssignment.objects.create(admin=user, role=read_only, scope=geo['US-TX-AUS'])
        assert client.post(url(riders['both'], 'note/'), {'body': 'x'},
                           format='json').status_code == 201
        assert client.post(url(riders['aus'], 'note/'), {'body': 'x'},
                           format='json').status_code == 403


def _tokens(user, n=2):
    from rest_framework_simplejwt.tokens import RefreshToken

    for _ in range(n):
        RefreshToken.for_user(user)


def _live(user):
    from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

    return OutstandingToken.objects.filter(
        user=user, blacklistedtoken__isnull=True).count()


def _end(trip, code='completed'):
    trip.status_id = TripStatus.objects.get_or_create(status_code=code)[0]
    trip.save()


def _block(client, user):
    return client.post(url(user, 'block/'), {'reason_code': 'fraud', 'note': 'x'},
                       format='json')


class TestBlockSignsOut:
    def test_block_with_no_trip_signs_out_at_once(self, geo, riders):
        client, _ = operator('city_ops_manager', geo)
        rider = riders['new_in']
        _tokens(rider)
        assert _block(client, rider).status_code == 201
        assert _live(rider) == 0
        from test_riders_module import audit_rows
        assert audit_rows(rider, 'rider_signed_out').count() == 1

    @pytest.mark.parametrize('ending', ['completed', 'cancelled'])
    def test_block_mid_trip_waits_for_the_trip_to_end(self, geo, zones, riders, ending):
        client, _ = operator('city_ops_manager', geo)
        rider = riders['hyd']
        trip = _trip(rider, zones['hyd'], code='in_progress')
        _tokens(rider)
        assert _block(client, rider).status_code == 201
        assert _live(rider) == 2  # still signed in: a trip is under way
        assert RiderBlock.objects.get(rider__user_id=rider, lifted_at=None).signout_pending
        _end(trip, ending)
        assert _live(rider) == 0
        assert not RiderBlock.objects.get(rider__user_id=rider, lifted_at=None).signout_pending
        from test_riders_module import audit_rows
        assert audit_rows(rider, 'rider_signed_out').count() == 1
        # Idempotent: more saves, and a later session, are left alone.
        _tokens(rider, 1)
        trip.save()
        assert _live(rider) == 1

    @pytest.mark.parametrize('active', ['requested', 'accepted', 'reached', 'in_progress'])
    def test_every_active_status_defers(self, geo, zones, riders, active):
        client, _ = operator('city_ops_manager', geo)
        rider = riders['hyd']
        _trip(rider, zones['hyd'], code=active)
        _tokens(rider, 1)
        _block(client, rider)
        assert _live(rider) == 1

    def test_unblocking_before_the_trip_ends_cancels_the_sign_out(self, geo, zones, riders):
        client, _ = operator('city_ops_manager', geo)
        rider = riders['hyd']
        trip = _trip(rider, zones['hyd'], code='in_progress')
        _tokens(rider, 1)
        _block(client, rider)
        client.post(url(rider, 'unblock/'), {'note': 'mistake'}, format='json')
        _end(trip)
        assert _live(rider) == 1

    def test_another_active_trip_keeps_the_sign_out_waiting(self, geo, zones, riders):
        client, _ = operator('city_ops_manager', geo)
        rider = riders['hyd']
        first = _trip(rider, zones['hyd'], code='in_progress')
        second = _trip(rider, zones['hyd'], code='requested')
        _tokens(rider, 1)
        _block(client, rider)
        _end(first)
        assert _live(rider) == 1
        _end(second, 'cancelled')
        assert _live(rider) == 0

    def test_bulk_block_signs_out_too(self, geo, riders):
        from servers.approvals.actions import BULK_ACTION  # noqa: F401
        client, _ = operator('city_ops_manager', geo)
        rider = riders['new_in']
        _tokens(rider, 1)
        from django.core.management import call_command
        from io import StringIO
        from conftest import seed_actor
        call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())
        response = client.post('/api/v1/rider/admin/bulk/', {
            'action': 'block', 'reason_code': 'fraud', 'idempotency_key': 'k1',
            'rider_ids': [rider.id]}, format='json')
        assert response.status_code == 200, response.content
        assert _live(rider) == 0


class TestSosAndRequestsForABlockedRider:
    def test_a_blocked_rider_mid_trip_can_raise_sos(self, geo, zones, riders):
        from rest_framework.test import APIClient

        from servers.sos.models import SOSEvent

        ops, _ = operator('city_ops_manager', geo)
        rider = riders['hyd']
        trip = _trip(rider, zones['hyd'], code='in_progress')
        assert _block(ops, rider).status_code == 201
        api = _client_for(rider)  # signed in: the sign-out is waiting for trip end
        response = api.post('/api/v1/sos/', {'trip_id': trip.id, 'event_type': 'panic'},
                            format='json')
        assert response.status_code in (200, 201), response.content
        assert SOSEvent.objects.filter(user=rider, trip=trip).count() == 1
        # And again after the trip ended and the rider was signed out: an
        # access token still in hand keeps working for SOS (no block check).
        _end(trip)
        again = api.post('/api/v1/sos/', {'event_type': 'panic'}, format='json')
        assert again.status_code in (200, 201), again.content

    def test_ride_requests_stay_refused(self, geo, riders):
        from servers.rider.eligibility import may_request_rides

        ops, _ = operator('city_ops_manager', geo)
        assert may_request_rides(riders['hyd'])
        _block(ops, riders['hyd'])
        assert not may_request_rides(riders['hyd'])
