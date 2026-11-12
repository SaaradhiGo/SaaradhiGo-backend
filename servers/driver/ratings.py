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


def low_threshold():
    return (Decimal(str(getattr(settings, 'DRIVER_LOW_RATING_BELOW', '4.5'))),
            int(getattr(settings, 'DRIVER_LOW_RATING_MIN_RATINGS', 10)))


def low_rating_q():
    """For a queryset carrying `rating_annotations()`."""
    below, minimum = low_threshold()
    return Q(rating_count__gte=minimum, rating_average__lt=below)


def summary(driver):
    """{'average', 'count', 'low'} for one driver."""
    from servers.ride.models import Rating

    agg = (Rating.objects.filter(trip_id__driver_id=driver).exclude(rater_id=driver.user_id)
           .aggregate(avg=Avg('score'), count=Count('id')))
    below, minimum = low_threshold()
    average = round(Decimal(str(agg['avg'])), 2) if agg['avg'] is not None else None
    return {'average': str(average) if average is not None else None, 'count': agg['count'],
            'low': bool(average is not None and agg['count'] >= minimum and average < below)}
