"""A block also signs the rider out, but never in the middle of a trip.

Blocking revokes the rider's refresh tokens (the primitive force sign-out
uses). If the rider has a trip under way (requested, accepted, reached, in
progress) the sign-out is deferred: the block row is marked
`signout_pending`, and the sign-out runs when the trip reaches a final state
(`on_trip_saved`, connected to `Trip` post_save in `RiderConfig.ready`).

Access tokens already issued stay valid up to 15 minutes either way
(`auth_user.session_revocation`). SOS needs no session state of its own and
is never gated on a block: see `servers/sos/views.py::raise_sos`.

Idempotent: the pending flag is claimed with a single conditional UPDATE, so
two trips ending together sign the rider out once; unblocking before the trip
ends removes the row from the claim (it is lifted), so nothing runs.
"""

import logging

from django.db import transaction

logger = logging.getLogger(__name__)

ACTIVE_TRIP_STATUSES = ('requested', 'accepted', 'reached', 'in_progress')
FINAL_TRIP_STATUSES = ('completed', 'cancelled')


def has_active_trip(user_id):
    from servers.ride.models import Trip

    return Trip.objects.filter(user_id_id=user_id,
                               status_id__status_code__in=ACTIVE_TRIP_STATUSES).exists()


def _sign_out(user, block_id, *, deferred, http_request=None):
    from servers.admin_audit.services import record_admin_action
    from servers.auth_user.session_revocation import revoke_all_refresh_tokens

    revoked = revoke_all_refresh_tokens(user)
    if revoked:
        record_admin_action(
            http_request, action='rider_signed_out', target_type='rider', target_id=user.pk,
            after={'refresh_tokens_revoked': revoked, 'block_id': block_id,
                   'deferred_to_trip_end': deferred},
            reason='block: trip ended' if deferred else 'block')
    return revoked


def sign_out_or_defer(rider, block, *, http_request=None):
    """Called inside `operator_block`, after the block row exists."""
    if has_active_trip(rider.user_id_id):
        type(block).objects.filter(pk=block.pk).update(signout_pending=True)
        block.signout_pending = True
        return 'deferred'
    _sign_out(rider.user_id, block.pk, deferred=False, http_request=http_request)
    return 'signed_out'


def on_trip_saved(sender, instance, **kwargs):
    from .models import RiderBlock

    status = getattr(instance, 'status_id', None)
    if status is None or status.status_code not in FINAL_TRIP_STATUSES:
        return
    user_id = instance.user_id_id
    pending = RiderBlock.objects.filter(rider__user_id_id=user_id, lifted_at__isnull=True,
                                        signout_pending=True)
    if not pending.exists() or has_active_trip(user_id):
        return
    with transaction.atomic():
        claimed = list(pending.select_for_update().values_list('pk', flat=True))
        if not claimed or not RiderBlock.objects.filter(
                pk__in=claimed, signout_pending=True).update(signout_pending=False):
            return
        try:
            _sign_out(instance.user_id, claimed[0], deferred=True)
        except Exception:  # noqa: BLE001 - never break the trip's own save
            logger.exception('rider block sign-out failed for user %s', user_id)
