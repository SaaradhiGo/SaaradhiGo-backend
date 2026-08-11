"""Regression locks for the India-launch hardening batch.

Each test here corresponds to a defect found in the pre-launch architecture
review. They are grouped by the failure they prevent, and each one fails
against the code as it was before this batch.

Covered:
  1. Trip-group IDOR — a driver who has not been assigned the trip must not
     be subscribed to it, and must be dropped once someone else accepts.
  2. Wallet split — driver settlement money must not be spendable through
     the rider credit wallet.
  3. Commission — taken from the zone's RateCard, not a global env default
     of 0.
  4. Cash confirm — idempotent, gated on trip completion, and never writes
     a second TransactionHistory row.
  5. Trip side-effects — no receipt render / e-mail inside the trip
     transaction.
  6. Driver-details IDOR — a third party must not be able to read a
     driver's phone number by guessing trip ids.
  7. Dispatch — vehicle-type filtering happens in Redis, and stale
     (heartbeat-less) drivers are never dispatched to.
"""
from decimal import Decimal
from unittest.mock import patch

import pytest

from servers.driver.models import Driver, Vehicle, VehicleType
from servers.pricing.models import RateCard, ServiceZone
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Wallet, get_wallet


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A small square around Hyderabad's Hitec City, used as the pickup zone.
HYD_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[
        [78.30, 17.40], [78.50, 17.40], [78.50, 17.50], [78.30, 17.50], [78.30, 17.40],
    ]],
}
PICKUP = (Decimal('17.4450'), Decimal('78.3800'))


@pytest.fixture
def vehicle_type(db):
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    return vt


@pytest.fixture
def zone_with_card(db, vehicle_type):
    zone = ServiceZone.objects.create(
        code='IN-TG-HYD-TEST', name='Hyderabad test', zone_type='city',
        city='Hyderabad', polygon_geojson=HYD_POLYGON, priority=10,
    )
    RateCard.objects.create(
        zone=zone, vehicle_type=vehicle_type,
        base_fare=Decimal('30'), per_km_fare=Decimal('12'),
        per_min_fare=Decimal('2'), min_fare=Decimal('50'),
        commission_percent=Decimal('22.00'),
    )
    return zone


@pytest.fixture
def completed_trip(db, auth_client_rider, auth_client_driver, zone_with_card, vehicle_type):
    """A cash trip in `completed`, assigned to the driver, inside the zone."""
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    driver = driver_user.driver

    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vehicle_type,
        vehicle_number='TS09AB1234', brand='Maruti', model='Dzire', color='White',
    )
    driver.active_vehicle = vehicle
    driver.approved = True
    driver.save()

    status_obj, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver, zone=zone_with_card,
        requested_vehicle_type=vehicle_type,
        pickup_lat=PICKUP[0], pickup_long=PICKUP[1],
        destination_lat=Decimal('17.4600'), destination_long=Decimal('78.3500'),
        estimated_fare=Decimal('200.00'), final_fare=Decimal('200.00'),
        status_id=status_obj, payment_method='cash', otp='123456',
    )
    return trip, rider_user, driver


# ---------------------------------------------------------------------------
# 3. Commission comes from the RateCard, not the env default
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_commission_uses_zone_rate_card_not_settings(completed_trip, settings):
    """The zone card says 22%; the global setting says something else.

    Before the fix, `credit_driver_wallet` read
    settings.PLATFORM_COMMISSION_PERCENT, which shipped defaulting to "0" —
    so an unset env var meant the platform earned nothing on every ride and
    the per-city commission on each RateCard was decorative.
    """
    from servers.driver.utils import credit_driver_wallet

    settings.PLATFORM_COMMISSION_PERCENT = Decimal('5')  # deliberately wrong
    trip, _, driver = completed_trip

    credit_driver_wallet(trip)

    wallet = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER)
    # Cash trip: the driver holds the fare, so they owe us the commission.
    # 22% of 200.00 = 44.00 -> balance goes negative by the commission.
    assert wallet.balance == Decimal('-44.00'), (
        f'expected 22% zone commission (-44.00), got {wallet.balance}'
    )


@pytest.mark.django_db
def test_commission_falls_back_to_settings_without_a_zone(
    db, auth_client_rider, auth_client_driver, settings,
):
    """A legacy trip with no zone still settles, at the configured rate."""
    from servers.driver.utils import credit_driver_wallet

    settings.PLATFORM_COMMISSION_PERCENT = Decimal('10')
    _, rider_user = auth_client_rider
    _, driver_user = auth_client_driver
    status_obj, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver_user.driver,
        pickup_lat=Decimal('0.0'), pickup_long=Decimal('0.0'),
        destination_lat=Decimal('0.1'), destination_long=Decimal('0.1'),
        final_fare=Decimal('100.00'), status_id=status_obj, payment_method='online',
    )

    credit_driver_wallet(trip)

    wallet = get_wallet(driver_user.driver.user_id, Wallet.SCOPE_DRIVER)
    assert wallet.balance == Decimal('90.00')  # 100 - 10%


# ---------------------------------------------------------------------------
# 2. Rider credits and driver settlement are different balances
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_driver_settlement_is_not_spendable_as_rider_credit(completed_trip):
    """A driver who is also a rider must not be able to pay for rides with
    money the platform owes them as settlement."""
    from base.utils import wallet_payment
    from servers.driver.utils import credit_driver_wallet

    trip, _, driver = completed_trip
    trip.payment_method = 'online'  # platform owes the driver
    trip.save(update_fields=['payment_method'])
    credit_driver_wallet(trip)

    driver_user = driver.user_id
    settlement = get_wallet(driver_user, Wallet.SCOPE_DRIVER)
    assert settlement.balance > 0, 'driver should have earned something'

    # Same human, now acting as a rider, tries to spend it on a ride.
    result = wallet_payment(
        user=driver_user, amount=float(settlement.balance),
        purpose='Trip payment', reference_id='TRIP_X',
        idempotency_key='trip_x_payment',
    )
    assert result.get('success') is False, (
        'settlement balance must not be spendable through the rider wallet'
    )

    settlement.refresh_from_db()
    assert settlement.balance > 0, 'settlement balance must be untouched'


@pytest.mark.django_db
def test_rider_and_driver_wallets_are_separate_rows(db, auth_client_driver):
    _, driver_user = auth_client_driver
    rider_side = get_wallet(driver_user, Wallet.SCOPE_RIDER)
    driver_side = get_wallet(driver_user, Wallet.SCOPE_DRIVER)
    assert rider_side.pk != driver_side.pk
    assert Wallet.objects.filter(user_id=driver_user).count() == 2


# ---------------------------------------------------------------------------
# 4. Cash confirmation is idempotent and gated
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_cash_settlement_writes_one_transaction_history_row(completed_trip):
    """Completing then confirming a cash trip must not double-count GMV.

    `_create_payment_on_complete` wrote a TransactionHistory row, and then
    `_confirm_cash_payment` wrote a second one for the same trip.
    """
    from servers.consumers import TripStatusConsumer
    from servers.payments.models import TransactionHistory

    trip, _, _ = completed_trip
    trip.payments.all().delete()

    consumer = TripStatusConsumer()
    # Completion path: writes the Payment row and settles the driver.
    consumer._create_payment_on_complete(trip)
    # Driver then taps "confirm cash" — and the client retries the frame.
    consumer.trip_id = trip.id
    consumer._confirm_cash_payment.__wrapped__(consumer)
    consumer._confirm_cash_payment.__wrapped__(consumer)

    rows = TransactionHistory.objects.filter(trip_id=trip)
    assert rows.count() == 1, (
        f'expected exactly one settlement row for the trip, got {rows.count()}'
    )


@pytest.mark.django_db
def test_credit_driver_wallet_is_idempotent_on_balance(completed_trip):
    from servers.driver.utils import credit_driver_wallet

    trip, _, driver = completed_trip
    credit_driver_wallet(trip)
    first = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance
    credit_driver_wallet(trip)
    second = get_wallet(driver.user_id, Wallet.SCOPE_DRIVER).balance
    assert first == second


# ---------------------------------------------------------------------------
# 6. Driver phone numbers are not readable by trip-id enumeration
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_trip_driver_details_rejects_unrelated_user(completed_trip, api_client):
    """Any authenticated user could previously walk trip ids and harvest
    driver names, phone numbers and number plates from this endpoint."""
    from django.contrib.auth import get_user_model
    from rest_framework_simplejwt.tokens import AccessToken

    trip, _, _ = completed_trip
    User = get_user_model()
    stranger = User.objects.create_user(phone_number='+919000000123', role='rider')
    api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(stranger)}')

    resp = api_client.get(f'/api/v1/ride/trip/{trip.id}/details/')
    assert resp.status_code == 403, resp.content
    assert b'phone' not in resp.content.lower() or resp.status_code == 403


@pytest.mark.django_db
def test_trip_driver_details_allows_the_rider(completed_trip, auth_client_rider):
    trip, _, _ = completed_trip
    rider_client, _ = auth_client_rider
    resp = rider_client.get(f'/api/v1/ride/trip/{trip.id}/details/')
    assert resp.status_code == 200, resp.content


# ---------------------------------------------------------------------------
# 5. Receipt issuance is deferred, not done inside the trip transaction
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_receipt_is_issued_by_a_celery_task_not_inline():
    """The receipt path must be reachable as a task.

    Rendering a PDF, uploading it to S3 and sending an email used to happen
    inside `transaction.atomic()` while holding SELECT FOR UPDATE on the
    Trip row, so trip-completion latency included an SES round-trip.
    """
    from servers.ride.tasks import issue_receipt_for_trip

    assert issue_receipt_for_trip.name == 'ride.issue_receipt_for_trip'
    # Unknown trip must not raise — the task is fired via on_commit and a
    # deleted trip should not poison the queue.
    assert 'not found' in issue_receipt_for_trip.run(999999)


@pytest.mark.django_db
def test_completed_trip_does_not_create_a_gateway_order_inline(completed_trip):
    """Trip completion records payment intent only; the Cashfree order is
    created later by POST /payments/create-order/."""
    from servers.consumers import TripStatusConsumer
    from servers.payments.models import Payment

    trip, _, _ = completed_trip
    trip.payment_method = 'online'
    trip.save(update_fields=['payment_method'])

    consumer = TripStatusConsumer()
    with patch(
        'servers.payments.payment_gateways.factory.get_payment_gateway'
    ) as gw:
        consumer._create_payment_on_complete(trip)
        gw.assert_not_called()

    payment = Payment.objects.get(trip_id=trip)
    assert payment.status == 'pending'
    assert not payment.gateway_order_id


# ---------------------------------------------------------------------------
# 7. Dispatch: vehicle-type filtering and stale-driver eviction
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_nearby_drivers_filters_by_vehicle_type_at_the_redis_layer():
    """A sedan request in a bike-dense area must still find sedans.

    The old implementation did GEOSEARCH COUNT 50 against one global key
    and filtered by vehicle type in Python afterwards, so 50 nearby bikes
    hid every sedan.
    """
    import servers.redis_client as rc

    if rc.redis_client is None:
        pytest.skip('Redis not available')

    bike_key = rc.geo_key_for('bike')
    sedan_key = rc.geo_key_for('sedan')
    rc.redis_client.delete(bike_key, sedan_key)
    try:
        # 60 bikes right on top of the pickup, one sedan 200m away.
        for i in range(60):
            rc.redis_client.geoadd(bike_key, [78.3800, 17.4450, f'driver:{1000 + i}:bike'])
            rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}{1000 + i}', 30, '1')
        rc.redis_client.geoadd(sedan_key, [78.3820, 17.4450, 'driver:2001:sedan'])
        rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}2001', 30, '1')

        found = rc.nearby_drivers(lng=78.3800, lat=17.4450, radius=5000, vehicle_type='sedan')
        members = [f[0] for f in found]
        assert members == ['driver:2001:sedan'], members
    finally:
        rc.redis_client.delete(bike_key, sedan_key)
        for i in range(60):
            rc.redis_client.delete(f'{rc.HEARTBEAT_PREFIX}{1000 + i}')
        rc.redis_client.delete(f'{rc.HEARTBEAT_PREFIX}2001')


@pytest.mark.django_db
def test_drivers_without_a_heartbeat_are_not_dispatched_to():
    """Ghost drivers: a killed app skips WS disconnect, so the geo entry
    survives. Riders then wait out the full accept timeout on a driver who
    is not there."""
    import servers.redis_client as rc

    if rc.redis_client is None:
        pytest.skip('Redis not available')

    key = rc.geo_key_for('sedan')
    rc.redis_client.delete(key)
    try:
        rc.redis_client.geoadd(key, [78.3800, 17.4450, 'driver:3001:sedan'])  # ghost
        rc.redis_client.geoadd(key, [78.3801, 17.4450, 'driver:3002:sedan'])  # live
        rc.redis_client.setex(f'{rc.HEARTBEAT_PREFIX}3002', 30, '1')

        found = rc.nearby_drivers(lng=78.3800, lat=17.4450, radius=5000, vehicle_type='sedan')
        assert [f[0] for f in found] == ['driver:3002:sedan']

        assert rc.sweep_stale_drivers() >= 1
        assert rc.redis_client.zscore(key, 'driver:3001:sedan') is None
        assert rc.redis_client.zscore(key, 'driver:3002:sedan') is not None
    finally:
        rc.redis_client.delete(key, f'{rc.HEARTBEAT_PREFIX}3002')


@pytest.mark.django_db
def test_offer_set_round_trips_for_trip_taken_fanout():
    """Losing drivers can only be told to dismiss the card if we remember
    who we offered the trip to."""
    import servers.redis_client as rc

    if rc.redis_client is None:
        pytest.skip('Redis not available')

    rc.clear_offered_drivers(4242)
    rc.add_offered_drivers(4242, ['11', '12', '13'])
    popped = sorted(rc.pop_offered_drivers(4242))
    assert popped == ['11', '12', '13']
    # Popping is destructive — a second accept must not re-notify.
    assert rc.pop_offered_drivers(4242) == []


# ---------------------------------------------------------------------------
# 1. Trip-group participation
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_unassigned_driver_is_a_candidate_not_a_subscriber(completed_trip, auth_client_driver):
    """A driver offered a trip may accept it, but must not be subscribed to
    the trip group until their accept commits."""
    from servers.consumers import TripStatusConsumer

    trip, _, driver = completed_trip
    # Put the trip back to an open state with no driver assigned.
    requested, _ = TripStatus.objects.get_or_create(status_code='requested')
    trip.driver_id = None
    trip.status_id = requested
    trip.save()

    consumer = TripStatusConsumer()
    consumer.trip_id = trip.id
    consumer.user = driver.user_id

    assert consumer._resolve_participation.__wrapped__(consumer) == 'candidate_driver'


@pytest.mark.django_db
def test_losing_driver_is_denied_the_trip_socket(completed_trip, db):
    """Once another driver holds the trip, an unrelated driver must be
    refused the socket outright — not merely muted."""
    from django.contrib.auth import get_user_model
    from servers.consumers import TripStatusConsumer

    trip, _, _ = completed_trip
    User = get_user_model()
    other_user = User.objects.create_user(phone_number='+918000000777', role='driver')
    other_driver = Driver.objects.create(user_id=other_user, approved=True)

    consumer = TripStatusConsumer()
    consumer.trip_id = trip.id
    consumer.user = other_user

    assert other_driver.id != trip.driver_id_id
    assert consumer._resolve_participation.__wrapped__(consumer) == 'none'


@pytest.mark.django_db
def test_rider_is_always_a_participant(completed_trip, auth_client_rider):
    from servers.consumers import TripStatusConsumer

    trip, rider_user, _ = completed_trip
    consumer = TripStatusConsumer()
    consumer.trip_id = trip.id
    consumer.user = rider_user
    assert consumer._resolve_participation.__wrapped__(consumer) == 'rider'


# ---------------------------------------------------------------------------
# Pricing: estimates must not inflate their own surge
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_fare_estimate_does_not_record_demand(zone_with_card, vehicle_type):
    """Recording a rider ping on every estimate let a rider drive up the
    surge they were quoted just by reopening the estimate screen."""
    from servers.pricing import services

    with patch('servers.redis_client.add_rider_ping') as ping:
        services.quote_fare(
            distance_km=5, duration_min=15, vehicle_type='sedan',
            pickup_lat=PICKUP[0], pickup_lon=PICKUP[1], rider_id=1,
        )
        ping.assert_not_called()


@pytest.mark.django_db
def test_booking_records_demand(zone_with_card, vehicle_type):
    from servers.pricing import services

    with patch('servers.redis_client.add_rider_ping') as ping:
        services.quote_fare(
            distance_km=5, duration_min=15, vehicle_type='sedan',
            pickup_lat=PICKUP[0], pickup_lon=PICKUP[1], rider_id=1,
            record_demand=True,
        )
        ping.assert_called_once()


@pytest.mark.django_db
def test_quote_refuses_when_no_rate_card_and_defaults_disabled(settings):
    """In production a missing rate card is a seeding error. Quoting the
    hardcoded fallback would sell rides at a made-up price."""
    from servers.pricing.services import PricingUnavailable, quote_fare

    settings.PRICING_ALLOW_DEFAULT_FARE = False
    with pytest.raises(PricingUnavailable):
        quote_fare(
            distance_km=5, duration_min=15, vehicle_type='sedan',
            pickup_lat=Decimal('1.0'), pickup_lon=Decimal('1.0'),
        )
