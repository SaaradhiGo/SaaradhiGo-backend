"""What happens to a user account instead of deleting it.

THE RULING (owner, 2026-10-27)
------------------------------
  * An account that appears as the actor on any audit row is never deleted.
  * Staff and operator accounts are deactivated (`is_active=False`), never
    deleted, whether or not they have acted yet.
  * A rider or driver who asks to be erased (DPDP Act 2023, CCPA) is
    ANONYMISED: the row and its id stay, every direct identifier on it is
    replaced with a placeholder that cannot be reversed, and an audit row
    records that it happened, why, and under which request reference.

WHY DELETION IS THE WRONG TOOL
------------------------------
Deleting a user row cascades. Trips, payments, wallet transactions, ratings,
receipts and support tickets all point at the user with `on_delete=CASCADE`,
and most of those have legal retention periods (seven years under Income
Tax and GST for anything financial) or are the only record a dispute or a
safety investigation can turn to. Deleting the person deletes the evidence.

Deleting an actor is worse. The audit log used to hold the actor as a FK
with SET_NULL, so deleting a staff account would blank the author on every
row they ever wrote -- and on PostgreSQL the append-only trigger refuses
that UPDATE, so the delete raised from inside the cascade. The actor is now
a snapshot (`servers.admin_audit.models`), which fixes the raise; this
module fixes the policy, so that nobody tries.

WHERE THE REFUSAL HAPPENS, AND WHY TWICE
----------------------------------------
`customUser.delete()` and the user queryset's `delete()` call
`refuse_if_undeletable` / `refuse_if_any_undeletable` BEFORE Django's
collector opens its transaction, so a refused delete is a clean exception:
nothing was started, and a caller inside `ATOMIC_REQUESTS` or a test can
carry on. A `pre_delete` receiver repeats the check as the backstop for
the path those two do not see -- a cascade arriving from some other model's
delete -- where raising mid-collector is still right, because the
alternative is the row going. Raw SQL is not covered, and is not meant to
be: that is somebody with database credentials, which is what the hash
chain is for.

WHAT ANONYMISATION KEEPS AND WHAT IT WIPES
------------------------------------------
Kept: the row and its primary key, so every FK stays valid; `role`;
`created_at`; trips, payments, wallet, ratings, receipts, notifications,
support tickets. Wiped: phone number and username (replaced by
`deleted-<id>`, which is unique and plainly a placeholder), email, name,
gender, date of birth, address fields, emergency contact, avatar, push
token, email verification, the password (set unusable), the driver's licence
images and payout UPI id, the documents on the driver's vehicles, and the
rider's saved places. Every outstanding refresh token is blacklisted. The
account is deactivated.

Not wiped, and recorded here so the gap is visible: the plain-text labels on
SOS events, the bodies of past notifications and support messages, and
anything in another system (payment-provider records, SMS logs). Each is a
separate decision about retention, not a field on this row.
"""

import logging

from django.db import transaction
from django.db.models import ProtectedError
from django.db.models.signals import pre_delete

logger = logging.getLogger(__name__)

AUDIT_ACTION = 'account_anonymised'
_DISPATCH_UID = 'auth_user.erasure.guard_user_deletion'
PLACEHOLDER_PREFIX = 'deleted-'

#: Fields on the user row that are set to NULL by anonymisation.
_NULLED_USER_FIELDS = (
    'email', 'email_verified_at', 'gender', 'dob', 'house_no', 'street',
    'city', 'zip_code', 'emergency_contact', 'fcm_token',
)
#: Document fields on a Driver that are removed from storage and cleared.
_DRIVER_FILE_FIELDS = ('license_doc', 'license_doc_back')
#: Document fields on a Vehicle that are removed from storage and cleared.
#: They are the vehicle's papers, but each one names its owner.
_VEHICLE_FILE_FIELDS = ('rc_doc', 'permit_doc', 'insurance_doc',
                        'fitness_doc', 'puc_doc', 'inspection_doc')


class AccountDeletionRefused(ProtectedError):
    """Raised from `pre_delete` when a user row must not be removed.

    A `ProtectedError` so that the Django admin and anything else that
    already handles PROTECT cascades reports it properly, and so a caller
    that catches `IntegrityError` catches this too. `protected_objects`
    holds what is doing the protecting: the audit rows naming the actor, or
    the account itself when the rule is that staff are never deleted.
    """


class AnonymisationRefused(ValueError):
    """The account cannot be anonymised as asked: an operator, or no reason."""


def _is_staff_or_operator(user):
    from base.permissions import is_operator

    return bool(
        getattr(user, 'is_staff', False)
        or getattr(user, 'is_superuser', False)
        or is_operator(user)
    )


def _audit_rows_naming(user):
    from servers.admin_audit.models import AdminAuditLog

    return AdminAuditLog.objects.filter(actor_id=user.pk)


def _staff_refusal(user):
    return AccountDeletionRefused(
        f'User {user.pk} is a staff/operator account. Staff accounts are '
        f'deactivated, never deleted: set is_active=False (and revoke their '
        f'assignments) instead.',
        {user},
    )


def _actor_refusal(user, naming):
    return AccountDeletionRefused(
        f'User {user.pk} appears as the actor on {naming.count()} audit '
        f'row(s) and cannot be deleted; the log must keep naming them. '
        f'Deactivate the account, or for an erasure request use '
        f'servers.auth_user.erasure.anonymise_user, which keeps the row and '
        f'wipes the identifiers.',
        set(naming.order_by('id')[:10]),
    )


def refuse_if_undeletable(user):
    """Raise `AccountDeletionRefused` if this one account must not go."""
    if user.pk is None:
        return
    if _is_staff_or_operator(user):
        raise _staff_refusal(user)
    naming = _audit_rows_naming(user)
    if naming.exists():
        raise _actor_refusal(user, naming)


def refuse_if_any_undeletable(queryset):
    """Raise if ANY account the queryset would delete must not go.

    Two indexed queries, before the collector touches anything: a bulk
    delete that would have removed a thousand riders and one operator
    removes nothing, and says which one.
    """
    from django.db.models import Q

    from servers.admin_audit.models import AdminAuditLog

    staff = (queryset.filter(Q(is_staff=True) | Q(is_superuser=True))
             .order_by('pk').first())
    if staff is not None:
        raise _staff_refusal(staff)
    actors = AdminAuditLog.objects.filter(actor_id__isnull=False).values('actor_id')
    acted = queryset.filter(pk__in=actors).order_by('pk').first()
    if acted is not None:
        raise _actor_refusal(acted, _audit_rows_naming(acted))


def guard_user_deletion(sender, instance, **kwargs):
    """`pre_delete` backstop for cascades: see the module docstring.

    Raising here aborts the whole `Collector.delete()` inside its
    transaction, so nothing that would have cascaded is touched either.
    """
    refuse_if_undeletable(instance)


def connect():
    """Wire the guard. Called from `AuthUserConfig.ready()`.

    `dispatch_uid` makes a second call a no-op rather than a second check.
    """
    from django.contrib.auth import get_user_model

    pre_delete.connect(guard_user_deletion, sender=get_user_model(),
                       dispatch_uid=_DISPATCH_UID)


# ---------------------------------------------------------------------------
# Anonymisation
# ---------------------------------------------------------------------------

def _clear_file(instance, field_name, failures):
    """Remove the file behind `field_name` from storage and clear the field.

    Storage removal is best-effort: an object store that is unreachable must
    not leave the identifiers on the row in place, so the field is cleared
    regardless and the failure is recorded in the audit row's payload.
    """
    file_field = getattr(instance, field_name, None)
    if not file_field:
        return False
    try:
        file_field.delete(save=False)
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal
        failures.append(f'{instance._meta.label}.{field_name}: {type(exc).__name__}')
        setattr(instance, field_name, None)
    return True


def _anonymise_driver_profile(user, failures):
    """Licence images, payout id and vehicle papers. Returns what was cleared."""
    cleared = []
    try:
        driver = user.driver
    except Exception:  # noqa: BLE001 - RelatedObjectDoesNotExist: not a driver
        return cleared

    changed = []
    for name in _DRIVER_FILE_FIELDS:
        if _clear_file(driver, name, failures):
            changed.append(name)
    if getattr(driver, 'upi_id', None):
        driver.upi_id = None
        changed.append('upi_id')
    if changed:
        driver.save(update_fields=changed)
        cleared.extend(f'driver.{name}' for name in changed)

    for vehicle in driver.vehicle_set.all():
        vehicle_changed = [name for name in _VEHICLE_FILE_FIELDS
                           if _clear_file(vehicle, name, failures)]
        if vehicle_changed:
            vehicle.save(update_fields=vehicle_changed)
            cleared.extend(f'vehicle[{vehicle.pk}].{name}' for name in vehicle_changed)
    return cleared


def _anonymise_rider_profile(user):
    """Saved places are home and work addresses. Returns what was cleared."""
    places = getattr(user, 'favorite_places', None)
    if places is None:
        return []
    deleted, _ = places.all().delete()
    return [f'favorite_places({deleted})'] if deleted else []


def anonymise_user(user, *, reason, reference, actor=None, actor_label='',
                   request=None):
    """Erase a rider's or driver's identifiers in place. Returns the audit row.

    `reason` is why (the request as the person put it, or the policy under
    which it is done) and `reference` is the ticket or request id, so the
    audit row can be matched to the request six months later. Both are
    required: an erasure with no stated reason is indistinguishable from a
    mistake, and under DPDP/CCPA the request itself must be answerable.

    `actor` is who is doing it -- the person themselves for a self-service
    request, an operator for one handled by support -- and becomes the
    audit row's actor snapshot; `actor_label` names the caller when there
    is no user to snapshot (a management command). Refuses an operator
    target outright: staff accounts are deactivated through the access
    tooling, and their identifiers stay, because an audit trail that names
    `deleted-17` as the approver of a payout names nobody.
    """
    from servers.admin_audit.models import AdminAuditLog, actor_snapshot
    from servers.admin_audit.services import _client_ip, _user_agent
    from servers.auth_user.session_revocation import revoke_all_refresh_tokens

    # Taken NOW, before anything is wiped: for a self-service request the
    # actor IS the user, and a row that named `deleted-17` as the person who
    # asked would have lost the one fact it exists to keep.
    who = actor_snapshot(actor)
    if actor_label:
        who['actor_label'] = actor_label[:255]

    if not (reason or '').strip():
        raise AnonymisationRefused('A reason is required to anonymise an account.')
    if not (reference or '').strip():
        raise AnonymisationRefused(
            'A request reference (ticket or request id) is required to '
            'anonymise an account.'
        )
    if _is_staff_or_operator(user):
        raise AnonymisationRefused(
            f'User {user.pk} is a staff/operator account and is not '
            f'anonymised: deactivate it (is_active=False) and revoke its '
            f'assignments instead. Its identifiers stay so the audit log '
            f'keeps naming it.'
        )

    before = {
        'is_active': bool(user.is_active),
        'role': user.role,
        'had_email': bool(user.email),
        'had_avatar': bool(user.avatar),
    }
    failures = []
    placeholder = f'{PLACEHOLDER_PREFIX}{user.pk}'

    with transaction.atomic():
        cleared = ['phone_number', 'username', 'full_name', 'password']
        user.phone_number = placeholder
        user.username = placeholder
        user.full_name = ''
        for name in _NULLED_USER_FIELDS:
            if getattr(user, name, None) not in (None, ''):
                cleared.append(name)
            setattr(user, name, None)
        if _clear_file(user, 'avatar', failures):
            cleared.append('avatar')
        user.set_unusable_password()
        user.must_change_password = False
        user.is_active = False
        user.save()

        cleared.extend(_anonymise_driver_profile(user, failures))
        cleared.extend(_anonymise_rider_profile(user))
        revoked = revoke_all_refresh_tokens(user)

        row = AdminAuditLog.objects.create(
            **who,
            action=AUDIT_ACTION,
            target_type='User',
            target_id=str(user.pk),
            before=before,
            after={
                'is_active': False,
                'anonymised': True,
                'placeholder': placeholder,
                'fields_cleared': cleared,
                'storage_failures': failures,
                'refresh_tokens_revoked': revoked,
                'reference': reference.strip(),
            },
            reason=reason.strip()[:10000],
            ip_address=_client_ip(request),
            user_agent=_user_agent(request),
        )

    logger.warning(
        'account_anonymised user_id=%s reference=%s actor_id=%s',
        user.pk, reference.strip(), getattr(actor, 'pk', None),
    )
    return row
