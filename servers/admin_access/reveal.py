"""The reveal-and-log endpoint: one record, the fields asked for, one audit row.

    POST /api/v1/admin-access/reveal/
    {"target_type": "driver", "target_id": 42,
     "fields": ["phone_number", "email"], "reason": "payout dispute #881"}

    -> {"target_type": "driver", "target_id": 42,
        "fields": {"phone_number": "+91...", "email": "..."},
        "audit_id": 1234, "reveals_in_window": 3}

THE SHAPE OF THE RULE
---------------------
Every list and detail response in the operator namespace is masked
(`masking`). This is the way to a real value, and it is deliberately
narrow:

  * `pii.view`, held AT THE RECORD'S NODE. The permission class checks
    "held anywhere"; the view then asks again with the node, so Hyderabad's
    `pii.view` does not unmask an Austin driver. A record outside the
    caller's scope answers NOT FOUND, like every other scoped endpoint.
  * ONE target per call. `target_id` must be a single integer; a list, or
    a `target_ids` key, is refused with `BULK_REVEAL_REFUSED`. A bulk
    hand-over is an export, and exports have their own rule.
  * Only the fields asked for, only from the catalogue for that target
    type. An unknown field is a 400 that names the allowed ones.
  * One audit row per call, written BEFORE the values are returned, naming
    the operator, the record and the fields -- never the values. If the
    row cannot be written the reveal is refused (503): a hand-over that
    left no trace is the one outcome this endpoint exists to prevent.
  * Rate-limited per operator (`pii_reveal`, 60/min by default) and the
    count in the window is reported on the response and on the audit row,
    so a scripted walk through the driver table shows up as a number.

SOS incidents are NOT revealable here. `/sos/admin/<id>/reveal/` has the
active-incident rule (a closed incident has nobody left to reach) and
writes the same audit shape through `masking.record_reveal`.
"""

import logging

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.throttling import UserRateThrottle

from base.utils import error_response, success_response

from .bridge import ScopedPermission, granted, node_of
from .masking import REVEAL_ACTION, mark_revealed, record_reveal
from .scoping import scoped_or_none

logger = logging.getLogger(__name__)

#: Fields a reveal may hand over per target type. The catalogue IS the
#: contract: a field not listed here cannot be revealed through this door.
REVEALABLE_FIELDS = {
    'driver': (
        'full_name', 'phone_number', 'email', 'username', 'emergency_contact',
        'dob', 'address', 'upi_id', 'bank_account',
        'license_doc_url', 'license_doc_back_url',
    ),
    'user': (
        'full_name', 'phone_number', 'email', 'username', 'emergency_contact',
        'dob', 'address',
    ),
    'rider': (
        'full_name', 'phone_number', 'email', 'username', 'emergency_contact',
        'dob', 'address',
    ),
    'trip': (
        'rider_name', 'rider_phone', 'rider_email',
        'driver_name', 'driver_phone', 'driver_email',
        'pickup_address', 'destination_address',
        'pickup_coordinates', 'destination_coordinates', 'receipt_email',
    ),
    'support_ticket': ('requester_name', 'requester_phone', 'requester_email'),
    'withdrawal': ('driver_name', 'driver_phone', 'driver_email', 'upi_id', 'bank_account'),
    'payment': ('payer_name', 'payer_phone', 'payer_email'),
}

#: Target types that exist but are revealed elsewhere, with the door.
REVEALED_ELSEWHERE = {
    'sos_event': 'POST /api/v1/sos/admin/<id>/reveal/ (active incidents only)',
}


class PiiRevealThrottle(UserRateThrottle):
    """Per-operator cap on reveals; the count in the window is kept.

    `UserRateThrottle` keys on the user id, which is what "per operator"
    means. After a request is allowed, `history` holds every timestamp in
    the window including this one, so its length is the running count.
    """

    scope = 'pii_reveal'

    def allow_request(self, request, view):
        allowed = super().allow_request(request, view)
        if allowed:
            request.pii_reveal_count = len(getattr(self, 'history', []) or [])
        return allowed


def _bad(code, message, field, issue):
    return error_response(code=code, message=message, field=field, issue=issue,
                          status=status.HTTP_400_BAD_REQUEST)


def _not_found(target_type, target_id):
    """The one answer for "does not exist" AND "not in your scope"."""
    return error_response(
        code='NOT_FOUND', message=f'{target_type} not found',
        field='target_id', issue=f'No {target_type} with id {target_id}',
        status=status.HTTP_404_NOT_FOUND,
    )


# ---------------------------------------------------------------------------
# Resolving a target: the row, its node, and a function per field
# ---------------------------------------------------------------------------

def _account_fields(user):
    """The revealable fields of one account, as functions."""
    def address():
        if user is None:
            return None
        parts = [user.house_no, user.street, user.city, user.zip_code]
        return ', '.join(p for p in parts if p) or None

    return {
        'full_name': lambda: getattr(user, 'full_name', None),
        'phone_number': lambda: getattr(user, 'phone_number', None),
        'email': lambda: getattr(user, 'email', None),
        'username': lambda: getattr(user, 'username', None),
        'emergency_contact': lambda: getattr(user, 'emergency_contact', None),
        'dob': lambda: user.dob.isoformat() if getattr(user, 'dob', None) else None,
        'address': address,
    }


def _document_url(field):
    if not field:
        return None
    try:
        return field.url
    except Exception:  # noqa: BLE001 - a missing link beats a failed reveal
        logger.exception('reveal_document_url_failed name=%s', getattr(field, 'name', '?'))
        return None


def _bank_account(driver):
    account = getattr(driver, 'bank_account', None) if driver else None
    if account is None:
        return None
    return {
        'bank_name': account.bank_name,
        'account_number': account.account_number,
        'ifsc_code': account.ifsc_code,
    }


def _driver_fields(driver):
    fields = _account_fields(getattr(driver, 'user_id', None))
    fields.update({
        'upi_id': lambda: driver.upi_id,
        'bank_account': lambda: _bank_account(driver),
        'license_doc_url': lambda: _document_url(driver.license_doc),
        'license_doc_back_url': lambda: _document_url(driver.license_doc_back),
    })
    return fields


def _prefixed(prefix, user):
    """`_account_fields(user)` with `<prefix>_` on the name/phone/email."""
    base = _account_fields(user)
    return {
        f'{prefix}_name': base['full_name'],
        f'{prefix}_phone': base['phone_number'],
        f'{prefix}_email': base['email'],
    }


def _resolve_driver(request, target_id):
    from servers.driver.models import Driver

    driver = scoped_or_none(
        request.user,
        Driver.objects.select_related('user_id', 'home_city'),
        id=target_id,
    )
    if driver is None:
        return None, None, None
    return driver, driver.home_city, _driver_fields(driver)


def _resolve_user(request, target_id, role=None):
    """An account. A rider is placed by `servers/rider/scope.py`; a driver's
    account is scoped through their driver profile."""
    from django.contrib.auth import get_user_model
    from servers.driver.models import Driver
    from servers.rider.models import Rider
    from servers.rider.scope import anchor_nodes, rider_in_scope

    user = get_user_model().objects.filter(id=target_id).first()
    if user is None or (role and user.role != role):
        return None, None, None
    rider = Rider.objects.filter(user_id=user).first()
    if rider is not None and Driver.objects.filter(user_id=user).first() is None:
        # A rider is placed by `servers/rider/scope.py` (trip cities, home
        # city, else phone country), the rule the rider page uses. The
        # capability is then decided at the rider's anchors below.
        if not rider_in_scope(request.user, rider):
            return None, None, None
        request.reveal_rider = rider
        anchors = anchor_nodes(rider)
        return user, anchors[0] if anchors else None, _account_fields(user)
    profile = Driver.objects.select_related('home_city').filter(user_id=user).first()
    if profile is not None:
        if scoped_or_none(request.user, Driver.objects.all(), id=profile.id) is None:
            return None, None, None
        return user, profile.home_city, _account_fields(user)
    return user, None, _account_fields(user)


def _resolve_trip(request, target_id):
    from servers.ride.models import Receipt, Trip

    trip = scoped_or_none(
        request.user,
        Trip.objects.select_related('user_id', 'driver_id', 'driver_id__user_id',
                                    'zone', 'zone__geo_node'),
        id=target_id,
    )
    if trip is None:
        return None, None, None
    driver_user = trip.driver_id.user_id if trip.driver_id else None
    fields = {}
    fields.update(_prefixed('rider', trip.user_id))
    fields.update(_prefixed('driver', driver_user))
    fields.update({
        'pickup_address': lambda: trip.pickup_address,
        'destination_address': lambda: trip.destination_address,
        'pickup_coordinates': lambda: {
            'lat': str(trip.pickup_lat), 'lng': str(trip.pickup_long)},
        'destination_coordinates': lambda: {
            'lat': str(trip.destination_lat), 'lng': str(trip.destination_long)},
        'receipt_email': lambda: (
            Receipt.objects.filter(trip_id=trip).order_by('-version')
            .values_list('sent_to_email', flat=True).first()
        ),
    })
    return trip, node_of(trip), fields


def _resolve_ticket(request, target_id):
    from servers.support.models import SupportTicket

    ticket = scoped_or_none(
        request.user,
        SupportTicket.objects.select_related('user_id', 'trip_id', 'trip_id__zone',
                                             'trip_id__zone__geo_node'),
        id=target_id,
    )
    if ticket is None:
        return None, None, None
    return ticket, node_of(ticket), _prefixed('requester', ticket.user_id)


def _resolve_withdrawal(request, target_id):
    from servers.driver.models import WithdrawalRequest

    withdrawal = scoped_or_none(
        request.user,
        WithdrawalRequest.objects.select_related('driver', 'driver__user_id',
                                                 'driver__home_city'),
        id=target_id,
    )
    if withdrawal is None:
        return None, None, None
    driver = withdrawal.driver
    fields = _prefixed('driver', driver.user_id)
    fields.update({
        'upi_id': lambda: driver.upi_id,
        'bank_account': lambda: _bank_account(driver),
    })
    return withdrawal, node_of(withdrawal), fields


def _resolve_payment(request, target_id):
    from servers.payments.models import Payment

    payment = scoped_or_none(
        request.user,
        Payment.objects.select_related('user_id', 'trip_id', 'trip_id__zone',
                                       'trip_id__zone__geo_node'),
        id=target_id,
    )
    if payment is None:
        return None, None, None
    node = node_of(payment.trip_id) if payment.trip_id else None
    return payment, node, _prefixed('payer', payment.user_id)


RESOLVERS = {
    'driver': _resolve_driver,
    'user': _resolve_user,
    'rider': lambda request, target_id: _resolve_user(request, target_id, role='rider'),
    'trip': _resolve_trip,
    'support_ticket': _resolve_ticket,
    'withdrawal': _resolve_withdrawal,
    'payment': _resolve_payment,
}

if set(RESOLVERS) != set(REVEALABLE_FIELDS):  # pragma: no cover - import-time guard
    raise RuntimeError('reveal: RESOLVERS and REVEALABLE_FIELDS disagree')


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('pii.view')])
@throttle_classes([PiiRevealThrottle])
def reveal_pii(request):
    """Reveal the requested personal fields of ONE record, and log it."""
    data = request.data if hasattr(request, 'data') else {}

    if 'target_ids' in data or isinstance(data.get('target_id'), (list, tuple, set)):
        return _bad('BULK_REVEAL_REFUSED',
                    'A reveal is one record at a time.',
                    'target_id', 'Send one target_id per call; a list is refused.')

    target_type = str(data.get('target_type') or '').strip()
    if target_type in REVEALED_ELSEWHERE:
        return _bad('REVEALED_ELSEWHERE',
                    f'{target_type} is revealed through its own endpoint.',
                    'target_type', REVEALED_ELSEWHERE[target_type])
    if target_type not in REVEALABLE_FIELDS:
        return _bad('UNKNOWN_TARGET_TYPE',
                    f'target_type must be one of: {", ".join(sorted(REVEALABLE_FIELDS))}',
                    'target_type', f'Got {target_type!r}')

    raw_id = data.get('target_id')
    try:
        target_id = int(str(raw_id).strip())
        if isinstance(raw_id, bool) or target_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return _bad('INVALID_TARGET_ID', 'target_id must be a positive integer.',
                    'target_id', f'Got {raw_id!r}')

    fields = data.get('fields')
    if not isinstance(fields, (list, tuple)) or not fields:
        return _bad('FIELDS_REQUIRED', 'fields must be a non-empty list.',
                    'fields', f'Got {fields!r}')
    fields = [str(f).strip() for f in fields]
    allowed = REVEALABLE_FIELDS[target_type]
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        return _bad('UNKNOWN_FIELD',
                    f'Unknown field(s) for {target_type}: {", ".join(unknown)}. '
                    f'Allowed: {", ".join(allowed)}',
                    'fields', f'Unknown: {unknown}')

    reason = str(data.get('reason') or '')[:10000]

    target, node, resolvers = RESOLVERS[target_type](request, target_id)
    if target is None:
        return _not_found(target_type, target_id)

    # `pii.view` AT THE RECORD'S NODE. The permission class only asked
    # "anywhere"; an operator with `pii.view` in Hyderabad and a read-only
    # role in Austin reaches an Austin driver through scope and is refused
    # here. A record with no node (a rider) was passed by scope above and
    # is decided on "anywhere", which is the rule for riders everywhere.
    rider = getattr(request, 'reveal_rider', None)
    if rider is not None:
        from servers.rider.scope import rider_may

        allowed_here = rider_may(request.user, 'pii.view', rider)
    else:
        allowed_here, _how = granted(request.user, 'pii.view', node)
    if not allowed_here:
        return error_response(
            code='PII_VIEW_NOT_HELD_HERE',
            message='You do not hold pii.view where this record sits.',
            field='target_id',
            issue=f'pii.view not held at {getattr(node, "code", "(no node)")}',
            status=status.HTTP_403_FORBIDDEN,
        )

    count = getattr(request, 'pii_reveal_count', None)
    row = record_reveal(
        request, target_type=target_type, target_id=target_id,
        fields=fields, reason=reason, action=REVEAL_ACTION, node=node,
        extra={'reveals_in_window': count, 'via': 'admin-access/reveal'},
    )
    if row is None:
        return error_response(
            code='AUDIT_UNAVAILABLE',
            message='The reveal could not be logged, so nothing was revealed.',
            field='general', issue='audit write failed',
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    revealed = {name: resolvers[name]() for name in dict.fromkeys(fields)}
    mark_revealed(request)
    return success_response(
        {
            'target_type': target_type,
            'target_id': target_id,
            'fields': revealed,
            'audit_id': row.id,
            'reveals_in_window': count,
        },
        status.HTTP_200_OK,
    )
