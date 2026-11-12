"""A driver's ratings, counted one way everywhere (12 Nov, console QA).

The rating riders gave on the driver's trips (never the driver's own rating of a rider). The
console list, the "Low rating" filter, the console detail and the driver's profile all read this.
"Low" needs enough ratings to mean something: an average below DRIVER_LOW_RATING_BELOW from at
least DRIVER_LOW_RATING_MIN_RATINGS ratings. A driver with none is never low.
"""
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg, Count, DecimalField, IntegerField, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce


def _given():
    from servers.ride.models import Rating

    return Rating.objects.exclude(rater_id=OuterRef('user_id')).filter(trip_id__driver_id=OuterRef('pk'))


def rating_annotations():
    """`rating_count` and `rating_average` for a Driver queryset, in the same query."""
    count = _given().order_by().values('trip_id__driver_id').annotate(n=Count('id')).values('n')
    avg = _given().order_by().values('trip_id__driver_id').annotate(a=Avg('score')).values('a')
    return {'rating_count': Coalesce(Subquery(count, output_field=IntegerField()), 0),
            'rating_average': Subquery(avg, output_field=DecimalField(max_digits=4, decimal_places=2))}


def low_threshold(node=None):
    """(below, minimum) for a driver whose home city is `node` (13 Nov): the city's zone setting
    (Console > Zones), else its parent zone's, else DRIVER_LOW_RATING_BELOW / _MIN_RATINGS."""
    below = Decimal(str(getattr(settings, 'DRIVER_LOW_RATING_BELOW', '4.5')))
    minimum = int(getattr(settings, 'DRIVER_LOW_RATING_MIN_RATINGS', 10))
    if node is None:
        return below, minimum
    from .review_target import _zone_for

    zone, seen, got_below, got_min = _zone_for(node), 0, None, None
    while zone is not None and seen < 8 and (got_below is None or got_min is None):
        if got_below is None and zone.low_rating_below is not None:
            got_below = Decimal(str(zone.low_rating_below))
        if got_min is None and zone.low_rating_min_ratings:
            got_min = int(zone.low_rating_min_ratings)
        zone, seen = zone.parent, seen + 1
    return (got_below if got_below is not None else below), (got_min if got_min is not None else minimum)


def low_rating_q():
    """For a Driver queryset carrying `rating_annotations()`: each driver against their own city's
    line. One group of cities per distinct (below, minimum), so the query stays small."""
    from servers.admin_access.models import GeoNode

    from .models import Driver

    default = low_threshold()
    groups = {}
    node_ids = set(Driver.objects.exclude(home_city__isnull=True).values_list('home_city_id', flat=True).distinct())
    for node in GeoNode.objects.filter(id__in=node_ids).select_related('parent'):
        groups.setdefault(low_threshold(node), []).append(node.id)
    q = Q(pk__in=[])
    default_ids = groups.pop(default, [])
    q |= (Q(home_city__isnull=True) | Q(home_city_id__in=default_ids)) & Q(
        rating_count__gte=default[1], rating_average__lt=default[0])
    for (below, minimum), ids in groups.items():
        q |= Q(home_city_id__in=ids, rating_count__gte=minimum, rating_average__lt=below)
    return q


def summary(driver):
    """{'average', 'count', 'low'} for one driver."""
    from servers.ride.models import Rating

    agg = (Rating.objects.filter(trip_id__driver_id=driver).exclude(rater_id=driver.user_id)
           .aggregate(avg=Avg('score'), count=Count('id')))
    below, minimum = low_threshold(getattr(driver, 'home_city', None))
    average = round(Decimal(str(agg['avg'])), 2) if agg['avg'] is not None else None
    return {'average': str(average) if average is not None else None, 'count': agg['count'],
            'low_below': str(below), 'low_min_ratings': minimum,
            'low': bool(average is not None and agg['count'] >= minimum and average < below)}
