"""Human-readable record IDs: one spelling everywhere (owner, 2026-11-03).

The console, alerts, notifications, search and the audit log all name a record
the same way, so an operator who reads "SOS-1042" on a push notification can
type it into search and land on the same case:

    T-881230   a trip                 D-2231   a driver (Driver.id)
    R-55120    a rider (User.id)      SOS-1042 a safety incident raised by an SOS
    INC-1043   any other safety incident (a ticket, a signal, by hand)
    PO-77      a withdrawal / payout  TKT-310  a support ticket
    APR-5      a maker-checker request

SOS and INC share the incident's number, so the two can never collide and an
incident keeps its number if it is later linked to an SOS. A second SOS press on
the same trip joins the open incident, so it never gets a number of its own;
the SOS event's own primary key is internal and never shown.
"""

import re

TRIP, DRIVER, RIDER, SOS, INCIDENT, PAYOUT, TICKET, APPROVAL = (
    'T', 'D', 'R', 'SOS', 'INC', 'PO', 'TKT', 'APR')

#: Prefix -> what it names, for search and for the console's links.
KINDS = {
    TRIP: 'trip', DRIVER: 'driver', RIDER: 'rider', SOS: 'incident',
    INCIDENT: 'incident', PAYOUT: 'payout', TICKET: 'ticket', APPROVAL: 'approval',
}

_PATTERN = re.compile(r'^\s*(SOS|INC|TKT|APR|PO|T|D|R)\s*[-#]?\s*(\d{1,12})\s*$', re.IGNORECASE)


def ref(prefix, pk):
    return f'{prefix}-{pk}' if pk is not None else None


def incident_ref(incident):
    """SOS-n when an SOS raised or joined the incident, else INC-n."""
    if incident is None:
        return None
    has_sos = getattr(incident, '_has_sos', None)
    if has_sos is None:
        has_sos = incident.source == 'sos' or incident.links.filter(kind='sos').exists()
    return ref(SOS if has_sos else INCIDENT, incident.pk)


def parse(text):
    """("trip", 881230) for "T-881230" (any case, '#', or a space), else None."""
    m = _PATTERN.match(text or '')
    if not m:
        return None
    return KINDS[m.group(1).upper()], int(m.group(2))
