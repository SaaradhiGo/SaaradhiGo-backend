"""Ruling (c), AUDIT 27.56: a confirmed payment-abuse flag can restrict the rider to ONLINE payment
(no cash). Set from the flag by `fraud.review`, shown and cleared on the Riders page with
`rider.block`. A booking or a switch to cash is refused with the stable code `CASH_NOT_AVAILABLE`
and a neutral message. Blocking stays the existing block action.
"""

from decimal import Decimal as D

import pytest

from fraud_money_support import (  # noqa: F401
    ADM, block_the_network, body_of, client_for, decide, error_code, flag_url, geo, get, make_flag,
    make_rider, operator, post, seed_money, world,
)
from servers.admin_audit.models import AdminAuditLog
from servers.rider.models import (
    CASH_NOT_AVAILABLE, CASH_NOT_AVAILABLE_MESSAGE, Rider, RiderBlock, RiderRestriction,
    cash_disabled_for,
)

pytestmark = pytest.mark.django_db

RESTRICT = 'restrict-cash/'


def payment_flag(w, rider, *, confirm='payment_abuse'):
    flag = make_flag(rider, w.geo['IN-TG-HYD'], kind='payment_abuse', evidence={
        'signals': ['chargebacks'], 'refs': {'trip_ids': []}, 'measures': {}, 'hit_count': 1})
    if confirm:
        assert decide(w.ops[0], flag, 'confirm', confirm).status_code == 200
    flag.refresh_from_db()
    return flag


def restrict(w, flag, *, client=None, expect=201, note='Three chargebacks on cash trips'):
    return post(client or w.ops[0], flag_url(flag, RESTRICT), {'note': note}, expect=expect)


class TestSettingIt:
    def test_set_from_a_confirmed_payment_abuse_flag(self, world):
        w = world
        rider = make_rider('S1', '91')
        flag = payment_flag(w, rider)
        data = body_of(restrict(w, flag))
        assert data['kind'] == 'cash_disabled' and data['created'] is True
        row = RiderRestriction.objects.get()
        assert (row.flag_id, row.set_by_id, row.cleared_at) == (flag.pk, w.ops[1].pk, None)
        assert row.reason and row.set_at
        assert cash_disabled_for(rider) and not cash_disabled_for(make_rider('S1b', '91'))
        audit = AdminAuditLog.objects.get(action='rider_cash_disabled')
        assert audit.target_id == str(rider.pk) and audit.after['flag_id'] == flag.pk
        assert flag.events.filter(reason='cash_disabled').exists()

    def test_setting_it_twice_is_the_same_restriction(self, world):
        w = world
        rider = make_rider('S2', '91')
        flag = payment_flag(w, rider)
        restrict(w, flag)
        again = restrict(w, flag, expect=200)
        assert body_of(again)['created'] is False and RiderRestriction.objects.count() == 1

    def test_only_a_confirmed_payment_abuse_flag_can_set_it(self, world):
        w = world
        rider = make_rider('S3', '91')
        open_flag = payment_flag(w, rider, confirm=None)
        assert error_code(restrict(w, open_flag, expect=409)) == 'RESTRICTION_NOT_ALLOWED'
        other = make_rider('S3b', '91')
        wrong = payment_flag(w, other, confirm='other')
        assert error_code(restrict(w, wrong, expect=409)) == 'RESTRICTION_NOT_ALLOWED'
        assert not RiderRestriction.objects.exists()

    def test_it_needs_fraud_review_and_scope_and_a_rider_note(self, world):
        w = world
        rider = make_rider('S4', '91')
        flag = payment_flag(w, rider)
        for who in (w.mkt, w.finance, w.analyst, w.support):
            assert post(who[0], flag_url(flag, RESTRICT), {'note': 'long enough'}
                        ).status_code == 403
        assert post(w.ops_us[0], flag_url(flag, RESTRICT), {'note': 'long enough'}
                    ).status_code == 404
        assert error_code(restrict(w, flag, note='no', expect=400)) == 'INVALID_NOTE'

    def test_it_is_not_a_block(self, world):
        w = world
        rider = make_rider('S5', '91')
        flag = payment_flag(w, rider)
        restrict(w, flag)
        assert not RiderBlock.objects.filter(rider__user_id=rider).exists()
        from servers.rider.eligibility import may_request_rides

        assert may_request_rides(rider)                              # can still request rides

    def test_the_flag_detail_offers_it_only_for_payment_abuse_and_points_at_the_block_action(
            self, world):
        w = world
        rider = make_rider('S6', '91')
        flag = payment_flag(w, rider)
        cash = get(w.ops[0], flag_url(flag))['money']['cash']
        assert cash['offered'] and cash['may_set'] and not cash['active']
        assert cash['block_href'] == f'/riders/{rider.pk}' and 'repeat' in cash['note']
        restrict(w, flag)
        assert get(w.ops[0], flag_url(flag))['money']['cash']['active'] is True
        assert get(w.mkt[0], flag_url(flag))['money']['cash']['may_set'] is False


class TestTheRidersPage:
    def test_it_shows_the_restriction_and_clears_it_with_rider_block(self, world):
        w = world
        rider = make_rider('P1', '91')
        flag = payment_flag(w, rider)
        restrict(w, flag)
        # The City Ops Manager (rider.block) sees and clears it; Marketing cannot.
        detail = get(w.ops[0], f'/api/v1/rider/admin/{rider.pk}/')
        assert [r['kind'] for r in detail['restrictions']] == ['cash_disabled']
        assert detail['restrictions'][0]['flag_id'] == flag.pk
        assert detail['actions']['clear_restriction'] is True
        url = f'/api/v1/rider/admin/{rider.pk}/restriction/clear/'
        assert post(w.mkt[0], url, {'note': 'Cleared on appeal'}).status_code == 403
        assert post(w.ops[0], url, {'note': ''}).status_code == 400
        assert post(w.ops[0], url, {'note': 'Cleared on appeal'}).status_code == 200
        assert not cash_disabled_for(rider)
        assert get(w.ops[0], f'/api/v1/rider/admin/{rider.pk}/')['restrictions'] == []
        row = RiderRestriction.objects.get()
        assert row.cleared_by_id == w.ops[1].pk and row.clear_note
        assert AdminAuditLog.objects.filter(action='rider_cash_enabled',
                                            target_id=str(rider.pk)).count() == 1

    def test_clearing_something_not_set_changes_nothing(self, world):
        w = world
        rider = make_rider('P2', '91')
        r = post(w.ops[0], f'/api/v1/rider/admin/{rider.pk}/restriction/clear/',
                 {'note': 'Cleared on appeal'})
        assert r.status_code == 200 and r.json()['data']['cleared'] == 0
        assert not AdminAuditLog.objects.filter(action='rider_cash_enabled').exists()

    def test_out_of_scope_is_a_404(self, world):
        w = world
        rider = make_rider('P3', '91')
        flag = payment_flag(w, rider)
        restrict(w, flag)
        assert post(w.ops_us[0], f'/api/v1/rider/admin/{rider.pk}/restriction/clear/',
                    {'note': 'Cleared on appeal'}).status_code == 404


class TestCashIsRefusedOnlineIsAllowed:
    def _completed_trip(self, rider):
        from servers.driver.models import Driver
        from servers.payments.models import Payment
        from servers.ride.models import Trip, TripStatus
        from django.contrib.auth import get_user_model

        User = get_user_model()
        du = User.objects.create_user(phone_number='+918880009001', role='driver',
                                      username='+918880009001')
        driver = Driver.objects.create(user_id=du)
        completed, _ = TripStatus.objects.get_or_create(status_code='completed')
        trip = Trip.objects.create(
            user_id=rider, driver_id=driver, pickup_lat=D('17.4450'), pickup_long=D('78.3800'),
            destination_lat=D('17.4650'), destination_long=D('78.4200'),
            pickup_address='A', destination_address='B', estimated_fare=D('180.00'),
            final_fare=D('195.00'), status_id=completed, payment_method='deferred',
            payment_status='pending')
        Payment.objects.create(trip_id=trip, user_id=rider, amount=D('195.00'),
                               method='deferred', status='pending')
        return trip

    def test_switching_to_cash_is_refused_with_the_stable_code(self, world, monkeypatch):
        from servers.payments import views

        class NoGateway:
            def get_order_status(self, order_id):  # pragma: no cover
                raise AssertionError('no gateway call expected')

        monkeypatch.setattr(views, 'get_payment_gateway_for_payments', lambda *a, **k: NoGateway())
        w = world
        rider = make_rider('K1', '91')
        trip = self._completed_trip(rider)
        flag = payment_flag(w, rider)
        restrict(w, flag)
        client = client_for(rider)
        refused = client.post('/api/v1/payments/switch/',
                              {'trip_id': trip.pk, 'new_method': 'cash'}, format='json')
        assert refused.status_code == 400
        error = refused.json()['error']
        assert error['code'] == CASH_NOT_AVAILABLE == 'CASH_NOT_AVAILABLE'
        assert error['message'] == CASH_NOT_AVAILABLE_MESSAGE
        assert error['message'] == "Cash isn't available for your account. Please pay online."
        assert 'fraud' not in refused.content.decode().lower()
        from servers.payments.models import Payment

        assert Payment.objects.get(trip_id=trip).method == 'deferred'     # nothing changed

    def test_online_is_still_allowed(self, world, monkeypatch):
        from servers.payments import views

        class NoGateway:
            def get_order_status(self, order_id):
                return {'order_status': 'ACTIVE'}

        monkeypatch.setattr(views, 'get_payment_gateway_for_payments', lambda *a, **k: NoGateway())
        w = world
        rider = make_rider('K2', '91')
        trip = self._completed_trip(rider)
        restrict(w, payment_flag(w, rider))
        r = client_for(rider).post('/api/v1/payments/switch/',
                                   {'trip_id': trip.pk, 'new_method': 'online'}, format='json')
        assert r.status_code != 400 or r.json()['error']['code'] != CASH_NOT_AVAILABLE

    def test_an_unrestricted_rider_can_still_switch_to_cash(self, world, monkeypatch):
        from servers.payments import views

        class NoGateway:
            def get_order_status(self, order_id):
                return {'order_status': 'ACTIVE'}

        monkeypatch.setattr(views, 'get_payment_gateway_for_payments', lambda *a, **k: NoGateway())
        rider = make_rider('K3', '91')
        trip = self._completed_trip(rider)
        r = client_for(rider).post('/api/v1/payments/switch/',
                                   {'trip_id': trip.pk, 'new_method': 'cash'}, format='json')
        assert r.status_code == 200, r.content

    def test_a_cleared_restriction_allows_cash_again(self, world, monkeypatch):
        from servers.payments import views

        class NoGateway:
            def get_order_status(self, order_id):
                return {'order_status': 'ACTIVE'}

        monkeypatch.setattr(views, 'get_payment_gateway_for_payments', lambda *a, **k: NoGateway())
        w = world
        rider = make_rider('K4', '91')
        trip = self._completed_trip(rider)
        restrict(w, payment_flag(w, rider))
        post(w.ops[0], f'/api/v1/rider/admin/{rider.pk}/restriction/clear/',
             {'note': 'Cleared on appeal'}, expect=200)
        r = client_for(rider).post('/api/v1/payments/switch/',
                                   {'trip_id': trip.pk, 'new_method': 'cash'}, format='json')
        assert r.status_code == 200


FRAME = {'pickup_lat': '17.40', 'pickup_lng': '78.40', 'destination_lat': '17.45',
         'destination_lng': '78.40', 'pickup_address': 'A', 'destination_address': 'B',
         'vehicle_type': 'sedan'}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_a_cash_booking_is_refused_at_the_socket_and_no_trip_is_created():
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer
    from servers.ride.models import Trip

    def setup():
        user = make_rider('W1', '91')
        RiderRestriction.objects.create(rider=Rider.objects.get(user_id=user),
                                        kind='cash_disabled', reason='Reviewed')
        return user

    user = await sync_to_async(setup)()
    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = user
    connected, _ = await comm.connect()
    assert connected
    await comm.receive_json_from()                                  # connection_established
    await comm.send_json_to({**FRAME, 'payment_method': 'cash'})
    msg = await comm.receive_json_from(timeout=5)
    assert msg == {'type': 'error', 'code': 'CASH_NOT_AVAILABLE',
                   'message': "Cash isn't available for your account. Please pay online."}
    await comm.disconnect()
    assert await sync_to_async(Trip.objects.filter(user_id=user).count)() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_an_online_booking_is_not_refused_for_cash():
    from asgiref.sync import sync_to_async
    from channels.testing import WebsocketCommunicator

    from servers.consumers import RideRequestConsumer

    def setup():
        user = make_rider('W2', '91')
        RiderRestriction.objects.create(rider=Rider.objects.get(user_id=user),
                                        kind='cash_disabled', reason='Reviewed')
        return user

    user = await sync_to_async(setup)()
    comm = WebsocketCommunicator(RideRequestConsumer.as_asgi(), '/ws/ride/request/')
    comm.scope['user'] = user
    connected, _ = await comm.connect()
    assert connected
    await comm.receive_json_from()
    await comm.send_json_to({**FRAME, 'payment_method': 'online'})
    msg = await comm.receive_json_from(timeout=5)
    assert msg.get('code') != 'CASH_NOT_AVAILABLE'
    await comm.disconnect()
