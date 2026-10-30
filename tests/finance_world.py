"""One shared world for the Finance read-side tests (a helper, not a test file).

Two markets that must never be added together:

  * Hyderabad (IN, INR, Asia/Kolkata): a driver with a settled online trip (the
    balance), a pending withdrawal and a rejected one (the real endpoints, so
    the ledger is built by the code that runs in production), plus a cash trip.
  * Austin (US, USD, America/Chicago): a driver with a settled online trip and a
    pending withdrawal. The US has NO payout configuration, so its withdrawal is
    written directly (the request endpoint refuses it, by design).

Fixture passwords are low-entropy on purpose (the secret scan checks every
commit); nobody signs in with a password here, tokens are minted directly.
"""

from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.driver.models import Driver, Vehicle, VehicleType, WithdrawalRequest
from servers.payments.models import Payment, TransactionHistory, WebhookEvent
from servers.pricing.models import ServiceZone
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider, Wallet

User = get_user_model()

POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.20], [78.50, 17.20], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.20]]],
}

_counter = iter(range(1, 99999))


def client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


def operator(role_code, geo, scope_code='IN-TG-HYD'):
    i = next(_counter)
    phone = f'+9198100{i:05d}'
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=f'fin{i}@saaradhigo.test',
        full_name=f'Fin Operator {i}', role='admin', is_staff=True,
    )
    role = AdminRole.objects.get(code=role_code)
    scope = geo['GLOBAL'] if role.is_global_only else geo[scope_code]
    AdminAssignment.objects.create(admin=user, role=role, scope=scope)
    return client_for(user), user


def status_obj(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


def zone(code, node, **extra):
    z, _ = ServiceZone.objects.get_or_create(
        code=code,
        defaults={'name': node.name, 'zone_type': 'city', 'city': node.name,
                  'polygon_geojson': POLYGON, 'is_active': True, **extra},
    )
    if z.geo_node_id != node.id:
        z.geo_node = node
        z.save()
    return z


def seed_in_payout_config():
    """What `seed_payout_config` leaves behind on a deployed environment."""
    from conftest import seed_actor

    call_command('seed_payout_config', actor_email=seed_actor().email, stdout=StringIO())


def make_driver(geo, node_code, tag, *, currency, calling, city_node=True):
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')
    phone = f'+{calling}9{next(_counter):09d}'
    user = User.objects.create_user(
        phone_number=phone, username=phone, full_name=f'Driver {tag} Reddy', role='driver')
    driver = Driver.objects.create(
        user_id=user, home_city=geo[node_code] if city_node else None, approved=True,
        doc_status='approved', status='online', upi_id=f'driver{tag}@okaxis')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=sedan, vehicle_number=f'FIN{tag}{next(_counter):04d}',
        brand='Maruti', model='Dzire', color='white', year=2022, capacity=4)
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    Wallet.objects.get_or_create(
        user_id=user, scope=Wallet.SCOPE_DRIVER, defaults={'currency': currency})
    return driver


def make_rider(tag, calling):
    phone = f'+{calling}8{next(_counter):09d}'
    user = User.objects.create_user(
        phone_number=phone, username=phone, full_name=f'Rider {tag} Kumar', role='rider')
    Rider.objects.create(user_id=user)
    return user


def make_trip(zone_obj, rider, driver, *, fare, method='online', completed=True):
    now = timezone.now()
    return Trip.objects.create(
        user_id=rider, driver_id=driver, vehicle_id=driver.active_vehicle, zone=zone_obj,
        status_id=status_obj('completed' if completed else 'requested'),
        pickup_lat=Decimal('17.43'), pickup_long=Decimal('78.40'),
        destination_lat=Decimal('17.24'), destination_long=Decimal('78.42'),
        estimated_fare=Decimal(fare), final_fare=Decimal(fare), payment_method=method,
        payment_status='completed', completed_at=now if completed else None,
    )


def settle(trip):
    from servers.driver.utils import credit_driver_wallet

    credit_driver_wallet(trip)


def make_payment(trip, order_id, *, amount=None, status='completed'):
    return Payment.objects.create(
        trip_id=trip, user_id=trip.user_id, amount_minor=amount or trip.final_fare_minor,
        method='online', status=status, gateway_order_id=order_id,
        gateway_payment_id=f'cfpay{order_id}',
        gateway_metadata={'customer_details': {
            'customer_phone': '+919876543210', 'customer_email': 'x.y@example.test'}},
    )


def success_webhook(order_id, amount, currency, *, dedupe=None, received_at=None):
    event = WebhookEvent.objects.create(
        gateway='cashfree', dedupe_key=dedupe or f'sig-{order_id}',
        event_type='PAYMENT_SUCCESS_WEBHOOK', result='ok',
        processed_at=timezone.now(),
        raw_payload={'type': 'PAYMENT_SUCCESS_WEBHOOK', 'data': {
            'order': {'order_id': order_id, 'order_amount': str(amount), 'order_currency': currency},
            'payment': {'cf_payment_id': 'cf1', 'payment_amount': str(amount),
                        'payment_currency': currency, 'card': {'card_number': 'xxxx1111'}}}},
    )
    if received_at is not None:
        WebhookEvent.objects.filter(pk=event.pk).update(received_at=received_at)
    return event


def build_world(geo):
    """Both markets, settled, with payouts. Returns a namespace."""
    seed_in_payout_config()
    hyd = zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus = zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX',
               timezone_name='America/Chicago')

    in_driver = make_driver(geo, 'IN-TG-HYD', 'IN', currency='INR', calling='91')
    us_driver = make_driver(geo, 'US-TX-AUS', 'US', currency='USD', calling='1')
    in_rider = make_rider('IN', '91')
    us_rider = make_rider('US', '1')

    in_trip = make_trip(hyd, in_rider, in_driver, fare='2000.00')
    in_cash = make_trip(hyd, in_rider, in_driver, fare='100.00', method='cash')
    us_trip = make_trip(aus, us_rider, us_driver, fare='30.00')
    for t in (in_trip, in_cash, us_trip):
        settle(t)

    in_payment = make_payment(in_trip, 'order_IN_0001')
    us_payment = make_payment(us_trip, 'order_US_0001')
    success_webhook('order_IN_0001', '2000.00', 'INR')
    success_webhook('order_US_0001', '30.00', 'USD')

    # The real withdrawal endpoint, as the driver: one pending, one rejected.
    driver_client = client_for(in_driver.user_id)
    r1 = driver_client.post('/api/v1/driver/withdrawals/request/', {'amount': '500.00'}, format='json')
    assert r1.status_code == 201, r1.content
    r2 = driver_client.post('/api/v1/driver/withdrawals/request/', {'amount': '600.00'}, format='json')
    assert r2.status_code == 201, r2.content
    in_pending = WithdrawalRequest.objects.get(id=r1.json()['data']['id'])
    in_rejected = WithdrawalRequest.objects.get(id=r2.json()['data']['id'])
    fin_client, fin_user = operator('finance', geo)
    rej = fin_client.post(f'/api/v1/driver/admin/withdrawals/{in_rejected.id}/reject/',
                          {'admin_notes': 'bank details unclear'}, format='json')
    assert rej.status_code == 200, rej.content
    in_rejected.refresh_from_db()

    us_withdrawal = WithdrawalRequest.objects.create(
        driver=us_driver, amount=Decimal('20.00'), payout_method='upi', status='pending')

    return SimpleNamespace(
        geo=geo, hyd=hyd, aus=aus, in_driver=in_driver, us_driver=us_driver,
        in_rider=in_rider, us_rider=us_rider, in_trip=in_trip, in_cash=in_cash,
        us_trip=us_trip, in_payment=in_payment, us_payment=us_payment,
        in_pending=in_pending, in_rejected=in_rejected, us_withdrawal=us_withdrawal,
        finance=(fin_client, fin_user),
    )
