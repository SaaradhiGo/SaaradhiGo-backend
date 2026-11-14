"""QA only: a Treasure Valley test market (14 Nov, USA from day 1).

    python manage.py seed_us_idaho_qa --actor-email you@saaradhigo.com     (password: env QA_US_TEST_PASSWORD)

Refuses in production. Idempotent: rows are found again by fixed keys. Everything it writes is
labelled as test data:

  * the geo tree through `seed_admin_access` (it owns it), then the Treasure Valley zone ACTIVE and
    its city node live on QA only (production starts closed and opens through the launch gate);
  * QA TEST rate cards for Standard and XL (base, per mile, per minute, minimum) and a booking
    fee -- not real prices; production starts with none;
  * a QA TEST Fair Share fee plan for US-ID (active on QA only) and a QA TEST platform insurance
    policy on the US-ID regulation row ("not a real policy");
  * an on-call rota for the Treasure Valley with fictional 555 numbers;
  * one US test rider and one US test driver (+1 208 555 01xx, qa.us.*@saaradhigo.test; the driver
    flagged `is_seed`; a licence-style name, as the checklist requires),
    the driver fully onboarded: licence, registration and insurance with expiry dates, profile
    photo, 5 vehicle photos (placeholder images), a 2019 4-door Standard car, the FCRA disclosure
    and authorisation, the fake background check CLEAR, the accessibility acknowledgement and a
    test payout account.
"""
import base64
import os
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

PNG = base64.b64decode(  # 1x1 grey PNG: a placeholder, never a real document
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mN89+7dfwAJWAPpCbPvHgAAAABJRU5ErkJggg==')
RIDER = ('+12085550110', 'qa.us.rider@saaradhigo.test', 'Avery Rider')
DRIVER = ('+12085550120', 'qa.us.driver@saaradhigo.test', 'Jordan Driver')
CARDS = {  # QA TEST rates, USD, per MILE in this zone: base, per mile, per minute, minimum
    'standard': (Decimal('2.50'), Decimal('1.60'), Decimal('0.30'), Decimal('8.00')),
    'xl': (Decimal('3.50'), Decimal('2.40'), Decimal('0.45'), Decimal('12.00')),
}


class Command(BaseCommand):
    help = 'QA only: seed the Treasure Valley (Idaho) test market.'

    def add_arguments(self, parser):
        parser.add_argument('--actor-email', required=True)

    def handle(self, *args, actor_email, **opts):
        if getattr(settings, 'IS_PRODUCTION', True) or os.environ.get('ENVIRONMENT', '').lower() in ('prod', 'production'):
            raise CommandError('Refusing: this seeds QA test data and never runs in production.')
        password = os.environ.get('QA_US_TEST_PASSWORD', '')
        if len(password) < 12:
            raise CommandError('Set QA_US_TEST_PASSWORD (12+ characters) for the two test logins.')
        call_command('seed_admin_access', actor_email=actor_email, stdout=self.stdout)
        with transaction.atomic():
            node, zone = self._market()
            self._cards(zone)
            self._fee_plan()
            self._insurance()
            self._rota(node)
            rider = self._rider(password)
            driver = self._driver(password, node)
        from servers.admin_audit.services import record_admin_action
        record_admin_action(None, action='qa_seed_us_idaho', target_type='geo_node', target_id=str(node.id),
                            before={}, after={'zone': zone.code, 'rider': rider.id, 'driver': driver.id},
                            reason=f'QA Treasure Valley test market seeded by {actor_email}')
        self.stdout.write(f'Treasure Valley QA market ready: zone {zone.code}, rider user {rider.id}, '
                          f'driver {driver.id}.')

    def _market(self):
        from servers.admin_access.linking import link_zones_to_nodes
        from servers.admin_access.models import GeoNode
        from servers.pricing.models import ServiceZone
        from servers.pricing.services import invalidate_polygon_cache

        node = GeoNode.objects.get(code='US-ID-TV')
        zone = ServiceZone.objects.get(code='US-ID-TV')
        zone.is_active = True
        zone.per_trip_fee = Decimal('1.50')
        zone.per_trip_fee_label = 'Booking fee (QA test)'
        zone.metadata = {**(zone.metadata or {}), 'qa_test_rates': True}
        zone.save()
        link_zones_to_nodes(ServiceZone, GeoNode)
        if node.status != 'active':
            GeoNode.objects.filter(pk=node.pk).update(status='active')  # QA only; production uses the gate
            node.refresh_from_db()
        invalidate_polygon_cache()
        return node, zone

    def _cards(self, zone):
        from servers.driver.models import VehicleType
        from servers.pricing.models import RateCard

        for name, (base, per_mile, per_min, minimum) in CARDS.items():
            vt = VehicleType.objects.get(type=name)
            if RateCard.objects.filter(zone=zone, vehicle_type=vt, is_active=True).exists():
                continue
            RateCard.objects.create(zone=zone, vehicle_type=vt, base_fare=base, per_km_fare=per_mile,
                                    per_min_fare=per_min, min_fare=minimum, commission_percent=Decimal('0'),
                                    gst_percent=Decimal('0'), is_active=True)

    def _fee_plan(self):
        from servers.pricing.fee_plan_models import DriverFeePlan

        if DriverFeePlan.objects.filter(country='US', scope_code='US-ID', status='active').exists():
            return
        from django.db.models import Max

        version = (DriverFeePlan.objects.filter(country='US', scope_code='US-ID')
                   .aggregate(v=Max('version'))['v'] or 0) + 1
        DriverFeePlan.objects.create(
            country='US', scope_code='US-ID', currency='USD', plan_type='fair_share', version=version,
            amounts={'*': {'per_trip_fee_minor': 100}}, daily_cap_minor=1500, status='active',
            effective_from=timezone.localdate() - timedelta(days=1), activated_at=timezone.now(),
            note='QA TEST PLAN - not a real plan', created_by_label='seed_us_idaho_qa')

    def _insurance(self):
        from servers.pricing.regulation_models import MarketRegulation

        MarketRegulation.objects.filter(country='US', state_code='ID').update(platform_insurance={
            'insurer': 'QA TEST INSURER (not a real policy)', 'policy_number': 'QA-TEST-0000',
            'holder': 'platform', 'logged_in': '$50,000 / $100,000 / $25,000',
            'on_trip': '$1,000,000 combined', 'certificate_url': '', 'expires': '12/31/2027'})

    def _rota(self, node):
        from servers.safety.models import OnCallRota

        OnCallRota.objects.get_or_create(city=node, defaults={
            'primary_name': 'QA Safety Primary', 'primary_phone': '+12085550190',
            'backup_name': 'QA Safety Backup', 'backup_phone': '+12085550191',
            'head_name': 'QA Safety Head', 'head_phone': '+12085550192'})

    def _user(self, phone, email, name, role, password):
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.filter(phone_number=phone).first()
        if user is None:
            user = User.objects.create_user(phone_number=phone, role=role, email=email, full_name=name)
        user.email, user.full_name = email, name
        user.set_password(password)
        for field, value in (('is_email_verified', True), ('email_verified', True), ('phone_verified', True)):
            if hasattr(user, field):
                setattr(user, field, value)
        user.save()
        return user

    def _rider(self, password):
        from servers.rider.models import Rider

        user = self._user(*RIDER, 'rider', password)
        Rider.objects.get_or_create(user_id=user)
        return user

    def _driver(self, password, node):
        from servers.driver.models import (Driver, DriverAddress, DriverEmergencyContact, Vehicle,
                                           VehicleType)
        from servers.driver.requirements import approve_reviewed_files

        user = self._user(*DRIVER, 'driver', password)
        user.dob = date(1990, 4, 12)
        if not user.avatar:
            user.avatar.save('qa_us_driver.png', ContentFile(PNG), save=False)
        user.save()
        driver, _ = Driver.objects.get_or_create(user_id=user)
        now = timezone.now()
        driver.home_city = node
        driver.is_seed = True
        driver.license_number = 'ID-QA-0000120'
        driver.license_expiry = date(2029, 4, 12)
        for col in ('license_doc', 'license_doc_back'):
            if not getattr(driver, col):
                getattr(driver, col).save(f'qa_us_{col}.png', ContentFile(PNG), save=False)
        driver.background_check_provider = 'fake'
        driver.background_check_ref = f'fake-seed-{driver.id}'
        driver.background_check_status = 'clear'
        driver.background_checked_at = now
        driver.background_disclosure_at = driver.background_disclosure_at or now
        driver.background_consent_at = driver.background_consent_at or now
        driver.accessibility_ack_at = driver.accessibility_ack_at or now
        driver.stripe_account_id = driver.stripe_account_id or 'acct_qa_test_seed'
        driver.stripe_onboarding_status = 'ready'
        driver.save()
        DriverAddress.objects.get_or_create(driver=driver, kind='current', defaults={
            'line1': '100 W Main St (QA test)', 'city': 'Boise', 'state_code': 'ID', 'postal_code': '83702',
            'country': 'US'})
        DriverEmergencyContact.objects.get_or_create(driver=driver, position=1, defaults={
            'name': 'QA Contact', 'relationship': DriverEmergencyContact.RELATIONSHIPS[0][0],
            'phone_number': '+12085550199'})
        vt = VehicleType.objects.get(type='standard')
        vehicle = Vehicle.objects.filter(driver_id=driver).first() or Vehicle(driver_id=driver)
        vehicle.vehicle_type_id = vt
        vehicle.vehicle_number = '1QATV20'
        vehicle.brand, vehicle.model, vehicle.color, vehicle.year, vehicle.doors = 'Toyota', 'Camry', 'Silver', 2019, 4
        vehicle.registration_expiry = date(2027, 6, 30)
        vehicle.insurance_expiry = date(2027, 6, 30)
        if hasattr(vehicle, 'is_seed'):
            vehicle.is_seed = True
        for col in ('rc_doc', 'insurance_doc', 'vehicle_pic', 'photo_back', 'photo_left', 'photo_right',
                    'photo_interior'):
            if hasattr(vehicle, col) and not getattr(vehicle, col):
                getattr(vehicle, col).save(f'qa_us_{col}.png', ContentFile(PNG), save=False)
        vehicle.save()
        if hasattr(driver, 'active_vehicle'):
            driver.active_vehicle = vehicle
            driver.save(update_fields=['active_vehicle'])
        approve_reviewed_files(driver, None)
        driver.approved = True
        driver.save(update_fields=['approved'])
        return driver
