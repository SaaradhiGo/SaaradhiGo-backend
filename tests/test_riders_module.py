"""Riders (console module 4): list, detail and the writes.

WHAT THESE PROVE
----------------
  * a rider's city is their latest trip's: Hyderabad operators see riders
    whose latest trip is in Hyderabad, Austin riders (and a Hyderabad rider
    who last rode in Austin) are 404 to them, Super Admin sees everybody
    including riders with no trip; a refused `?scope=` is 403; a role
    without `rider.read` is 403;
  * every filter, the sort and the pages work server-side, and the joined
    range is each rider's own city day;
  * the detail carries profile, wallet (minor units, per currency), trips,
    tickets, SOS, ratings, counts, history and actions, and never a raw
    phone number or email;
  * each write: `rider.write` (403 without), out of scope 404, audited,
    idempotent;
  * a blocked rider is refused at ride request in the socket's existing
    error frame and no trip is created; unblocking lets them book again;
  * bulk over 50 needs a checker, and the checker's approval executes it;
  * no migration is missing.
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from conftest import seed_actor
from servers.admin_access.models import AdminRole
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.ride.models import Rating, Trip, TripStatus
from servers.rider.eligibility import BLOCKED_MESSAGE, may_request_rides
from servers.rider.models import (
    FavoritePlace, Rider, RiderBlock, RiderFlag, RiderNote, Wallet, WalletTransaction,
)
from test_sos_support_zones_matrix import (  # noqa: F401 - fixtures
    _client_for, _rider, _sos, _ticket, _zone, geo, operator,
)

pytestmark = pytest.mark.django_db

LIST = '/api/v1/rider/admin/'
BULK = '/api/v1/rider/admin/bulk/'
APPROVALS = '/api/v1/approvals/admin/requests/'

HYD_PHONE = '+919876533301'
HYD_EMAIL = 'lakshmi.devi@example.test'
AUS_PHONE = '+15125533301'
AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[-97.85, 30.15], [-97.60, 30.15], [-97.60, 30.40],
                     [-97.85, 30.40], [-97.85, 30.15]]],
}


def url(user, suffix=''):
    return f'{LIST}{user.id}/{suffix}'


def ids(response):
    assert response.status_code == 200, response.content
    return [row['id'] for row in response.json()['data']['results']]


def audit_rows(user, action):
    return AdminAuditLog.objects.filter(target_type='rider', target_id=str(user.id),
                                        action=action)


def _trip(user, zone, code='completed', when=None):
    status, _ = TripStatus.objects.get_or_create(status_code=code)
    trip = Trip.objects.create(
        user_id=user, status_id=status, zone=zone,
        pickup_lat=Decimal('17.4'), pickup_long=Decimal('78.4'),
        destination_lat=Decimal('17.5'), destination_long=Decimal('78.5'),
        estimated_fare_minor=12_500, currency=zone.currency if hasattr(zone, 'currency') else 'INR')
    if when is not None:
        Trip.objects.filter(pk=trip.pk).update(requested_at=when)
    return trip


def _joined(user, when):
    type(user).objects.filter(pk=user.pk).update(created_at=when)


@pytest.fixture
def policies(geo):
    call_command('seed_approval_policies', actor_email=seed_actor().email, stdout=StringIO())


@pytest.fixture
def rider_ops(geo):
    """Nobody but Super Admin holds `rider.write` in the templates (an open
    owner question), so the scoped writer is a custom Hyderabad role."""
    AdminRole.objects.get_or_create(code='rider_ops', defaults={
        'name': 'Rider Ops', 'permissions': ['rider.read', 'rider.write', 'trip.read']})
    return operator('rider_ops', geo)


@pytest.fixture
def world(geo):
    hyd = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
                timezone_name='America/Chicago', polygon_geojson=AUSTIN_POLYGON,
                currency='USD')
    now = timezone.now()

    lakshmi = _rider(HYD_PHONE, 'Lakshmi Devi', email=HYD_EMAIL)
    first = _trip(lakshmi, hyd, when=now - timedelta(days=20))
    _trip(lakshmi, hyd, when=now - timedelta(days=10))
    _trip(lakshmi, hyd, when=now - timedelta(days=2))
    _ticket(lakshmi, first)
    _sos(lakshmi, first)
    Rating.objects.create(trip_id=first, rater_id=lakshmi, score=4)
    FavoritePlace.objects.create(user_id=lakshmi, address_text='Home, Banjara Hills',
                                 latitude=Decimal('17.41'), longitude=Decimal('78.44'))
    Wallet.objects.create(user_id=lakshmi, scope=Wallet.SCOPE_RIDER, currency='INR',
                          balance_minor=45_050)
    WalletTransaction.objects.create(user_id=lakshmi, amount_minor=50_000, currency='INR',
                                     txn_type='credit', status='completed', purpose='top-up')
    WalletTransaction.objects.create(user_id=lakshmi, amount_minor=4_950, currency='INR',
                                     txn_type='debit', status='completed', purpose='trip')

    ravi = _rider('+919876533302', 'Ravi Kumar')
    _trip(ravi, hyd, code='cancelled', when=now - timedelta(days=1))

    traveller = _rider('+919876533303', 'Priya Traveller')
    _trip(traveller, hyd, when=now - timedelta(days=30))
    _trip(traveller, aus, when=now - timedelta(days=3))

    austin = _rider(AUS_PHONE, 'Austin Rider')
    _trip(austin, aus, when=now - timedelta(days=4))

    newcomer = _rider('+919876533304', 'New Rider')  # no trip: no city

    Rider.objects.filter(user_id=ravi).update(flagged_for_review=True, rating=Decimal('2.40'))
    return {'lakshmi': lakshmi, 'ravi': ravi, 'traveller': traveller, 'austin': austin,
            'newcomer': newcomer, 'hyd': hyd, 'aus': aus}


HYD = ('lakshmi', 'ravi')


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

class TestScope:
    def test_city_ops_sees_riders_whose_latest_trip_is_in_hyderabad(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert set(ids(client.get(LIST))) == {world[k].id for k in HYD}

    def test_super_admin_sees_everybody_including_riders_with_no_trip(self, geo, world):
        admin, _ = operator('super_admin', geo)
        assert set(ids(admin.get(LIST))) == {world[k].id for k in (
            'lakshmi', 'ravi', 'traveller', 'austin', 'newcomer')}
        assert set(ids(admin.get(LIST + '?scope=US-TX-AUS'))) == {
            world['traveller'].id, world['austin'].id}

    def test_a_scope_the_caller_does_not_hold_is_refused(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(LIST + '?scope=US-TX-AUS')
        assert response.status_code == 403
        assert response.json()['error']['code'] == 'SCOPE_NOT_HELD'

    @pytest.mark.parametrize('key', ['austin', 'traveller', 'newcomer'])
    def test_out_of_scope_detail_is_404(self, geo, world, key):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(url(world[key]))
        assert response.status_code == 404
        assert response.json()['error']['code'] == 'NOT_FOUND'

    def test_no_such_rider_is_the_same_404(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        assert client.get(f'{LIST}999999/').status_code == 404

    @pytest.mark.parametrize('role', ['marketing', 'kyc_verifier'])
    def test_a_role_without_rider_read_is_403(self, geo, world, role):
        client, _ = operator(role, geo)
        assert client.get(LIST).status_code == 403
        assert client.get(url(world['lakshmi'])).status_code == 403

    @pytest.mark.parametrize('role', ['support_agent', 'safety_officer', 'finance', 'analyst'])
    def test_every_rider_read_role_reads_hyderabad(self, geo, world, role):
        client, _ = operator(role, geo)
        assert set(ids(client.get(LIST))) == {world[k].id for k in HYD}
        assert client.get(url(world['lakshmi'])).status_code == 200

    def test_a_rider_token_is_refused(self, world):
        client = _client_for(world['lakshmi'])
        assert client.get(LIST).status_code in (401, 403)


# ---------------------------------------------------------------------------
# Filters, sort, pages
# ---------------------------------------------------------------------------

class TestList:
    @pytest.mark.parametrize('query, expected', [
        ('trips_min=3', ('lakshmi',)),
        ('trips_max=0', ('ravi',)),
        ('has_open_ticket=true', ('lakshmi',)),
        ('has_open_ticket=false', ('ravi',)),
        ('had_sos=true', ('lakshmi',)),
        ('low_rating=true', ('ravi',)),
        ('blocked=false', ('lakshmi', 'ravi')),
        ('flagged=true', ()),
    ])
    def test_filters(self, geo, world, query, expected):
        client, _ = operator('city_ops_manager', geo)
        assert set(ids(client.get(f'{LIST}?{query}'))) == {world[k].id for k in expected}

    def test_last_trip_range(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        today = timezone.now().date()
        query = f'last_trip_from={today - timedelta(days=1)}&last_trip_to={today}'
        assert ids(client.get(f'{LIST}?{query}')) == [world['ravi'].id]

    def test_blocked_and_flagged_filters_follow_the_writes(self, geo, world, rider_ops):
        client, _ = rider_ops
        assert client.post(url(world['ravi'], 'block/'), {'reason_code': 'abuse'},
                           format='json').status_code == 201
        assert client.post(url(world['lakshmi'], 'flag/'), {'reason': 'promo_abuse'},
                           format='json').status_code == 201
        assert ids(client.get(f'{LIST}?blocked=true')) == [world['ravi'].id]
        assert ids(client.get(f'{LIST}?flagged=true')) == [world['lakshmi'].id]

    def test_joined_range_is_each_riders_own_city_day(self, geo, world):
        # 2026-10-01 20:00 UTC is 02 Oct 01:30 in Hyderabad and 01 Oct 15:00 in Austin.
        moment = datetime(2026, 10, 1, 20, 0, tzinfo=dt_timezone.utc)
        _joined(world['ravi'], moment)
        _joined(world['austin'], moment)
        admin, _ = operator('super_admin', geo)
        assert ids(admin.get(f'{LIST}?joined_from=2026-10-02&joined_to=2026-10-02')) == [
            world['ravi'].id]
        assert ids(admin.get(f'{LIST}?joined_from=2026-10-01&joined_to=2026-10-01')) == [
            world['austin'].id]

    @pytest.mark.parametrize('query', ['trips_min=x', 'trips_min=-1', 'trips_min=5&trips_max=1',
                                       'blocked=maybe', 'ordering=name',
                                       'joined_from=2026/10/01',
                                       'last_trip_from=2026-10-05&last_trip_to=2026-10-01'])
    def test_a_malformed_filter_is_400(self, geo, world, query):
        client, _ = operator('city_ops_manager', geo)
        assert client.get(f'{LIST}?{query}').status_code == 400

    def test_sort_and_pages(self, geo, world):
        admin, _ = operator('super_admin', geo)
        assert ids(admin.get(f'{LIST}?ordering=-trips'))[0] == world['lakshmi'].id
        assert ids(admin.get(f'{LIST}?ordering=-last_trip'))[0] == world['ravi'].id
        # A rider with no trip sorts last either way.
        assert ids(admin.get(f'{LIST}?ordering=last_trip'))[-1] == world['newcomer'].id
        page = admin.get(f'{LIST}?ordering=id&page_size=2&page=2').json()['data']
        assert page['count'] == 5 and page['total_pages'] == 3 and page['page'] == 2
        everyone = sorted(world[k].id for k in ('lakshmi', 'ravi', 'traveller', 'austin',
                                                'newcomer'))
        assert [r['id'] for r in page['results']] == everyone[2:4]

    def test_rows_carry_city_counts_and_mask_contact_details(self, geo, world):
        client, _ = operator('support_agent', geo)
        response = client.get(f'{LIST}?id={world["lakshmi"].id}')
        row = response.json()['data']['results'][0]
        assert row['city'] == {'code': 'IN-TG-HYD', 'name': row['city']['name'],
                               'timezone': 'Asia/Kolkata'}
        assert row['trips_completed'] == 3 and row['trips_total'] == 3
        assert row['has_open_ticket'] and row['had_sos'] and not row['blocked']
        body = response.content.decode()
        assert HYD_PHONE not in body and HYD_EMAIL not in body
        assert row['user_details']['phone_number'] != HYD_PHONE


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------

class TestDetail:
    def test_the_detail_sections(self, geo, world):
        client, _ = operator('city_ops_manager', geo)
        response = client.get(url(world['lakshmi']))
        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['city']['code'] == 'IN-TG-HYD' and data['city_source'] == 'latest_trip'
        assert data['wallet']['balances'] == [{'currency': 'INR', 'balance_minor': 45_050}]
        inr = data['wallet']['transactions']['INR']
        assert {t['amount_minor'] for t in inr} == {50_000, 4_950}
        assert all(isinstance(t['amount_minor'], int) for t in inr)
        assert data['trips']['total'] == 3 and data['trips']['by_status'] == {'completed': 3}
        assert data['trips']['recent'][0]['fare_minor'] == 12_500
        assert data['tickets']['open'] == 1 and data['sos']['total'] == 1
        assert data['ratings_summary']['given'] == {'count': 1, 'average': 4.0}
        assert data['saved_places_count'] == 1
        assert data['sessions'] == {'active_sessions': 0, 'push_device': False}
        assert data['block'] == {'active': False} and data['flags'] == []
        actions = data['actions']
        # City Ops holds rider.read, not rider.write.
        assert not any(actions[k] for k in ('block', 'unblock', 'note', 'flag', 'sign_out'))
        assert actions['reveal'] is True

    @pytest.mark.parametrize('role', ['support_agent', 'analyst', 'city_ops_manager'])
    def test_no_raw_phone_or_email_for_any_role(self, geo, world, role):
        client, _ = operator(role, geo)
        body = client.get(url(world['lakshmi'])).content.decode()
        assert HYD_PHONE not in body and HYD_EMAIL not in body
        assert 'Banjara Hills' not in body  # saved places are a count only

    def test_super_admin_actions_and_the_audit_trail(self, geo, world):
        admin, _ = operator('super_admin', geo)
        admin.post(url(world['newcomer'], 'note/'), {'body': 'called in'}, format='json')
        data = admin.get(url(world['newcomer'])).json()['data']
        assert data['city'] is None
        assert data['actions']['block'] and data['actions']['sign_out']
        assert [n['body'] for n in data['notes']] == ['called in']
        assert [a['action'] for a in data['audit_trail']] == ['rider_note_added']


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

WRITES = [
    ('block/', {'reason_code': 'fraud', 'note': 'chargebacks'}),
    ('unblock/', {'note': 'cleared'}),
    ('note/', {'body': 'x'}),
    ('flag/', {'reason': 'fraud_suspicion'}),
    ('unflag/', {}),
    ('sign-out/', {'reason': 'account takeover'}),
]


class TestWrites:
    @pytest.mark.parametrize('suffix, body', WRITES)
    def test_rider_read_alone_is_403(self, geo, world, suffix, body):
        client, _ = operator('city_ops_manager', geo)
        response = client.post(url(world['lakshmi'], suffix), body, format='json')
        assert response.status_code == 403

    @pytest.mark.parametrize('suffix, body', WRITES)
    def test_out_of_scope_is_404(self, geo, world, rider_ops, suffix, body):
        client, _ = rider_ops
        for key in ('austin', 'traveller', 'newcomer'):
            assert client.post(url(world[key], suffix), body,
                               format='json').status_code == 404

    def test_block_is_audited_and_idempotent(self, geo, world, rider_ops):
        client, _ = rider_ops
        rider = world['lakshmi']
        first = client.post(url(rider, 'block/'), {'reason_code': 'fraud', 'note': 'x'},
                            format='json')
        assert first.status_code == 201 and first.json()['data']['created'] is True
        again = client.post(url(rider, 'block/'), {'reason_code': 'fraud'}, format='json')
        assert again.status_code == 200 and again.json()['data']['created'] is False
        assert RiderBlock.objects.filter(rider__user_id=rider, lifted_at__isnull=True).count() == 1
        assert audit_rows(rider, 'rider_blocked').count() == 1
        detail = client.get(url(rider)).json()['data']
        assert detail['block']['active'] and detail['block']['reason_code'] == 'fraud'
        assert [h['action'] for h in detail['status_history']] == ['rider_blocked']
        assert detail['actions']['unblock'] and not detail['actions']['block']

    def test_block_needs_a_known_reason_and_other_needs_a_note(self, geo, world, rider_ops):
        client, _ = rider_ops
        for body in ({}, {'reason_code': 'lost'}, {'reason_code': 'other'}):
            assert client.post(url(world['lakshmi'], 'block/'), body,
                               format='json').status_code == 400
        assert not RiderBlock.objects.exists()

    def test_unblock_is_audited_and_idempotent(self, geo, world, rider_ops):
        client, _ = rider_ops
        rider = world['lakshmi']
        client.post(url(rider, 'block/'), {'reason_code': 'abuse'}, format='json')
        assert client.post(url(rider, 'unblock/'), {}, format='json').status_code == 400
        first = client.post(url(rider, 'unblock/'), {'note': 'apologised'}, format='json')
        assert first.json()['data']['changed'] is True
        again = client.post(url(rider, 'unblock/'), {'note': 'again'}, format='json')
        assert again.status_code == 200 and again.json()['data']['changed'] is False
        assert audit_rows(rider, 'rider_unblocked').count() == 1

    def test_note_is_audited_and_idempotent_by_key(self, geo, world, rider_ops):
        client, _ = rider_ops
        rider = world['lakshmi']
        body = {'body': 'asked about a refund', 'idempotency_key': 'n1'}
        assert client.post(url(rider, 'note/'), body, format='json').status_code == 201
        again = client.post(url(rider, 'note/'), body, format='json')
        assert again.status_code == 200 and again.json()['data']['created'] is False
        assert RiderNote.objects.count() == 1
        assert audit_rows(rider, 'rider_note_added').count() == 1

    def test_flag_and_unflag_are_audited_and_idempotent(self, geo, world, rider_ops):
        client, _ = rider_ops
        rider = world['lakshmi']
        assert client.post(url(rider, 'flag/'), {'reason': 'bogus'},
                           format='json').status_code == 400
        assert client.post(url(rider, 'flag/'), {'reason': 'payment_risk'},
                           format='json').status_code == 201
        assert client.post(url(rider, 'flag/'), {'reason': 'payment_risk'},
                           format='json').status_code == 200
        assert RiderFlag.objects.filter(cleared_at__isnull=True).count() == 1
        assert audit_rows(rider, 'rider_flagged').count() == 1
        assert client.post(url(rider, 'unflag/'), {'note': 'ok'},
                           format='json').json()['data']['cleared'] == 1
        assert client.post(url(rider, 'unflag/'), {},
                           format='json').json()['data']['cleared'] == 0
        assert audit_rows(rider, 'rider_unflagged').count() == 1

    def test_sign_out_revokes_refresh_tokens_once(self, geo, world, rider_ops):
        from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
        from rest_framework_simplejwt.tokens import RefreshToken

        client, _ = rider_ops
        rider = world['lakshmi']
        RefreshToken.for_user(rider)
        RefreshToken.for_user(rider)
        assert client.get(url(rider)).json()['data']['sessions']['active_sessions'] == 2
        assert client.post(url(rider, 'sign-out/'), {}, format='json').status_code == 400
        first = client.post(url(rider, 'sign-out/'), {'reason': 'lost phone'}, format='json')
        assert first.json()['data'] == {'rider_id': rider.id, 'revoked': 2, 'changed': True}
        assert BlacklistedToken.objects.filter(token__user=rider).count() == 2
        again = client.post(url(rider, 'sign-out/'), {'reason': 'again'}, format='json')
        assert again.json()['data']['changed'] is False
        assert audit_rows(rider, 'rider_signed_out').count() == 1
        assert client.get(url(rider)).json()['data']['sessions']['active_sessions'] == 0

    def test_nothing_here_moves_money(self, geo, world, rider_ops):
        client, _ = rider_ops
        rider = world['lakshmi']
        before = list(WalletTransaction.objects.values_list('id', flat=True))
        client.post(url(rider, 'block/'), {'reason_code': 'payment'}, format='json')
        client.post(url(rider, 'sign-out/'), {'reason': 'x'}, format='json')
        assert list(WalletTransaction.objects.values_list('id', flat=True)) == before
        assert Wallet.objects.get(user_id=rider).balance_minor == 45_050


# ---------------------------------------------------------------------------
# A blocked rider cannot request a ride
# ---------------------------------------------------------------------------

class TestBlockedAtRideRequest:
    def test_the_eligibility_rule(self, geo, world, rider_ops):
        client, _ = rider_ops
        rider = world['lakshmi']
        assert may_request_rides(rider)
        client.post(url(rider, 'block/'), {'reason_code': 'fraud'}, format='json')
        assert not may_request_rides(rider)
        assert may_request_rides(world['ravi'])
        client.post(url(rider, 'unblock/'), {'note': 'ok'}, format='json')
        assert may_request_rides(rider)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_blocked_rider_is_refused_at_the_socket_and_no_trip_is_created():
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer

    def setup():
        user = _rider('+919876533399', 'Blocked Rider')
        RiderBlock.objects.create(rider=Rider.objects.get(user_id=user), reason_code='fraud')
        return user

    user = await sync_to_async(setup)()
    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = user
    connected, _ = await comm.connect()
    assert connected
    await comm.receive_json_from()  # connection_established
    for frame in ({'pickup_lat': '17.40', 'pickup_lng': '78.40', 'destination_lat': '17.45',
                   'destination_lng': '78.40', 'pickup_address': 'A',
                   'destination_address': 'B', 'vehicle_type': 'sedan'},
                  {'action': 'retry', 'trip_id': 1}):
        await comm.send_json_to(frame)
        msg = await comm.receive_json_from(timeout=5)
        # The socket's existing error frame, unchanged in shape.
        assert msg == {'type': 'error', 'message': BLOCKED_MESSAGE}
    await comm.disconnect()
    assert await sync_to_async(Trip.objects.filter(user_id=user).count)() == 0


# ---------------------------------------------------------------------------
# Bulk
# ---------------------------------------------------------------------------

class TestBulk:
    def test_bulk_within_limit_runs_now(self, geo, world, rider_ops, policies):
        client, _ = rider_ops
        response = client.post(BULK, {
            'action': 'block', 'reason_code': 'fraud', 'idempotency_key': 'b1',
            'rider_ids': [world['lakshmi'].id, world['ravi'].id, world['austin'].id]},
            format='json')
        assert response.status_code == 200, response.content
        results = {r['id']: r['result'] for r in response.json()['data']['result']['results']}
        assert results == {world['lakshmi'].id: 'blocked', world['ravi'].id: 'blocked',
                           world['austin'].id: 'not_found'}
        assert audit_rows(world['lakshmi'], 'rider_blocked').count() == 1

    def test_over_50_needs_a_checker(self, geo, world, rider_ops, policies):
        client, _ = rider_ops
        many = []
        for i in range(51):
            user = _rider(f'+9198765{40000 + i}', f'Bulk Rider {i}')
            _trip(user, world['hyd'])
            many.append(user.id)
        response = client.post(BULK, {'action': 'block', 'reason_code': 'fraud',
                                      'rider_ids': many, 'idempotency_key': 'b2'},
                               format='json')
        assert response.status_code == 202, response.content
        data = response.json()['data']
        assert data['state'] == 'pending_approval' and data['count'] == 51
        assert data['approval_request']['action_key'] == 'bulk.action'
        assert not RiderBlock.objects.exists()
        again = client.post(BULK, {'action': 'block', 'reason_code': 'fraud',
                                   'rider_ids': many, 'idempotency_key': 'b2'}, format='json')
        assert again.status_code == 202 and again.json()['data']['duplicate'] is True
        detail = client.get(f'{LIST}{many[0]}/').json()['data']
        assert [r['status'] for r in detail['approval_requests']] == ['requested']
        checker, _ = operator('super_admin', geo)
        rid = data['approval_request']['id']
        assert checker.post(f'{APPROVALS}{rid}/approve/', {'reason': 'ok'},
                            format='json').status_code == 200
        assert ApprovalRequest.objects.get(pk=rid).result['counts'] == {'blocked': 51}
        assert RiderBlock.objects.filter(lifted_at__isnull=True).count() == 51

    def test_bulk_validation(self, geo, world, rider_ops, policies):
        client, _ = rider_ops
        for body in ({'action': 'delete', 'rider_ids': [1], 'idempotency_key': 'x'},
                     {'action': 'block', 'rider_ids': [], 'reason_code': 'fraud',
                      'idempotency_key': 'x'},
                     {'action': 'block', 'rider_ids': [1], 'reason_code': 'fraud'},
                     {'action': 'unblock', 'rider_ids': [1], 'idempotency_key': 'x'}):
            assert client.post(BULK, body, format='json').status_code == 400

    def test_bulk_needs_rider_write(self, geo, world, policies):
        client, _ = operator('city_ops_manager', geo)
        assert client.post(BULK, {'action': 'block', 'reason_code': 'fraud',
                                  'rider_ids': [world['lakshmi'].id], 'idempotency_key': 'x'},
                           format='json').status_code == 403


def test_no_migration_is_missing():
    out = StringIO()
    call_command('makemigrations', '--check', '--dry-run', stdout=out, stderr=out)
