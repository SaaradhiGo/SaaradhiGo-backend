"""What a real driver provides, per market (owner brief, 10 Nov; AUDIT §37).

India and the USA ask for different things; the checklist blocks approval (and so going online)
until every blocking item is done; make and model come from the catalogue ("Other" is reviewed);
uploads are reviewed with a reason; a bank or UPI change is stored masked and pauses payouts
for 24 hours; US payouts start with a Stripe onboarding link; the integrity scan finds and fixes.
"""
from datetime import date, timedelta
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from finance_world import client_for, make_driver, operator
from servers.driver import requirements as req
from servers.driver.models import (
    Driver, DriverAddress, DriverBankAccount, DriverDocumentReview, DriverEmergencyContact, Vehicle,
    VehicleCatalogueEntry,
)
from servers.pricing.regulation_models import MarketRegulation
from test_payout_settings_extension import india  # noqa: F401
from test_pricing_module import geo, world  # noqa: F401

User = get_user_model()
pytestmark = [pytest.mark.django_db]
API = '/api/v1/driver/'


def new_driver(world, calling='91', node='IN-TG-HYD'):  # noqa: F811
    d = make_driver(world.geo, node, 'RQ', currency='INR' if calling == '91' else 'USD', calling=calling)
    Driver.objects.filter(pk=d.pk).update(approved=False, doc_status='pending', upi_id=None)
    Vehicle.objects.filter(driver_id=d).delete()
    Driver.objects.filter(pk=d.pk).update(active_vehicle=None)
    return Driver.objects.get(pk=d.pk)


def get(client, path):
    r = client.get(API + path)
    assert r.status_code == 200, r.content[:400]
    return r.json()['data']


class TestMarkets:
    def test_india_and_usa_ask_for_different_things(self, world):  # noqa: F811
        india = req.resolve('IN')
        usa = req.resolve('US')
        on = lambda c: {k for k, v in c['items'].items() if v['need'] != 'off'}  # noqa: E731
        assert {'aadhaar', 'pan', 'police_verification', 'address_proof', 'rc', 'puc'} <= on(india)
        assert not {'aadhaar', 'pan', 'police_verification', 'rc', 'puc'} & on(usa)
        assert {'background_check', 'registration', 'inspection'} <= on(usa)
        assert india['languages'] == ['en', 'te', 'hi'] and usa['languages'] == ['en', 'es']
        assert india['min_driver_age'] == 18 and usa['min_driver_age'] == 21

    def test_a_state_row_overrides_an_item(self, world):  # noqa: F811
        MarketRegulation.objects.create(country='US', state_code='TX', driver_requirements={
            'inspection': {'need': 'off', 'blocks_online': False}})
        assert req.resolve('US', 'TX')['items']['inspection']['need'] == 'off'
        assert req.resolve('US', 'TX')['items']['registration']['need'] == 'required'
        assert req.resolve('US', 'CA')['items']['inspection']['need'] == 'required'

    def test_the_driver_sees_their_markets_items_states_and_languages(self, world):  # noqa: F811
        india = get(client_for(new_driver(world).user_id), 'requirements/')
        assert india['config']['languages'] == ['en', 'te', 'hi']
        keys = {i['key'] for i in india['checklist']['items']}
        assert 'aadhaar' in keys and 'background_check' not in keys
        assert {'code': 'TG', 'name': 'Telangana'} in india['states']
        usa = get(client_for(new_driver(world, calling='1', node='US-TX-AUS').user_id), 'requirements/')
        keys = {i['key'] for i in usa['checklist']['items']}
        assert 'aadhaar' not in keys and 'pan' not in keys and 'background_check' in keys
        assert usa['payout_method']['kind'] == 'stripe'
        assert all(s['code'] != 'TG' for s in usa['states'])

    def test_requirements_change_through_maker_checker(self, world):  # noqa: F811
        client, _ = operator('super_admin', world.geo)
        r = client.put('/api/v1/pricing/admin/regulation/IN/', {
            'driver_requirements': {**MarketRegulation.objects.get(country='IN', state_code='').driver_requirements,
                                    'permanent_address': {'need': 'required', 'blocks_online': True}},
            'languages': ['en', 'te', 'hi'], 'reason': 'Ops asked for it'}, format='json')
        assert r.status_code == 202, r.content[:300]
        assert r.json()['data']['state'] in ('pending_approval', 'executed')
        bad = client.put('/api/v1/pricing/admin/regulation/IN/', {
            'languages': ['fr'], 'reason': 'x'}, format='json')
        assert bad.status_code == 400


class TestPersonal:
    def test_address_pin_must_match_the_state(self, world):  # noqa: F811
        client = client_for(new_driver(world).user_id)
        bad = client.patch(API + 'personal/', {'address': {
            'line1': '1 Road', 'city': 'Hyderabad', 'state_code': 'TG', 'postal_code': '560001'}}, format='json')
        assert bad.status_code == 400 and 'Telangana' in bad.json()['error']['message']
        ok = client.patch(API + 'personal/', {'address': {
            'line1': '1 Road', 'city': 'Hyderabad', 'state_code': 'TG', 'postal_code': '500034'},
            'permanent_same': True}, format='json')
        assert ok.status_code == 200
        assert DriverAddress.objects.filter(kind='permanent', postal_code='500034').exists()

    def test_emergency_contacts_and_age(self, world):  # noqa: F811
        driver = new_driver(world)
        client = client_for(driver.user_id)
        too_young = (timezone.localdate() - timedelta(days=365 * 17)).isoformat()
        assert client.patch(API + 'personal/', {'dob': too_young}, format='json').json()['error']['code'] == 'DOB_TOO_YOUNG'
        r = client.patch(API + 'personal/', {'dob': '1990-05-01', 'emergency_contacts': [
            {'name': 'Lakshmi', 'relationship': 'spouse', 'phone_number': '9876500001'}]}, format='json')
        assert r.status_code == 200, r.content[:300]
        assert DriverEmergencyContact.objects.get(driver=driver).phone_number == '+919876500001'

    def test_a_new_photo_waits_for_review_and_the_old_one_stays(self, world):  # noqa: F811
        driver = new_driver(world)
        user = driver.user_id
        user.avatar = 'avatars/old.png'
        user.save(update_fields=['avatar'])
        driver.photo_pending = 'driver_photos/new.png'
        driver.save(update_fields=['photo_pending'])
        st = req.item_status(driver, 'profile_photo', req.for_driver(driver))
        assert st['status'] == 'in_review'
        user.refresh_from_db()
        assert user.avatar.name == 'avatars/old.png'
        req.on_file_approved(driver, 'profile_photo')
        user.refresh_from_db()
        assert user.avatar.name == driver.photo_pending.name


class TestIdentity:
    def test_aadhaar_keeps_only_the_last_four_and_pan_is_encrypted(self, world):  # noqa: F811
        driver = new_driver(world)
        client = client_for(driver.user_id)
        full = client.patch(API + 'identity/', {'aadhaar_last4': '1234 5678 9012'}, format='json')
        assert full.status_code == 400 and full.json()['error']['code'] == 'AADHAAR_LAST4'
        r = client.patch(API + 'identity/', {'aadhaar_last4': '9012', 'pan_number': 'abcde1234f'}, format='json')
        assert r.status_code == 200
        assert r.json()['data']['identity'] == {**r.json()['data']['identity'],
                                                'aadhaar_masked': 'XXXX XXXX 9012', 'pan_masked': '••234F'}
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute('SELECT pan_number FROM driver_driver WHERE id = %s', [driver.id])
            stored = cur.fetchone()[0]
        assert stored.startswith('enc1:') and 'ABCDE' not in stored
        assert Driver.objects.get(pk=driver.pk).pan_number == 'ABCDE1234F'


class TestVehicle:
    def test_catalogue_year_plate_and_other(self, world):  # noqa: F811
        driver = new_driver(world)
        client = client_for(driver.user_id)
        cat = get(client, 'vehicle-catalogue/?type=sedan')
        dzire = next(m for mk in cat['makes'] if mk['make'] == 'Maruti Suzuki' for m in mk['models']
                     if m['model'] == 'Dzire')
        old = client.post(API + 'vehicles/add/', {'vehicle_type': 'sedan', 'catalogue_entry': dzire['id'],
                                                  'year': 2001, 'vehicle_number': 'TS09AB1234'}, format='json')
        assert old.status_code == 400 and old.json()['error']['code'] == 'YEAR'
        bad_plate = client.post(API + 'vehicles/add/', {'vehicle_type': 'sedan', 'catalogue_entry': dzire['id'],
                                                        'year': 2022, 'vehicle_number': 'HELLO'}, format='json')
        assert bad_plate.json()['error']['code'] == 'PLATE_FORMAT'
        ok = client.post(API + 'vehicles/add/', {'vehicle_type': 'sedan', 'catalogue_entry': dzire['id'],
                                                 'year': 2022, 'color': 'White', 'vehicle_number': 'ts 09 ab 1234'},
                         format='json')
        assert ok.status_code == 201, ok.content[:300]
        v = Vehicle.objects.get(pk=ok.json()['data']['id'])
        assert (v.brand, v.model, v.vehicle_number, v.custom_make_model) == ('Maruti Suzuki', 'Dzire', 'TS09AB1234', False)
        other = client_for(new_driver(world).user_id).post(API + 'vehicles/add/', {
            'vehicle_type': 'sedan', 'custom': True, 'brand': 'Hindustan', 'model': 'Ambassador',
            'year': 2015, 'vehicle_number': 'TS09AB1234'}, format='json')
        assert other.json()['error']['code'] == 'PLATE_TAKEN'
        other = client_for(new_driver(world).user_id).post(API + 'vehicles/add/', {
            'vehicle_type': 'sedan', 'custom': True, 'brand': 'Hindustan', 'model': 'Ambassador',
            'year': 2015, 'vehicle_number': 'TS10CD5678'}, format='json')
        assert other.status_code == 201 and Vehicle.objects.get(pk=other.json()['data']['id']).custom_make_model

    def test_the_catalogue_is_seeded_for_both_countries(self, world):  # noqa: F811
        assert VehicleCatalogueEntry.objects.filter(country='IN', curated=True).count() >= 50
        assert VehicleCatalogueEntry.objects.filter(country='US', vehicle_type='suv').count() >= 100
        assert VehicleCatalogueEntry.objects.filter(country='US', make='Toyota', model='Camry').exists()


class TestPayouts:
    def test_bank_saves_masked_encrypted_and_a_change_pauses_payouts(self, india):  # noqa: F811
        driver = new_driver(india)
        client = client_for(driver.user_id)
        body = {'method': 'bank', 'holder_name': 'Ravi Kumar', 'account_number': '123456789012',
                'account_number_confirm': '123456789012', 'ifsc': 'HDFC0001234'}
        with mock.patch('servers.driver.profile_views.lookup_ifsc', return_value={'bank': 'HDFC Bank', 'branch': 'Madhapur'}):
            mismatch = client.put(API + 'payout-method/', {**body, 'account_number_confirm': '1'}, format='json')
            assert mismatch.json()['error']['code'] == 'ACCOUNT_MISMATCH'
            first = client.put(API + 'payout-method/', body, format='json').json()['data']
        assert first['bank']['account_masked'] == '••9012' and first['held_until'] is None
        assert first['bank']['verification_status'] == 'pending_config'
        assert '123456789012' not in str(first)
        with mock.patch('servers.notifications.dispatcher.notify') as notify:
            changed = client.put(API + 'payout-method/', {'method': 'upi', 'upi_id': 'ravi@okaxis'}, format='json')
        assert changed.json()['data']['held_until'] and notify.called
        r = client.post(API + 'withdrawals/request/', {'amount': '200'}, format='json')
        assert r.json()['error']['code'] == 'PAYOUT_DETAILS_CHANGED'

    def test_us_stripe_onboarding_link_fake_on_qa(self, world, settings):  # noqa: F811
        settings.PAYOUT_FAKE_ALLOWED = True
        settings.STRIPE_SECRET_KEY = ''
        driver = new_driver(world, calling='1', node='US-TX-AUS')
        client = client_for(driver.user_id)
        r = client.post(API + 'payout-method/stripe/', {}, format='json')
        assert r.status_code == 200 and 'fake=1' in r.json()['data']['url']
        assert client.post(API + 'payout-method/stripe/refresh/', {'fake_done': True},
                           format='json').json()['data']['stripe_status'] == 'ready'
        assert client.put(API + 'payout-method/', {'method': 'upi', 'upi_id': 'x@y'},
                          format='json').json()['error']['code'] == 'USE_STRIPE'


class TestGate:
    def complete(self, driver):
        """Everything an Indian driver provides, as files and details."""
        user = driver.user_id
        user.dob = date(1990, 1, 1)
        user.save(update_fields=['dob'])
        for col in ('photo_pending', 'address_proof_doc', 'aadhaar_doc', 'pan_doc', 'police_verification_doc',
                    'license_doc', 'license_doc_back'):
            setattr(driver, col, f'{col}/a.pdf')
        driver.license_number, driver.license_expiry = 'TS0920200001234', date.today() + timedelta(days=900)
        driver.aadhaar_last4, driver.pan_number, driver.upi_id = '9012', 'ABCDE1234F', 'ravi@okaxis'
        driver.save()
        DriverAddress.objects.create(driver=driver, kind='current', line1='1 Rd', city='Hyd', state_code='TG',
                                     postal_code='500034', country='IN')
        DriverEmergencyContact.objects.create(driver=driver, position=1, name='L', relationship='spouse',
                                              phone_number='+919876500001')
        v = Vehicle.objects.create(driver_id=driver, vehicle_type_id_id=Vehicle._meta.get_field('vehicle_type_id')
                                   .related_model.objects.get_or_create(type='sedan')[0].id,
                                   vehicle_number='TS09ZZ0001', brand='Maruti Suzuki', model='Dzire', year=2022,
                                   insurance_expiry=date.today() + timedelta(days=300),
                                   permit_expiry=date.today() + timedelta(days=300),
                                   fitness_expiry=date.today() + timedelta(days=300),
                                   puc_expiry=date.today() + timedelta(days=300))
        for col in ('rc_doc', 'insurance_doc', 'permit_doc', 'fitness_doc', 'puc_doc', 'vehicle_pic', 'photo_back',
                    'photo_left', 'photo_right', 'photo_interior'):
            setattr(v, col, f'{col}/a.png')
        v.save()
        driver.active_vehicle = v
        driver.save(update_fields=['active_vehicle'])
        return v

    def test_approval_waits_for_every_blocking_item_then_unblocks(self, world):  # noqa: F811
        driver = new_driver(world)
        ops, _ = operator('kyc_verifier', world.geo)
        r = ops.post(f'/api/v1/driver/admin/{driver.id}/approve/', {}, format='json')
        assert r.status_code == 400 and 'Profile photo' in str(r.json()['error'])
        assert req.checklist(driver)['blocking']
        self.complete(driver)
        reject = ops.post(f'/api/v1/driver/admin/{driver.id}/documents/review/', {
            'document_key': 'police_verification', 'decision': 'reject', 'reason': 'Certificate is blurred'},
            format='json')
        assert reject.status_code == 201, reject.content[:300]
        row = next(i for i in get(client_for(driver.user_id), 'requirements/')['checklist']['items']
                   if i['key'] == 'police_verification')
        assert row['status'] == 'rejected' and row['reason'] == 'Certificate is blurred'
        assert ops.post(f'/api/v1/driver/admin/{driver.id}/approve/', {}, format='json').status_code == 400
        driver.police_verification_doc = 'police_verification_docs/b.pdf'
        driver.save(update_fields=['police_verification_doc'])
        r = ops.post(f'/api/v1/driver/admin/{driver.id}/approve/', {}, format='json')
        assert r.status_code == 200, r.content[:400]
        driver.refresh_from_db()
        assert driver.approved and driver.police_verification_status == 'verified'
        assert not req.checklist(driver)['blocking']
        assert DriverDocumentReview.objects.filter(driver=driver, document_key='vehicle_photo_interior',
                                                   state='approved').exists()
        assert driver.user_id.avatar.name == driver.photo_pending.name


class TestIntegrity:
    def test_the_scan_finds_and_fixes_profiles_and_wallets_and_alerts(self, world):  # noqa: F811
        from servers.command_centre.models import DATA_INTEGRITY, OperatorAlert
        from servers.platform_ops.integrity import scan
        from servers.rider.models import Wallet

        orphan = User.objects.create_user(phone_number='+919000000901', username='+919000000901', role='driver')
        walletless = new_driver(world)
        Wallet.objects.filter(user_id=walletless.user_id).delete()
        found = scan(fix=False)
        assert orphan.id in found['role_without_profile']['driver']
        assert walletless.id in found['driver_without_wallet']['drivers']
        assert OperatorAlert.objects.filter(kind=DATA_INTEGRITY).exists()
        fixed = scan(fix=True)
        assert fixed['role_without_profile']['fixed'] >= 1 and fixed['driver_without_wallet']['fixed'] >= 1
        assert Driver.objects.filter(user_id=orphan).exists()
        again = scan(fix=False)
        assert again['role_without_profile']['driver'] == [] and again['driver_without_wallet']['found'] == 0


class TestLanguagesAndCatalogueAdmin:
    def test_profile_offers_only_the_markets_languages(self, world):  # noqa: F811
        india = client_for(new_driver(world).user_id).get('/api/v1/auth/profile/').json()['data']
        assert india['market_languages'] == ['en', 'te', 'hi'] and india['market_country'] == 'IN'
        rider = User.objects.create_user(phone_number='+12125550123', username='+12125550123', role='rider')
        us = client_for(rider).get('/api/v1/auth/profile/').json()['data']
        assert us['market_languages'] == ['en', 'es']

    def test_admins_extend_and_retire_catalogue_entries(self, world):  # noqa: F811
        admin, _ = operator('super_admin', world.geo)
        r = admin.post('/api/v1/driver/admin/vehicle-catalogue/', {
            'country': 'IN', 'vehicle_type': 'sedan', 'make': 'Hindustan', 'model': 'Ambassador',
            'reason': 'Common in Kolkata'}, format='json')
        assert r.status_code == 201, r.content[:300]
        dup = admin.post('/api/v1/driver/admin/vehicle-catalogue/', {
            'country': 'IN', 'vehicle_type': 'sedan', 'make': 'Hindustan', 'model': 'Ambassador'}, format='json')
        assert dup.json()['error']['code'] == 'DUPLICATE'
        off = admin.patch(f"/api/v1/driver/admin/vehicle-catalogue/{r.json()['data']['id']}/",
                          {'is_active': False}, format='json')
        assert off.status_code == 200 and off.json()['data']['is_active'] is False
        from servers.admin_audit.models import AdminAuditLog
        assert AdminAuditLog.objects.filter(action='vehicle_catalogue_changed').exists()
        listed = admin.get('/api/v1/driver/admin/vehicle-catalogue/?country=IN&type=sedan&q=ambassador').json()['data']
        assert listed['count'] == 1
