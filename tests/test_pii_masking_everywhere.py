"""Personal data is masked in EVERY operator response, and revealed only through
the logged door. Phase 1(e).

THE RULE UNDER TEST (owner's ruling)
------------------------------------
  * The Analyst sees no personal data anywhere.
  * Every operator -- Super Admin included -- gets masked lists and details:
    phones, emails, usernames, street addresses, dates of birth, payout
    identifiers are never in the payload.
  * `pii.view` is the REVEAL capability: `POST /admin-access/reveal/` hands
    over the requested fields of ONE record and writes one audit row naming
    the operator, the record and the fields -- never the values.
  * What `pii.view` changes in a list: full names (first name + initial
    otherwise), exact coordinates (~1 km otherwise), document links
    (withheld otherwise).

HOW IT IS TESTED
----------------
One world with one of everything in Hyderabad (rider, driver, vehicle,
trip, receipt, payment, ledger row, payout request, ticket, SOS, alert),
each carrying a distinctive secret. Every operator route is called as every
seeded role and the WHOLE BODY is swept with regexes for raw phones,
emails, long digit runs, UPI handles and IFSC codes -- because a payload
that carries the number and a console that prints dots is masked in the
pixels and nowhere else.

Then the guards: the resolver is walked so an admin route that is not in
the inventory below fails the build; every strict-policy response is
walked key by key so a field NAMED like personal data that is returned raw
fails; the renderer cannot be swapped out per view; and `mark_revealed`
has exactly the call sites it should.
"""

import json
import pathlib
import re
import sys
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate
from rest_framework_simplejwt.tokens import AccessToken

from servers.admin_access.masking import (
    ALL_PII_FIELDS, BULLET, COORDINATE_FIELDS, DOCUMENT_FIELDS, EXPORT_REVEAL_ACTION,
    OPERATOR_IDENTITY_KEYS, PINNED_LOCATION, RAW_PII_PATTERNS, REVEAL_ACTION, exports_unmasked,
    mask_address, mask_last4, mask_payload, mask_upi, round_coordinate, STRICT, TRUSTED,
)
from servers.admin_access.models import AdminAssignment, AdminRole, GeoNode
from servers.admin_access.rendering import SELF_DESCRIBING, MaskingJSONRenderer, is_operator_path
from servers.admin_access.roles import ROLE_TEMPLATES
from servers.admin_audit.models import AdminAuditLog
from servers.approvals.models import ApprovalRequest
from servers.command_centre.models import OperatorAlert
from servers.driver.models import (
    Driver, DriverBankAccount, Vehicle, VehicleType, WithdrawalRequest,
)
from servers.payments.models import Payment, TransactionHistory
from servers.pricing.models import ServiceZone
from servers.ride.models import FarePricing, Receipt, Trip, TripStatus
from servers.rider.models import Rider, Wallet
from servers.sos.models import SOSEvent
from servers.sos.views import CONTACT_REVEALED_ACTION
from servers.support.models import SupportMacro, SupportMessage, SupportTicket

pytestmark = pytest.mark.django_db

User = get_user_model()

ROLES = [t['code'] for t in ROLE_TEMPLATES]
PII_VIEW_ROLES = {t['code'] for t in ROLE_TEMPLATES if 'pii.view' in t['permissions']}
STRICT_ROLES = [r for r in ROLES if r not in PII_VIEW_ROLES]
assert 'analyst' in STRICT_ROLES and 'support_agent' in STRICT_ROLES

# --- the secrets planted in the world --------------------------------------

RIDER_PHONE = '+919876500001'
RIDER_EMAIL = 'ravi.kumar@example.test'
RIDER_NAME = 'Ravi Kumar'
RIDER_EMERGENCY = '+919876500099'
RIDER_HOUSE = '12-3-45'
RIDER_STREET = 'Jubilee Hills Road No 36'
RIDER_ZIP = '500033'
RIDER_DOB = '1990-05-15'

DRIVER_PHONE = '+919876500002'
DRIVER_EMAIL = 'suresh.reddy@example.test'
DRIVER_NAME = 'Suresh Reddy'
DRIVER_UPI = 'suresh.reddy@okaxis'
DRIVER_ACCOUNT = '123456789012'
DRIVER_IFSC = 'HDFC0001234'
DRIVER_LICENCE_DOC = 'license_docs/suresh-front.jpg'
VEHICLE_RC_DOC = 'rc_docs/ts09-rc.jpg'

PICKUP_ADDRESS = 'Plot 12, Jubilee Hills Road No 36, Jubilee Hills, Hyderabad, Telangana 500033'
DROP_ADDRESS = 'Gate 2, Rajiv Gandhi International Airport, Shamshabad, Hyderabad'
PICKUP_LAT, PICKUP_LNG = Decimal('17.4312345'), Decimal('78.4098765')
DROP_LAT, DROP_LNG = Decimal('17.2403456'), Decimal('78.4294567')
RECEIPT_EMAIL = 'ravi.kumar@example.test'

#: Substrings that must never appear in a masked body, whoever is reading.
NEVER_IN_A_LIST = [
    RIDER_PHONE, DRIVER_PHONE, RIDER_EMERGENCY, RIDER_EMAIL, DRIVER_EMAIL,
    RIDER_HOUSE, RIDER_STREET, RIDER_ZIP, RIDER_DOB,
    'suresh.reddy@', DRIVER_ACCOUNT, DRIVER_IFSC, 'Plot 12', 'Gate 2',
    # the raw digit tails after the country code: the mask keeps 4, not 10
    '9876500001', '9876500002', '9876500099',
]
#: ... and for anybody WITHOUT pii.view, these too.
NEVER_FOR_STRICT = [
    'Kumar', 'Reddy', str(PICKUP_LAT), str(PICKUP_LNG), str(DROP_LAT), str(DROP_LNG),
    'license_docs/', 'rc_docs/',
]

POLYGON = {
    'type': 'Polygon',
    'coordinates': [[[78.30, 17.20], [78.50, 17.20], [78.50, 17.50],
                     [78.30, 17.50], [78.30, 17.20]]],
}

_n = iter(range(1, 9999))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_throttle_window():
    """The locmem cache outlives a test's transaction, and SQLite hands the
    next test the same user ids, so one test's reveals would count against
    the next test's operator."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def local_document_storage(tmp_path, monkeypatch):
    """The document fields point at private S3, which cannot sign a URL
    here. A local storage lets `license_doc` render a link, which is what
    the documents rule is about."""
    from django.core.files.storage import FileSystemStorage

    local = FileSystemStorage(location=str(tmp_path), base_url='/media/test/')
    for model, name in ((Driver, 'license_doc'), (Driver, 'license_doc_back'),
                        (Vehicle, 'rc_doc'), (Vehicle, 'vehicle_pic'), (User, 'avatar'),
                        (Receipt, 'pdf_file')):
        monkeypatch.setattr(model._meta.get_field(name), 'storage', local, raising=False)


@pytest.fixture
def geo(db):
    from conftest import seed_admin_access

    seed_admin_access()
    return {n.code: n for n in GeoNode.objects.all()}


def _zone(code, node, **extra):
    zone, _ = ServiceZone.objects.get_or_create(
        code=code,
        defaults={'name': node.name, 'zone_type': 'city', 'city': node.name,
                  'polygon_geojson': POLYGON, 'is_active': True, **extra},
    )
    if zone.geo_node_id != node.id:
        zone.geo_node = node
        zone.save()
    return zone


def _status(code):
    obj, _ = TripStatus.objects.get_or_create(status_code=code)
    return obj


@pytest.fixture
def world(geo):
    """One of everything in Hyderabad, each carrying a secret; the Austin
    driver is the out-of-scope control."""
    hyd_zone = _zone('IN-TG-HYD', geo['IN-TG-HYD'])
    aus_zone = _zone('US-TX-AUS', geo['US-TX-AUS'], country='US', state_code='TX')
    sedan, _ = VehicleType.objects.get_or_create(type='sedan')

    rider_user = User.objects.create_user(
        phone_number=RIDER_PHONE, username=RIDER_PHONE, email=RIDER_EMAIL,
        full_name=RIDER_NAME, role='rider', emergency_contact=RIDER_EMERGENCY,
        house_no=RIDER_HOUSE, street=RIDER_STREET, city='Hyderabad',
        zip_code=RIDER_ZIP, dob=RIDER_DOB,
    )
    Rider.objects.create(user_id=rider_user)

    driver_user = User.objects.create_user(
        phone_number=DRIVER_PHONE, username=DRIVER_PHONE, email=DRIVER_EMAIL,
        full_name=DRIVER_NAME, role='driver',
    )
    driver = Driver.objects.create(
        user_id=driver_user, home_city=geo['IN-TG-HYD'], upi_id=DRIVER_UPI,
        license_doc=DRIVER_LICENCE_DOC, approved=True, doc_status='approved',
    )
    DriverBankAccount.objects.create(
        driver=driver, bank_name='HDFC', account_number=DRIVER_ACCOUNT, ifsc_code=DRIVER_IFSC,
    )
    vehicle = Vehicle.objects.create(
        driver_id=driver, vehicle_type_id=sedan, vehicle_number='TS09AB1234',
        brand='Maruti', model='Dzire', color='white', year=2022, capacity=4,
        rc_doc=VEHICLE_RC_DOC,
    )
    driver.active_vehicle = vehicle
    driver.save(update_fields=['active_vehicle'])
    Wallet.objects.get_or_create(user_id=driver_user, scope=Wallet.SCOPE_DRIVER,
                                 defaults={'currency': 'INR'})

    aus_user = User.objects.create_user(
        phone_number='+15125550002', username='+15125550002', full_name='Austin Driver',
        role='driver',
    )
    aus_driver = Driver.objects.create(user_id=aus_user, home_city=geo['US-TX-AUS'])

    trip = Trip.objects.create(
        user_id=rider_user, driver_id=driver, vehicle_id=vehicle, zone=hyd_zone,
        status_id=_status('completed'), requested_vehicle_type=sedan,
        pickup_address=PICKUP_ADDRESS, destination_address=DROP_ADDRESS,
        pickup_lat=PICKUP_LAT, pickup_long=PICKUP_LNG,
        destination_lat=DROP_LAT, destination_long=DROP_LNG,
        estimated_fare=Decimal('210.00'), final_fare=Decimal('215.00'),
        payment_method='online', payment_status='completed',
    )
    FarePricing.objects.create(
        trip_id=trip, base_fare=Decimal('60'), distance_fare=Decimal('120'),
        time_fare=Decimal('30'), surge_multiplier=Decimal('1.00'), total_fare=Decimal('210'),
    )
    Receipt.objects.create(
        trip_id=trip, user_id=rider_user, receipt_number='RCPT-1', currency='INR',
        total_fare=Decimal('215.00'), gst_amount=Decimal('10.00'),
        sent_to_email=RECEIPT_EMAIL, html_body='<p>receipt</p>',
    )
    payment = Payment.objects.create(
        trip_id=trip, user_id=rider_user, amount=Decimal('215.00'), method='online',
        status='completed', gateway_order_id='order_1', driver_name=DRIVER_NAME,
        gateway_metadata={'customer_details': {
            'customer_phone': RIDER_PHONE, 'customer_email': RIDER_EMAIL,
            'customer_name': RIDER_NAME}},
    )
    TransactionHistory.objects.create(
        trip_id=trip, user_id=rider_user, driver_id=driver, amount=Decimal('180.00'),
        method='online', status='completed', txn_type='credit', user_name=RIDER_NAME,
    )
    withdrawal = WithdrawalRequest.objects.create(
        driver=driver, amount=Decimal('500.00'), payout_method='upi', currency='INR',
    )
    aus_trip = Trip.objects.create(
        user_id=aus_user, driver_id=aus_driver, zone=aus_zone, status_id=_status('completed'),
        pickup_lat=Decimal('30.26'), pickup_long=Decimal('-97.74'),
        destination_lat=Decimal('30.27'), destination_long=Decimal('-97.75'),
    )

    ticket = SupportTicket.objects.create(
        user_id=rider_user, trip_id=trip, issue_type='lost_item',
        description='Left my bag; call me.',
    )
    SupportMessage.objects.create(ticket=ticket, author=rider_user, author_role='user',
                                  body='Left my bag; call me.')
    SupportMessage.objects.create(ticket=ticket, author=driver_user, author_role='user',
                                  body='Found it.')
    macro = SupportMacro.objects.create(title='Lost item', body='Hi {first_name}, we are looking.')

    sos = SOSEvent.objects.create(
        user=rider_user, user_label=RIDER_NAME, initiated_by='rider', trip=trip,
        event_type='panic', status='open', latitude=PICKUP_LAT, longitude=PICKUP_LNG,
    )
    alert = OperatorAlert.objects.create(
        kind='sos_unanchored', severity='critical', title='SOS could not be routed',
        payload={'sos_id': sos.id}, source_type='sos_event', source_id=str(sos.id),
    )
    # A maker-checker request for the payout: its maker/checker blocks are
    # staff attribution; its payload names the payee like a payout row does.
    maker = User.objects.create_user(
        phone_number='+919800009999', username='+919800009999', role='admin', is_staff=True,
        email='maker.hyd@saaradhigo.test', full_name='Maker Operator',
    )
    from django.utils import timezone as _tz
    approval = ApprovalRequest(
        action_key='finance.payout', country='IN', scope_node=geo['IN-TG-HYD'],
        amount=50000, currency='INR', target_type='withdrawal_request',
        target_id=str(withdrawal.id),
        payload={'driver_id': driver.id, 'driver_name': DRIVER_NAME,
                 'driver_phone': DRIVER_PHONE, 'upi_id': DRIVER_UPI},
        request_reason='payout above the maker limit',
        expires_at=_tz.now() + _tz.timedelta(days=1),
        idempotency_key=f'test-payout-{withdrawal.id}',
    )
    approval.set_maker(maker)
    approval.save()
    return {
        'rider_user': rider_user, 'driver_user': driver_user, 'driver': driver,
        'vehicle': vehicle, 'trip': trip, 'payment': payment, 'withdrawal': withdrawal,
        'ticket': ticket, 'macro': macro, 'sos': sos, 'alert': alert, 'aus_driver': aus_driver,
        'aus_trip': aus_trip, 'hyd_zone': hyd_zone, 'approval': approval, 'maker': maker,
    }


def _client_for(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return client


def operator(role_code, geo, scope_code='IN-TG-HYD'):
    i = next(_n)
    phone = f'+9198000{i:05d}'
    user = User.objects.create_user(
        phone_number=phone, username=phone, email=f'op{i}@saaradhigo.test',
        full_name=f'Operator {role_code}', role='admin', is_staff=True,
    )
    role = AdminRole.objects.get(code=role_code)
    scope = geo['GLOBAL'] if role.is_global_only else geo[scope_code]
    AdminAssignment.objects.create(admin=user, role=role, scope=scope)
    return _client_for(user), user


@pytest.fixture
def city_ops(geo):
    return operator('city_ops_manager', geo)


@pytest.fixture
def analyst(geo):
    return operator('analyst', geo)


@pytest.fixture
def support(geo):
    return operator('support_agent', geo)


def body_of(response):
    return json.dumps(response.json(), ensure_ascii=False)


def _without_staff(payload):
    """The body minus staff attribution blocks (`maker`, `checker`,
    `created_by`), which carry an operator's email by design."""
    if isinstance(payload, dict):
        return {k: _without_staff(v) for k, v in payload.items()
                if not (k in OPERATOR_IDENTITY_KEYS and isinstance(v, dict))}
    if isinstance(payload, list):
        return [_without_staff(v) for v in payload]
    return payload


def customer_body_of(response):
    return json.dumps(_without_staff(response.json()), ensure_ascii=False)


def raw_pii_in(text):
    """`{pattern name: matches}` for every raw-PII pattern that fires."""
    return {name: rx.findall(text) for name, rx in RAW_PII_PATTERNS.items() if rx.search(text)}


# ---------------------------------------------------------------------------
# THE INVENTORY. Every operator GET route, with how its ids are filled in.
# `test_every_admin_get_route_is_inventoried` walks the resolver and fails
# on a route missing from here, so a new endpoint has to be added -- and
# the moment it is added it is swept.
# ---------------------------------------------------------------------------

def _ids(world):
    return {
        'driver_id': world['driver'].id, 'trip_id': world['trip'].id,
        'rider_id': world['rider_user'].id,
        'ticket_id': world['ticket'].id, 'macro_id': world['macro'].id, 'sos_id': world['sos'].id,
        'alert_id': world['alert'].id, 'withdrawal_id': world['withdrawal'].id,
        'pk': world['hyd_zone'].id, 'request_id': world['approval'].id,
    }


#: route pattern (as the resolver prints it) -> whether it names a person,
#: so a TRUSTED viewer must see the full name there.
INVENTORY = {
    'api/v1/auth/admin/users/': True,
    'api/v1/driver/admin/': True,
    'api/v1/driver/admin/<int:driver_id>/': True,
    'api/v1/driver/admin/<int:driver_id>/full/': True,
    # Empty in this world (no driver has documents awaiting review); the rows
    # are the list's rows, and test_drivers_module checks them masked.
    'api/v1/driver/admin/kyc-queue/': False,
    'api/v1/driver/admin/<int:driver_id>/vehicles/': False,
    'api/v1/driver/admin/withdrawals/': True,
    # Riders module (console module 4): list rows and the detail name the rider.
    'api/v1/rider/admin/': True,
    'api/v1/rider/admin/<int:rider_id>/': True,
    'api/v1/ride/admin/trips/': True,
    'api/v1/ride/admin/trips/<int:trip_id>/': True,
    'api/v1/ride/admin/live-locations/': False,
    'api/v1/ride/admin/dashboard/': False,
    'api/v1/payments/admin/payments/': True,
    'api/v1/payments/admin/transactions/': True,
    'api/v1/support/admin/tickets/': True,
    'api/v1/support/admin/tickets/<int:ticket_id>/': True,
    # Support module (console module 5). Free text is masked by the module;
    # the macro / SLA routes name no person.
    'api/v1/support/admin/tickets/<int:ticket_id>/assignees/': False,
    'api/v1/support/admin/macros/': False,
    'api/v1/support/admin/macros/<int:macro_id>/preview/': False,
    'api/v1/support/admin/sla/': False,
    # Follow-ups (AUDIT 27.36): the dashboard is counts and staff names only; the
    # inbox is the caller's own notifications; macro history is staff-authored text.
    'api/v1/support/admin/sla/dashboard/': False,
    'api/v1/support/admin/inbox/': False,
    'api/v1/support/admin/macros/<int:macro_id>/versions/': False,
    # SOS labels are contact-masked for everybody (incident rule); the
    # reveal there is per active incident.
    'api/v1/sos/admin/': False,
    'api/v1/sos/admin/<int:sos_id>/': False,
    'api/v1/command-centre/admin/alerts/': False,
    # Command Centre (#80): per-city counts, rates and thresholds; no person.
    'api/v1/command-centre/admin/overview/': False,
    'api/v1/command-centre/admin/thresholds/': False,
    # Maker-checker. `maker` / `checker` blocks are staff attribution and
    # are left alone (OPERATOR_IDENTITY_KEYS); the payload is masked.
    'api/v1/approvals/admin/requests/': True,
    'api/v1/approvals/admin/requests/<int:request_id>/': True,
    'api/v1/approvals/admin/policies/': False,
    # The caller's own second-factor state: counts and booleans (#77).
    'api/v1/auth/admin/mfa/status/': False,
    # Console search (#78). Called with a real term (SEARCH_TERMS) so the
    # sweep sees result rows, not a 400.
    'api/v1/search/admin/trips/': False,
    'api/v1/search/admin/drivers/': True,
    'api/v1/search/admin/riders/': True,
    'api/v1/search/admin/vehicles/': False,
    'api/v1/pricing/admin/zones/': False,
    'api/v1/pricing/admin/zones/(?P<pk>[^/.]+)/': False,
    'api/v1/pricing/admin/rate-cards/': False,
    'api/v1/pricing/admin/rate-cards/(?P<pk>[^/.]+)/': False,
    'api/v1/pricing/admin/rate-cards/effective/': False,
}

#: GET routes in the namespace that are deliberately not swept. The DRF
#: router's `.<format>` suffix aliases of the routes above are skipped by
#: pattern (they share their view with the listed route).
NOT_SWEPT = {
    'api/v1/admin-access/me/': 'Self-describing: the caller’s own account (see SELF_DESCRIBING).',
}

FORMAT_SUFFIX = re.compile(r'\(\?P<format>')


#: Query strings for routes that need one to return rows.
SEARCH_TERMS = {
    'api/v1/search/admin/trips/': lambda ids: f'?q={ids["trip_id"]}',
    'api/v1/search/admin/drivers/': lambda ids: '?q=Suresh',
    'api/v1/search/admin/riders/': lambda ids: '?q=Ravi',
    'api/v1/search/admin/vehicles/': lambda ids: '?q=TS09AB1234',
    'api/v1/support/admin/macros/<int:macro_id>/preview/': lambda ids: f'?ticket={ids["ticket_id"]}',
}


def _fill(route, ids):
    query = SEARCH_TERMS[route](ids) if route in SEARCH_TERMS else ''
    route = re.sub(r'\(\?P<(\w+)>[^)]*\)', lambda m: str(ids[m.group(1)]), route)
    route = re.sub(r'<(?:[^:>]+:)?([^>]+)>', lambda m: str(ids[m.group(1)]), route)
    return '/' + route + query


def _all_routes(urlconf=None):
    found = []

    def clean(pattern):
        return str(pattern).lstrip('^').rstrip('$')

    def walk(patterns, prefix):
        for entry in patterns:
            if isinstance(entry, URLResolver):
                walk(entry.url_patterns, prefix + clean(entry.pattern))
            elif isinstance(entry, URLPattern):
                found.append((prefix + clean(entry.pattern), entry.callback))

    walk(get_resolver(urlconf).url_patterns, '')
    return found


def _admin_get_routes():
    routes = {}
    for route, callback in _all_routes():
        if not is_operator_path('/' + route):
            continue
        actions = getattr(callback, 'actions', None)
        cls = getattr(callback, 'cls', None)
        if actions:
            methods = set(actions)
        else:
            methods = set(getattr(cls, 'http_method_names', ())) if cls else set()
        if 'get' in methods and not FORMAT_SUFFIX.search(route):
            routes[route] = callback
    return routes


# ---------------------------------------------------------------------------
# The maskers added for this phase
# ---------------------------------------------------------------------------

class TestTheNewMaskers:
    def test_an_address_keeps_locality_and_city_and_drops_the_door(self):
        masked = mask_address(PICKUP_ADDRESS)

        assert 'Plot 12' not in masked
        assert 'Road No 36' not in masked
        assert 'Jubilee Hills, Hyderabad, Telangana' in masked
        assert '500033' not in masked
        assert masked.startswith(BULLET)

    def test_a_short_address_is_withheld_rather_than_guessed(self):
        assert mask_address('12-3-45') == BULLET * 4
        assert 'Hyderabad' in mask_address('Banjara Hills, Hyderabad')
        assert 'Banjara' not in mask_address('Banjara Hills, Hyderabad')

    def test_masking_is_idempotent(self):
        """A view may mask and the renderer masks again; the second pass
        must not strip more."""
        once = mask_address(PICKUP_ADDRESS)
        assert mask_address(once) == once
        assert mask_last4(mask_last4(DRIVER_ACCOUNT)) == mask_last4(DRIVER_ACCOUNT)
        assert mask_upi(mask_upi(DRIVER_UPI)) == mask_upi(DRIVER_UPI)
        assert round_coordinate(round_coordinate('17.4312345')) == '17.43'

    def test_identifiers_keep_four(self):
        assert mask_last4(DRIVER_ACCOUNT).endswith('9012')
        assert '12345678' not in mask_last4(DRIVER_ACCOUNT)
        assert mask_last4(DRIVER_IFSC).endswith('1234') and 'HDFC' not in mask_last4(DRIVER_IFSC)
        assert mask_upi(DRIVER_UPI).endswith('@okaxis') and 'suresh' not in mask_upi(DRIVER_UPI)

    def test_coordinates_round_to_about_a_kilometre_in_their_own_type(self):
        assert round_coordinate('17.4312345') == '17.43'
        assert round_coordinate(78.4098765) == 78.41
        assert round_coordinate(Decimal('17.4312345')) == Decimal('17.43')
        assert round_coordinate(None) is None

    def test_the_two_policies_differ_only_where_they_should(self):
        payload = {'full_name': RIDER_NAME, 'phone_number': RIDER_PHONE,
                   'lat': '17.4312345', 'license_doc': 'license_docs/x.jpg',
                   'nested': [{'email': RIDER_EMAIL, 'driver_name': DRIVER_NAME}]}
        strict = mask_payload(json.loads(json.dumps(payload)), STRICT)
        trusted = mask_payload(json.loads(json.dumps(payload)), TRUSTED)

        assert strict['full_name'] == 'Ravi K.' and trusted['full_name'] == RIDER_NAME
        assert strict['lat'] == '17.43' and trusted['lat'] == '17.4312345'
        assert strict['license_doc'] is None and trusted['license_doc'] == 'license_docs/x.jpg'
        for policy in (strict, trusted):
            assert RIDER_PHONE not in json.dumps(policy)
            assert RIDER_EMAIL not in json.dumps(policy)
        assert strict['nested'][0]['driver_name'] == 'Suresh R.'

    def test_the_raw_patterns_do_not_fire_on_masked_values(self):
        from servers.admin_access.masking import mask_email, mask_phone

        masked = ' '.join([mask_phone(RIDER_PHONE), mask_email(RIDER_EMAIL),
                           mask_upi(DRIVER_UPI), mask_last4(DRIVER_IFSC),
                           mask_last4(DRIVER_ACCOUNT)])
        assert raw_pii_in(masked) == {}
        raw = ' '.join([RIDER_PHONE, RIDER_EMAIL, DRIVER_UPI, DRIVER_IFSC, DRIVER_ACCOUNT])
        assert set(raw_pii_in(raw)) == set(RAW_PII_PATTERNS)


# ---------------------------------------------------------------------------
# Every inventoried endpoint, as every role
# ---------------------------------------------------------------------------

class TestEveryEndpointAsEveryRole:
    @pytest.mark.parametrize('route', sorted(INVENTORY))
    @pytest.mark.parametrize('role', ROLES)
    def test_no_raw_personal_data_whatever_the_role(self, geo, world, route, role):
        """Allowed or refused, never leaked. A 200 is swept whole."""
        client, _ = operator(role, geo)
        response = client.get(_fill(route, _ids(world)))

        # 404: the KYC Verifier's queue is drivers still in KYC, and the
        # world's driver is approved; out of their view answers NOT FOUND.
        # 400: `rate-cards/effective/` wants its query params.
        assert response.status_code in (200, 400, 403, 404), (route, role, response.content[:300])
        body = customer_body_of(response)
        assert raw_pii_in(body) == {}, (route, role, raw_pii_in(body))
        for secret in NEVER_IN_A_LIST:
            assert secret not in body, (route, role, secret)
        if role not in PII_VIEW_ROLES:
            for secret in NEVER_FOR_STRICT:
                assert secret not in body, (route, role, secret)

    @pytest.mark.parametrize('route', sorted(r for r, names in INVENTORY.items() if names))
    def test_a_pii_view_holder_still_recognises_the_person(self, geo, world, route):
        """Masked, not anonymous: the queue is unusable if nobody can be
        named. Super Admin reaches everything, so it is the viewer here."""
        client, _ = operator('super_admin', geo)
        response = client.get(_fill(route, _ids(world)))

        assert response.status_code == 200, (route, response.content[:300])
        body = body_of(response)
        assert ('Kumar' in body) or ('Reddy' in body), route
        assert RIDER_PHONE not in body and DRIVER_PHONE not in body

    def test_the_analyst_is_refused_the_rider_list_rather_than_shown_a_masked_one(
        self, analyst, world,
    ):
        """`/auth/admin/users/` stays gated on `pii.view` (it is global and
        another change owns that view); the Analyst is refused outright."""
        client, _ = analyst
        assert client.get('/api/v1/auth/admin/users/').status_code == 403

    def test_the_analyst_sees_the_driver_queue_with_initials(self, analyst, world):
        client, _ = analyst
        body = body_of(client.get('/api/v1/driver/admin/'))

        assert 'Suresh R.' in body and 'Reddy' not in body

    def test_the_analyst_sees_a_trip_in_a_neighbourhood_not_at_a_door(self, analyst, world):
        client, _ = analyst
        data = client.get(f'/api/v1/ride/admin/trips/{world["trip"].id}/').json()['data']

        assert data['pickup']['lat'] == '17.43' and data['pickup']['lng'] == '78.41'
        assert data['pickup']['address'].startswith(BULLET)
        assert 'Jubilee Hills, Hyderabad' in data['pickup']['address']
        assert data['rider']['full_name'] == 'Ravi K.'
        assert data['rider']['phone_number'].endswith('0001')

    def test_a_city_ops_manager_sees_exact_coordinates_and_full_names_but_no_phone(
        self, city_ops, world,
    ):
        client, _ = city_ops
        data = client.get(f'/api/v1/ride/admin/trips/{world["trip"].id}/').json()['data']

        assert data['pickup']['lat'] == str(PICKUP_LAT)
        assert data['rider']['full_name'] == RIDER_NAME
        assert data['rider']['phone_number'] != RIDER_PHONE
        assert data['rider']['email'].endswith('@example.test') and 'ravi' not in data['rider']['email']

    def test_the_payout_queue_hides_the_upi_handle_from_finance_too(self, geo, world):
        client, _ = operator('finance', geo)
        rows = client.get('/api/v1/driver/admin/withdrawals/').json()['data']['results']

        assert rows[0]['driver_name'] == DRIVER_NAME
        assert rows[0]['driver_upi_id'].endswith('@okaxis') and 'suresh' not in rows[0]['driver_upi_id']
        assert rows[0]['driver_phone'].endswith('0002') and rows[0]['driver_phone'] != DRIVER_PHONE

    def test_documents_are_served_to_kyc_and_withheld_from_the_analyst(self, geo, world):
        kyc, _ = operator('kyc_verifier', geo)
        analyst, _ = operator('analyst', geo)
        # The world's driver is approved; the KYC Verifier's queue is drivers
        # still in KYC, so put one there for this check.
        world['driver'].approved = False
        world['driver'].save(update_fields=['approved'])
        url = f'/api/v1/driver/admin/{world["driver"].id}/full/'

        assert 'license_docs/' in body_of(kyc.get(url))
        assert analyst.get(url).json()['data']['license_doc_url'] is None

    def test_gateway_metadata_is_masked_inside_the_json_blob(self, geo, world):
        """Cashfree echoes the customer's phone and email into the metadata
        the payment row keeps; the walker reaches into it by key name."""
        client, _ = operator('finance', geo)
        rows = client.get('/api/v1/payments/admin/payments/').json()['data']['results']
        details = rows[0]['gateway_metadata']['customer_details']

        assert details['customer_phone'] != RIDER_PHONE and details['customer_phone'].endswith('0001')
        assert 'ravi.kumar' not in details['customer_email']

    def test_ticket_bodies_are_left_alone_and_authors_are_initials(self, support, world):
        """Support reads the message; the requester's contact is what is masked."""
        client, _ = support
        data = client.get(f'/api/v1/support/admin/tickets/{world["ticket"].id}/').json()['data']

        assert data['description'] == 'Left my bag; call me.'
        assert {m['author_name'] for m in data['messages']} == {'Ravi K.', 'Suresh R.'}

    def test_rider_and_driver_endpoints_are_not_touched(self, world):
        """The renderer masks the OPERATOR namespace. A rider reading their
        own profile gets their own number; the mobile contracts depend on it."""
        rider = _client_for(world['rider_user'])
        response = rider.get('/api/v1/auth/profile/')

        assert response.status_code == 200, response.content
        assert RIDER_PHONE in body_of(response)

    def test_staff_attribution_on_an_approval_is_not_masked_but_its_payload_is(
        self, geo, world,
    ):
        """A checker must see which colleague raised the request; the payee
        named inside the payload is masked like the payout queue."""
        client, _ = operator('finance', geo)
        data = client.get(f'/api/v1/approvals/admin/requests/{world["approval"].id}/').json()['data']

        assert data['maker']['email'] == world['maker'].email
        assert data['payload']['driver_name'] == DRIVER_NAME
        assert data['payload']['driver_phone'] != DRIVER_PHONE
        assert 'suresh' not in data['payload']['upi_id']

    def test_the_operator_still_sees_their_own_email_on_whoami(self, city_ops):
        client, user = city_ops
        data = client.get('/api/v1/admin-access/me/').json()['data']

        assert data['email'] == user.email


# ---------------------------------------------------------------------------
# The guards
# ---------------------------------------------------------------------------

SENSITIVE_KEY = re.compile(
    r'phone|email|address|upi|ifsc|account_number|licen[cs]e|aadhaar|ssn|dob|contact',
    re.I,
)
#: Key suffixes that match the pattern above and are not personal data.
NOT_PERSONAL_SUFFIXES = ('_expiry', '_at', '_status', '_count', '_masked', '_configured',
                         '_sent', '_placeholder', '_type', '_verified', '_enabled')


def _walk(payload, path=''):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in OPERATOR_IDENTITY_KEYS and isinstance(value, dict):
                continue  # staff attribution, by design (see masking.py)
            yield from _walk(value, f'{path}.{key}' if path else key)
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            yield from _walk(item, f'{path}[{i}]')
    else:
        yield path, payload


def _is_masked(key, value):
    if value in (None, '', [], {}):
        return True
    if not isinstance(value, str):
        return True
    if BULLET in value or value == PINNED_LOCATION:
        return True
    if key in COORDINATE_FIELDS:
        return len(value.split('.')[-1]) <= 2 if '.' in value else True
    return False


class TestTheSweepGuard:
    def test_every_admin_get_route_is_inventoried(self):
        """A route added tomorrow fails here until it is listed above -- and
        listing it is what puts it under the sweep."""
        routes = set(_admin_get_routes())
        unlisted = routes - set(INVENTORY) - set(NOT_SWEPT)
        stale = (set(INVENTORY) | set(NOT_SWEPT)) - routes

        assert not unlisted, f'admin GET routes not in the inventory: {sorted(unlisted)}'
        assert not stale, f'inventory lists routes that no longer exist: {sorted(stale)}'
        assert len(NOT_SWEPT) <= 1

    @pytest.mark.parametrize('role', STRICT_ROLES)
    def test_no_field_named_like_personal_data_is_returned_raw_to_a_strict_viewer(
        self, geo, world, role,
    ):
        """Key-by-key, not just regex: a field NAMED phone/email/address/
        upi/ifsc/account_number/licence/aadhaar/ssn/dob/contact must come
        back masked, withheld or rounded for an operator without pii.view."""
        client, _ = operator(role, geo)
        offenders = []
        for route in INVENTORY:
            response = client.get(_fill(route, _ids(world)))
            if response.status_code != 200:
                continue
            for path, value in _walk(response.json()):
                key = path.rsplit('.', 1)[-1].split('[')[0]
                if not SENSITIVE_KEY.search(key) or key.endswith(NOT_PERSONAL_SUFFIXES):
                    continue
                if key in DOCUMENT_FIELDS:
                    if value is not None:
                        offenders.append((route, path, value))
                    continue
                if not _is_masked(key, value):
                    offenders.append((route, path, value))

        assert offenders == [], offenders

    def test_every_sensitive_key_the_responses_carry_is_in_the_masking_table(self, geo, world):
        """The table in `masking.py` is the policy; a response key that looks
        personal and is not in it is a decision nobody made."""
        client, _ = operator('super_admin', geo)
        unknown = set()
        for route in INVENTORY:
            response = client.get(_fill(route, _ids(world)))
            if response.status_code != 200:
                continue
            for path, _value in _walk(response.json()):
                key = path.rsplit('.', 1)[-1].split('[')[0]
                if SENSITIVE_KEY.search(key) and not key.endswith(NOT_PERSONAL_SUFFIXES):
                    if key not in ALL_PII_FIELDS:
                        unknown.add(key)

        assert unknown == set(), unknown

    def test_no_admin_view_swaps_the_masking_renderer_out(self):
        """The renderer is the whole guarantee. A view that lists its own
        `renderer_classes` without it is a view that leaks."""
        offenders = []
        for route, callback in _all_routes():
            if not is_operator_path('/' + route):
                continue
            cls = getattr(callback, 'cls', None)
            if cls is None:
                continue
            renderers = list(getattr(cls, 'renderer_classes', []) or [])
            if not any(issubclass(r, MaskingJSONRenderer) for r in renderers):
                offenders.append((route, renderers))

        assert offenders == [], offenders

    def test_mark_revealed_has_exactly_the_call_sites_it_should(self):
        """A list endpoint must never mark itself revealed."""
        root = pathlib.Path(__file__).resolve().parents[1] / 'servers'
        callers = set()
        for path in root.rglob('*.py'):
            if 'migrations' in path.parts or 'tests' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            # A bare call. `masking.mark_revealed(` in a docstring does not count.
            if re.search(r'(?<![.\w])mark_revealed\(', text):
                callers.add(path.relative_to(root).as_posix())

        assert callers == {
            'admin_access/masking.py',   # the definition, and the export rule
            'admin_access/reveal.py',    # the generic reveal
            'sos/views.py',              # the incident reveal
        }, callers

    def test_the_self_describing_allowlist_is_small_and_real(self):
        routes = {'/' + r for r, _ in _all_routes()}
        for path, reason in SELF_DESCRIBING.items():
            assert reason.strip()
            assert path in routes, path
        assert len(SELF_DESCRIBING) <= 2

    def test_no_api_view_outside_the_frozen_console_exports_a_file_without_the_rule(self):
        """There is no CSV/XLSX export in the operator API today. The first
        one has to go through `masking.exports_unmasked`; this fails the
        moment a Content-Disposition appears in a view that does not."""
        root = pathlib.Path(__file__).resolve().parents[1] / 'servers'
        offenders = []
        for path in root.rglob('*.py'):
            if 'migrations' in path.parts or 'admin_dashboard' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            if 'Content-Disposition' in text and 'exports_unmasked' not in text:
                offenders.append(path.relative_to(root).as_posix())

        assert offenders == [], offenders


# ---------------------------------------------------------------------------
# The reveal endpoint
# ---------------------------------------------------------------------------

REVEAL = '/api/v1/admin-access/reveal/'


def reveal(client, **body):
    return client.post(REVEAL, body, format='json')


class TestReveal:
    def test_a_pii_view_holder_reveals_one_record_and_it_is_logged(self, city_ops, world):
        client, user = city_ops
        before = AdminAuditLog.objects.count()

        response = reveal(client, target_type='driver', target_id=world['driver'].id,
                          fields=['phone_number', 'email', 'upi_id', 'bank_account'],
                          reason='payout dispute #881')

        assert response.status_code == 200, response.content
        data = response.json()['data']
        assert data['fields'] == {
            'phone_number': DRIVER_PHONE, 'email': DRIVER_EMAIL, 'upi_id': DRIVER_UPI,
            'bank_account': {'bank_name': 'HDFC', 'account_number': DRIVER_ACCOUNT,
                             'ifsc_code': DRIVER_IFSC},
        }
        assert data['reveals_in_window'] == 1

        assert AdminAuditLog.objects.count() == before + 1
        row = AdminAuditLog.objects.order_by('-id').first()
        assert row.action == REVEAL_ACTION
        assert row.target_type == 'driver' and row.target_id == str(world['driver'].id)
        assert row.actor_id == user.id
        assert row.reason == 'payout dispute #881'
        assert row.after['fields'] == ['bank_account', 'email', 'phone_number', 'upi_id']
        assert row.after['scope_node'] == 'IN-TG-HYD'
        assert row.after['reveals_in_window'] == 1
        assert data['audit_id'] == row.id
        # WHAT was revealed, never the value.
        for secret in (DRIVER_PHONE, DRIVER_EMAIL, DRIVER_UPI, DRIVER_ACCOUNT, DRIVER_IFSC):
            assert secret not in json.dumps(row.after) + json.dumps(row.before)

    def test_only_the_requested_fields_come_back(self, city_ops, world):
        client, _ = city_ops
        data = reveal(client, target_type='driver', target_id=world['driver'].id,
                      fields=['email']).json()['data']

        assert list(data['fields']) == ['email']
        assert DRIVER_PHONE not in json.dumps(data)

    @pytest.mark.parametrize('role', STRICT_ROLES)
    def test_without_pii_view_the_reveal_is_forbidden(self, geo, world, role):
        client, _ = operator(role, geo)
        response = reveal(client, target_type='driver', target_id=world['driver'].id,
                          fields=['phone_number'])

        assert response.status_code == 403
        assert DRIVER_PHONE not in body_of(response)
        assert not AdminAuditLog.objects.filter(action=REVEAL_ACTION).exists()

    def test_out_of_scope_is_not_found_not_forbidden(self, city_ops, world):
        client, _ = city_ops
        response = reveal(client, target_type='driver', target_id=world['aus_driver'].id,
                          fields=['phone_number'])

        assert response.status_code == 404
        assert not AdminAuditLog.objects.filter(action=REVEAL_ACTION).exists()

    def test_pii_view_held_elsewhere_does_not_reveal_here(self, geo, world):
        """City Ops in Hyderabad plus Analyst in Austin: the Austin driver is
        IN SCOPE (the Analyst assignment) and `pii.view` is NOT held there."""
        client, user = operator('city_ops_manager', geo)
        AdminAssignment.objects.create(
            admin=user, role=AdminRole.objects.get(code='analyst'), scope=geo['US-TX-AUS'],
        )
        response = reveal(client, target_type='driver', target_id=world['aus_driver'].id,
                          fields=['phone_number'])

        assert response.status_code == 403
        assert response.json()['error']['code'] == 'PII_VIEW_NOT_HELD_HERE'

    @pytest.mark.parametrize('body', [
        {'target_type': 'driver', 'target_id': [1, 2], 'fields': ['phone_number']},
        {'target_type': 'driver', 'target_ids': [1, 2], 'fields': ['phone_number']},
    ])
    def test_bulk_is_refused(self, city_ops, world, body):
        client, _ = city_ops
        response = client.post(REVEAL, body, format='json')

        assert response.status_code == 400
        assert response.json()['error']['code'] == 'BULK_REVEAL_REFUSED'

    def test_unknown_field_and_type_are_refused_naming_the_catalogue(self, city_ops, world):
        client, _ = city_ops
        r1 = reveal(client, target_type='driver', target_id=world['driver'].id, fields=['password'])
        r2 = reveal(client, target_type='operator', target_id=1, fields=['email'])
        r3 = reveal(client, target_type='driver', target_id=world['driver'].id, fields=[])

        assert r1.status_code == 400 and r1.json()['error']['code'] == 'UNKNOWN_FIELD'
        assert 'phone_number' in r1.json()['error']['message']
        assert r2.status_code == 400 and r2.json()['error']['code'] == 'UNKNOWN_TARGET_TYPE'
        assert r3.status_code == 400 and r3.json()['error']['code'] == 'FIELDS_REQUIRED'
        assert not AdminAuditLog.objects.filter(action=REVEAL_ACTION).exists()

    def test_an_sos_event_is_revealed_through_its_own_door(self, geo, world):
        client, _ = operator('safety_officer', geo)
        response = reveal(client, target_type='sos_event', target_id=world['sos'].id,
                          fields=['phone_number'])

        assert response.status_code == 400
        assert response.json()['error']['code'] == 'REVEALED_ELSEWHERE'
        assert '/sos/admin/' in response.json()['error']['details']['issue']

    def test_a_trip_reveals_its_people_and_its_doors(self, city_ops, world):
        client, _ = city_ops
        data = reveal(client, target_type='trip', target_id=world['trip'].id,
                      fields=['rider_phone', 'driver_name', 'pickup_address',
                              'pickup_coordinates', 'receipt_email']).json()['data']

        assert data['fields'] == {
            'rider_phone': RIDER_PHONE, 'driver_name': DRIVER_NAME,
            'pickup_address': PICKUP_ADDRESS,
            'pickup_coordinates': {'lat': str(PICKUP_LAT), 'lng': str(PICKUP_LNG)},
            'receipt_email': RECEIPT_EMAIL,
        }

    def test_a_rider_reveals_on_pii_view_at_one_of_their_anchors(self, city_ops, world):
        client, _ = city_ops
        data = reveal(client, target_type='rider', target_id=world['rider_user'].id,
                      fields=['phone_number', 'address', 'dob']).json()['data']

        assert data['fields']['phone_number'] == RIDER_PHONE
        assert data['fields']['address'] == f'{RIDER_HOUSE}, {RIDER_STREET}, Hyderabad, {RIDER_ZIP}'
        assert data['fields']['dob'] == RIDER_DOB
        row = AdminAuditLog.objects.filter(action=REVEAL_ACTION).order_by('-id').first()
        # Placed by the rider rule (`servers/rider/scope.py`): the Hyderabad trip.
        assert row.after['scope_node'] == 'IN-TG-HYD'

    @pytest.mark.parametrize('target_type, key', [
        ('support_ticket', 'ticket'), ('withdrawal', 'withdrawal'), ('payment', 'payment'),
    ])
    def test_the_other_targets_resolve_in_scope(self, city_ops, world, target_type, key):
        from servers.admin_access.reveal import REVEALABLE_FIELDS

        client, _ = city_ops
        fields = [f for f in REVEALABLE_FIELDS[target_type] if f.endswith('_phone')]
        data = reveal(client, target_type=target_type, target_id=world[key].id,
                      fields=fields).json()['data']

        assert set(data['fields'].values()) <= {RIDER_PHONE, DRIVER_PHONE}
        assert data['fields']

    def test_the_list_stays_masked_after_a_reveal(self, city_ops, world):
        client, _ = city_ops
        reveal(client, target_type='driver', target_id=world['driver'].id, fields=['phone_number'])

        assert DRIVER_PHONE not in body_of(client.get('/api/v1/driver/admin/'))

    def test_no_audit_row_means_no_reveal(self, city_ops, world, monkeypatch):
        """The SOS reveal returns anyway (a person in danger outranks a log
        row); this one does not. A hand-over that left no trace is the
        failure this endpoint exists to prevent."""
        from servers.admin_audit import services

        monkeypatch.setattr(services, 'record_admin_action', lambda *a, **k: None)
        client, _ = city_ops
        response = reveal(client, target_type='driver', target_id=world['driver'].id,
                          fields=['phone_number'])

        assert response.status_code == 503
        assert response.json()['error']['code'] == 'AUDIT_UNAVAILABLE'
        assert DRIVER_PHONE not in body_of(response)

    def test_the_sixty_first_reveal_in_a_minute_is_throttled(self, city_ops, world):
        client, _ = city_ops
        statuses = []
        for _i in range(61):
            statuses.append(reveal(client, target_type='driver', target_id=world['driver'].id,
                                   fields=['email']).status_code)

        assert statuses[:60] == [200] * 60
        assert statuses[60] == 429
        assert AdminAuditLog.objects.filter(action=REVEAL_ACTION).count() == 60
        last = AdminAuditLog.objects.filter(action=REVEAL_ACTION).order_by('-id').first()
        assert last.after['reveals_in_window'] == 60

    def test_the_throttle_is_per_operator(self, geo, world):
        """One operator's budget is not another's."""
        a, _ = operator('city_ops_manager', geo)
        b, _ = operator('city_ops_manager', geo)
        for _i in range(60):
            reveal(a, target_type='driver', target_id=world['driver'].id, fields=['email'])

        assert reveal(a, target_type='driver', target_id=world['driver'].id,
                      fields=['email']).status_code == 429
        assert reveal(b, target_type='driver', target_id=world['driver'].id,
                      fields=['email']).status_code == 200

    def test_a_rider_and_an_anonymous_caller_are_refused(self, world):
        rider = _client_for(world['rider_user'])
        body = {'target_type': 'driver', 'target_id': world['driver'].id, 'fields': ['phone_number']}

        assert rider.post(REVEAL, body, format='json').status_code in (401, 403)
        assert APIClient().post(REVEAL, body, format='json').status_code in (401, 403)


class TestTheSosRevealSharesTheAuditShape:
    def test_the_incident_reveal_writes_the_same_shape_under_its_own_action(self, geo, world):
        client, user = operator('safety_officer', geo)
        response = client.post(f'/api/v1/sos/admin/{world["sos"].id}/reveal/',
                               {'reason': 'not answering'}, format='json')

        assert response.status_code == 200, response.content
        assert response.json()['data']['contact']['phone_number'] == RIDER_PHONE
        row = AdminAuditLog.objects.order_by('-id').first()
        assert row.action == CONTACT_REVEALED_ACTION
        assert row.actor_id == user.id
        assert row.before == {'masked': True}
        assert row.after['masked'] is False
        assert row.after['target_type'] == 'sos_event'
        assert row.after['scope_node'] == 'IN-TG-HYD'
        assert set(row.after['fields']) == {'full_name', 'phone_number', 'user_label', 'emergency_contact'}
        assert RIDER_PHONE not in json.dumps(row.after)

    def test_the_sos_queue_is_masked_for_the_safety_officer_and_rounded_for_nobody_else(
        self, geo, world,
    ):
        """Safety holds pii.view: exact coordinates (the incident is a place),
        contact labels masked (the incident rule)."""
        client, _ = operator('safety_officer', geo)
        row = client.get('/api/v1/sos/admin/').json()['data']['results'][0]

        assert row['latitude'] == str(PICKUP_LAT)
        assert row['user_label'] == 'Ravi K.'
        assert row['user_phone'].endswith('0001') and row['user_phone'] != RIDER_PHONE


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

class TestExports:
    def _request(self, user, query):
        request = APIRequestFactory().get('/api/v1/driver/admin/export/' + query)
        force_authenticate(request, user=user)
        from rest_framework.request import Request

        return Request(request)

    def test_an_export_is_masked_unless_asked_and_allowed(self, geo, world):
        _, city_ops_user = operator('city_ops_manager', geo)
        _, analyst_user = operator('analyst', geo)

        assert exports_unmasked(self._request(city_ops_user, ''),
                                target_type='driver', row_count=3) is False
        assert exports_unmasked(self._request(analyst_user, '?reveal=1'),
                                target_type='driver', row_count=3) is False
        assert not AdminAuditLog.objects.filter(action=EXPORT_REVEAL_ACTION).exists()

    def test_a_revealed_export_is_audited_once_with_its_row_count(self, geo, world):
        _, user = operator('city_ops_manager', geo)
        request = self._request(user, '?reveal=1')

        assert exports_unmasked(request, target_type='driver', row_count=3,
                                reason='quarterly reconciliation') is True
        rows = AdminAuditLog.objects.filter(action=EXPORT_REVEAL_ACTION)
        assert rows.count() == 1
        assert rows.first().after['row_count'] == 3
        assert rows.first().actor_id == user.id
        assert request.pii_revealed == 'export'

    def test_a_revealed_export_that_cannot_be_logged_stays_masked(self, geo, world, monkeypatch):
        from servers.admin_audit import services

        monkeypatch.setattr(services, 'record_admin_action', lambda *a, **k: None)
        _, user = operator('city_ops_manager', geo)

        assert exports_unmasked(self._request(user, '?reveal=1'),
                                target_type='driver', row_count=3) is False


# ---------------------------------------------------------------------------
# Proof the sweep bites: a planted leaking route is caught by the renderer
# ---------------------------------------------------------------------------

def test_a_new_admin_endpoint_is_masked_without_opting_in(geo, world, settings):
    """Plant a view that returns a raw phone under an admin path, with no
    masking call of its own. The renderer must still mask it."""
    import types

    from django.urls import clear_url_caches, path
    from rest_framework.decorators import api_view, permission_classes
    from rest_framework.permissions import IsAuthenticated
    from rest_framework.response import Response

    from servers.admin_access.bridge import ScopedPermission

    @api_view(['GET'])
    @permission_classes([IsAuthenticated, ScopedPermission('driver.read')])
    def planted(request):
        return Response({'results': [{'full_name': DRIVER_NAME, 'phone_number': DRIVER_PHONE,
                                      'email': DRIVER_EMAIL, 'upi_id': DRIVER_UPI}]})

    import importlib

    root = importlib.import_module(settings.ROOT_URLCONF)
    module = types.ModuleType('tests_planted_pii_urlconf')
    module.urlpatterns = [path('api/v1/planted/admin/', planted)] + list(root.urlpatterns)
    sys.modules[module.__name__] = module
    settings.ROOT_URLCONF = module.__name__
    clear_url_caches()
    try:
        client, _ = operator('analyst', geo)
        body = body_of(client.get('/api/v1/planted/admin/'))
    finally:
        sys.modules.pop(module.__name__, None)
        clear_url_caches()

    assert DRIVER_PHONE not in body and DRIVER_EMAIL not in body and 'suresh' not in body
    assert 'Suresh R.' in body
    assert raw_pii_in(body) == {}

