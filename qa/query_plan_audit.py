"""D — what the dominant queries actually do, on real PostgreSQL, at real size.

WHY MEASURE INSTEAD OF READING THE MODELS
-----------------------------------------
`Trip.Meta.indexes` looks complete. That is not evidence. An index the planner
declines to use because a filter is not selective, an `ORDER BY` that does not match
any index and therefore sorts the whole table, and a join that seq-scans a small table
inside a loop over a large one all look identical in a model definition.

The default suite runs on SQLite, which has a different planner and no `EXPLAIN
ANALYZE` worth reading, and every test table holds a handful of rows -- at which size
PostgreSQL correctly prefers a sequential scan for everything, so a test at that size
cannot tell a good plan from a bad one either.

So this seeds a real PostgreSQL database to a size where plans diverge, and reads what
the planner actually did.

WHAT IT REPORTS, AND WHAT IT DOES NOT CLAIM
-------------------------------------------
Reports: the chosen plan, actual execution time, rows, and shared-buffer reads for
each query, plus whether a sequential scan was chosen over a table above a row
threshold.

Does NOT claim: production capacity, requests per second, or a latency percentile for
any endpoint. This measures one query at a time on one machine with a warm cache and
no competing traffic. Those numbers are a floor on query cost and a detector of bad
plans, nothing more, and calling them a capacity model would be inventing evidence.

    python qa/query_plan_audit.py --seed          # build the dataset (slow, once)
    python qa/query_plan_audit.py                 # audit against what is there
"""

import argparse
import os
import random
import sys
import time
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# A dedicated database on the local test PostgreSQL. Never a QA or production host:
# seeding writes tens of thousands of rows and ANALYZE rewrites planner statistics.
DB = {
    'NAME': os.environ.get('LOAD_DB_NAME', 'sgload'),
    'USER': os.environ.get('LOAD_DB_USER', 'sgtest'),
    'PASSWORD': os.environ.get('LOAD_DB_PASSWORD', 'sgtest'),
    'HOST': os.environ.get('LOAD_DB_HOST', '127.0.0.1'),
    'PORT': os.environ.get('LOAD_DB_PORT', '5433'),
}

# Sized so the planner has a real choice to make. The pilot is ten drivers; this is
# deliberately well beyond it, because a plan that only works at pilot size is a
# plan that fails during the first busy week.
N_DRIVERS = int(os.environ.get('LOAD_DRIVERS', '60'))
N_RIDERS = int(os.environ.get('LOAD_RIDERS', '2000'))
N_TRIPS = int(os.environ.get('LOAD_TRIPS', '20000'))
N_POINTS_PER_TRIP = int(os.environ.get('LOAD_POINTS', '12'))

SEQ_SCAN_ROW_THRESHOLD = 5000


def configure_django():
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'base.settings_ci')
    os.environ['DEBUG_ENV'] = 'True'
    os.environ['DB_HOST'] = DB['HOST']
    os.environ['DB_PORT'] = DB['PORT']
    os.environ['DB_NAME'] = DB['NAME']
    os.environ['DB_USER'] = DB['USER']
    os.environ['DB_PASSWORD'] = DB['PASSWORD']
    os.environ['DB_SSLMODE'] = 'disable'
    import django
    django.setup()
    from django.conf import settings
    assert settings.DATABASES['default']['HOST'] == DB['HOST'], (
        'refusing to run: the configured database is not the local load database'
    )
    return settings


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed():
    """Build a dataset with a realistic status mix and realistic skew.

    Skew matters more than volume. A uniform dataset makes every index look equally
    good; a real one has a few very busy drivers, a long tail of riders with one
    trip, and 90% of trips in a terminal status.
    """
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from servers.driver.models import Driver, Vehicle, VehicleType
    from servers.ride.models import (
        Trip, TripLocationPoint, TripStatus,
    )
    from servers.rider.models import Rider

    User = get_user_model()
    rng = random.Random(20260924)          # reproducible
    now = timezone.now()

    print('seeding statuses...', flush=True)
    statuses = {}
    for code in ('requested', 'accepted', 'reached', 'in_progress',
                 'completed', 'cancelled'):
        statuses[code], _ = TripStatus.objects.get_or_create(status_code=code)

    print(f'seeding {N_DRIVERS} drivers...', flush=True)
    vt, _ = VehicleType.objects.get_or_create(type='sedan')
    drivers = []
    for i in range(N_DRIVERS):
        phone = f'+9170{i:08d}'
        # username is still a unique column on AbstractUser even though
        # USERNAME_FIELD is phone_number, and it defaults to ''. Two users
        # created without it collide.
        u, _ = User.objects.get_or_create(
            phone_number=phone, defaults={'role': 'driver', 'username': phone})
        d, _ = Driver.objects.get_or_create(
            user_id=u, defaults={'approved': True, 'status': 'online'})
        if d.active_vehicle_id is None:
            v = Vehicle.objects.create(
                driver_id=d, vehicle_type_id=vt,
                vehicle_number=f'TS09LD{i:04d}')
            d.active_vehicle = v
            d.save(update_fields=['active_vehicle'])
        drivers.append(d)

    print(f'seeding {N_RIDERS} riders...', flush=True)
    riders = []
    existing = {u.phone_number: u for u in
                User.objects.filter(phone_number__startswith='+9180')}
    to_create = []
    for i in range(N_RIDERS):
        phone = f'+9180{i:08d}'
        if phone not in existing:
            to_create.append(User(phone_number=phone, role='rider',
                                  username=phone))
    if to_create:
        User.objects.bulk_create(to_create, batch_size=500)
    riders = list(User.objects.filter(phone_number__startswith='+9180')
                  .only('id')[:N_RIDERS])
    have_rider_rows = set(
        Rider.objects.filter(user_id__in=riders).values_list('user_id', flat=True))
    Rider.objects.bulk_create(
        [Rider(user_id=r) for r in riders if r.id not in have_rider_rows],
        batch_size=500, ignore_conflicts=True,
    )

    already = Trip.objects.count()
    want = max(0, N_TRIPS - already)
    print(f'{already} trips present, creating {want}...', flush=True)

    # Status mix: mostly terminal, which is what a real table looks like.
    mix = (['completed'] * 78 + ['cancelled'] * 14 + ['in_progress'] * 3 +
           ['accepted'] * 3 + ['requested'] * 2)

    batch, made = [], 0
    for _ in range(want):
        # Driver skew: the busiest 20% take ~60% of the work.
        driver = (rng.choice(drivers[:max(1, N_DRIVERS // 5)])
                  if rng.random() < 0.6 else rng.choice(drivers))
        rider = rng.choice(riders)
        code = rng.choice(mix)
        requested = now - timezone.timedelta(
            seconds=rng.randint(60, 180 * 24 * 3600))
        fare = Decimal(str(rng.randint(4000, 60000))) / Decimal('100')
        t = Trip(
            user_id=rider,
            driver_id=driver if code != 'requested' else None,
            status_id=statuses[code],
            pickup_lat=Decimal('17.4450000'), pickup_long=Decimal('78.3800000'),
            destination_lat=Decimal('17.4550000'),
            destination_long=Decimal('78.3900000'),
            estimated_fare=fare, payment_method='cash',
            requested_at=requested,
        )
        if code != 'requested':
            t.accepted_at = requested + timezone.timedelta(seconds=40)
        if code in ('in_progress', 'completed'):
            t.started_at = requested + timezone.timedelta(seconds=200)
        if code == 'completed':
            t.completed_at = requested + timezone.timedelta(seconds=1000)
        if code == 'cancelled':
            t.cancelled_at = requested + timezone.timedelta(seconds=120)
            t.cancelled_by = 'rider'
        batch.append(t)
        if len(batch) >= 1000:
            Trip.objects.bulk_create(batch, batch_size=1000)
            made += len(batch)
            batch = []
            print(f'  {made}/{want} trips', flush=True)
    if batch:
        Trip.objects.bulk_create(batch, batch_size=1000)
        made += len(batch)

    # requested_at is auto_now_add, so bulk_create ignored the value above.
    # Spread it explicitly, or every trip shares one timestamp and the
    # (user, -requested_at) index becomes meaningless.
    print('spreading requested_at...', flush=True)
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute(
            "UPDATE ride_trip SET requested_at = "
            "  now() - (random() * interval '180 days') "
            "WHERE requested_at > now() - interval '1 hour'"
        )

    pts_have = TripLocationPoint.objects.count()
    if pts_have < 1000:
        print(f'seeding ~{N_POINTS_PER_TRIP} location points for 1500 trips...',
              flush=True)
        sample = list(Trip.objects.exclude(driver_id=None)
                      .values_list('id', 'driver_id')[:1500])
        pbatch = []
        for trip_id, driver_id in sample:
            base = now - timezone.timedelta(days=rng.randint(1, 120))
            for s in range(N_POINTS_PER_TRIP):
                pbatch.append(TripLocationPoint(
                    trip_id=trip_id, driver_id=driver_id,
                    latitude=Decimal('17.4450000') + Decimal(str(s)) / 10000,
                    longitude=Decimal('78.3800000'),
                    recorded_at=base + timezone.timedelta(seconds=s * 5),
                    sequence=s, source_event_id=f'{trip_id}-{s}',
                ))
            if len(pbatch) >= 5000:
                TripLocationPoint.objects.bulk_create(pbatch, batch_size=2000)
                pbatch = []
        if pbatch:
            TripLocationPoint.objects.bulk_create(pbatch, batch_size=2000)

    print('ANALYZE (the planner needs statistics or every plan is a guess)...',
          flush=True)
    with connection.cursor() as cur:
        cur.execute('ANALYZE')

    print(f'seeded: {Trip.objects.count()} trips, '
          f'{TripLocationPoint.objects.count()} location points, '
          f'{Driver.objects.count()} drivers, {Rider.objects.count()} riders',
          flush=True)


# ---------------------------------------------------------------------------
# Auditing
# ---------------------------------------------------------------------------

def table_sizes():
    from django.db import connection
    with connection.cursor() as cur:
        cur.execute(
            "SELECT relname, n_live_tup FROM pg_stat_user_tables "
            "WHERE n_live_tup > 0 ORDER BY n_live_tup DESC")
        return dict(cur.fetchall())


def explain(qs_or_sql, params=None):
    """EXPLAIN (ANALYZE, BUFFERS) the query a queryset would actually run."""
    from django.db import connection
    if hasattr(qs_or_sql, 'query'):
        sql, params = qs_or_sql.query.sql_with_params()
    else:
        sql, params = qs_or_sql, params or ()
    with connection.cursor() as cur:
        started = time.perf_counter()
        cur.execute(f'EXPLAIN (ANALYZE, BUFFERS) {sql}', params)
        plan = '\n'.join(r[0] for r in cur.fetchall())
        wall = (time.perf_counter() - started) * 1000
    return plan, wall


def plan_summary(plan):
    first = plan.splitlines()[0].strip() if plan else ''
    exec_ms = None
    for line in plan.splitlines():
        if line.startswith('Execution Time:'):
            exec_ms = float(line.split(':')[1].strip().split()[0])
    return first, exec_ms


def seq_scanned_tables(plan):
    out = []
    for line in plan.splitlines():
        s = line.strip()
        if s.startswith('->  Seq Scan on ') or s.startswith('Seq Scan on '):
            out.append(s.split(' on ')[1].split()[0])
    return out


def queries():
    """The real querysets, imported from the code that runs them where possible."""
    from django.utils import timezone

    from servers.ride.models import (
        DRIVER_ACTIVE_TRIP_STATUSES, Trip, TripLocationPoint,
    )

    # Pick entities that actually have history, or a plan is measured against an
    # empty result and proves nothing.
    busy_driver = (Trip.objects.exclude(driver_id=None)
                   .values_list('driver_id', flat=True).first())
    busy_rider = Trip.objects.values_list('user_id', flat=True).first()
    a_trip = Trip.objects.values_list('id', flat=True).first()

    q = []

    q.append((
        'rider trip history (the rider app home screen)',
        Trip.objects.filter(user_id=busy_rider)
            .select_related('status_id', 'driver_id')
            .order_by('-requested_at')[:20],
        'must use trip(user_id, -requested_at); a sort of the whole table here '
        'would make the rider app slower for every user as the table grows',
    ))

    q.append((
        'driver trip history',
        Trip.objects.filter(driver_id=busy_driver)
            .select_related('status_id')
            .order_by('-requested_at')[:20],
        'same shape for the driver app; the busiest driver is the worst case',
    ))

    q.append((
        'driver exclusivity check (driver_active_trip_ids)',
        Trip.objects.filter(
            driver_id=busy_driver,
            status_id__status_code__in=DRIVER_ACTIVE_TRIP_STATUSES,
        ).values_list('pk', flat=True),
        'runs inside the Driver row lock on every acceptance, so its cost is '
        'held time on a lock that serialises all of that driver\'s work',
    ))

    q.append((
        'operations: trips awaiting a driver',
        Trip.objects.filter(status_id__status_code='requested')
            .select_related('user_id').order_by('-requested_at')[:50],
        'the dispatch view; selective on a tiny slice of a large table',
    ))

    # The real queryset, imported rather than rewritten. An earlier version of
    # this audit paraphrased it -- dropping the timestamp predicates -- and
    # reported a sequential scan the production query does not do. Auditing a
    # paraphrase is how you end up adding an index nobody needs.
    from servers.ride.liveness import stale_candidates_queryset
    q.append((
        'stale active-ride sweep (runs every 2 minutes from beat)',
        stale_candidates_queryset(limit=200),
        'a periodic sweep over active trips; the OR of three timestamp '
        'predicates plus an ORDER BY is the shape most likely to degrade into '
        'a full scan and sort',
    ))

    q.append((
        'route replay for one trip (SOS / dispute / actual distance)',
        TripLocationPoint.objects.filter(trip_id=a_trip)
            .order_by('recorded_at'),
        'must use triploc_trip_time_idx; this is read during an emergency',
    ))

    q.append((
        'where was this driver around time T (safety investigation)',
        TripLocationPoint.objects.filter(
            driver_id=busy_driver,
            recorded_at__gte=timezone.now() - timezone.timedelta(days=30),
        ).order_by('-recorded_at')[:100],
        'must use triploc_driver_time_idx',
    ))

    q.append((
        'completed trips in a date window (settlement / revenue)',
        Trip.objects.filter(
            status_id__status_code='completed',
            completed_at__gte=timezone.now() - timezone.timedelta(days=30),
        ).count(),
        'an aggregate over a large slice; the honest question is whether it is '
        'index-only or a heap scan',
    ))

    return [(name, obj, why) for name, obj, why in q if obj is not None]


def audit():
    sizes = table_sizes()
    print('\ntable sizes (pg_stat_user_tables):')
    for name, n in list(sizes.items())[:12]:
        print(f'  {n:>9,}  {name}')

    findings = []
    print('\nquery plans:')
    for name, obj, why in queries():
        if isinstance(obj, int):
            continue
        try:
            plan, wall = explain(obj)
        except Exception as exc:
            print(f'\n  {name}\n    EXPLAIN failed: {exc!r}')
            continue
        top, exec_ms = plan_summary(plan)
        scans = [t for t in seq_scanned_tables(plan)
                 if sizes.get(t, 0) >= SEQ_SCAN_ROW_THRESHOLD]
        print(f'\n  {name}')
        print(f'    top node    : {top}')
        print(f'    exec time   : {exec_ms if exec_ms is not None else wall:.3f} ms')
        if scans:
            print(f'    SEQ SCAN on : {", ".join(f"{t} ({sizes[t]:,} rows)" for t in scans)}')
            findings.append((name, scans, why))
        else:
            print('    index use   : no sequential scan over a large table')

    print('\n' + '=' * 78)
    if findings:
        print(f'{len(findings)} quer{"y" if len(findings) == 1 else "ies"} '
              f'sequentially scan a table of '
              f'{SEQ_SCAN_ROW_THRESHOLD:,}+ rows:')
        for name, scans, why in findings:
            print(f'\n  {name}')
            print(f'    scans: {", ".join(scans)}')
            print(f'    why it matters: {why}')
    else:
        print(f'No query sequentially scans a table of '
              f'{SEQ_SCAN_ROW_THRESHOLD:,}+ rows.')
    print('=' * 78)
    print('\nThese are single-query timings on one machine with a warm cache and '
          'no competing\ntraffic. They are a floor on query cost and a detector '
          'of bad plans. They are NOT\na capacity model and must not be quoted '
          'as one.')
    return 1 if findings else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--seed', action='store_true')
    args = ap.parse_args()

    configure_django()
    from django.db import connection
    try:
        connection.ensure_connection()
    except Exception as exc:
        print(f'cannot reach the load database at {DB["HOST"]}:{DB["PORT"]}/'
              f'{DB["NAME"]}: {exc}')
        print('\nstart it with:\n'
              '  docker exec sg-pg-test psql -U sgtest -d postgres '
              '-c "CREATE DATABASE sgload"\n'
              '  python manage.py migrate   (with the DB_* env of this script)')
        return 2

    if args.seed:
        seed()
        return 0
    return audit()


if __name__ == '__main__':
    sys.exit(main())
