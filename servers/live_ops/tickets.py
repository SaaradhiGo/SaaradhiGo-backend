"""The single-use ticket that opens the admin live-locations socket.

WHY NOT THE JWT IN THE URL
--------------------------
`ws://host/ws/admin/live-locations/?token=<jwt>` put a bearer credential,
good for its whole lifetime and usable on every admin API, into the query
string, where proxies, access logs, browser history and Referer headers keep
it. For a socket that shows where every driver is, that is the wrong place for
the one secret that unlocks the console.

The replacement is a ticket good for ONE connection within 30 seconds:

  * `POST /live-ops/admin/ws-ticket/` (a normal Authorization header, so it is
    scoped, capability-gated and audited like any admin call) returns a random
    ticket;
  * Redis stores only its SHA-256, bound to the user id, with a 30 s TTL;
  * the socket consumes it with GETDEL: whoever connects first gets the
    session, a replay finds nothing;
  * a ticket found in a log is worthless a minute later, and unusable once
    the real client has connected.
"""

import hashlib
import secrets

from .config import TICKET_KEY_PREFIX, TICKET_TTL_SECONDS


class TicketStoreUnavailable(Exception):
    """Redis could not store or read a ticket. Refuse; never fall back."""


def _key(ticket):
    return TICKET_KEY_PREFIX + hashlib.sha256(ticket.encode('utf-8')).hexdigest()


def _client():
    from servers.redis_client import redis_client

    if redis_client is None:
        raise TicketStoreUnavailable('no Redis')
    return redis_client


def issue_ticket(user_id):
    """A fresh ticket for `user_id`. Only its hash is stored."""
    ticket = secrets.token_urlsafe(32)
    try:
        stored = _client().set(_key(ticket), str(int(user_id)), ex=TICKET_TTL_SECONDS, nx=True)
    except TicketStoreUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TicketStoreUnavailable(str(exc)) from exc
    if not stored:  # a 256-bit collision: refuse rather than reuse
        raise TicketStoreUnavailable('ticket collision')
    return ticket


def consume_ticket(ticket):
    """The user id the ticket was issued to, or None. GETDEL: it works once.

    Raises `TicketStoreUnavailable` when Redis cannot answer, which is
    different from "no such ticket" and gets a different close code.
    """
    if not ticket or len(ticket) > 200:
        return None
    try:
        value = _client().getdel(_key(ticket))
    except TicketStoreUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TicketStoreUnavailable(str(exc)) from exc
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None
