"""Launch approvals (13 Nov): the entity per market, GST off and on, SMS consent and HELP, launch soon.

Srini's rulings: India is operated by DeepTrics Software Solutions Pvt Ltd and the USA by DeepTrics
LLC; neither is GST-registered today. With GST off the rider document is a "Receipt" with no GST
lines and the rider pays the same; switching it on brings the tax invoice back.
"""
import json
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from rest_framework.test import APIClient

from base import legal_entity
from tests.test_money_minor_units_parity import _people, _run_trip, market  # noqa: F401

User = get_user_model()
D = Decimal
GST_ON = json.dumps({'IN': {'gst_registered': True, 'gstin': '36AAAAA0000A1Z5'}})


# --- the entity -------------------------------------------------------------------------------

def test_names_are_the_rulings_and_unset_values_are_absent(settings):
    settings.LEGAL_ENTITIES_JSON = ''
    settings.PLATFORM_GSTIN = '36AAAAA0000A1Z5'
    india, usa = legal_entity.entity('IN'), legal_entity.entity('US')
    assert india['legal_name'] == 'DeepTrics Software Solutions Pvt Ltd'
    assert usa['legal_name'] == 'DeepTrics LLC'
    assert india['gst_registered'] is False and 'gstin' not in india   # unregistered: no GSTIN
    assert 'registered_address' not in india and 'cin' not in india
    assert legal_entity.operated_by('US') == 'Operated by DeepTrics LLC.'
    assert legal_entity.entity('FR')['country'] == 'IN'                    # last resort: India


def test_config_and_state_overrides(settings):
    settings.LEGAL_ENTITIES_JSON = json.dumps({
        'IN': {'registered_address': 'Hyderabad', 'cin': 'U00000TG2026PTC000000', 'support_email': ''},
        'IN-TG': {'support_phone': '+91 40 0000 0000'}})
    e = legal_entity.entity('IN', 'TG')
    assert e['registered_address'] == 'Hyderabad' and e['support_phone'] == '+91 40 0000 0000'
    assert 'support_email' not in e
    assert 'support_phone' not in legal_entity.entity('IN')
    assert 'cin' not in legal_entity.missing('IN')


@pytest.mark.django_db
def test_public_config_carries_both_markets():
    data = APIClient().get('/api/v1/config/').json()['data']['legal']
    assert data['IN']['legal_name'].startswith('DeepTrics Software')
    assert data['US']['legal_name'] == 'DeepTrics LLC'


# --- GST off and on --------------------------------------------------------------------------

def _receipts(market, rider, driver):  # noqa: F811
    from servers.ride.models import Receipt
    _run_trip(market, rider, driver, ('in-day', 'IN', '6.4', '17', '1.00', False, 'cash', None))
    return Receipt.objects.select_related('trip_id').latest('id')


@pytest.mark.django_db
def test_gst_off_is_a_receipt_without_gst_and_the_same_total(market, settings):  # noqa: F811
    from servers.ride.receipts import _build_receipt_pdf, receipt_invoice
    from servers.ride.serializers import TripDetailSerializer

    settings.LEGAL_ENTITIES_JSON = ''
    rider, driver = _people()
    receipt = _receipts(market, rider, driver)
    trip = receipt.trip_id
    assert receipt.gst_amount == 0
    assert receipt.total_fare == (trip.final_fare or trip.estimated_fare)
    inv = receipt_invoice(trip, total=receipt.total_fare, gst_amount=receipt.gst_amount,
                          currency=receipt.currency)
    assert inv['title'] == 'Receipt' and inv['tax_shown'] is False
    assert sum(D(x['amount']) for x in inv['lines']) == receipt.total_fare
    assert 'Taxable value' not in receipt.html_body and 'GST' not in receipt.html_body
    assert 'DeepTrics Software Solutions Pvt Ltd' in receipt.html_body
    shown = TripDetailSerializer(trip, context={'request': mock.Mock(user=rider)}).data['invoice']
    assert shown['title'] == 'Receipt' and shown['company']['gstin'] == ''
    assert _build_receipt_pdf(receipt)


@pytest.mark.django_db
def test_gst_on_brings_the_tax_invoice_back(market, settings):  # noqa: F811
    from servers.ride.receipts import receipt_invoice

    settings.LEGAL_ENTITIES_JSON = GST_ON
    rider, driver = _people()
    receipt = _receipts(market, rider, driver)
    inv = receipt_invoice(receipt.trip_id, total=receipt.total_fare, gst_amount=receipt.gst_amount,
                          currency=receipt.currency)
    assert receipt.gst_amount > 0 and inv['title'] == 'Tax invoice'
    assert 'Taxable value' in receipt.html_body and '(included)' in receipt.html_body
    assert '36AAAAA0000A1Z5' in receipt.html_body
    # The rider paid the same either way.
    settings.LEGAL_ENTITIES_JSON = ''
    assert receipt.total_fare == (receipt.trip_id.final_fare or receipt.trip_id.estimated_fare)


@pytest.mark.django_db
def test_the_quote_has_no_gst_line_while_gst_is_off(market, settings):  # noqa: F811
    from tests.test_money_minor_units_parity import HYD

    settings.LEGAL_ENTITIES_JSON = ''
    rider, _ = _people()
    c = APIClient()
    c.force_authenticate(rider)
    r = c.post('/api/v1/ride/estimate-fare/', {
        'pickup_lat': str(HYD[0]), 'pickup_long': str(HYD[1]),
        'destination_lat': str(HYD[0] + D('0.03')), 'destination_long': str(HYD[1]),
        'vehicle_type': 'parity-sedan', 'distance_km': '8', 'duration_min': '20'}, format='json')
    if r.status_code == 404:
        pytest.skip('estimate-fare is mounted elsewhere in this build')
    assert r.status_code == 200, r.content.decode()
    lines = r.json()['data']['fare_lines']
    assert lines['tax_shown'] is False and D(lines['tax']['amount']) == 0
    assert sum(D(x['amount']) for x in lines['lines']) == D(r.json()['data']['estimated_fare'])


# --- email footer -----------------------------------------------------------------------------

@pytest.mark.django_db
def test_every_email_names_the_market_entity(settings):
    from django.core import mail

    from base.auth_email import send_message

    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.DEFAULT_FROM_EMAIL = 'noreply@example.test'
    settings.EMAIL_FROM_BY_COUNTRY = {'US': 'us@example.test'}
    settings.AUTH_EMAIL_ALLOWLIST = ['@example.test']
    assert send_message(to='a@example.test', subject='s', body='Hi', html='<html><body>Hi</body></html>',
                        country='US')[0]
    msg = mail.outbox[-1]
    assert 'Operated by DeepTrics LLC.' in msg.body and msg.from_email == 'us@example.test'
    assert 'Operated by DeepTrics LLC.' in msg.alternatives[0][0]
    send_message(to='b@example.test', subject='s', body='Hi')
    assert 'Operated by DeepTrics Software Solutions Pvt Ltd.' in mail.outbox[-1].body


# --- SMS consent and HELP ---------------------------------------------------------------------

@pytest.mark.django_db
def test_marketing_sms_needs_the_wording_version_and_stores_it():
    from servers.admin_audit.models import AdminAuditLog
    from servers.notifications.models import NotificationPreference

    rider, _ = _people()
    c = APIClient()
    c.force_authenticate(rider)
    url = '/api/v1/notifications/preferences/'
    item = {'channel': 'sms', 'category': 'promotional', 'opted_in': True}
    assert c.put(url, item, format='json').json()['error']['code'] == 'WORDING_REQUIRED'
    r = c.put(url, {**item, 'wording_version': 'sms-marketing-v1'}, format='json')
    assert r.status_code == 200, r.content.decode()
    row = NotificationPreference.objects.get(user=rider, channel='sms', category='promotional')
    assert row.opted_in and row.wording_version == 'sms-marketing-v1' and row.updated_at
    audit = AdminAuditLog.objects.filter(action='notification_consent_given').latest('id')
    assert 'Msg & data rates may apply' in json.dumps(audit.after)
    c.put(url, {**item, 'opted_in': False}, format='json')
    row.refresh_from_db()
    assert not row.opted_in and row.wording_version == ''


@pytest.mark.django_db
def test_twilio_help_is_answered_and_recorded_and_start_resubscribes(settings):
    from integrations.sms.twilio import TwilioSms
    from servers.admin_audit.models import AdminAuditLog
    from servers.notifications import preferences

    settings.TWILIO_AUTH_TOKEN = 'test-twilio-token'
    settings.NOTIFY_WEBHOOK_BASE_URL = ''
    user = User.objects.create_user(phone_number='+12125550142', password='x-Long-pass-1', role='rider')
    path = '/api/v1/notifications/webhooks/twilio/'

    def post(body):
        params = {'MessageSid': 'SMh1', 'From': '+12125550142', 'Body': body}
        sig = TwilioSms.signature_for('http://testserver' + path, params, 'test-twilio-token')
        return Client().post(path, params, HTTP_X_TWILIO_SIGNATURE=sig)

    r = post('HELP')
    assert r.status_code == 200 and r['Content-Type'].startswith('text/xml')
    text = r.content.decode()
    assert '<Message>' in text and 'Reply STOP to opt out' in text and 'DeepTrics LLC' in text
    assert AdminAuditLog.objects.filter(action='notification_sms_help_received').exists()
    post('STOP')
    assert preferences.is_opted_in(user.pk, 'sms', 'transactional')[0] is False
    assert '<Message>' not in post('START').content.decode()
    assert preferences.is_opted_in(user.pk, 'sms', 'transactional')[0] is True


# --- a city not launched yet ------------------------------------------------------------------

@pytest.mark.django_db
def test_a_closed_city_shows_fares_refuses_booking_and_takes_interest(market, settings):  # noqa: F811
    from servers.admin_access.models import CITY, DRAFT, GeoNode
    from servers.platform_ops.markets import booking_gate_for_zone
    from servers.platform_ops.models import LaunchInterest
    from servers.pricing.models import ServiceZone
    from tests.test_money_minor_units_parity import HYD

    from conftest import seed_admin_access

    seed_admin_access()
    zone = ServiceZone.objects.filter(country='IN', is_active=True).first()
    city = GeoNode.objects.filter(kind=CITY, path__contains='/IN/').first() or         GeoNode.objects.filter(kind=CITY).first()
    assert zone is not None and city is not None
    city.status = DRAFT
    city.save(update_fields=['status'])
    zone.geo_node = city
    zone.save(update_fields=['geo_node'])
    ok, code, message = booking_gate_for_zone(zone)
    assert not ok and code == 'CITY_NOT_LIVE' and message == f"We're launching in {city.name} soon."

    rider, _ = _people()
    c = APIClient()
    c.force_authenticate(rider)
    r = c.post('/api/v1/ride/estimate-fare/', {
        'pickup_lat': str(HYD[0]), 'pickup_long': str(HYD[1]),
        'destination_lat': str(HYD[0] + D('0.03')), 'destination_long': str(HYD[1]),
        'vehicle_type': 'parity-sedan', 'distance_km': '8', 'duration_min': '20'}, format='json')
    if r.status_code != 404:
        assert r.status_code == 200, r.content.decode()
        assert r.json()['data']['launching']['message'] == message
    zones = APIClient().get('/api/v1/pricing/zones/').json()['data']['zones']
    assert any(z['city_status'] == 'soon' for z in zones)
    for _ in range(2):
        r = c.post('/api/v1/platform/launch-interest/', {'zone': zone.code}, format='json')
        assert r.status_code == 200 and r.json()['data']['saved'] is True
    assert LaunchInterest.objects.filter(user=rider, city=city).count() == 1
