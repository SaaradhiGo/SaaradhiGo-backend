"""Operator-created accounts: operators, and -- outside production -- QA test accounts.

  POST /auth/admin/invite/               invite an OPERATOR          admin.manage
  POST /auth/admin/invite/test-rider/    create a rider test account   rider.write   QA ONLY
  POST /auth/admin/invite/test-driver/   create a driver test account  driver.write  QA ONLY

WHY AN INVITE RATHER THAN "CREATE WITH PASSWORD"
------------------------------------------------
No password is generated, returned, logged or stored by this endpoint. The
invited person sets their own through the reset flow, and using that link is
what proves they control the address.

The alternative -- an operator types a password and reads it out, or the API
returns a generated one -- means a credential exists in a chat message, a
ticket, a terminal history and an audit log. For QA accounts that would be
convenient and it is still the wrong shape: a QA account with a password
somebody pasted into Slack is a production-adjacent account with a leaked
credential.

THREE ROUTES, THREE CAPABILITIES (owner's ruling, 2026-10-28)
-------------------------------------------------------------
One endpoint used to create all three kinds of account behind `IsAdmin` plus
an internal `ops_role` check, and so sat on the structural guard's allowlist
as the one admin route with no capability. The ruling: no allowlist.

  * Creating an OPERATOR is the one action here that can raise the
    platform's privilege ceiling. It needs `admin.manage` -- held by Super
    Admin only -- AND a fresh second factor on the calling session.
  * Rider and driver TEST accounts are a QA convenience, so the routes that
    create them are registered only when `settings.IS_PRODUCTION` is false
    (`urls.qa_only_urlpatterns`). In a production build the URL does not
    resolve; `tests/test_production_boot_guards.py` proves it. On QA they
    need `rider.write` / `driver.write` respectively, and a driver test
    account is created IN A CITY the caller holds `driver.write` over.

Who holds what on QA: `rider.write` is held by Super Admin only (no other
template carries it), so Support Agents -- who created rider test accounts
through the old endpoint -- can no longer do so unless the owner grants
`rider.write` to `support_agent`. `driver.write` is held by City Ops Manager
and Safety Officer in their scope, and by Super Admin.

THE INVARIANTS THAT DID NOT CHANGE
----------------------------------
  * `is_superuser` is never settable here. A superuser is created by
    `manage.py` on a machine someone has access to, not over HTTP.
  * An existing account is never escalated. If the address or phone already
    belongs to someone, the request is refused rather than "updated" -- a
    silent role change on an existing account is exactly the escalation this
    guards against.
  * Every successful creation is written to `AdminAuditLog`.
"""

import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base import ops_mfa
from base.auth_email import send_account_invite
from base.countries import normalize_phone
from base.permissions import is_operator
from base.utils import error_response, success_response
from servers.admin_access.access import scope_paths
from servers.admin_access.bridge import ScopedPermission, granted
from servers.admin_access.models import CITY, GeoNode
from servers.admin_audit.services import record_admin_action

from .identity import (
    EmailAlreadyLinked,
    InvalidEmail,
    assert_email_available,
    normalize_email,
)
from .password_views import _uid, reset_tokens

logger = logging.getLogger(__name__)

user_model = get_user_model()

_ROLE_LABELS = {
    'rider': 'a rider',
    'driver': 'a driver',
    'admin': 'an operator',
}

#: Where the QA-only routes live, for the message the operator route gives
#: when asked to create a test account.
TEST_RIDER_PATH = '/api/v1/auth/admin/invite/test-rider/'
TEST_DRIVER_PATH = '/api/v1/auth/admin/invite/test-driver/'


def _refuse(code, message, field, issue, http_status):
    return error_response(code=code, message=message, field=field,
                          issue=issue, status=http_status)


def _not_in_production():
    """The QA routes are not registered in production; this is the second
    lock on the same door, should the URLconf ever be built differently."""
    return _refuse('NOT_FOUND', 'Not found.', 'general',
                   'Test-account creation does not exist in production.',
                   status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('admin.manage')])
def invite_operator(request):
    """
    POST /api/v1/auth/admin/invite/

    Request:
        {
          "email": "ops@example.com",
          "role": "admin",                    # optional; anything else is refused
          "ops_role": "support",              # legacy console role, default support
          "phone_number": "+919XXXXXXXXX",    # optional
          "full_name": "New Operator"         # optional
        }

    Response 201:
        {"user_id": 42, "email": "...", "role": "admin", "ops_role": "support",
         "invite_sent": true, "email_delivery_configured": true}

    No password is ever returned. The new operator holds NO assignment: who
    may do what, where, is granted afterwards through `migrate_operators`.
    """
    role = (request.data.get('role') or 'admin').strip().lower()
    if role != 'admin':
        return _refuse(
            'AUTH_INVALID_ROLE',
            'This endpoint invites operators only. Rider and driver test '
            f'accounts are created through {TEST_RIDER_PATH} and '
            f'{TEST_DRIVER_PATH}, which exist outside production only.',
            'role', f'Got {role!r}', status.HTTP_400_BAD_REQUEST,
        )

    # Creating an operator is the one action here that can raise the
    # platform's privilege ceiling, so the caller's own second factor must
    # be fresh on this session.
    if not ops_mfa.console_access_allowed(request):
        logger.warning('invite_refused reason=mfa_not_cleared actor_id=%s',
                       request.user.pk)
        return _refuse('AUTH_MFA_REQUIRED',
                       'Clear multi-factor authentication before creating '
                       'an operator account.', 'mfa',
                       'Session has not cleared MFA',
                       status.HTTP_401_UNAUTHORIZED)

    ops_role = (request.data.get('ops_role') or 'support').strip()
    valid = {c[0] for c in user_model._meta.get_field('ops_role').choices}
    if ops_role not in valid:
        return _refuse('AUTH_INVALID_OPS_ROLE',
                       f'ops_role must be one of {sorted(valid)}', 'ops_role',
                       f'Got {ops_role!r}', status.HTTP_400_BAD_REQUEST)

    return _create_account(request, role='admin', ops_role=ops_role)


# ---------------------------------------------------------------------------
# QA test accounts. NOT ROUTED IN PRODUCTION -- see urls.qa_only_urlpatterns.
# ---------------------------------------------------------------------------

@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('rider.write')])
def invite_test_rider(request):
    """
    POST /api/v1/auth/admin/invite/test-rider/      (QA only)

    Request: {"email": "qa-rider@example.com", "phone_number": "...",
              "full_name": "QA Rider"}

    A rider has no city (`scoping.RIDERS_ARE_GLOBAL`), so `rider.write`
    held anywhere is enough.
    """
    if settings.IS_PRODUCTION:
        return _not_in_production()
    return _create_account(request, role='rider')


@api_view(['POST'])
@permission_classes([IsAuthenticated, ScopedPermission('driver.write')])
def invite_test_driver(request):
    """
    POST /api/v1/auth/admin/invite/test-driver/     (QA only)

    Request: {"email": "qa-driver@example.com", "home_city": "IN-TG-HYD",
              "phone_number": "...", "full_name": "QA Driver"}

    SCOPED. The test driver is created in `home_city`, and the caller must
    hold `driver.write` over that city -- a Hyderabad City Ops Manager
    creates Hyderabad test drivers and nobody else's. Only a caller whose
    scope is unrestricted (Global) may omit `home_city`, because a driver
    with no home city is visible at Global only and would otherwise be
    created by someone who could not then see it.
    """
    if settings.IS_PRODUCTION:
        return _not_in_production()

    code = (request.data.get('home_city') or '').strip()
    home_city = None
    if code:
        home_city = GeoNode.objects.filter(code=code, kind=CITY).first()
        if home_city is None:
            return _refuse('HOME_CITY_UNKNOWN',
                           f'{code!r} is not a known city.', 'home_city',
                           'No city GeoNode with that code',
                           status.HTTP_400_BAD_REQUEST)
        allowed, _how = granted(request.user, 'driver.write', home_city)
        if not allowed:
            return _refuse('SCOPE_NOT_HELD',
                           f'You do not hold driver.write over {code}.',
                           'home_city', 'Requested city is outside your '
                           'assignments', status.HTTP_403_FORBIDDEN)
    elif scope_paths(request.user) is not None:
        return _refuse('HOME_CITY_REQUIRED',
                       'Name the city (home_city) this test driver belongs '
                       'to; it must be one you hold driver.write over.',
                       'home_city', 'Scoped caller omitted home_city',
                       status.HTTP_400_BAD_REQUEST)

    return _create_account(request, role='driver', home_city=home_city)


# ---------------------------------------------------------------------------
# The shared body
# ---------------------------------------------------------------------------

def _create_account(request, *, role, ops_role=None, home_city=None):
    actor = request.user

    # Re-checked beyond the permission class: `role == 'admin'` without
    # `is_staff` is not an operator, and that distinction has been a real
    # privilege boundary here before.
    if not is_operator(actor):
        logger.warning('invite_refused reason=not_operator actor_id=%s',
                       actor.pk)
        return _refuse('AUTH_FORBIDDEN', 'Operator access is required.',
                       'account', 'Caller is not an operator',
                       status.HTTP_403_FORBIDDEN)

    is_operator_account = role == 'admin'

    # --- the address ------------------------------------------------------
    try:
        email = normalize_email(request.data.get('email'))
    except InvalidEmail:
        return _refuse('AUTH_EMAIL_INVALID',
                       'That does not look like an email address.', 'email',
                       'Invalid address', status.HTTP_400_BAD_REQUEST)
    if not email:
        return _refuse('AUTH_EMAIL_REQUIRED', 'An email address is required.',
                       'email', 'Missing email', status.HTTP_400_BAD_REQUEST)

    try:
        assert_email_available(email)
    except EmailAlreadyLinked as exc:
        # NOT an update. Re-pointing an existing account's role from here is
        # the escalation path this endpoint must not have.
        return _refuse('AUTH_EMAIL_IN_USE', str(exc), 'email',
                       'Address belongs to an existing account',
                       status.HTTP_409_CONFLICT)

    # --- the phone --------------------------------------------------------
    #
    # `phone_number` is the USERNAME_FIELD and is NOT NULL + unique, so one is
    # always needed. When the operator does not supply one, a reserved
    # placeholder is generated: it is syntactically a phone number so the
    # column's constraints hold, and it can never receive an OTP, so the
    # account is email-only until someone sets a real number.
    raw_phone = request.data.get('phone_number')
    if raw_phone:
        from servers.platform_ops.markets import phone_country_for_operator
        phone = normalize_phone(raw_phone,
                                default_country=phone_country_for_operator(request.user))
        if not phone:
            return _refuse('AUTH_INVALID_PHONE',
                           'That phone number is not valid.', 'phone_number',
                           'Could not normalise the number',
                           status.HTTP_400_BAD_REQUEST)
        if user_model.objects.filter(phone_number=phone).exists():
            return _refuse('AUTH_PHONE_IN_USE',
                           'That phone number is already on another account.',
                           'phone_number', 'Phone belongs to an existing '
                           'account', status.HTTP_409_CONFLICT)
    else:
        # Reserved range, deliberately not dialable and not E.164-valid for
        # any country, so it cannot collide with a real number and cannot be
        # used to request an OTP.
        phone = f'invite-{secrets.token_hex(8)}'

    try:
        with transaction.atomic():
            user = user_model(
                phone_number=phone,
                email=email,
                full_name=(request.data.get('full_name') or '').strip() or None,
                role=role,
                is_staff=is_operator_account,
                is_active=True,
            )
            # Never a generated password, not even a discarded one. An
            # unusable password means `check_password` always fails until the
            # invitee sets their own.
            user.set_unusable_password()
            if is_operator_account:
                user.ops_role = ops_role

            # `username` is inherited from AbstractUser and is unique. It is
            # not used to sign in -- USERNAME_FIELD is phone_number -- so it
            # just has to be unique and stable.
            user.username = f'u-{secrets.token_hex(8)}'
            user.save()

            # Profile rows, so the account behaves like one created by OTP.
            if role == 'rider':
                from servers.rider.models import Rider
                Rider.objects.create(user_id=user)
            elif role == 'driver':
                from servers.driver.models import Driver
                Driver.objects.create(user_id=user, home_city=home_city)
    except IntegrityError as exc:
        logger.error('invite_failed reason=integrity detail=%s', exc)
        return _refuse('AUTH_INVITE_FAILED',
                       'Could not create that account. It may already exist.',
                       'account', 'Integrity error',
                       status.HTTP_409_CONFLICT)

    # The set-password link. Using it both sets the password and verifies the
    # address, so no separate verification step is needed for an invite.
    token = reset_tokens.make_token(user)
    delivered = send_account_invite(
        user=user,
        uid=_uid(user),
        token=token,
        role_label=_ROLE_LABELS[role],
        invited_by=(actor.full_name or actor.phone_number or f'#{actor.pk}'),
    )

    record_admin_action(
        request,
        action='account_invited',
        target_type='user',
        target_id=user.pk,
        after={
            'role': role,
            'ops_role': user.ops_role if is_operator_account else '',
            'is_staff': user.is_staff,
            'home_city': home_city.code if home_city else '',
            'email_delivered': delivered,
            'phone_placeholder': not bool(raw_phone),
        },
        reason=f'Operator invited {_ROLE_LABELS[role]}',
    )
    logger.info(
        'account_invited actor_id=%s user_id=%s role=%s delivered=%s',
        actor.pk, user.pk, role, delivered,
    )

    return success_response(
        data={
            'user_id': user.pk,
            'email': user.email,
            'role': user.role,
            'ops_role': user.ops_role if is_operator_account else None,
            'home_city': home_city.code if home_city else None,
            'phone_number': user.phone_number if raw_phone else None,
            'invite_sent': delivered,
            # Told plainly, because an operator who thinks a link went out
            # when it did not will wait instead of acting.
            'email_delivery_configured': delivered,
            'next_step': (
                'The invitee sets their own password from the emailed link.'
                if delivered else
                'Email delivery is not configured, so NO link was sent. '
                'Configure EMAIL_HOST/DEFAULT_FROM_EMAIL, then re-send with '
                '/auth/password/forgot/.'
            ),
        },
        status_code=status.HTTP_201_CREATED,
    )
