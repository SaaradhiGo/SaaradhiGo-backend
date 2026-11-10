"""Drivers who have not finished setting up (owner, 10 Nov).

Srinu's QA account had role 'driver' and no Driver row: IsDriver refused every driver
endpoint with 403 and the web showed "We couldn't load". Every driver read must answer
200 for each incomplete case, with money as 0.00 in the market's currency (the phone
number's country when there is no home city), documents listed with their status, and
payout and fee-plan lookups falling back city -> state -> country.
"""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from finance_world import client_for, make_driver
from servers.driver.models import Driver, Vehicle
from servers.rider.models import Wallet
from test_payout_settings_extension import india  # noqa: F401
from test_pricing_module import geo, world  # noqa: F401

User = get_user_model()
pytestmark = pytest.mark.django_db

READS = [
    'driver/profile/', 'driver/onboarding/', 'driver/incentives/', 'driver/elite-status/',
    'earnings/?page_size=50', 'earnings/summary/', 'earnings/daily/?days=7', 'fee-plan/',
    'earnings/period/?period=today', 'earnings/period/?period=week', 'earnings/period/?period=month',
    'withdrawals/balance/', 'withdrawals/instant/quote/?amount=200', 'withdrawals/history/',
    'withdrawals/block-status/', 'vehicles/',
]
OTHER = ['/api/v1/ride/driver-history/?page_size=50', '/api/v1/auth/profile/']


def incomplete(world, case, calling='91'):
    if case == 'no_driver_row':
        phone = f'+{calling}9000000{len(case):03d}'
        return User.objects.create_user(phone_number=phone, username=phone, full_name='Srinu', role='driver')
    driver = make_driver(world.geo, 'IN-TG-HYD', case[:6].upper(), currency='INR', calling=calling)
    if case == 'no_vehicle':
        Driver.objects.filter(pk=driver.pk).update(active_vehicle=None)
        Vehicle.objects.filter(driver_id=driver).delete()
    elif case == 'no_home_city':
        Driver.objects.filter(pk=driver.pk).update(home_city=None)
    elif case == 'not_approved':
        Driver.objects.filter(pk=driver.pk).update(approved=False, doc_status='pending', status='offline')
    elif case == 'no_wallet':
        Wallet.objects.filter(user_id=driver.user_id).delete()
    return driver.user_id                                                  # 'no_trips': none are made


CASES = ['no_driver_row', 'no_vehicle', 'no_home_city', 'not_approved', 'no_wallet', 'no_trips']


@pytest.mark.parametrize('case', CASES)
def test_every_driver_read_answers_for_an_incomplete_driver(india, case):  # noqa: F811
    client = client_for(incomplete(india, case))
    for path in ['/api/v1/driver/' + p for p in READS] + OTHER:
        r = client.get(path)
        assert r.status_code == 200, (case, path, r.status_code, r.content[:300])
    data = lambda p: (lambda b: b.get('data', b))(client.get('/api/v1/driver/' + p).json())  # noqa: E731
    bal = data('withdrawals/balance/')
    assert Decimal(str(bal['available_balance'])) == 0 or case not in ('no_driver_row', 'no_wallet')
    assert bal['currency'] == 'INR'
    for p in ('earnings/period/?period=today', 'earnings/daily/?days=7', 'earnings/summary/'):
        assert data(p)['currency'] == 'INR', (case, p)
    assert data('fee-plan/') is not None
    docs = data('driver/onboarding/')
    assert docs['driver_documents'], case
    assert all(d['status'] and 'required' in d for d in docs['driver_documents'] + docs['vehicle_documents']), case


def test_a_us_phone_with_no_city_is_counted_in_dollars(india):  # noqa: F811
    client = client_for(incomplete(india, 'no_driver_row', calling='1'))
    for p in ('withdrawals/balance/', 'earnings/period/?period=today', 'earnings/daily/?days=7',
              'earnings/summary/'):
        r = client.get('/api/v1/driver/' + p)
        assert r.status_code == 200, (p, r.content[:300])
        assert r.json().get('data', r.json())['currency'] == 'USD', p


def test_the_missing_driver_row_is_created_once(india):  # noqa: F811
    user = incomplete(india, 'no_driver_row')
    client = client_for(user)
    client.get('/api/v1/driver/driver/profile/')
    client.get('/api/v1/driver/withdrawals/balance/')
    assert Driver.objects.filter(user_id=user).count() == 1
    d = Driver.objects.get(user_id=user)
    assert d.approved is False and d.home_city is None


def test_a_rider_is_still_refused_driver_reads(india):  # noqa: F811
    phone = '+919000000777'
    rider = User.objects.create_user(phone_number=phone, username=phone, full_name='R', role='rider')
    assert client_for(rider).get('/api/v1/driver/withdrawals/balance/').status_code == 403
    assert not Driver.objects.filter(user_id=rider).exists()


# ---- The edit options (owner, 10 Nov): through the existing review and verification flows.

def test_adding_a_vehicle_goes_to_review_and_becomes_the_active_one(india):  # noqa: F811
    user = incomplete(india, 'no_vehicle')
    client = client_for(user)
    r = client.post('/api/v1/driver/vehicles/add/', {
        'vehicle_type': 'sedan', 'vehicle_number': 'TS09AB1234', 'brand': 'Maruti', 'model': 'Dzire',
        'color': 'White'}, format='json')
    assert r.status_code == 201, r.content[:300]
    d = Driver.objects.get(user_id=user)
    assert d.active_vehicle_id == r.json()['data']['id']
    assert d.doc_status == 'pending' and d.approved is False                  # "In review", offline


def test_editing_what_riders_see_goes_back_to_review(india):  # noqa: F811
    user = incomplete(india, 'no_trips')                                      # approved, with a car
    client = client_for(user)
    vid = Driver.objects.get(user_id=user).active_vehicle_id
    r = client.patch(f'/api/v1/driver/vehicles/{vid}/', {'vehicle_type': 'auto', 'color': 'Red'}, format='json')
    assert r.status_code == 200, r.content[:300]
    d = Driver.objects.get(user_id=user)
    assert d.doc_status == 'pending' and d.approved is False
    assert d.active_vehicle.vehicle_type_id.type == 'auto' and d.active_vehicle.color == 'Red'


def test_an_expiry_date_alone_is_not_reviewed_again(india):  # noqa: F811
    user = incomplete(india, 'no_trips')
    vid = Driver.objects.get(user_id=user).active_vehicle_id
    r = client_for(user).patch(f'/api/v1/driver/vehicles/{vid}/', {'puc_expiry': '2027-12-31'}, format='json')
    assert r.status_code == 200
    assert Driver.objects.get(user_id=user).approved is True


def test_name_and_email_are_editable_and_a_new_email_is_unverified(india):  # noqa: F811
    from django.utils import timezone
    user = incomplete(india, 'no_trips')
    User.objects.filter(pk=user.pk).update(email='old@saaradhigo.test', email_verified_at=timezone.now())
    r = client_for(user).patch('/api/v1/auth/update/', {'full_name': 'Srinu Reddy', 'email': 'srinu@saaradhigo.test'},
                               format='json')
    assert r.status_code == 200, r.content[:300]
    user.refresh_from_db()
    assert user.full_name == 'Srinu Reddy' and user.email == 'srinu@saaradhigo.test'
    assert user.email_verified_at is None


def test_the_phone_changes_only_with_a_code_for_the_new_number(india):  # noqa: F811
    from servers.auth_user import phone_proof
    user = incomplete(india, 'no_trips')
    client = client_for(user)
    new = '+919000000555'
    r = client.patch('/api/v1/auth/update/', {'phone_number': new}, format='json')
    user.refresh_from_db()
    assert user.phone_number != new                                            # read-only there
    r = client.post('/api/v1/auth/phone/change/', {'phone_number': new}, format='json')
    assert r.status_code == 400 and r.json()['error']['code'] == phone_proof.MISSING
    r = client.post('/api/v1/auth/phone/change/', {
        'phone_number': new, 'phone_proof': phone_proof.issue('+919000000666', 'driver')}, format='json')
    assert r.status_code == 400 and r.json()['error']['code'] == phone_proof.INVALID
    r = client.post('/api/v1/auth/phone/change/', {
        'phone_number': new, 'phone_proof': phone_proof.issue(new, 'driver')}, format='json')
    assert r.status_code == 200, r.content[:300]
    user.refresh_from_db()
    assert user.phone_number == new


def test_the_phone_cannot_take_another_accounts_number(india):  # noqa: F811
    from servers.auth_user import phone_proof
    user = incomplete(india, 'no_trips')
    other = incomplete(india, 'no_wallet')
    r = client_for(user).post('/api/v1/auth/phone/change/', {
        'phone_number': other.phone_number, 'phone_proof': phone_proof.issue(other.phone_number, 'driver')},
        format='json')
    assert r.status_code == 400 and r.json()['error']['code'] == 'AUTH_PHONE_TAKEN'


def test_the_backfill_creates_the_missing_rows():
    from importlib import import_module
    from django.apps import apps
    user = User.objects.create_user(phone_number='+919000000888', username='+919000000888', role='driver')
    import_module('servers.driver.migrations.0041_backfill_missing_driver_rows').backfill(apps, None)
    assert Driver.objects.filter(user_id=user, approved=False).count() == 1
