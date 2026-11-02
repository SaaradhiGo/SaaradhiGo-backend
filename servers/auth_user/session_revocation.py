"""Revoke every session an account holds, after its credential changes.

WHY THIS EXISTS
---------------
`change_password` and `reset_password` set a new password and saved it. That
invalidated outstanding *reset* tokens -- the generator hashes the password
hash -- and the code says so, which made it look as though revocation were
handled.

It was not. JWTs are what the apps and the operator console actually
authenticate with, and they are unaffected by a password change:

  * a refresh token lives **14 days** and keeps minting access tokens;
  * `ROTATE_REFRESH_TOKENS` means each use issues a fresh one, so a thief who
    refreshes once a fortnight keeps the session alive indefinitely.

So the one action a person takes when they believe their password is
compromised -- change it -- did not end the attacker's session. For an
operator account, which approves KYC and releases payouts, that is the
difference between a contained incident and an uncontained one.

WHAT IT DOES, AND THE ONE THING IT CANNOT DO
--------------------------------------------
Every outstanding refresh token for the account is blacklisted, using the
`token_blacklist` app that is already installed and already used by logout.
After this, no refresh token the account has ever issued can be exchanged
again.

**Access tokens already minted stay valid until they expire.** They are
stateless and nothing is consulted when they are presented, so the only way
to kill one mid-flight would be a per-request revocation check -- a database
read on every authenticated request, for a 15-minute window. The exposure is
bounded by `ACCESS_TOKEN_LIFETIME` (15 minutes) and that trade is deliberate;
it is recorded here rather than left for someone to discover.
"""

import logging

logger = logging.getLogger(__name__)


def revoke_all_refresh_tokens(user) -> int:
    """Blacklist every outstanding refresh token for `user`.

    Returns the number newly blacklisted. Never raises: a credential change
    must not fail because the blacklist table is unavailable -- that would
    leave the caller with a changed password and an error, unsure which
    happened. A failure is logged at error level instead.
    """
    if user is None or not getattr(user, 'pk', None):
        return 0

    try:
        from rest_framework_simplejwt.token_blacklist.models import (
            BlacklistedToken, OutstandingToken,
        )
    except ImportError:                                  # pragma: no cover
        logger.error('session_revocation_unavailable: token_blacklist not installed')
        return 0

    revoked = 0
    try:
        for token in OutstandingToken.objects.filter(user=user).iterator():
            _, created = BlacklistedToken.objects.get_or_create(token=token)
            if created:
                revoked += 1
    except Exception as exc:                             # noqa: BLE001
        logger.error(
            'session_revocation_failed user_id=%s error=%s',
            getattr(user, 'pk', None), type(exc).__name__,
        )
        return revoked

    logger.info('sessions_revoked user_id=%s count=%s', user.pk, revoked)
    return revoked


def revoke_after_credential_change(user, request=None, *, reason=''):
    """Revoke sessions and, for an operator, leave an audit row.

    The audit log is for consequential operator actions, so a rider changing
    their own password does not write one -- that would bury KYC approvals and
    payout releases under routine traffic. An operator's credential change is
    exactly the kind of event an investigation needs, so it does.
    """
    # The password has ALREADY been saved by the time this is called, so
    # nothing in here may raise: a 500 now would leave the caller with a
    # changed password and an error, unable to tell which took effect.
    try:
        revoked = revoke_all_refresh_tokens(user)
    except Exception as exc:                             # noqa: BLE001
        logger.error(
            'session_revocation_raised user_id=%s error=%s',
            getattr(user, 'pk', None), type(exc).__name__,
        )
        revoked = 0

    try:
        from base.permissions import is_operator
        if is_operator(user):
            from servers.admin_audit.services import record_admin_action
            record_admin_action(
                request,
                action='operator_credential_changed',
                target_type='user',
                target_id=user.pk,
                after={'refresh_tokens_revoked': revoked},
                reason=reason or 'Password changed; all sessions revoked.',
            )
    except Exception as exc:                             # noqa: BLE001
        # Auditing must not break the credential change it is recording.
        logger.error(
            'session_revocation_audit_failed user_id=%s error=%s',
            getattr(user, 'pk', None), type(exc).__name__,
        )

    return revoked


# ---------------------------------------------------------------------------
# Deactivation ends every session (AUDIT 27.78, Low 3)
# ---------------------------------------------------------------------------
#
# Deactivating an account set `is_active=False` and left its refresh tokens
# alive. JWTAuthentication refuses an inactive user's ACCESS token, but
# `/auth/refresh/` did not look, and a refresh token is good for 14 days.
# Connected to the model's save, so it holds for every path that deactivates
# -- the driver deactivation workflow, erasure, an operator, a shell -- and not
# only for callers who remember. (A queryset `.update(is_active=False)` skips
# signals; nothing in the codebase deactivates users that way, and the refresh
# and login doors refuse an inactive account regardless.)

_WAS_ACTIVE = '_session_revocation_was_active'
REVOKED_ON_DEACTIVATION = '_sessions_revoked_on_deactivation'


def _remember_previous_state(sender, instance, update_fields=None, **kwargs):
    if not instance.pk or instance.is_active:
        return
    if update_fields is not None and 'is_active' not in update_fields:
        return
    previous = sender.objects.filter(pk=instance.pk).values_list('is_active', flat=True).first()
    setattr(instance, _WAS_ACTIVE, bool(previous))


def _revoke_on_deactivation(sender, instance, created=False, **kwargs):
    if created or not getattr(instance, _WAS_ACTIVE, False):
        return
    setattr(instance, _WAS_ACTIVE, False)
    try:
        revoked = revoke_all_refresh_tokens(instance)
    except Exception as exc:                             # noqa: BLE001
        logger.error('deactivation_revocation_failed user_id=%s error=%s',
                     instance.pk, type(exc).__name__)
        return
    # Kept on the instance for a caller that reports the count (erasure's
    # audit row): by the time it revokes, this has already done it.
    setattr(instance, REVOKED_ON_DEACTIVATION, revoked)
    logger.info('deactivated_sessions_revoked user_id=%s count=%s', instance.pk, revoked)


def connect():
    from django.contrib.auth import get_user_model
    from django.db.models.signals import post_save, pre_save

    model = get_user_model()
    pre_save.connect(_remember_previous_state, sender=model,
                     dispatch_uid='session_revocation.pre_save')
    post_save.connect(_revoke_on_deactivation, sender=model,
                      dispatch_uid='session_revocation.post_save')
