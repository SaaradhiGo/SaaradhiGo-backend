"""USA from day 1: Idaho, the Treasure Valley (14 Nov). Per-state rules in MarketRegulation (US-ID),
from the Idaho TNC Act (Idaho Code Title 49, Chapter 37) and 41-2519, never Idaho-only code.
"""
import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.models import GeoNode
from servers.driver.models import Driver, DriverSuspension, Vehicle, VehicleType
from servers.pricing.models import RateCard, ServiceZone
from servers.pricing.regulation import market_rules, rules_for_zone
from servers.ride.models import Trip, TripStatus
from servers.rider.models import Rider
from tests.conftest import seed_admin_access

User = get_user_model()
pytestmark = pytest.mark.django_db

BOISE = (Decimal('43.6150'), Decimal('-116.2023'))


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


@pytest.fixture
def idaho():
    seed_admin_access()
    zone = ServiceZone.objects.get(code='US-ID-TV')
    ServiceZone.objects.filter(pk=zone.pk).update(is_active=True)
    zone.refresh_from_db()
    from servers.pricing.services import invalidate_polygon_cache
    invalidate_polygon_cache()
    node = GeoNode.objects.get(code='US-ID-TV')
    return zone, node


def _driver(node, phone='+12085550101', approved=True):
    u = User.objects.create_user(phone_number=phone, role='driver', dob=date(1990, 1, 1))
    d = Driver.objects.create(user_id=u, approved=approved, status='offline', home_city=node)
    return d


# ---- rules ----------------------------------------------------------------

def test_idaho_rules_come_from_the_statute_and_india_is_unchanged():
    r = market_rules('US', 'ID')
    assert r['cash_allowed'] is False and r['tips_enabled'] is True and r['sales_tax_enabled'] is False
    assert r['vehicle_classes'] == ['standard', 'xl']
    assert r['trip_record_retention_days'] == 730          # 49-3714(1): two years
    assert r['driver_record_retention_days'] == 365        # 49-3714(2): one year after deactivation
    assert r['complaint_record_retention_days'] == 730     # 49-3709(3)
    assert r['zero_tolerance_suspend'] and r['platform_insurance_required'] and r['fee_plan_required']
    assert r['citations']['cash_allowed'] == 'Idaho Code 49-3712'
    india = market_rules('IN')
    assert india['cash_allowed'] is True and india['tips_enabled'] is False and india['sales_tax_enabled'] is True
    assert india['fee_plan_required'] is False and india['platform_insurance_required'] is False


def test_the_console_edits_the_new_rules_through_maker_checker():
    from servers.pricing.regulation_workflow import FIELDS, _parse

    for name in ('cash_allowed', 'tip_presets', 'platform_insurance', 'service_agent', 'trip_record_retention_days'):
        assert name in FIELDS
    assert _parse('tip_presets', [20, 15, 18]) == [15, 18, 20]
    assert _parse('service_agent', {'name': ' Agent Co ', 'address': 'Boise'})['name'] == 'Agent Co'


# ---- cash and the quote ---------------------------------------------------

def test_no_cash_in_idaho(idaho):
    from servers.ride.rider_booking import cash_allowed_at, payment_options

    rider = User.objects.create_user(phone_number='+12085550201', role='rider')
    assert cash_allowed_at(*BOISE) is False
    assert 'cash' not in payment_options(rider, *BOISE)['methods']
    assert cash_allowed_at(Decimal('17.44'), Decimal('78.38')) is True   # Hyderabad keeps cash


def test_treasure_valley_is_priced_per_mile_in_dollars_with_no_sales_tax(idaho):
    zone, _ = idaho
    standard = VehicleType.objects.get(type='standard')
    RateCard.objects.create(zone=zone, vehicle_type=standard, base_fare=Decimal('2.00'),
                            per_km_fare=Decimal('1.50'), per_min_fare=Decimal('0.30'), min_fare=Decimal('7.00'),
                            commission_percent=Decimal('0'), gst_percent=Decimal('6.000'), is_active=True)
    from servers.pricing.services import quote_fare

    q = quote_fare(pickup_lat=BOISE[0], pickup_lon=BOISE[1], distance_km=Decimal('16.09344'),
                   duration_min=Decimal('20'), vehicle_type='standard', record_demand=False)
    assert q['currency'] == 'USD' and q['distance_unit'] == 'mi'
    assert q['distance_fare'] == Decimal('15.00')           # 10 miles x $1.50
    assert q['tax_amount'] == Decimal('0.00')               # sales tax off until the CPA says otherwise
    assert q['total_fare'] == Decimal('23.00')              # 2.00 + 15.00 + 6.00


# ---- launch gate ----------------------------------------------------------

def test_treasure_valley_cannot_open_without_fee_plan_insurance_and_its_own_cards(idaho):
    from servers.admin_access.readiness import city_readiness

    _, node = idaho
    items = {i['key']: i for i in city_readiness(node)}
    assert items['fee_plan']['ok'] is False
    assert items['platform_insurance']['ok'] is False
    assert items['fare_card']['ok'] is False
    assert 'standard' in items['fare_card']['detail'] and 'xl' in items['fare_card']['detail']
    assert 'auto' not in items['fare_card']['detail']
    assert node.status == 'draft'


# ---- drivers --------------------------------------------------------------

def test_the_idaho_checklist(idaho):
    from servers.driver.requirements import checklist

    _, node = idaho
    d = _driver(node)
    keys = {r['key']: r for r in checklist(d)['items']}
    for k in ('background_consent', 'background_check', 'accessibility_ack', 'driving_licence',
              'registration', 'insurance', 'vehicle_photos', 'profile_photo'):
        assert k in keys, k
    for k in ('aadhaar', 'pan', 'police_verification', 'rc', 'permit', 'puc'):
        assert k not in keys, k
    assert keys['insurance']['label'] == 'Personal auto insurance'


def test_minimum_age_19_and_vehicle_year_2011_with_four_doors(idaho):
    from servers.driver.requirements import for_driver, item_status

    _, node = idaho
    d = _driver(node)
    d.user_id.dob = date(timezone.localdate().year - 18, 1, 1)
    d.user_id.save(update_fields=['dob'])
    cfg = for_driver(d)
    assert cfg['min_driver_age'] == 19
    assert item_status(d, 'date_of_birth', cfg)['status'] == 'rejected'
    v = Vehicle.objects.create(driver_id=d, vehicle_type_id=VehicleType.objects.get(type='standard'),
                               vehicle_number='1ABC234', brand='Toyota', model='Camry', year=2010, doors=4)
    assert item_status(d, 'vehicle', cfg, vehicle=v, reviews={})['status'] == 'rejected'
    v.year, v.doors = 2015, 2
    assert item_status(d, 'vehicle', cfg, vehicle=v, reviews={})['status'] == 'rejected'
    v.doors = 4
    assert item_status(d, 'vehicle', cfg, vehicle=v, reviews={})['status'] in ('approved', 'done')


def test_no_us_driver_goes_online_before_the_fee_plan_and_platform_insurance(idaho):
    from servers.driver.requirements import market_gates_unmet
    from servers.pricing.regulation_models import MarketRegulation

    _, node = idaho
    d = _driver(node)
    gates = market_gates_unmet(d)
    assert any('insurance' in g for g in gates) and any('fee plan' in g for g in gates)
    MarketRegulation.objects.filter(country='US', state_code='ID').update(platform_insurance={
        'insurer': 'Test Insurer', 'policy_number': 'TNC-TEST-1', 'holder': 'platform'})
    assert not any('insurance' in g for g in market_gates_unmet(d))
    india = Driver.objects.create(user_id=User.objects.create_user(phone_number='+919811100001', role='driver'))
    assert market_gates_unmet(india) == []


@override_settings(BACKGROUND_CHECK_PROVIDERS={'US': 'fake'}, BACKGROUND_CHECK_WEBHOOK_SECRET='test-secret',
                   IS_PRODUCTION=False)
def test_background_check_disclosure_consent_webhook_and_review(idaho):
    _, node = idaho
    d = _driver(node, approved=False)
    c = _client(d.user_id)
    # The disclosure comes first, on its own; authorising before reading it is refused.
    assert c.post('/api/v1/driver/background/authorise/', {'authorise': True}, format='json').status_code == 409
    res = c.get('/api/v1/driver/background/disclosure/')
    assert res.status_code == 200 and 'consumer report' in json.dumps(res.json()).lower()
    c.post('/api/v1/driver/background/disclosure/', {}, format='json')
    res = c.post('/api/v1/driver/background/authorise/', {'authorise': True}, format='json')
    assert res.status_code == 200
    d.refresh_from_db()
    assert d.background_check_status == 'pending' and d.background_check_provider == 'fake'
    body = json.dumps({'ref': d.background_check_ref, 'status': 'consider'}).encode()
    sig = hmac.new(b'test-secret', body, hashlib.sha256).hexdigest()
    hook = APIClient()
    assert hook.post('/api/v1/driver/webhooks/background/fake/', body, content_type='application/json',
                     HTTP_X_SIGNATURE='bad').status_code == 401
    assert hook.post('/api/v1/driver/webhooks/background/fake/', body, content_type='application/json',
                     HTTP_X_SIGNATURE=sig).status_code == 200
    d.refresh_from_db()
    assert d.background_check_status == 'consider'
    # Never stored: no SSN anywhere on the driver.
    assert not any('ssn' in f.name.lower() for f in Driver._meta.get_fields())


@override_settings(BACKGROUND_CHECK_PROVIDERS={'US': 'fake'}, IS_PRODUCTION=True)
def test_the_fake_provider_never_runs_in_production(idaho):
    from servers.driver.background import provider_for

    assert provider_for('US') is None


@override_settings(IS_PRODUCTION=False)
def test_a_failed_check_takes_an_approved_driver_off_the_road(idaho):
    from servers.driver.background import apply_status

    _, node = idaho
    d = _driver(node)
    apply_status(d, 'failed', note='Disqualifying record (49-3710(2)(b))')
    d.refresh_from_db()
    assert d.approved is False
    assert DriverSuspension.objects.filter(driver=d, reason_code='background_check', lifted_at__isnull=True).exists()


# ---- zero tolerance, identification ----------------------------------------

def _trip(zone, driver, status='completed'):
    rider = User.objects.create_user(phone_number='+12085550301', role='rider')
    Rider.objects.create(user_id=rider)
    vt = VehicleType.objects.get(type='standard')
    v = Vehicle.objects.create(driver_id=driver, vehicle_type_id=vt, vehicle_number='7ID1234', brand='Toyota',
                               model='Camry', year=2018, doors=4)
    now = timezone.now()
    return Trip.objects.create(
        user_id=rider, driver_id=driver, vehicle_id=v, zone=zone,
        status_id=TripStatus.objects.get_or_create(status_code=status)[0],
        pickup_lat=BOISE[0], pickup_long=BOISE[1], destination_lat=Decimal('43.60'), destination_long=Decimal('-116.39'),
        estimated_fare=Decimal('18.00'), payment_method='card', accepted_at=now,
        completed_at=now if status == 'completed' else None)


def test_an_impairment_report_pauses_the_driver_at_once(idaho):
    zone, node = idaho
    d = _driver(node)
    trip = _trip(zone, d)
    c = _client(trip.user_id)
    res = c.post(f'/api/v1/ride/trips/{trip.id}/report-impairment/', {'description': 'Smelled of alcohol'},
                 format='json')
    assert res.status_code == 201 and res.json()['data']['driver_paused'] is True
    d.refresh_from_db()
    assert d.approved is False and d.status == 'blocked'
    assert DriverSuspension.objects.filter(driver=d, reason_code='zero_tolerance', lifted_at__isnull=True).exists()
    again = c.post(f'/api/v1/ride/trips/{trip.id}/report-impairment/', {}, format='json')
    assert again.json()['data']['ticket_id'] == res.json()['data']['ticket_id']


def test_the_rider_sees_the_driver_photo_and_plate_before_pickup(idaho):
    """Idaho 49-3707: a picture of the driver and the plate, before the rider gets in."""
    zone, node = idaho
    d = _driver(node)
    trip = _trip(zone, d, status='accepted')
    with mock.patch('servers.driver.document_access.signed_url', return_value='https://files.test/driver.jpg'):
        res = _client(trip.user_id).get(f'/api/v1/ride/trip/{trip.id}/')
    data = res.json()['data']
    assert data['driver_photo_url'] == 'https://files.test/driver.jpg'
    assert '7ID1234' in json.dumps(data)


@override_settings(IS_PRODUCTION=False)
def test_the_qa_seed_builds_a_bookable_treasure_valley_with_an_onboarded_driver(monkeypatch, tmp_path, settings):
    from django.core.management import call_command

    from servers.driver.requirements import checklist, market_gates_unmet
    from tests.conftest import seed_actor

    from django.db.models.fields.files import FieldFile

    def stored(self, name, content, save=True):  # no bucket in tests: keep the name only
        self.name, self._committed = f'test/{name}', True
        if save:
            self.instance.save()
    monkeypatch.setattr(FieldFile, 'save', stored)
    monkeypatch.setenv('QA_US_TEST_PASSWORD', 'qa-us-test-password-1')
    monkeypatch.delenv('ENVIRONMENT', raising=False)
    call_command('seed_us_idaho_qa', actor_email=seed_actor().email)
    call_command('seed_us_idaho_qa', actor_email=seed_actor().email)   # idempotent
    d = Driver.objects.get(user_id__phone_number='+12085550120')
    data = checklist(d)
    assert data['blocking'] == [], data['blocking']
    assert market_gates_unmet(d) == []
    assert ServiceZone.objects.get(code='US-ID-TV').is_active
    assert RateCard.objects.filter(zone__code='US-ID-TV', is_active=True).count() == 2


@override_settings(IS_PRODUCTION=True)
def test_the_qa_seed_refuses_production(monkeypatch):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command('seed_us_idaho_qa', actor_email='x@y.z')
