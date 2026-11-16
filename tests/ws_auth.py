"""Sockets sign in with a single-use ticket, never `?token=` (16 Nov ruling)."""
from rest_framework_simplejwt.tokens import AccessToken


def ws_q(token):
    """The query that opens a socket as the holder of `token`: `ticket=<single-use ticket>`."""
    from servers.auth_user.ws_ticket import RIDE_TICKET_PREFIX
    from servers.live_ops.tickets import issue_ticket

    return f'ticket={issue_ticket(AccessToken(str(token))["user_id"], prefix=RIDE_TICKET_PREFIX)}'
