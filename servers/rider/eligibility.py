"""May this account request a ride? One rule, read by the ride-request socket.

A rider with an active operator block (`RiderBlock`, Riders module) may not
create a trip or restart a search. Everything else the account does is
unaffected; see `RiderBlock`.
"""

BLOCKED_MESSAGE = ('Your account cannot request rides right now. '
                   'Please contact support.')


def may_request_rides(user):
    from .models import RiderBlock

    if user is None or not getattr(user, 'pk', None):
        return False
    return not RiderBlock.objects.filter(
        rider__user_id_id=user.pk, lifted_at__isnull=True).exists()
