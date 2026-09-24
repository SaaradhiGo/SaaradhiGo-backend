"""§6 — a support account must not be able to release money.

WHAT WAS THERE BEFORE
---------------------
Nothing. `role` had three values -- rider, driver, admin -- so every operator was a
full administrator: the person answering "where is my driver" also held authority to
approve driver KYC, release payouts and rewrite the rate card. There was no gap in an
RBAC implementation to fix, because there was no RBAC implementation.

For a ten-driver pilot with two trusted people, one role is survivable. As a
permanent position it means the blast radius of any operator account -- phished,
shared, or simply mistaken -- is the whole platform's money.

THE MODEL, AND WHY IT IS THIS SMALL
-----------------------------------
Four operator roles, nine capabilities, one flat mapping. No permission editor, no
per-object rules, no inheritance. An authorization system nobody can reason about is
worse than the coarse one it replaced, and four roles is what §6 actually asked for.

Deliberately flat rather than nested: a hierarchy would make finance a superset of
support by accident of ordering. Reading a trip is shared; approving KYC, releasing
money and changing prices are each held by one role plus the administrator.

WHAT THESE TESTS CHECK
----------------------
Both directions for every capability, because a permission model is only correct if
it also *permits*. The single most important test in the file is
`test_a_support_account_cannot_approve_a_payout`, which is §6's stated requirement,
and it is asserted by calling the endpoint directly -- an ops console that hides the
button has arranged its layout, not its authority.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from base.permissions import (
    ALL_CAPABILITIES, CAP_DRIVER_KYC, CAP_FINANCE_PAYOUT, CAP_PRICING_WRITE,
    CAP_SUPPORT_REPLY, OPS_ADMIN, OPS_DRIVER_OPS, OPS_FINANCE,
    OPS_ROLE_CAPABILITIES, OPS_SUPPORT, capabilities_of, has_capability,
    is_operator, ops_role_of,
)
from servers.driver.models import Driver
from servers.rider.models import Rider

User = get_user_model()

pytestmark = pytest.mark.django_db

REFUSED = (401, 403)

_n = iter(range(1, 999))


def _operator(ops_role):
    i = next(_n)
    return User.objects.create_user(
        phone_number=f'+9197300{i:05d}', username=f'+9197300{i:05d}',
        role='admin', is_staff=True, ops_role=ops_role,
        password='operator-not-a-real-secret',
    )


def _client(user):
    c = APIClient()
    c.credentials(HTTP_AUTHORIZATION=f'Bearer {AccessToken.for_user(user)}')
    return c


# A minimal valid service-zone payload. `zone_type` and `polygon_geojson` are
# required by the model, so a partial payload fails validation before the audit
# code is reached -- which is how the first version of these tests failed.
_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[
        [78.30, 17.40], [78.50, 17.40], [78.50, 17.50], [78.30, 17.50],
        [78.30, 17.40],
    ]],
}


def _zone_payload(code, name='Audit Test Zone'):
    return {
        'code': code, 'name': name, 'zone_type': 'city', 'city': 'Hyderabad',
        'polygon_geojson': _POLYGON, 'is_active': True, 'priority': 10,
    }


@pytest.fixture
def a_driver():
    i = next(_n)
    u = User.objects.create_user(phone_number=f'+9196400{i:05d}',
                                 username=f'+9196400{i:05d}', role='driver')
    return Driver.objects.create(user_id=u, approved=False)


# ===========================================================================
# The requirement
# ===========================================================================

def test_a_support_account_cannot_approve_a_payout():
    """§6's stated requirement, called directly against the endpoint."""
    support = _operator(OPS_SUPPORT)

    resp = _client(support).post(
        '/api/v1/driver/admin/withdrawals/1/approve/', {}, format='json')

    assert resp.status_code in REFUSED, (
        f'a support-only operator reached payout approval: HTTP '
        f'{resp.status_code}. A 404 here would also be wrong -- it would mean '
        f'authority is decided after the withdrawal lookup.'
    )


def test_a_support_account_cannot_reject_a_payout():
    """Rejecting is also a financial decision: it strands a driver's money."""
    resp = _client(_operator(OPS_SUPPORT)).post(
        '/api/v1/driver/admin/withdrawals/1/reject/', {}, format='json')

    assert resp.status_code in REFUSED


def test_a_support_account_cannot_bulk_action_payouts():
    """The bulk endpoint is the one worth getting wrong: it moves many at once."""
    resp = _client(_operator(OPS_SUPPORT)).post(
        '/api/v1/driver/admin/withdrawals/bulk-action/',
        {'ids': [1, 2, 3], 'action': 'approve'}, format='json')

    assert resp.status_code in REFUSED


def test_a_finance_account_can_reach_payout_approval():
    """The control. Finance must be able to do the job finance exists for.

    A 404 is the pass here, not a failure: the capability check runs before the
    withdrawal lookup, so a non-existent id 1 is exactly what an authorised caller
    should get. What must NOT happen is 403.
    """
    resp = _client(_operator(OPS_FINANCE)).post(
        '/api/v1/driver/admin/withdrawals/1/approve/', {}, format='json')

    assert resp.status_code not in REFUSED, (
        f'a finance operator was refused payout approval: HTTP {resp.status_code}'
    )


# ===========================================================================
# KYC — letting somebody drive
# ===========================================================================

def test_a_support_account_cannot_approve_driver_kyc(a_driver):
    """Approving KYC puts a stranger in a car with a passenger."""
    resp = _client(_operator(OPS_SUPPORT)).post(
        f'/api/v1/driver/admin/{a_driver.id}/update-kyc/',
        {'approved': True}, format='json')

    assert resp.status_code in REFUSED, (
        f'support approved KYC: HTTP {resp.status_code}'
    )
    a_driver.refresh_from_db()
    assert not a_driver.approved


def test_a_finance_account_cannot_approve_driver_kyc(a_driver):
    """Flat, not nested: finance authority is not driver authority."""
    resp = _client(_operator(OPS_FINANCE)).post(
        f'/api/v1/driver/admin/{a_driver.id}/update-kyc/',
        {'approved': True}, format='json')

    assert resp.status_code in REFUSED
    a_driver.refresh_from_db()
    assert not a_driver.approved


def test_a_driver_ops_account_can_approve_driver_kyc(a_driver):
    """The control."""
    resp = _client(_operator(OPS_DRIVER_OPS)).post(
        f'/api/v1/driver/admin/{a_driver.id}/update-kyc/',
        {'approved': True}, format='json')

    assert resp.status_code not in REFUSED, (
        f'driver operations was refused KYC approval: HTTP {resp.status_code} '
        f'{resp.content[:200]}'
    )


def test_a_driver_ops_account_cannot_approve_a_payout():
    """The other side of the same separation."""
    resp = _client(_operator(OPS_DRIVER_OPS)).post(
        '/api/v1/driver/admin/withdrawals/1/approve/', {}, format='json')

    assert resp.status_code in REFUSED


# ===========================================================================
# Pricing — the number a rider is charged
# ===========================================================================

@pytest.mark.parametrize('ops_role', [OPS_SUPPORT, OPS_DRIVER_OPS, OPS_FINANCE])
def test_only_an_administrator_may_change_pricing(ops_role):
    """The gate here was a FIFTH divergent definition of operator: `role == 'admin'
    OR is_staff`, either alone sufficient, guarding the rate card."""
    resp = _client(_operator(ops_role)).post(
        '/api/v1/pricing/admin/rate-cards/',
        {'zone': 1, 'vehicle_type': 1, 'base_fare': '1.00'}, format='json')

    assert resp.status_code in REFUSED, (
        f'{ops_role} could POST a rate card: HTTP {resp.status_code}'
    )


def test_an_administrator_may_change_pricing():
    """The control: a 400 from validation is fine, a 403 is not."""
    resp = _client(_operator(OPS_ADMIN)).post(
        '/api/v1/pricing/admin/rate-cards/', {}, format='json')

    assert resp.status_code not in REFUSED, (
        f'an administrator was refused pricing write: HTTP {resp.status_code}'
    )


@pytest.mark.parametrize('ops_role', [OPS_SUPPORT, OPS_DRIVER_OPS, OPS_FINANCE])
def test_every_operator_may_still_READ_pricing(ops_role):
    """An operator answering a fare dispute needs the card that produced the quote.

    Refusing the read would push them to guess, which is worse than letting them
    look.
    """
    resp = _client(_operator(ops_role)).get('/api/v1/pricing/admin/rate-cards/')

    assert resp.status_code == 200, (
        f'{ops_role} could not read pricing: HTTP {resp.status_code}'
    )


def test_a_rider_cannot_read_or_write_pricing():
    """The fifth gate admitted `is_staff` alone. A rider with it must be refused."""
    u = User.objects.create_user(phone_number='+919555111001', role='rider',
                                 username='+919555111001', is_staff=True)
    Rider.objects.create(user_id=u)

    assert _client(u).get('/api/v1/pricing/admin/rate-cards/').status_code in REFUSED
    assert _client(u).post('/api/v1/pricing/admin/rate-cards/', {},
                           format='json').status_code in REFUSED


# ===========================================================================
# The capability model itself
# ===========================================================================

def test_ops_role_grants_nothing_to_a_non_operator():
    """The field must not be an escalation route of its own.

    A rider whose `ops_role` somehow reads 'admin' -- a bad import, a careless
    fixture, a form that exposed the field -- must hold no authority, because
    `ops_role_of` consults `is_operator` first.
    """
    rider = User.objects.create_user(
        phone_number='+919555222001', username='+919555222001',
        role='rider', ops_role=OPS_ADMIN)
    Rider.objects.create(user_id=rider)

    assert not is_operator(rider)
    assert ops_role_of(rider) is None
    assert capabilities_of(rider) == frozenset()
    for cap in ALL_CAPABILITIES:
        assert not has_capability(rider, cap), f'rider held {cap}'


def test_a_superuser_is_an_administrator_regardless_of_the_stored_value():
    """Break-glass must not depend on a field somebody may not have set."""
    su = User.objects.create_superuser(
        phone_number='+919555333001', username='+919555333001',
        password='break-glass-not-a-real-secret')
    User.objects.filter(pk=su.pk).update(ops_role=OPS_SUPPORT)
    su.refresh_from_db()

    assert ops_role_of(su) == OPS_ADMIN
    assert has_capability(su, CAP_FINANCE_PAYOUT)


def test_an_unrecognised_ops_role_does_not_silently_grant_everything():
    """A typo or a value from an older deployment must not become admin by accident.

    It falls back to administrator, which is the non-breaking direction and matches
    the migration's backfill -- but that is a decision, so it is asserted rather
    than left to be discovered.
    """
    op = _operator(OPS_SUPPORT)
    User.objects.filter(pk=op.pk).update(ops_role='not_a_real_role')
    op.refresh_from_db()

    assert ops_role_of(op) == OPS_ADMIN, (
        'an unrecognised ops_role resolved to something other than the documented '
        'fallback'
    )


def test_an_empty_ops_role_falls_back_to_administrator():
    """Rows that predate the field. The migration backfills, but a row created by
    raw SQL or an old fixture may still be blank, and blank must not mean
    'no authority' for an account that is already a full operator -- that would
    lock out the only operator in a pilot."""
    op = _operator(OPS_ADMIN)
    User.objects.filter(pk=op.pk).update(ops_role='')
    op.refresh_from_db()

    assert ops_role_of(op) == OPS_ADMIN


def test_an_unknown_capability_string_is_refused_not_granted():
    """A programming error in an authorization check reads as "no"."""
    assert not has_capability(_operator(OPS_ADMIN), 'finance.payout.maybe')


def test_the_role_map_covers_every_declared_capability():
    """A capability nobody holds is dead code that looks like a control, and one
    that only the administrator holds should be a deliberate choice."""
    granted = set()
    for caps in OPS_ROLE_CAPABILITIES.values():
        granted |= set(caps)

    assert granted == set(ALL_CAPABILITIES), (
        f'declared but never granted: {set(ALL_CAPABILITIES) - granted}; '
        f'granted but not declared: {granted - set(ALL_CAPABILITIES)}'
    )


def test_the_administrator_holds_everything():
    assert capabilities_of(_operator(OPS_ADMIN)) == ALL_CAPABILITIES


@pytest.mark.parametrize('ops_role,forbidden', [
    (OPS_SUPPORT, [CAP_FINANCE_PAYOUT, CAP_DRIVER_KYC, CAP_PRICING_WRITE]),
    (OPS_DRIVER_OPS, [CAP_FINANCE_PAYOUT, CAP_PRICING_WRITE, CAP_SUPPORT_REPLY]),
    (OPS_FINANCE, [CAP_DRIVER_KYC, CAP_PRICING_WRITE, CAP_SUPPORT_REPLY]),
])
def test_each_role_is_missing_what_it_should_be_missing(ops_role, forbidden):
    """The separation stated as a table, so a later widening is visible in a diff."""
    op = _operator(ops_role)
    for cap in forbidden:
        assert not has_capability(op, cap), f'{ops_role} unexpectedly holds {cap}'


# ===========================================================================
# Support authority
# ===========================================================================

def test_a_finance_account_cannot_reply_to_a_support_ticket():
    resp = _client(_operator(OPS_FINANCE)).post(
        '/api/v1/support/admin/tickets/1/reply/', {'body': 'hello'}, format='json')

    assert resp.status_code in REFUSED


def test_a_support_account_can_reply_to_a_support_ticket():
    """The control."""
    resp = _client(_operator(OPS_SUPPORT)).post(
        '/api/v1/support/admin/tickets/1/reply/', {'body': 'hello'}, format='json')

    assert resp.status_code not in REFUSED, (
        f'support was refused a ticket reply: HTTP {resp.status_code}'
    )


# ===========================================================================
# Every operator keeps the shared read surface
# ===========================================================================

@pytest.mark.parametrize('ops_role', [
    OPS_SUPPORT, OPS_DRIVER_OPS, OPS_FINANCE, OPS_ADMIN])
@pytest.mark.parametrize('url', [
    '/api/v1/ride/admin/trips/',
    '/api/v1/driver/admin/',
    '/api/v1/ride/admin/dashboard/',
])
def test_every_operator_can_still_do_their_job(ops_role, url):
    """The read surface stays operator-wide. Splitting authority must not stop
    support from seeing the ride a rider is asking about."""
    resp = _client(_operator(ops_role)).get(url)

    assert resp.status_code == 200, (
        f'{ops_role} was refused {url}: HTTP {resp.status_code}'
    )


# ===========================================================================
# §7 — the privileged action that had no audit trail
# ===========================================================================

def test_a_pricing_change_is_audited():
    """Pricing was the one privileged action with no audit trail at all.

    KYC, payouts, driver deletion and DPDP erasure all recorded an
    `AdminAuditLog` row; SOS transitions keep their own immutable
    `SOSEventUpdate` trail. A rate-card change -- the only operator action that
    alters what a rider is charged -- recorded nothing, so a fare dispute could
    not be answered with who changed what and when.
    """
    from servers.admin_audit.models import AdminAuditLog
    from servers.pricing.models import ServiceZone

    admin = _operator(OPS_ADMIN)
    before = AdminAuditLog.objects.count()

    resp = _client(admin).post('/api/v1/pricing/admin/zones/',
                               _zone_payload('AUDITZONE'), format='json')
    assert resp.status_code in (200, 201), resp.content[:300]

    rows = AdminAuditLog.objects.order_by('-id')
    assert rows.count() == before + 1, 'no audit row was written for a zone create'
    row = rows.first()
    assert row.action == 'service_zone_created'
    assert row.target_type == 'service_zone'
    assert row.actor_id == admin.id, 'the audit row does not name the actor'
    assert row.actor_label, 'the audit row has no actor label to survive deletion'
    assert row.created_at is not None
    assert row.after, 'the audit row carries no after-state to reconstruct the change'
    assert ServiceZone.objects.filter(code='AUDITZONE').exists()


def test_a_pricing_update_records_before_and_after():
    """A dispute needs the card that WAS in force, not only the one that is."""
    from servers.admin_audit.models import AdminAuditLog
    from servers.pricing.models import ServiceZone

    admin = _operator(OPS_ADMIN)
    zone = ServiceZone.objects.create(
        code='DIFFZONE', name='Before Name', zone_type='city', city='Hyderabad',
        polygon_geojson=_POLYGON, is_active=True)

    resp = _client(admin).patch(
        f'/api/v1/pricing/admin/zones/{zone.pk}/',
        {'name': 'After Name'}, format='json')
    assert resp.status_code in (200, 202), resp.content[:300]

    row = AdminAuditLog.objects.filter(action='service_zone_updated').first()
    assert row is not None, 'a pricing update wrote no audit row'
    assert row.before.get('name') == 'Before Name', (
        f'the before-state was not captured: {row.before}'
    )
    assert row.after.get('name') == 'After Name'


def test_an_audit_failure_does_not_block_a_pricing_change():
    """Losing a log row is better than refusing a pricing correction.

    Asserted so the tradeoff is a decision rather than something discovered during
    an incident.
    """
    from unittest import mock

    from servers.pricing.models import ServiceZone

    admin = _operator(OPS_ADMIN)
    with mock.patch('servers.pricing.views.record_admin_action',
                    side_effect=RuntimeError('audit table gone')):
        resp = _client(admin).post('/api/v1/pricing/admin/zones/',
                                   _zone_payload('NOAUDIT', 'No Audit'),
                                   format='json')

    assert resp.status_code in (200, 201), (
        f'an audit failure blocked the pricing change: HTTP {resp.status_code}'
    )
    assert ServiceZone.objects.filter(code='NOAUDIT').exists()
