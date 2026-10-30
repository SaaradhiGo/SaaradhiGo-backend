"""Safety signals: detection only.

WHAT IS DETECTED, AND FROM WHAT
-------------------------------
* `long_stop`  the driver's trail has not left a `long_stop_radius_m` circle
  for `long_stop_minutes` during an in-progress trip, away from the trip's
  pickup and drop (`stop_exempt_radius_m`). Data: `ride.TripLocationPoint`,
  the durable sampled trail. The sampler drops points closer than 25 m to the
  last kept one, so a parked driver writes NOTHING: the stop is read as
  "latest kept point is old and the driver is still alive"
  (`Trip.last_driver_activity_at`, written at most once a minute), and a
  driver who is not alive is a lost-contact case, which the stale-ride
  detector owns, not a stop.
* `eta_overrun`  an in-progress trip running `eta_overrun_percent` AND
  `eta_overrun_minutes` past `Trip.estimated_duration_min`, measured from
  `started_at`.
* `route_deviation`  STUBBED. No planned route or polyline is stored per
  trip (only the estimated distance and duration), so there is nothing to
  measure a deviation against. `evaluate_route_deviation` is a no-op that
  says so; it becomes real the day a route is stored.

NOTHING THE RIDER OR DRIVER CAN FEEL
------------------------------------
This module writes `SafetySignal` rows, an incident and a Command Centre
alert, and nothing else. No push to the rider or driver, no trip state, no
fare, no dispatch effect.

COST (the sweep runs every minute)
----------------------------------
Two queries however many trips are in progress:
  1. in-progress trips started in the last `HORIZON_HOURS`, via
     `trip_status_recent_idx` (status, -requested_at), each carrying its
     latest kept point through correlated subqueries that use
     `triploc_trip_time_idx` (trip, recorded_at) -- one index probe per trip
     inside the database, no per-trip round trip;
  2. the recent points of only the trips that look stopped, via the same
     index.
No Redis. Existing signals are read in one more indexed query. Bounded by
`MAX_TRIPS`.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Subquery
from django.utils import timezone

from servers.command_centre.models import SAFETY_SIGNAL, WARNING
from servers.command_centre.services import raise_alert

from . import services
from .config import active_configs
from .models import SIG_ETA_OVERRUN, SIG_LONG_STOP, SafetySignal

logger = logging.getLogger(__name__)

#: In-progress trips older than this are not scanned: a trip that has been
#: "in progress" for two days is the stale-ride detector's problem.
HORIZON_HOURS = 24
MAX_TRIPS = 2000


def haversine_metres(lat1, lon1, lat2, lon2):
    from servers.ride.location_trail import haversine_metres as _h

    return _h(float(lat1), float(lon1), float(lat2), float(lon2))


def alive_seconds():
    """How recently the driver must have been seen for a stop to count."""
    from servers.ride.liveness import write_interval_seconds

    return write_interval_seconds() * 2 + 30


def _candidate_trips(now):
    from servers.ride.models import Trip, TripLocationPoint, TripStatus

    status_id = TripStatus.objects.filter(status_code='in_progress').values_list('id', flat=True).first()
    if status_id is None:
        return []
    last = TripLocationPoint.objects.filter(trip=OuterRef('pk')).order_by('-recorded_at')
    return list(
        Trip.objects.filter(
            status_id=status_id, started_at__isnull=False,
            requested_at__gte=now - timedelta(hours=HORIZON_HOURS))
        .select_related('zone', 'zone__geo_node', 'driver_id')
        .annotate(last_lat=Subquery(last.values('latitude')[:1]),
                  last_lng=Subquery(last.values('longitude')[:1]),
                  last_at=Subquery(last.values('recorded_at')[:1]))
        .order_by('started_at')[:MAX_TRIPS])


def measure_eta_overrun(trip, config, now):
    """`measured` dict if the trip is over both lines, else None."""
    pct_line, min_line = config.eta_overrun_percent, config.eta_overrun_minutes
    estimate = trip.estimated_duration_min
    if not pct_line or not min_line or not estimate or estimate <= 0:
        return None
    elapsed = Decimal(str((now - trip.started_at).total_seconds())) / 60
    over = elapsed - estimate
    over_pct = over / estimate * 100
    if over < min_line or over_pct < pct_line:
        return None
    return {'elapsed_minutes': round(float(elapsed), 1),
            'estimated_minutes': float(estimate),
            'over_minutes': round(float(over), 1), 'over_percent': round(float(over_pct), 1)}


def _near(trip, lat, lng, radius):
    """Within `radius` metres of the trip's pickup or drop."""
    for p_lat, p_lng in ((trip.pickup_lat, trip.pickup_long),
                         (trip.destination_lat, trip.destination_long)):
        if haversine_metres(lat, lng, p_lat, p_lng) <= radius:
            return True
    return False


def measure_long_stop(trip, config, now, recent_points):
    """`measured` dict if the driver has been parked past the line, else None.

    `recent_points` is this trip's kept points from the last 2 x
    `long_stop_minutes`, oldest first.
    """
    minutes, radius = config.long_stop_minutes, config.long_stop_radius_m
    if not minutes or not radius or trip.last_at is None:
        return None
    seen = trip.last_driver_activity_at
    if seen is None or (now - seen).total_seconds() > alive_seconds():
        return None
    if now - trip.started_at < timedelta(minutes=minutes):
        return None

    lat, lng = trip.last_lat, trip.last_lng
    exempt = config.stop_exempt_radius_m or 0
    if exempt and _near(trip, lat, lng, exempt):
        return None

    # Walk back from the newest kept point while it stays inside the circle.
    since = trip.last_at
    for point_at, p_lat, p_lng in reversed(recent_points):
        if haversine_metres(lat, lng, p_lat, p_lng) > radius:
            break
        since = min(since, point_at)
    stopped = (now - since).total_seconds() / 60
    if stopped < minutes:
        return None
    return {'stopped_minutes': round(stopped, 1), 'radius_m': radius,
            'since': since.isoformat()}


def evaluate_route_deviation(trip, config, now):
    """STUB: no planned route is stored per trip, so nothing can deviate."""
    return None


def _node_and_country(trip):
    node = services.trip_node(trip)
    return node, services.country_for(node, trip=trip)


def _incident_line(kind, measured, config):
    """Does this signal reach its country's incident line?"""
    if kind == SIG_LONG_STOP:
        line = config.long_stop_incident_minutes
        return bool(line) and measured['stopped_minutes'] >= line
    if kind == SIG_ETA_OVERRUN:
        pct, mins = config.eta_overrun_incident_percent, config.eta_overrun_incident_minutes
        return bool(pct) and bool(mins) and measured['over_percent'] >= pct \
            and measured['over_minutes'] >= mins
    return False


def _promote(signal, config):
    """Open or join the trip's incident and raise the alert. Once per signal."""
    if signal.incident_id:
        return False
    with transaction.atomic():
        locked = SafetySignal.objects.select_for_update().get(pk=signal.pk)
        if locked.incident_id or locked.dismissed_at:
            return False
        incident, _created = services.open_from_signal(
            locked, category=locked.kind, severity='high')
        locked.incident = incident
        locked.save(update_fields=['incident'])
        raise_alert(
            SAFETY_SIGNAL, severity=WARNING,
            title=f'{locked.get_kind_display()} on trip {locked.trip_id}',
            payload={'signal_id': locked.id, 'incident_id': incident.id, 'kind': locked.kind,
                     'trip_id': locked.trip_id, **locked.measured},
            source_type='safety_signal', source_id=locked.id, city=locked.anchor_node)
    signal.incident = incident
    return True


def evaluate(now=None):
    """One pass over the in-progress trips. Returns a summary dict."""
    from servers.ride.models import TripLocationPoint

    now = now or timezone.now()
    summary = {'trips': 0, 'created': 0, 'promoted': 0, 'unconfigured': 0}
    configs = active_configs()
    if not configs:
        return summary
    trips = _candidate_trips(now)
    summary['trips'] = len(trips)
    if not trips:
        return summary

    per_trip = {}
    for trip in trips:
        _node, country = _node_and_country(trip)
        config = configs.get(country)
        if config is None:
            summary['unconfigured'] += 1
            continue
        per_trip[trip.id] = (trip, config)

    # Query 2: recent points for the trips that look parked (kept point old
    # enough, driver alive), one statement for all of them.
    parked = [tid for tid, (trip, config) in per_trip.items()
              if config.long_stop_minutes and trip.last_at is not None
              and trip.last_driver_activity_at is not None]
    points = {}
    if parked:
        horizon = now - timedelta(minutes=2 * max(
            per_trip[tid][1].long_stop_minutes for tid in parked))
        rows = (TripLocationPoint.objects.filter(trip_id__in=parked, recorded_at__gte=horizon)
                .order_by('trip_id', 'recorded_at')
                .values_list('trip_id', 'recorded_at', 'latitude', 'longitude'))
        for trip_id, at, lat, lng in rows:
            points.setdefault(trip_id, []).append((at, lat, lng))

    existing = {(s.trip_id, s.kind): s
                for s in SafetySignal.objects.filter(trip_id__in=list(per_trip))}

    for trip_id, (trip, config) in per_trip.items():
        found = []
        measured = measure_long_stop(trip, config, now, points.get(trip_id, []))
        if measured:
            found.append((SIG_LONG_STOP, measured))
        measured = measure_eta_overrun(trip, config, now)
        if measured:
            found.append((SIG_ETA_OVERRUN, measured))
        evaluate_route_deviation(trip, config, now)

        for kind, measured in found:
            signal = existing.get((trip_id, kind))
            if signal is None:
                node, country = _node_and_country(trip)
                try:
                    with transaction.atomic():
                        signal = SafetySignal.objects.create(
                            kind=kind, trip=trip, country=country, anchor_node=node,
                            measured=measured, threshold_version=config.version,
                            created_at=now, updated_at=now)
                    summary['created'] += 1
                except IntegrityError:
                    signal = SafetySignal.objects.filter(trip=trip, kind=kind).first()
                    if signal is None:
                        continue
            elif not signal.dismissed_at:
                signal.measured, signal.updated_at = measured, now
                signal.threshold_version = config.version
                signal.save(update_fields=['measured', 'updated_at', 'threshold_version'])
            if not signal.dismissed_at and _incident_line(kind, measured, config):
                if _promote(signal, config):
                    summary['promoted'] += 1

    if summary['created'] or summary['promoted']:
        logger.warning('safety signal sweep: %s', summary)
    return summary
