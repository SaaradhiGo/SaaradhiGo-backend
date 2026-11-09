"""Trusted contacts, trip share links, the public trip page and SOS to contacts (2026-11-09)."""

import json
import re
from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

import pytest
from django.utils import timezone

from servers.ride import eta
from servers.ride.models import Trip, TripStatus
from servers.sharing import services, sms
from servers.sharing.models import ContactSms, SharingPreference, SmsOptOut, TripShare, TrustedContact

pytestmark = pytest.mark.django_db

CONTACTS = '/api/v1/sharing/contacts/'


def _trip(rider, driver, code, **kw):
    st, _ = TripStatus.objects.get_or_create(status_code=code)
    return Trip.objects.create(
        user_id=rider, driver_id=driver.driver if driver else None,
        pickup_lat=D('17.4'), pickup_long=D('78.4'),
        pickup_address='Flat 302, Sai Residency, Madhapur, Hyderabad, Telangana 500081, India',
        destination_lat=D('17.45'), destination_long=D('78.36'),
        destination_address='Plot 7, Road 12, Banjara Hills, Hyderabad, Telangana, India',
        estimated_fare=D('187.00'), status_id=st, **kw)


@pytest.fixture
def rider(auth_client_rider):
    client, user = auth_client_rider
    user.full_name = 'Asha Rao Kumar'
    user.save()
    return client, user


def _add(client, name='Ravi', phone='9876543210'):
    return client.post(CONTACTS, {'name': name, 'phone': phone}, format='json')


# --- contacts -----------------------------------------------------------------

def test_add_list_remove_and_the_added_sms(rider):
    client, user = rider
    r = _add(client)
    assert r.status_code == 201, r.content
    contact = r.json()['data']['contact']
    assert contact['phone'] == '+919876543210'
    sent = ContactSms.objects.get()
    assert (sent.kind, sent.status) == ('added', 'sink')
    listed = client.get(CONTACTS).json()['data']
    assert [c['id'] for c in listed['contacts']] == [contact['id']]
    assert listed['note'].startswith('Your contacts only see this trip')
    assert listed['contacts'][0]['last_message']['kind'] == 'added'
    assert listed['contacts'][0]['last_message']['status'] == 'sink'
    assert client.delete(f'{CONTACTS}{contact["id"]}/').status_code == 200
    assert not TrustedContact.objects.exists()


def test_the_added_text(rider, settings):
    settings.PLATFORM_BRAND_NAME = 'SaaradhiGo'
    with mock.patch.object(sms, 'send') as send:
        _add(rider[0])
    assert send.call_args.args[3] == ('Asha added you as a trusted contact on SaaradhiGo. '
                                      'Reply STOP to opt out.')


def test_five_contacts_at_most(rider):
    client = rider[0]
    for i in range(5):
        assert _add(client, f'C{i}', f'98765432{i}0').status_code == 201
    r = _add(client, 'Sixth', '9876543299')
    assert r.status_code == 409 and r.json()['error']['code'] == 'CONTACT_LIMIT'


@pytest.mark.parametrize('phone', ['12345', '+15551234567', 'abc', ''])
def test_phone_must_fit_the_riders_market(rider, phone):
    r = _add(rider[0], phone=phone)
    assert r.status_code == 400 and r.json()['error']['details']['field'] == 'phone'


def test_same_number_twice_is_refused(rider):
    _add(rider[0])
    assert _add(rider[0], 'Again', '+91 98765 43210').status_code == 409


def test_contacts_are_private_to_the_rider(rider, auth_client_driver):
    contact = TrustedContact.objects.create(rider=auth_client_driver[1], name='X', phone='+919876500000')
    assert rider[0].delete(f'{CONTACTS}{contact.id}/').status_code == 404


# --- SMS rules ---------------------------------------------------------------

def test_stop_opts_a_number_out(rider, api_client, settings):
    settings.SHARING_SMS_INBOUND_TOKEN = 'hook-test-value'
    client, user = rider
    _add(client)
    r = api_client.post('/api/v1/sharing/sms/inbound/', {'from': '+919876543210', 'text': ' stop '},
                        format='json', HTTP_X_SMS_WEBHOOK_TOKEN='hook-test-value')
    assert r.status_code == 200 and SmsOptOut.objects.filter(phone='+919876543210').exists()
    contact = TrustedContact.objects.get()
    assert sms.send(user, contact, 'sos', 'x') is False
    assert ContactSms.objects.last().status == 'opted_out'


def test_inbound_needs_the_webhook_token(api_client, settings):
    settings.SHARING_SMS_INBOUND_TOKEN = 'hook-test-value'
    r = api_client.post('/api/v1/sharing/sms/inbound/', {'from': '+919876543210', 'text': 'STOP'},
                        format='json', HTTP_X_SMS_WEBHOOK_TOKEN='wrong')
    assert r.status_code == 404 and not SmsOptOut.objects.exists()


def test_rate_limits(rider, settings):
    settings.SHARING_SMS_PER_CONTACT_PER_HOUR = 2
    settings.SHARING_SMS_PER_RIDER_PER_HOUR = 3
    user = rider[1]
    a = TrustedContact.objects.create(rider=user, name='A', phone='+919876500001')
    b = TrustedContact.objects.create(rider=user, name='B', phone='+919876500002')
    assert [sms.send(user, a, 'trip_share', 'x') for _ in range(3)] == [True, True, False]
    assert sms.send(user, b, 'trip_share', 'x') is True
    assert sms.send(user, b, 'trip_share', 'x') is False       # rider limit (3 counted)
    assert sms.send(user, b, 'sos', 'x') is True               # SOS ignores the rider limit
    assert sms.send(user, b, 'sos', 'x') is False              # but not the contact's


def test_live_mode_uses_the_login_code_provider_and_logs_no_text(rider, settings):
    settings.SHARING_SMS_MODE = 'live'
    user = rider[1]
    c = TrustedContact.objects.create(rider=user, name='A', phone='+919876500001')
    with mock.patch('base.utils.send_otp_via_sns') as task, \
            mock.patch('base.utils.stash_sms_message', return_value='ref-1'):
        assert sms.send(user, c, 'sos', 'secret words') is True
    task.delay.assert_called_once_with('+919876500001', 'ref-1')
    row = ContactSms.objects.get()
    assert row.status == 'queued'
    assert 'secret words' not in json.dumps(
        {f.name: str(getattr(row, f.name)) for f in ContactSms._meta.fields})


# --- share links -------------------------------------------------------------

def test_manual_share_stop_and_reissue(rider, auth_client_driver):
    client, user = rider
    trip = _trip(user, auth_client_driver[1], 'accepted')
    url = f'/api/v1/sharing/trips/{trip.id}/share/'
    first = client.post(url).json()['data']
    assert first['active'] and first['url'].endswith(TripShare.objects.get().token)
    token = TripShare.objects.get().token
    assert len(token) >= 43                                       # 32 random bytes
    assert client.post(url).json()['data']['url'] == first['url']  # one per trip
    assert client.delete(url).json()['data']['active'] is False
    again = client.post(url).json()['data']
    assert again['url'] != first['url']                           # a stopped link never returns


def test_only_the_rider_can_share(rider, auth_client_driver):
    trip = _trip(rider[1], auth_client_driver[1], 'accepted')
    assert auth_client_driver[0].post(f'/api/v1/sharing/trips/{trip.id}/share/').status_code == 404


def test_cannot_share_before_a_driver_or_after_the_end(rider):
    for code in ('requested', 'completed'):
        trip = _trip(rider[1], None, code)
        assert rider[0].post(f'/api/v1/sharing/trips/{trip.id}/share/').status_code == 409


def test_link_expires_30_minutes_after_the_trip_ends(rider, auth_client_driver):
    trip = _trip(rider[1], auth_client_driver[1], 'in_progress')
    share, _ = services.start_share(trip)
    done = TripStatus.objects.get_or_create(status_code='completed')[0]
    Trip.objects.filter(pk=trip.pk).update(status_id=done, completed_at=timezone.now())
    share.refresh_from_db()
    assert services.share_is_live(share)
    later = timezone.now() + timedelta(minutes=31)
    assert not services.share_is_live(share, now=later)


# --- the public page ---------------------------------------------------------

def _public(api_client, token):
    r = api_client.get(f'/api/v1/sharing/public/{token}/')
    assert r.status_code == 200 and 'noindex' in r['X-Robots-Tag']
    return r.json()['data']


def test_public_page_shows_areas_names_vehicle_and_eta(rider, auth_client_driver, api_client):
    driver_user = auth_client_driver[1]
    driver_user.full_name = 'Suresh Babu'
    driver_user.save()
    trip = _trip(rider[1], driver_user, 'in_progress')
    share, _ = services.start_share(trip)
    with mock.patch.object(eta, 'driver_position', return_value=(17.42, 78.38)):
        page = _public(api_client, share.token)
    assert page['live'] and page['status'] == 'in_progress'
    assert page['rider_first_name'] == 'Asha'
    assert page['driver_first_name'] == 'Suresh'
    assert page['pickup_area'] == 'Madhapur, Hyderabad'
    assert page['destination_area'] == 'Banjara Hills, Hyderabad'
    assert page['position'] == {'lat': 17.42, 'lng': 78.38}
    assert page['arrive_at'] and page['eta_min'] >= 1


def test_public_page_never_shows_phones_fare_or_full_name(rider, auth_client_driver, api_client):
    """The automated privacy check for the public trip page."""
    client, user = rider
    _add(client)
    trip = _trip(user, auth_client_driver[1], 'accepted')
    share, _ = services.start_share(trip)
    with mock.patch.object(eta, 'driver_position', return_value=(17.39, 78.40)):
        raw = api_client.get(f'/api/v1/sharing/public/{share.token}/').content.decode()
    for phone in (user.phone_number, auth_client_driver[1].phone_number, '+919876543210'):
        assert phone.lstrip('+')[-10:] not in raw
    assert not re.search(r'\d{10}', raw)
    assert '187' not in raw and 'fare' not in raw.lower()
    assert 'Asha Rao Kumar' not in raw and 'Rao' not in raw
    assert 'Flat 302' not in raw and 'Sai Residency' not in raw and '500081' not in raw


def test_after_expiry_and_for_unknown_tokens_the_trip_has_ended(rider, auth_client_driver, api_client):
    trip = _trip(rider[1], auth_client_driver[1], 'accepted')
    share, _ = services.start_share(trip)
    services.stop_share(trip)
    assert _public(api_client, share.token) == {'live': False, 'message': 'This trip has ended.'}
    assert _public(api_client, 'not-a-token') == {'live': False, 'message': 'This trip has ended.'}


# --- auto-share and SOS ------------------------------------------------------

def test_auto_share_is_off_by_default_and_shares_on_start(rider, auth_client_driver):
    client, user = rider
    _add(client)
    trip = _trip(user, auth_client_driver[1], 'in_progress')
    assert client.get('/api/v1/sharing/settings/').json()['data']['auto_share'] is False
    assert services.on_trip_started(trip.id) == 0
    assert client.patch('/api/v1/sharing/settings/', {'auto_share': True},
                        format='json').json()['data']['auto_share'] is True
    assert services.on_trip_started(trip.id) == 1
    assert TripShare.objects.get(trip=trip).source == 'auto'
    assert ContactSms.objects.filter(kind='trip_share', status='sink').count() == 1


def test_sos_sends_every_contact_the_link(rider, auth_client_driver, settings):
    settings.PLATFORM_BRAND_NAME = 'SaaradhiGo'
    client, user = rider
    _add(client, 'Ravi', '9876543210')
    _add(client, 'Meena', '9876543211')
    trip = _trip(user, auth_client_driver[1], 'in_progress')
    texts = []
    real = sms.send

    def spy(*args, **kw):
        texts.append(args[3])
        return real(*args, **kw)

    with mock.patch.object(sms, 'send', side_effect=spy):
        assert services.on_sos(trip, user) == 2
    link = services.share_url(TripShare.objects.get(trip=trip))
    assert texts == [f'Asha pressed SOS during a SaaradhiGo trip. Track the trip: {link}. '
                     f'If you think they are in danger, call the local emergency number.'] * 2
    assert ContactSms.objects.filter(kind='sos').count() == 2


def test_a_driver_sos_does_not_message_the_riders_contacts(rider, auth_client_driver):
    client, user = rider
    _add(client)
    trip = _trip(user, auth_client_driver[1], 'in_progress')
    assert services.on_sos(trip, auth_client_driver[1]) == 0


def test_dispatch_sos_reaches_the_contacts(rider, auth_client_driver):
    from servers.sos.models import SOSEvent
    from servers.sos.tasks import dispatch_sos

    client, user = rider
    _add(client)
    trip = _trip(user, auth_client_driver[1], 'in_progress')
    event = SOSEvent.objects.create(user=user, trip=trip, initiated_by='rider')
    with mock.patch('servers.auth_user.services.send_push_notification'):
        dispatch_sos.run(event.id)
    assert ContactSms.objects.filter(kind='sos').count() == 1


def test_area_of():
    assert services.area_of('Mindspace, Hitech City') == 'Mindspace, Hitech City'
    assert services.area_of('Gachibowli') == 'Gachibowli'
    assert services.area_of('') == ''
    assert services.area_of('1600 Amphitheatre Pkwy, Mountain View, CA 94043, USA') == 'Mountain View, CA'
    assert SharingPreference.objects.count() == 0
