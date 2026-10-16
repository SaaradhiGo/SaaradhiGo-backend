"""The operator console, rendered for a driver in each market.

THE BLOCKER THIS GUARDS
-----------------------
The KYC screen asked every driver for India's five statutory vehicle documents
and required `all()` of them:

    rc        Registration Certificate
    insurance
    permit    commercial passenger permit
    fitness   Fitness Certificate
    puc       Pollution Under Control

Three have no US equivalent, so `all(...)` could never be true, so a US driver
could never be approved -- not a confusing message, no possible path. The
checklist shown beside the gate was a second hand-written copy of the same five,
so the operator also saw a score that could not pass 50%.

These tests render the actual page and read what an operator would read. A unit
test on `evaluate_vehicle` passes whether or not the view uses it.
"""

from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from servers.driver.models import Driver, Vehicle, VehicleType

User = get_user_model()

pytestmark = pytest.mark.django_db

_n = iter(range(1, 9999))


@pytest.fixture
def operator():
    return User.objects.create_user(
        phone_number='+919700000001', username='ops1', role='admin',
        is_staff=True, is_superuser=True,
    )


@pytest.fixture
def console(operator):
    client = Client()
    client.force_login(operator)
    return client


def _driver_with_documents(calling_code, documents):
    """A driver whose vehicle carries exactly `documents`."""
    i = next(_n)
    user = User.objects.create_user(
        phone_number=f'+{calling_code}7{i:09d}'[:16], role='driver',
        username=f'cd{i}', full_name=f'Driver {i}')
    driver = Driver.objects.create(user_id=user, approved=False,
                                   license_doc='license_docs/x.pdf')
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number=f'CN{i:05d}',
        **{column: f'{column}/x.pdf' for column in documents},
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    return driver


def _bare_driver(calling_code):
    """A driver with no document files.

    The profile page signs an S3 URL for every stored document, and these two
    tests are about the revenue figure, not the documents -- a placeholder
    filename would make botocore the thing under test.
    """
    i = next(_n)
    user = User.objects.create_user(
        phone_number=f'+{calling_code}6{i:09d}'[:16], role='driver',
        username=f'bd{i}', full_name=f'Driver {i}')
    driver = Driver.objects.create(user_id=user, approved=True)
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=vt, vehicle_number=f'BN{i:05d}')
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    return driver


def _onboarding(console, driver):
    resp = console.get(reverse('driver_onboarding'), {'driver': driver.pk})
    assert resp.status_code == 200
    selected = resp.context['selected']
    assert selected is not None, 'the console did not select the driver'
    return selected


# ===========================================================================
# The US driver, who could not be approved at all
# ===========================================================================

US_COMPLETE = ('rc_doc', 'insurance_doc', 'inspection_doc')


def test_a_fully_documented_us_driver_reaches_full_compliance(console):
    """Registration, insurance, inspection. In India's checklist this driver is
    missing a permit, a fitness certificate and a PUC, and can never be
    approved."""
    driver = _driver_with_documents('1', US_COMPLETE)
    selected = _onboarding(console, driver)

    assert selected['compliance_percentage'] == 100
    assert selected['compliance_complete'] is True
    assert all(v['passed'] == len(v['checks']) for v in selected['vehicles'])


def test_the_us_checklist_does_not_ask_for_documents_that_country_lacks(
        console):
    driver = _driver_with_documents('1', US_COMPLETE)
    selected = _onboarding(console, driver)

    names = {check['name'] for check in selected['compliance_checks']}
    assert 'Pollution Under Control' not in names
    assert 'Commercial permit' not in names
    assert 'Fitness certificate' not in names
    # And it DOES ask for what the US requires.
    assert 'Safety inspection' in names
    assert 'Vehicle registration' in names


def test_the_us_safety_inspection_has_somewhere_to_be_stored(console):
    """`inspection` had no column, so it was required-but-unrecordable, which
    counts as not compliant -- a permanent block. It now has a column, and the
    console reports no unrecordable documents."""
    driver = _driver_with_documents('1', US_COMPLETE)
    selected = _onboarding(console, driver)

    assert selected['missing_document_columns'] == []
    inspection = [d for v in selected['vehicles'] for d in v['documents']
                  if d['key'] == 'inspection']
    assert len(inspection) == 1
    assert inspection[0]['recordable'] is True
    assert inspection[0]['present'] is True


def test_a_us_driver_missing_their_inspection_is_still_blocked(console):
    """The control. Removing the compliance block must not mean approving
    anyone: a genuinely missing US document still fails."""
    driver = _driver_with_documents('1', ('rc_doc', 'insurance_doc'))
    selected = _onboarding(console, driver)

    assert selected['compliance_complete'] is False
    inspection = [d for v in selected['vehicles'] for d in v['documents']
                  if d['key'] == 'inspection'][0]
    assert inspection['present'] is False
    assert inspection['recordable'] is True, (
        'a missing document must read as missing, not as a platform gap'
    )


# ===========================================================================
# India must be reviewed exactly as before
# ===========================================================================

IN_COMPLETE = ('rc_doc', 'insurance_doc', 'permit_doc', 'fitness_doc',
               'puc_doc')


def test_an_indian_driver_is_still_asked_for_all_five_documents(console):
    driver = _driver_with_documents('91', IN_COMPLETE)
    selected = _onboarding(console, driver)

    names = {check['name'] for check in selected['compliance_checks']}
    assert names == {
        'Driving License',
        'Registration Certificate',
        'Insurance',
        'Commercial permit',
        'Fitness certificate',
        'Pollution Under Control',
    }
    assert selected['compliance_percentage'] == 100


@pytest.mark.parametrize('withheld', [
    'rc_doc', 'insurance_doc', 'permit_doc', 'fitness_doc', 'puc_doc',
])
def test_an_indian_driver_missing_any_one_document_is_blocked(
        console, withheld):
    """Each of the five, individually. This is the property the old `all()`
    had, and losing it would approve unverified drivers in the live market."""
    documents = tuple(d for d in IN_COMPLETE if d != withheld)
    driver = _driver_with_documents('91', documents)
    selected = _onboarding(console, driver)

    assert selected['compliance_complete'] is False
    assert selected['compliance_percentage'] < 100


def test_an_indian_driver_is_not_asked_for_a_us_inspection(console):
    driver = _driver_with_documents('91', IN_COMPLETE)
    selected = _onboarding(console, driver)

    keys = {d['key'] for v in selected['vehicles'] for d in v['documents']}
    assert 'inspection' not in keys
    assert keys == {'rc', 'insurance', 'permit', 'fitness', 'puc'}


# ===========================================================================
# Money on the console
# ===========================================================================

def test_the_console_does_not_print_a_rupee_sign_for_a_dollar_row(console):
    """Rendered, not grepped from the source: the template is where the forty
    hardcoded symbols were."""
    from servers.pricing.models import ServiceZone
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider

    zone = ServiceZone.objects.create(
        code='C-US-SFO', name='San Francisco', country='US', state_code='CA',
        city='San Francisco', zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [-122.52, 37.70], [-122.35, 37.70], [-122.35, 37.84],
            [-122.52, 37.84], [-122.52, 37.70]]]},
        priority=50, is_active=True, timezone_name='America/Los_Angeles',
    )
    rider_user = User.objects.create_user(
        phone_number='+15550000111', role='rider', username='crider1')
    Rider.objects.create(user_id=rider_user)
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    trip = Trip.objects.create(
        user_id=rider_user, status_id=completed, zone=zone,
        pickup_lat=Decimal('37.7749'), pickup_long=Decimal('-122.4194'),
        destination_lat=Decimal('37.7900'), destination_long=Decimal('-122.4194'),
        estimated_fare=Decimal('30.83'), final_fare=Decimal('30.83'),
        payment_method='cash',
    )
    assert trip.currency == 'USD'

    resp = console.get(reverse('ride_detail', args=[trip.pk]))
    assert resp.status_code == 200
    body = resp.content.decode('utf-8')

    assert '$30.83' in body
    assert '₹30.83' not in body


# ===========================================================================
# The approval POST itself -- the gate, not the screen
# ===========================================================================

def test_a_us_driver_can_actually_be_approved(console):
    """The end of the blocker.

    The gate counted `total_checks = 6` -- India's document count -- and read
    `item["permit"]` out of a dict keyed by the documents the driver's country
    requires. For a US driver that count was unreachable AND that lookup raised
    KeyError, so the POST would have 500'd rather than refused.
    """
    driver = _driver_with_documents('1', US_COMPLETE)

    resp = console.post(reverse('driver_onboarding'),
                        {'action': 'approve', 'driver_id': driver.pk})
    assert resp.status_code in (200, 302), resp.content[:400]

    driver.refresh_from_db()
    assert driver.approved is True


def test_an_incomplete_us_driver_is_refused_not_crashed(console):
    """The control. Still a refusal, still not an exception."""
    driver = _driver_with_documents('1', ('rc_doc',))

    resp = console.post(reverse('driver_onboarding'),
                        {'action': 'approve', 'driver_id': driver.pk})
    assert resp.status_code in (200, 302), resp.content[:400]

    driver.refresh_from_db()
    assert driver.approved is False


def test_an_indian_driver_approval_is_unchanged(console):
    driver = _driver_with_documents('91', IN_COMPLETE)

    resp = console.post(reverse('driver_onboarding'),
                        {'action': 'approve', 'driver_id': driver.pk})
    assert resp.status_code in (200, 302)

    driver.refresh_from_db()
    assert driver.approved is True


@pytest.mark.parametrize('withheld', [
    'rc_doc', 'insurance_doc', 'permit_doc', 'fitness_doc', 'puc_doc',
])
def test_an_indian_driver_missing_one_document_is_not_approved(
        console, withheld):
    """Each of India's five, individually, through the POST. This is the
    property the old `all()` provided and the one thing this change must not
    have loosened."""
    documents = tuple(d for d in IN_COMPLETE if d != withheld)
    driver = _driver_with_documents('91', documents)

    console.post(reverse('driver_onboarding'),
                 {'action': 'approve', 'driver_id': driver.pk})

    driver.refresh_from_db()
    assert driver.approved is False


# ===========================================================================
# Per-driver money on the console
# ===========================================================================

def test_a_us_drivers_profile_shows_dollars(console):
    """The driver-profile revenue figure was `Sum("final_fare")` rendered with
    a symbol the template chose. For a US driver that symbol was a rupee sign
    on a dollar total."""
    from servers.pricing.models import ServiceZone
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider

    zone = ServiceZone.objects.create(
        code='P-US-SFO', name='San Francisco', country='US', state_code='CA',
        city='San Francisco', zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [-122.52, 37.70], [-122.35, 37.70], [-122.35, 37.84],
            [-122.52, 37.84], [-122.52, 37.70]]]},
        priority=50, is_active=True, timezone_name='America/Los_Angeles',
    )
    driver = _bare_driver('1')
    rider_user = User.objects.create_user(
        phone_number='+15550000222', role='rider', username='prider1')
    Rider.objects.create(user_id=rider_user)
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')

    from django.utils import timezone as dj_timezone
    now = dj_timezone.now()
    Trip.objects.create(
        user_id=rider_user, driver_id=driver, status_id=completed, zone=zone,
        pickup_lat=Decimal('37.7749'), pickup_long=Decimal('-122.4194'),
        destination_lat=Decimal('37.7900'), destination_long=Decimal('-122.4194'),
        estimated_fare=Decimal('30.83'), final_fare=Decimal('30.83'),
        payment_method='cash', accepted_at=now, started_at=now,
        completed_at=now,
    )

    resp = console.get(reverse('driver_profile', args=[driver.pk]))
    assert resp.status_code == 200
    body = resp.content.decode('utf-8')

    assert '$30.83' in body
    assert '₹30.83' not in body


def test_an_indian_drivers_profile_still_shows_rupees(console):
    """The control."""
    from servers.pricing.models import ServiceZone
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider
    from django.utils import timezone as dj_timezone

    zone = ServiceZone.objects.create(
        code='P-IN-HYD', name='Hyderabad', country='IN', state_code='TG',
        city='Hyderabad', zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
            [78.30, 17.50], [78.30, 17.40]]]},
        priority=50, is_active=True,
    )
    driver = _bare_driver('91')
    rider_user = User.objects.create_user(
        phone_number='+919550000222', role='rider', username='prider2')
    Rider.objects.create(user_id=rider_user)
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')

    now = dj_timezone.now()
    Trip.objects.create(
        user_id=rider_user, driver_id=driver, status_id=completed, zone=zone,
        pickup_lat=Decimal('17.4450'), pickup_long=Decimal('78.3800'),
        destination_lat=Decimal('17.4660'), destination_long=Decimal('78.3800'),
        estimated_fare=Decimal('190.00'), final_fare=Decimal('190.00'),
        payment_method='cash', accepted_at=now, started_at=now,
        completed_at=now,
    )

    resp = console.get(reverse('driver_profile', args=[driver.pk]))
    body = resp.content.decode('utf-8')
    assert '₹190.00' in body
    assert '$190.00' not in body


# ===========================================================================
# The two money dashboards, rendered with rows in both markets
# ===========================================================================

def _completed_payment(zone, phone, username, amount):
    from servers.payments.models import Payment
    from servers.ride.models import Trip, TripStatus
    from servers.rider.models import Rider
    from django.utils import timezone as dj_timezone

    user = User.objects.create_user(
        phone_number=phone, role='rider', username=username)
    Rider.objects.create(user_id=user)
    completed, _ = TripStatus.objects.get_or_create(status_code='completed')
    point = ((Decimal('37.7749'), Decimal('-122.4194')) if zone.country == 'US'
             else (Decimal('17.4450'), Decimal('78.3800')))
    now = dj_timezone.now()
    trip = Trip.objects.create(
        user_id=user, status_id=completed, zone=zone,
        pickup_lat=point[0], pickup_long=point[1],
        destination_lat=point[0] + Decimal('0.02'), destination_long=point[1],
        estimated_fare=Decimal(amount), final_fare=Decimal(amount),
        payment_method='online', accepted_at=now, started_at=now,
        completed_at=now,
    )
    payment = Payment.objects.create(
        trip_id=trip, user_id=user, amount=Decimal(amount), method='online',
        status='completed',
    )
    return trip, payment


def _two_market_zones():
    from servers.pricing.models import ServiceZone

    india = ServiceZone.objects.create(
        code='D-IN-HYD', name='Hyderabad', country='IN', state_code='TG',
        city='Hyderabad', zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [78.30, 17.40], [78.50, 17.40], [78.50, 17.50],
            [78.30, 17.50], [78.30, 17.40]]]},
        priority=50, is_active=True,
    )
    usa = ServiceZone.objects.create(
        code='D-US-SFO', name='San Francisco', country='US', state_code='CA',
        city='San Francisco', zone_type='city',
        polygon_geojson={'type': 'Polygon', 'coordinates': [[
            [-122.52, 37.70], [-122.35, 37.70], [-122.35, 37.84],
            [-122.52, 37.84], [-122.52, 37.70]]]},
        priority=50, is_active=True, timezone_name='America/Los_Angeles',
    )
    return india, usa


def test_the_payment_dashboard_shows_two_totals_not_their_sum(console):
    """`Sum("amount")` over ₹190 and $30.83 is 220.83, which is not an amount
    of money in either currency -- and the template rendered it with a rupee
    sign, asserting it was rupees."""
    india, usa = _two_market_zones()
    _completed_payment(india, '+919550000333', 'drider1', '190.00')
    _completed_payment(usa, '+15550000333', 'drider2', '30.83')

    resp = console.get(reverse('payment_dashboard'))
    assert resp.status_code == 200
    body = resp.content.decode('utf-8')

    assert '₹190.00' in body
    assert '$30.83' in body
    assert '220.83' not in body, 'rupees were added to dollars'


def test_the_transaction_reconciliation_shows_two_totals(console):
    india, usa = _two_market_zones()
    _completed_payment(india, '+919550000444', 'trider1', '190.00')
    _completed_payment(usa, '+15550000444', 'trider2', '30.83')

    resp = console.get(reverse('transaction_dashboard'))
    assert resp.status_code == 200
    body = resp.content.decode('utf-8')

    assert '₹190.00' in body
    assert '$30.83' in body
    assert '220.83' not in body


def test_a_single_market_dashboard_still_shows_one_number(console):
    """The control, and the state every day of the India pilot: with one
    currency in the table the grouped total renders as one amount, exactly as
    before."""
    india, _ = _two_market_zones()
    _completed_payment(india, '+919550000555', 'srider1', '190.00')
    _completed_payment(india, '+919550000556', 'srider2', '110.00')

    resp = console.get(reverse('payment_dashboard'))
    body = resp.content.decode('utf-8')

    assert '₹300.00' in body
    assert '+' not in body.split('₹300.00')[0][-40:], (
        'a single-market total should not be rendered as a sum of markets'
    )
