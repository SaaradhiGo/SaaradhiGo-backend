"""Operator-created accounts, including the QA test accounts.

  POST /auth/admin/invite/   create or invite an account with a role

WHY AN INVITE RATHER THAN "CREATE WITH PASSWORD"
------------------------------------------------
No password is generated, returned, logged or stored by this endpoint. The
invited person sets their own through the reset flow, and using that link is
what proves they control the address.

The alternative — an operator types a password and reads it out, or the API
returns a generated one — means a credential exists in a chat message, a
ticket, a terminal history and an audit log. For QA accounts that would be
convenient and it is still the wrong shape: a QA account with a password
somebody pasted into Slack is a production-adjacent account with a leaked
credential.

PRIVILEGE ESCALATION IS THE WHOLE RISK HERE
-------------------------------------------
This endpoint assigns a `role`, so it is the most sensitive thing in the auth
surface. The rules:

  * Operator-only, enforced by `IsAdmin` **and** re-checked with
    `is_operator` — the same belt-and-braces the console uses, because
    `role == 'admin'` alone was once enough to reach operator screens.
  * MFA must already be cleared on the calling session for an operator to
    invite another operator. Inviting a rider or a driver does not require it.
  * Only an operator whose `ops_role` is `admin` may invite an operator.
    Support, driver-ops and finance may create rider and driver test accounts
    and nothing else. So a compromised support account cannot mint an admin.
  * `is_superuser` is never settable here. A superuser is created by
    `manage.py` on a machine someone has access to, not over HTTP.
  * An existing account is never escalated. If the address or phone already
    belongs to someone, the request is refused rather than "updated" — a
    silent role change on an existing account is exactly the escalation this
    guards against.

Every call is written to `AdminAuditLog`, successes and refusals alike.
"""

import logging
import secrets

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated

from base import ops_mfa
from base.auth_email import send_account_invite
from base.countries import normalize_phone
from base.permissions import IsAdmin, is_operator, ops_role_of
from base.utils import error_response, success_response
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

#: Roles this endpoint may assign, and who may assign them.
#:
#: 'admin' creates an OPERATOR account, which can approve KYC and release
#: payouts, so it is restricted to a full administrator.
_ROLE_LABELS = {
    'rider': 'a rider',
    'driver': 'a driver',
    'admin': 'an operator',
}
_OPERATOR_ROLES = ('admin',)


def _refuse(code, message, field, issue, http_status):
    return error_response(code=code, message=message, field=field,
                          issue=issue, status=http_status)


@api_view(['POST'])
@permission_classes([IsAuthenticated, IsAdmin])
def invite_account(request):
    """
    POST /api/v1/auth/admin/invite/

    Request:
        {
          "email": "qa-rider@example.com",
          "role": "rider" | "driver" | "admin",
          "phone_number": "+919XXXXXXXXX",   # optional
          "full_name": "QA Rider",            # optional
          "ops_role": "support"               # only with role=admin
        }

    Response 201:
        {"user_id": 42, "email": "...", "role": "rider",
         "invite_sent": true, "email_delivery_configured": true}

    No password is ever returned.
    """
    actor = request.user

    # Re-checked beyond IsAdmin: `role == 'admin'` without `is_staff` is not
    # an operator, and that distinction has been a real privilege boundary
    # here before.
    if not is_operator(actor):
        logger.warning('invite_refused reason=not_operator actor_id=%s',
                       actor.pk)
        return _refuse('AUTH_FORBIDDEN', 'Operator access is required.',
                       'account', 'Caller is not an operator',
                       status.HTTP_403_FORBIDDEN)

    role = (request.data.get('role') or '').strip().lower()
    if role not in _ROLE_LABELS:
        return _refuse('AUTH_INVALID_ROLE',
                       f'Role must be one of {sorted(_ROLE_LABELS)}.',
                       'role', f'Got {role!r}', status.HTTP_400_BAD_REQUEST)

    # --- who may invite whom ---------------------------------------------
    if role in _OPERATOR_ROLES:
        if ops_role_of(actor) != 'admin':
            logger.warning(
                'invite_refused reason=insufficient_ops_role actor_id=%s '
                'actor_ops_role=%s', actor.pk, ops_role_of(actor),
            )
            record_admin_action(
                request, action='operator_invite_refused', target_type='user',
                target_id='', after={'role': role},
                reason='Caller ops_role may not create operators',
            )
            return _refuse('AUTH_FORBIDDEN',
                           'Only a full administrator can create an operator '
                           'account.', 'role',
                           'ops_role is not admin', status.HTTP_403_FORBIDDEN)

        # Creating an operator is the one action here that can raise the
        # platform's privilege ceiling, so the caller's own second factor must
        # be fresh on this session.
        if not ops_mfa.console_access_allowed(request):
            logger.warning('invite_refused reason=mfa_not_cleared actor_id=%s',
                           actor.pk)
            return _refuse('AUTH_MFA_REQUIRED',
                           'Clear multi-factor authentication before creating '
                           'an operator account.', 'mfa',
                           'Session has not cleared MFA',
                           status.HTTP_401_UNAUTHORIZED)

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
        phone = normalize_phone(raw_phone)
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
                is_staff=role in _OPERATOR_ROLES,
                is_active=True,
            )
            # Never a generated password, not even a discarded one. An
            # unusable password means `check_password` always fails until the
            # invitee sets their own.
            user.set_unusable_password()

            if role in _OPERATOR_ROLES:
                requested_ops = (request.data.get('ops_role') or 'support').strip()
                valid = {c[0] for c in user_model._meta.get_field('ops_role').choices}
                if requested_ops not in valid:
                    raise ValueError(f'ops_role must be one of {sorted(valid)}')
                user.ops_role = requested_ops

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
                Driver.objects.create(user_id=user)
    except ValueError as exc:
        return _refuse('AUTH_INVALID_OPS_ROLE', str(exc), 'ops_role',
                       str(exc), status.HTTP_400_BAD_REQUEST)
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
            'ops_role': user.ops_role if role in _OPERATOR_ROLES else '',
            'is_staff': user.is_staff,
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
            'ops_role': user.ops_role if role in _OPERATOR_ROLES else None,
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
