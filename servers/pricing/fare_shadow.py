"""Measure what a metered fare would have charged, without charging it.

The rider is quoted up front and `final_fare` is never written, so the platform
has no evidence about how a metered fare would compare. That evidence has to
come from real traffic before anything changes what riders pay -- the formula
can be reasoned about, but the *distribution* of differences cannot.

Two rules shape everything here.

**The canonical fare function is the only fare function.** Every amount below
comes from `pricing.services.quote_fare`. Re-implementing the formula for a
shadow would mean the shadow eventually measures a fare nobody charges, which is
worse than having no shadow at all.

**Quote both legs at the same instant.** `quote_fare` reads the live surge
multiplier, the currently effective rate card and the current wall clock for
night surcharge. Comparing a fresh actual-distance quote against the historical
`quoted_fare` would therefore blend the metric difference together with hours of
pricing drift. So the estimate is *re-quoted* alongside the actual, and the two
differences are reported separately:

    shadow_actual - shadow_estimate   -> distance and duration only
    shadow_estimate - quoted_fare     -> the pricing context having moved

**No side effects, monetary or otherwise.** `record_demand=False` on every call:
a shadow quote that registered demand would raise the surge multiplier for real
riders, which is a shadow changing prices. Nothing here writes to Trip,
FarePricing, payments, wallets or commission -- only to `TripFareShadow`.

A caveat worth knowing before reading the numbers
-------------------------------------------------
`Trip.estimated_distance_km` holds the **client-supplied** distance, while the
fare was computed from the server-validated distance (see `_create_trip`, which
passes `validated_km` to the quote but stores `dist`). So `shadow_estimate` is
not guaranteed to reproduce `quoted_fare` even with no drift at all, and
`context_delta` absorbs that discrepancy. This is recorded rather than
worked around: see the fare-finalisation report.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def shadow_enabled():
    return bool(getattr(settings, 'FARE_SHADOW_ENABLED', False))


def _vehicle_type_for(trip):
    """The vehicle type to price against, preferring what was requested.

    `requested_vehicle_type` is what the rider chose and what the original quote
    used. The assigned vehicle's type is a fallback for older trips; without
    either there is nothing to price, because `quote_fare` requires one.
    """
    vt = getattr(trip, 'requested_vehicle_type', None)
    if vt is not None and getattr(vt, 'type', None):
        return vt.type
    vehicle = getattr(trip, 'vehicle_id', None)
    vt = getattr(vehicle, 'vehicle_type_id', None) if vehicle is not None else None
    return getattr(vt, 'type', None) or None


def _quote(distance_km, duration_min, trip, vehicle_type):
    """One canonical quote. Returns None when pricing refuses.

    `record_demand=False` is the load-bearing argument: a shadow must never
    contribute to the surge signal.
    """
    from servers.pricing.services import PricingUnavailable, quote_fare

    try:
        return quote_fare(
            distance_km=distance_km,
            duration_min=duration_min,
            vehicle_type=vehicle_type,
            pickup_lat=trip.pickup_lat,
            pickup_lon=trip.pickup_long,
            rider_id=None,
            record_demand=False,
        )
    except PricingUnavailable:
        # A zone with no rate card. Real in production for a newly opened city,
        # and not an error here -- it just means this trip cannot be shadowed.
        return None
    except (ValueError, TypeError, ArithmeticError) as exc:
        logger.warning('fare shadow quote failed for trip %s: %s', trip.id, exc)
        return None


def compute_shadow(trip):
    """Compare the quoted fare with metered fares. Reads only; writes nothing.

    Returns a dict whose `status` says why any amount is missing, so an analysis
    can see its own denominator instead of silently dropping trips.
    """
    out = {
        'status': 'computed',
        'quoted_fare': trip.estimated_fare,
        'shadow_estimate': None,
        'shadow_actual': None,
        'estimated_distance_km': trip.estimated_distance_km,
        'actual_distance_km': trip.actual_distance_km,
        'estimated_duration_min': trip.estimated_duration_min,
        'actual_duration_min': trip.actual_duration_min,
        'zone_code': '',
        'vehicle_type': '',
        'rate_card_version': None,
        'surge_multiplier': None,
        'quote_source': '',
    }

    vehicle_type = _vehicle_type_for(trip)
    if not vehicle_type:
        out['status'] = 'no_vehicle_type'
        return out
    out['vehicle_type'] = vehicle_type

    if trip.actual_distance_km is None or trip.actual_duration_min is None:
        # No measured journey yet. Recording the row anyway, with the reason,
        # keeps "how many trips could we even shadow?" answerable.
        out['status'] = 'no_actuals'
        return out

    actual = _quote(trip.actual_distance_km, trip.actual_duration_min, trip, vehicle_type)
    if actual is None:
        out['status'] = 'pricing_unavailable'
        return out

    # The estimate leg is re-quoted in the same instant and the same pricing
    # context, which is the whole point: it makes the difference attributable.
    est_km = trip.estimated_distance_km
    est_min = trip.estimated_duration_min
    estimate = None
    if est_km is not None and est_min is not None:
        estimate = _quote(est_km, est_min, trip, vehicle_type)

    out['shadow_actual'] = actual['total_fare']
    out['shadow_estimate'] = estimate['total_fare'] if estimate else None
    out['zone_code'] = actual.get('zone_code') or ''
    out['rate_card_version'] = actual.get('rate_card_version')
    out['surge_multiplier'] = actual.get('surge_multiplier')
    out['quote_source'] = actual.get('source') or ''
    return out


def record_shadow(trip, force=False):
    """Persist one observation. Idempotent; never touches money.

    `get_or_create` plus the one-to-one column means a redelivered task cannot
    double-count a trip in the analysis.
    """
    from servers.pricing.models import TripFareShadow
    from servers.ride.actual_metrics import compute_metrics

    existing = TripFareShadow.objects.filter(trip=trip).first()
    if existing is not None and not force:
        return {'written': False, 'skipped': 'already_observed',
                'status': existing.status, 'shadow_id': existing.id}

    data = compute_shadow(trip)

    # Coverage comes from the actuals module rather than being recomputed, so
    # there is one definition of trail quality in the codebase.
    metrics = compute_metrics(trip)
    data['coverage_ratio'] = metrics.get('coverage_ratio')
    data['trail_points'] = metrics.get('points') or 0

    if existing is not None:
        for key, value in data.items():
            setattr(existing, key, value)
        existing.observed_at = timezone.now()
        existing.save()
        row = existing
    else:
        row = TripFareShadow.objects.create(trip=trip, **data)

    logger.info(
        'fare_shadow_observed',
        extra={
            'event': 'fare_shadow_observed',
            'trip_id': trip.id,
            'status': row.status,
            'quoted_fare': str(row.quoted_fare),
            'shadow_estimate': str(row.shadow_estimate),
            'shadow_actual': str(row.shadow_actual),
            'metric_delta': str(row.metric_delta),
            'context_delta': str(row.context_delta),
            'coverage_ratio': str(row.coverage_ratio),
            'zone_code': row.zone_code,
        },
    )
    return {'written': True, 'status': row.status, 'shadow_id': row.id,
            'metric_delta': row.metric_delta, 'context_delta': row.context_delta}


def trips_awaiting_shadow(limit=200, since_hours=72):
    """Completed trips with measured actuals and no observation yet.

    A sweep rather than a hook on trip completion, for two reasons: the actuals
    themselves land on a delay, so completion is too early to observe; and a
    sweep also picks up trips whose trail or actuals arrived late, which a
    one-shot hook would lose forever.
    """
    from datetime import timedelta

    from servers.ride.models import Trip

    cutoff = timezone.now() - timedelta(hours=since_hours)
    return list(
        Trip.objects
        .filter(status_id__status_code='completed',
                completed_at__gte=cutoff,
                actual_distance_km__isnull=False,
                fare_shadow__isnull=True)
        .select_related('requested_vehicle_type')
        .order_by('completed_at')[:limit]
    )


def sweep_shadow(limit=None, since_hours=None):
    """Observe a bounded batch of trips. Safe to run on a schedule.

    Bounded so one tick cannot become an unbounded unit of work, and a no-op
    while `FARE_SHADOW_ENABLED` is False, which is the default.
    """
    if not shadow_enabled():
        return {'enabled': False}

    limit = limit or int(getattr(settings, 'FARE_SHADOW_BATCH_SIZE', 200))
    since_hours = since_hours or int(getattr(settings, 'FARE_SHADOW_LOOKBACK_HOURS', 72))

    totals = {'enabled': True, 'considered': 0, 'observed': 0, 'failed': 0}
    by_status = {}

    for trip in trips_awaiting_shadow(limit=limit, since_hours=since_hours):
        totals['considered'] += 1
        try:
            result = record_shadow(trip)
        except Exception as exc:  # noqa: BLE001
            # One unshadowable trip must not stop the sweep; the analysis
            # tolerates a gap far better than a stalled observer.
            totals['failed'] += 1
            logger.exception('fare shadow failed for trip %s: %s', trip.id, exc)
            continue
        if result.get('written'):
            totals['observed'] += 1
        status = result.get('status') or 'unknown'
        by_status[status] = by_status.get(status, 0) + 1

    totals['by_status'] = by_status
    logger.info('fare_shadow_sweep', extra={'event': 'fare_shadow_sweep', **{
        k: v for k, v in totals.items() if k != 'by_status'
    }})
    return totals


def summarise(rows):
    """Aggregate observations into the few numbers a fare decision needs.

    Deliberately reports the share of trips where metering would charge *less*
    as well as more. A shadow that only reports upside is a sales pitch, not
    evidence.
    """
    considered = [r for r in rows if r.metric_delta is not None]
    out = {
        'rows': len(rows),
        'comparable': len(considered),
        'metering_higher': 0,
        'metering_lower': 0,
        'unchanged': 0,
        'total_metric_delta': Decimal('0.00'),
        'worst_increase': None,
        'worst_decrease': None,
    }
    for row in considered:
        delta = row.metric_delta
        out['total_metric_delta'] += delta
        if delta > 0:
            out['metering_higher'] += 1
            if out['worst_increase'] is None or delta > out['worst_increase']:
                out['worst_increase'] = delta
        elif delta < 0:
            out['metering_lower'] += 1
            if out['worst_decrease'] is None or delta < out['worst_decrease']:
                out['worst_decrease'] = delta
        else:
            out['unchanged'] += 1

    if considered:
        out['mean_metric_delta'] = (
            out['total_metric_delta'] / Decimal(len(considered))
        ).quantize(Decimal('0.01'))
    else:
        out['mean_metric_delta'] = None
    return out
