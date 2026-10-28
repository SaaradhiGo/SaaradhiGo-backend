"""Where an SOS is, for the people who answer it.

THE ORDER (owner's ruling, 2026-10-28)
--------------------------------------
  1. The event has a trip            -> the trip's zone's node       `trip`
  2. The request carried a GPS fix   -> city via ServiceZone polygons `gps`
  3. The raiser is a driver with a home city                         `home_city`
  4. Nothing                          -> unanchored: every safety responder
                                         plus Global, and a Command Centre
                                         alert                       `unanchored`

Riders have no home city by design (`admin_access.scoping.RIDERS_ARE_GLOBAL`;
`rider.Rider` carries no city field), so step 3 is for drivers only. A
rider's SOS outside any trip is therefore anchored by GPS or not at all --
which is why the apps send `lat`/`lng` with every SOS and why step 4 exists.

GPS -> CITY
-----------
`pricing.services.zones_containing_point` walks the ACTIVE zones that
contain the point, most specific first, and the first one that is LINKED
to a `GeoNode` wins. Normally that node is a city; it may be a state or a
country when only a wider zone is drawn there, which is still a place with
a safety team. A zone drawn before its node existed is skipped rather than
anchoring the event to nothing.
"""

import logging

from .models import ANCHOR_GPS, ANCHOR_HOME_CITY, ANCHOR_TRIP, ANCHOR_UNANCHORED

logger = logging.getLogger(__name__)


def _trip_node(trip):
    zone = getattr(trip, 'zone', None) if trip is not None else None
    return getattr(zone, 'geo_node', None) if zone is not None else None


def _gps_node(lat, lng):
    if lat is None or lng is None:
        return None
    from servers.pricing.services import zones_containing_point

    for zone in zones_containing_point(lat, lng):
        if zone.geo_node_id:
            return zone.geo_node
    return None


def _home_city(user):
    if user is None:
        return None
    # `Driver.user_id` is a OneToOne with related_name 'driver'; a rider
    # raises RelatedObjectDoesNotExist, an AttributeError subclass.
    profile = getattr(user, 'driver', None)
    return getattr(profile, 'home_city', None) if profile is not None else None


def resolve_anchor(event):
    """`(GeoNode or None, anchor_source)` for an event about to be saved."""
    node = _trip_node(event.trip)
    if node is not None:
        return node, ANCHOR_TRIP

    node = _gps_node(event.latitude, event.longitude)
    if node is not None:
        return node, ANCHOR_GPS

    node = _home_city(event.user)
    if node is not None:
        return node, ANCHOR_HOME_CITY

    return None, ANCHOR_UNANCHORED


def alert_unanchored(event):
    """Raise the Command Centre alert for an event nobody could place.

    Same transaction as the event's own save (called from `SOSEvent.save`),
    so the alert exists whenever the event does. The payload names the
    event and nothing about the person: the alert list is read by every
    `report.read` holder, and the masked SOS endpoints exist for the rest.
    """
    from servers.command_centre.models import CRITICAL, SOS_UNANCHORED
    from servers.command_centre.services import raise_alert

    alert = raise_alert(
        SOS_UNANCHORED,
        severity=CRITICAL,
        title=f'SOS #{event.id} could not be routed to a city',
        payload={
            'sos_id': event.id,
            'initiated_by': event.initiated_by,
            'event_type': event.event_type,
            'has_location': event.latitude is not None and event.longitude is not None,
        },
        source_type='sos_event',
        source_id=event.id,
    )
    logger.warning('sos_unanchored', extra={
        'event': 'sos_unanchored', 'sos_id': event.id, 'alert_id': alert.id,
        'initiated_by': event.initiated_by,
    })
    return alert
