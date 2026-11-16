# Runs in `manage.py shell` on QA via qrun.sh, PHASE = 'dry' or 'apply'. Removes every account the
# 16 Nov load test made (full name "LT1116 ...", phones +917000100xxx/+917000200xxx) and their trips.
# 'dry' rolls back and prints what would go; nothing else on QA is touched.
from collections import Counter

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models.deletion import Collector, ProtectedError

from servers.ride.models import Trip

U = get_user_model()
users = U.objects.filter(full_name__startswith='LT1116').filter(phone_number__regex=r'^\+9170001|^\+9170002')
trips = Trip.objects.filter(user_id__in=users) | Trip.objects.filter(driver_id__user_id__in=users)
foreign = trips.exclude(user_id__in=users)          # a real rider's trip a bot driver held: must be none
print('CL users', users.count(), 'trips', trips.count(), 'foreign_rider_trips', foreign.count())


def plan(qs):
    col = Collector(using='default')
    col.collect(list(qs))
    out = Counter()
    for model, objs in col.data.items():
        out[model._meta.label] += len(objs)
    for qs_ in col.fast_deletes:
        n = qs_.count()
        if n:
            out[qs_.model._meta.label] += n
    return out


try:
    with transaction.atomic():
        if foreign.exists():
            raise SystemExit('refusing: a non-test rider has a trip with a test driver')
        print('CL trips plan', dict(plan(trips.distinct())))
        n_trips = Trip.objects.filter(pk__in=list(trips.values_list('pk', flat=True))).delete()
        print('CL trips deleted', n_trips[0])
        print('CL users plan', dict(plan(users)))
        n_users = users.delete()
        print('CL users deleted', n_users[0])
        if PHASE != 'apply':  # noqa: F821
            raise RuntimeError('dry run: rolled back')
except ProtectedError as e:
    print('CL protected', sorted({o._meta.label for o in e.protected_objects})[:20])
except RuntimeError as e:
    print('CL', e)
print('CL remaining users', U.objects.filter(full_name__startswith='LT1116').count())
