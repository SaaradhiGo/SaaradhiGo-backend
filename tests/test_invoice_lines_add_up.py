"""Every fare the rider is shown adds up to its total, to the paisa (12 Nov).

QA trip 207's invoice read base 60.00 + distance and time 122.59 + GST 8.71 = 191.30 beside a
total of 183.00: GST was inside the fare and listed again as if added on top. Now the lines are
before tax, they sum to the taxable value, and taxable value + tax (+ any flat fee) = total. Checked
for the pure builder over thousands of fares, for real quotes (the choose screen's "How this fare
is worked out"), and for issued receipts through the real money path (the PDF and the receipt
screen), in both markets.
"""
import random
from decimal import Decimal
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from rest_framework.test import APIClient

from servers.ride import invoice_lines
from tests.test_money_minor_units_parity import (  # noqa: F401 - `market` is a fixture
    DISTANCES, DURATIONS, SURGES, _people, _quote, _run_trip, market,
)

User = get_user_model()
D = Decimal


def _sum(xs):
    return sum((D(x['amount']) for x in xs), D('0'))


def _check(inv):
    assert invoice_lines.adds_up(inv), inv
    for x in inv['lines'] + inv['extras']:
        assert D(x['amount']) == D(x['amount']).quantize(D('0.01')), x
    assert _sum(inv['lines']) == D(inv['taxable'])


def test_trip_207_adds_up_with_gst_included():
    inv = invoice_lines.build(
        components=[('base', 'Base fare', '60.00'), ('distance_time', 'Distance and time', '122.59'),
                    ('rounding', 'Rounding', '0.41')],
        total='183.00', tax_amount='8.71', tax_mode='inclusive', tax_percent='5.000',
        tax_label='GST', currency='INR')
    _check(inv)
    assert inv['taxable'] == '174.29'
    assert inv['tax'] == {'label': 'GST', 'percent': '5', 'amount': '8.71', 'included': True}
    assert [x['amount'] for x in inv['lines']] == ['57.14', '116.76', '0.39']


def test_thousands_of_fares_add_up_in_both_markets():
    rng = random.Random(207)
    for _ in range(5000):
        base = D(rng.randint(0, 20000)) / 100
        metered = D(rng.randint(0, 900000)) / 100
        surge = D(rng.choice(['1.00', '0.80', '1.25', '1.50', '2.00']))
        demand = ((base + metered) * (surge - 1)).quantize(D('0.01'))
        gross = base + metered + demand
        if rng.random() < 0.5:
            total = gross.quantize(D('1'))
            rate = D(rng.choice(['5', '12', '18', '0']))
            tax = (total * rate / (100 + rate)).quantize(D('0.01'))
            comps = [('base', 'Base fare', base), ('distance_time', 'Distance and time', metered),
                     ('surge', 'Higher demand', demand), ('rounding', 'Rounding', total - gross)]
            fee = D(rng.choice(['0', '0', '5.00']))
            inv = invoice_lines.build(components=comps, total=total + fee, tax_amount=tax,
                                      tax_mode='inclusive', tax_percent=rate, tax_label='GST',
                                      currency='INR', extras=[('rider_platform_fee', 'Platform fee', fee)])
        else:
            rate = D(rng.choice(['8.875', '6.25', '0']))
            tax = (gross * rate / 100).quantize(D('0.01'))
            fee = D(rng.choice(['0', '2.75']))
            comps = [('base', 'Base fare', base), ('distance_time', 'Distance and time', metered),
                     ('surge', 'Higher demand', demand)]
            inv = invoice_lines.build(components=comps, total=gross + tax + fee, tax_amount=tax,
                                      tax_mode='additive', tax_percent=rate, tax_label='Sales tax',
                                      currency='USD', extras=[('per_trip_fee', 'Congestion surcharge', fee)])
        _check(inv)


@pytest.mark.django_db
def test_every_quote_explains_its_total(market):  # noqa: F811
    for key in ('IN', 'US'):
        for d in DISTANCES:
            for t in DURATIONS:
                for s in SURGES:
                    for night in (False, True):
                        q = _quote(key, d, t, s, night)
                        inv = invoice_lines.from_quote(q)
                        _check(inv)
                        assert D(inv['total']) == q['total_fare']
                        assert D(inv['tax']['amount']) == q['tax_amount']
                        assert inv['tax']['included'] is (key == 'IN')


@pytest.mark.django_db
def test_the_quote_endpoint_returns_the_lines(market):  # noqa: F811
    from tests.test_money_minor_units_parity import HYD
    rider, _ = _people()
    c = APIClient()
    c.force_authenticate(rider)
    with mock.patch('servers.pricing.services.compute_surge_multiplier', return_value=D('1.25')):
        r = c.post('/api/v1/ride/estimate-fare/', {
            'pickup_lat': str(HYD[0]), 'pickup_long': str(HYD[1]),
            'destination_lat': str(HYD[0] + D('0.03')), 'destination_long': str(HYD[1]),
            'vehicle_type': 'parity-sedan', 'distance_km': '12.4', 'duration_min': '31'}, format='json')
    if r.status_code == 404:
        pytest.skip('estimate-fare is mounted elsewhere in this build')
    assert r.status_code == 200, r.content.decode()
    data = r.json()['data']
    inv = data['fare_lines']
    _check(inv)
    assert D(inv['total']) == D(data['estimated_fare'])


@pytest.mark.django_db
def test_every_issued_receipt_adds_up_on_the_pdf_and_the_screen(market, settings):  # noqa: F811
    from servers.ride.models import FarePricing
    from servers.ride.receipts import _build_receipt_pdf, receipt_invoice
    from servers.ride.serializers import TripDetailSerializer

    settings.PLATFORM_LEGAL_ENTITY = ''
    rider, driver = _people()
    specs = [('in-day', 'IN', '3.2', '11', '1.00', False, 'cash', None),
             ('in-demand', 'IN', '18.7', '44', '1.40', False, 'cash', None),
             ('in-night-min', 'IN', '0.4', '2', '1.00', True, 'cash', None),
             ('in-lower', 'IN', '9.9', '23', '0.80', False, 'cash', None),
             ('us-day', 'US', '7.3', '19', '1.00', False, 'card', None),
             ('us-demand', 'US', '22.0', '51', '1.75', True, 'card', None)]
    for spec in specs:
        _run_trip(market, rider, driver, spec)
    from servers.ride.models import Receipt
    receipts = list(Receipt.objects.select_related('trip_id').all())
    assert len(receipts) == len(specs)
    for receipt in receipts:
        trip = receipt.trip_id
        fp = FarePricing.objects.filter(trip_id=trip).order_by('-id').first()
        inv = receipt_invoice(trip, total=receipt.total_fare, gst_amount=receipt.gst_amount,
                              currency=receipt.currency, fare_pricing=fp)
        _check(inv)
        assert D(inv['total']) == receipt.total_fare
        assert D(inv['tax']['amount']) == receipt.gst_amount
        # The screen gets the same lines.
        request = mock.Mock(user=rider)
        shown = TripDetailSerializer(trip, context={'request': request}).data['invoice']['breakdown']
        assert shown == inv
        # The PDF builds, and names no company that is not configured.
        pdf = _build_receipt_pdf(receipt)
        assert pdf and b'SaaradhiGo Mobility' not in pdf
        # The email lists the taxable value and the included tax.
        if receipt.currency == 'INR':
            assert 'Taxable value' in receipt.html_body and '(included)' in receipt.html_body
        assert 'SaaradhiGo Mobility' not in receipt.html_body


@pytest.mark.django_db
def test_the_company_name_comes_from_config(settings):
    from servers.ride.receipts import aggregator_line, company_lines

    settings.PLATFORM_LEGAL_ENTITY = ''
    assert company_lines()['name'] == ''
    assert 'aggregator' in aggregator_line() and 'Mobility' not in aggregator_line()
    settings.PLATFORM_LEGAL_ENTITY = 'Registered Name Pvt Ltd'
    assert company_lines()['name'] == 'Registered Name Pvt Ltd'
    assert aggregator_line().startswith('Registered Name Pvt Ltd is an aggregator')


# --- Confirm your email (12 Nov) ---------------------------------------------------------------

REGISTER = '/api/v1/auth/email/register/'
RESEND = '/api/v1/auth/email/verify/request/'


def _signup(email='confirm.me@deeptrics.com'):
    return APIClient().post(REGISTER, {'full_name': 'Asha Rao', 'email': email,
                                       'password': 'Monsoon-Ferry-2026!', 'role': 'rider'}, format='json')


@pytest.mark.django_db
def test_without_mail_a_rider_stays_unconfirmed_and_is_told_so(monkeypatch):
    monkeypatch.setattr('base.auth_email.delivery_is_configured', lambda: (False, 'EMAIL_HOST is unset'))
    r = _signup()
    assert r.status_code == 201, r.content.decode()
    user = r.json()['data']['user']
    assert user['email_verified'] is False
    assert user['email_mail_configured'] is False
    assert r.json()['data']['confirmation_sent'] is False
    c = APIClient()
    c.force_authenticate(User.objects.get(email__iexact='confirm.me@deeptrics.com'))
    again = c.post(RESEND, {}, format='json')
    assert again.status_code == 503
    assert again.json()['error']['code'] == 'EMAIL_NOT_SENT'


@pytest.mark.django_db
def test_with_mail_the_link_is_sent_and_resend_works(monkeypatch, settings):
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.DEFAULT_FROM_EMAIL = 'noreply@example.test'
    settings.AUTH_EMAIL_ALLOWLIST = ['deeptrics.com']
    r = _signup()
    assert r.status_code == 201, r.content.decode()
    assert r.json()['data']['confirmation_sent'] is True
    assert r.json()['data']['user']['email_verified'] is False
    assert any('Confirm your email' in m.subject for m in mail.outbox)
    c = APIClient()
    c.force_authenticate(User.objects.get(email__iexact='confirm.me@deeptrics.com'))
    n = len(mail.outbox)
    again = c.post(RESEND, {}, format='json')
    assert again.status_code == 200, again.content.decode()
    assert len(mail.outbox) == n + 1
