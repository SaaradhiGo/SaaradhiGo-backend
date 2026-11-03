"""Fill the LOAD STACK database with a clearly synthetic, at-scale dataset.

    python manage.py seed_scale_dataset                 # full size
    python manage.py seed_scale_dataset --scale 0.01    # 1 % smoke run
    python manage.py seed_scale_dataset --reset         # delete what it made, then reseed

PURPOSE
-------
Prove the admin console at the size the product is meant to reach: 5 countries,
120 cities, 50,000 drivers, 500,000 riders, 2,000,000 trips, with a realistic
share of SOS events (and the safety incidents they open), support tickets,
withdrawals and operator alerts. Every list, filter, search and count in the
console is then exercised against rows it will really hold.

REFUSES OUTSIDE A LOAD STACK
----------------------------
It writes ~3 million rows and several hundred thousand accounts, so it must
never run anywhere real. It refuses unless `settings.ENVIRONMENT` is exactly
`load`; this is the same check `seed_load_fixtures` makes, copied rather than
loosened. `tests/test_seed_scale_dataset.py` proves the refusal for qa,
production, development and an empty environment.

(`seed_load_fixtures` also calls the shared `require_seedable_environment`
gate, which demands a non-empty TEST_PHONE_NUMBERS. That gate exists because
that command's phone numbers look real and would receive real SMS. This
command's numbers cannot: see SYNTHETIC LABELS. So it uses the environment
check alone, which is the stricter of the two for "where may this run".)

SYNTHETIC LABELS
----------------
* Names:    "Synthetic Driver 12345", "Synthetic Rider 12345".
* Emails:   d12345@synthetic.invalid / r12345@synthetic.invalid (`.invalid` is
            reserved by RFC 2606 and can never resolve).
* Phones:   +<calling code>0<9 digits>. A national number starting with 0 is not
            a mobile number in any of the five countries, so none can dial a
            real person.
* Addresses "Synthetic Rd 42, Banjara Hills, Hyderabad". Cities beyond the real
            ones are real public place names; nothing personal.
* Trips:    client_request_id starts with `syn-`.
* Zones:    code ends with `-SYN`, metadata {"synthetic": true}.
* Alerts:   source_type `synthetic`.

`--reset` deletes exactly those rows (by marker), never anything else. Without
`--reset` the command refuses to run a second time.

HOW IT IS FAST
--------------
Everything but ~400 SOS events (written through `SOSEvent.save()` so the incident
and anchor invariants are the product's own) goes through `bulk_create` in large
batches, with `DEBUG` forced off so Django does not retain the SQL, and with the
timestamp auto-fields switched off for the run so rows carry their true history
rather than "now".

KNOWN LIMITS (also in the final run report)
-------------------------------------------
* `base.money.CURRENCIES` supports INR and USD only, and the product refuses to
  convert any other currency. Canada, UK and Australia exist as geography (nodes,
  zones, drivers, riders, trips) but their money rows are stamped USD. The zone
  metadata says so. Adding CAD/GBP/AUD is a product change, not a seed change.
* No FarePricing / Receipt / chat / GPS-trail rows: the console reads trip money
  from the trip row, and those tables would add ~6M rows without exercising a
  list the console shows at scale.
"""

import bisect
import math
import random
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models.functions import Coalesce
from django.db.models import Count, Max, Min, OuterRef, Subquery
from django.test.utils import override_settings
from django.utils import timezone

from . import _scale_data as D

User = get_user_model()

SYN_DOMAIN = 'synthetic.invalid'
TRIP_PREFIX = 'syn-'
ZONE_SUFFIX = '-SYN'
ALERT_SOURCE = 'synthetic'
RNG_SEED = 20261103

TRIP_BATCH = 4000
USER_BATCH = 5000
HISTORY_DAYS = 60
UTC = dt_timezone.utc

#: Product-level currency each country's money is stamped with (see KNOWN LIMITS).
CURRENCY = {'IN': 'INR', 'US': 'USD', 'CA': 'USD', 'GB': 'USD', 'AU': 'USD'}
DISTANCE_UNIT = {'IN': 'km', 'US': 'mi', 'CA': 'km', 'GB': 'mi', 'AU': 'km'}
TAX_MODE = {'IN': 'inclusive', 'US': 'additive', 'CA': 'additive',
            'GB': 'inclusive', 'AU': 'inclusive'}
AU_TZ = {'NSW': 'Australia/Sydney', 'VIC': 'Australia/Melbourne', 'QLD': 'Australia/Brisbane',
         'WA': 'Australia/Perth', 'SA': 'Australia/Adelaide'}


@contextmanager
def _real_timestamps(*models):
    """Switch off auto_now / auto_now_add so a row keeps the time it is given."""
    saved = []
    for model in models:
        for field in model._meta.concrete_fields:
            if getattr(field, 'auto_now', False) or getattr(field, 'auto_now_add', False):
                saved.append((field, field.auto_now, field.auto_now_add))
                field.auto_now = False
                field.auto_now_add = False
    try:
        yield
    finally:
        for field, a, b in saved:
            field.auto_now, field.auto_now_add = a, b


def _rd(queryset):
    """Delete without loading rows or walking cascades. Order is the caller's job."""
    return queryset._raw_delete(queryset.db)


def _apportion(total, weights, floor):
    """Split `total` by `weights` (largest remainder), every part at least `floor`."""
    s = sum(weights)
    raw = [total * w / s for w in weights]
    parts = [max(floor, int(x)) for x in raw]
    diff = total - sum(parts)
    order = sorted(range(len(parts)), key=lambda i: raw[i] - int(raw[i]), reverse=True)
    i = 0
    while diff > 0:
        parts[order[i % len(parts)]] += 1
        diff -= 1
        i += 1
    # Over-allocation from the floor: trim from the largest parts.
    while diff < 0:
        j = max(range(len(parts)), key=lambda k: parts[k])
        if parts[j] <= floor:
            break
        parts[j] -= 1
        diff += 1
    return parts


class Command(BaseCommand):
    help = ('Seed the LOAD STACK with a synthetic 5-country / 120-city / 50k-driver / '
            '500k-rider / 2M-trip dataset. Refuses outside ENVIRONMENT=load.')

    def add_arguments(self, parser):
        parser.add_argument('--scale', type=float, default=1.0,
                            help='Multiplier on every volume (0.01 = 1%%). Cities stay at 120.')
        parser.add_argument('--reset', action='store_true',
                            help='Delete the rows a previous run created (by marker), then reseed.')
        parser.add_argument('--seed', type=int, default=RNG_SEED,
                            help='Random seed. Same seed and scale give the same dataset.')

    # ------------------------------------------------------------------ entry

    def handle(self, *args, **options):
        # Copied from seed_load_fixtures and not to be weakened: the load stack
        # is the only place this may run, and the setting is what the deployment
        # actually booted with. Before any query.
        env = (getattr(settings, 'ENVIRONMENT', '') or '').strip().lower()
        if env != 'load' or getattr(settings, 'IS_PRODUCTION', False):
            raise CommandError(
                f'refusing to run: ENVIRONMENT is {env!r}, not "load". This '
                f'command writes millions of synthetic rows and accounts, so '
                f'it must only ever run against a disposable load stack.'
            )

        scale = options['scale']
        if scale <= 0:
            raise CommandError('--scale must be positive')

        started = time.time()
        # DEBUG=True would make Django keep every INSERT it sends.
        with override_settings(DEBUG=False):
            if self._exists():
                if not options['reset']:
                    raise CommandError(
                        'a synthetic dataset is already present. Run with --reset to '
                        'delete it (only rows carrying the synthetic marker) and reseed.')
                self._reset()
            self.rng = random.Random(options['seed'])
            self.now = timezone.now().replace(microsecond=0)
            self.scale = scale
            self._seed_all()
        self._p(f'done in {time.time() - started:.0f}s')

    # ----------------------------------------------------------------- output

    def _p(self, msg):
        self.stdout.write(f'[seed_scale_dataset] {msg}')

    def _phase(self, name):
        self._t = time.time()
        self._p(name + ' ...')

    def _end(self, extra=''):
        self._p(f'  done in {time.time() - self._t:.1f}s {extra}')

    # ------------------------------------------------------- existence / reset

    def _exists(self):
        from servers.pricing.models import ServiceZone
        return (User.objects.filter(email__endswith='@' + SYN_DOMAIN).exists()
                or ServiceZone.objects.filter(code__endswith=ZONE_SUFFIX).exists())

    @transaction.atomic
    def _reset(self):
        from servers.admin_access.models import GeoNode
        from servers.command_centre.models import OperatorAlert
        from servers.driver.models import (
            Driver, DriverBankAccount, DriverCancellation, DriverSuspension, Vehicle,
            WithdrawalRequest)
        from servers.pricing.models import ServiceZone
        from servers.rider.models import Rider
        from servers.ride.models import Trip
        from servers.safety.models import (
            SafetyIncident, SafetyIncidentLink, SafetyIncidentUpdate, SafetySignal)
        from servers.sos.models import SOSEvent, SOSEventUpdate
        from servers.support.models import SupportMessage, SupportTicket

        self._phase('reset: deleting synthetic rows')
        syn_user = '@' + SYN_DOMAIN
        trip_pref = {'client_request_id__startswith': TRIP_PREFIX}
        _rd(SOSEventUpdate.objects.filter(event__trip__client_request_id__startswith=TRIP_PREFIX))
        _rd(SafetyIncidentUpdate.objects.filter(incident__trip__client_request_id__startswith=TRIP_PREFIX))
        _rd(SafetyIncidentLink.objects.filter(incident__trip__client_request_id__startswith=TRIP_PREFIX))
        _rd(SafetySignal.objects.filter(trip__client_request_id__startswith=TRIP_PREFIX))
        _rd(SOSEvent.objects.filter(trip__client_request_id__startswith=TRIP_PREFIX))
        _rd(SafetyIncident.objects.filter(trip__client_request_id__startswith=TRIP_PREFIX))
        _rd(SupportMessage.everything.filter(ticket__user_id__email__endswith=syn_user))
        _rd(SupportTicket.objects.filter(user_id__email__endswith=syn_user))
        _rd(DriverCancellation.objects.filter(driver__user_id__email__endswith=syn_user))
        _rd(WithdrawalRequest.objects.filter(driver__user_id__email__endswith=syn_user))
        _rd(DriverBankAccount.objects.filter(driver__user_id__email__endswith=syn_user))
        _rd(DriverSuspension.objects.filter(driver__user_id__email__endswith=syn_user))
        _rd(Trip.objects.filter(**trip_pref))
        _rd(OperatorAlert.objects.filter(source_type=ALERT_SOURCE))
        _rd(Vehicle.objects.filter(driver_id__user_id__email__endswith=syn_user))
        _rd(Driver.objects.filter(user_id__email__endswith=syn_user))
        _rd(Rider.objects.filter(user_id__email__endswith=syn_user))
        _rd(User.objects.filter(email__endswith=syn_user))

        zones = ServiceZone.objects.filter(code__endswith=ZONE_SUFFIX)
        created_nodes = [z.geo_node_id for z in zones if (z.metadata or {}).get('geo_created')]
        _rd(zones)
        for node in GeoNode.objects.filter(pk__in=created_nodes):
            node.delete()
        self._end()

    # -------------------------------------------------------------- the seed

    def _seed_all(self):
        s = self.scale
        self.n_cities = sum(len(v[1]) for v in D.COUNTRIES.values())
        self.n_drivers = max(self.n_cities * 2, int(50_000 * s))
        self.n_riders = max(self.n_cities * 2, int(500_000 * s))
        self.n_trips = max(200, int(2_000_000 * s))
        self._p(f'scale {s}: {self.n_cities} cities, {self.n_drivers} drivers, '
                f'{self.n_riders} riders, {self.n_trips} trips')
        t0 = time.time()
        self._build_cities()
        self._build_reference()
        self._build_drivers()
        self._build_riders()
        self._build_trips()
        self._finish_drivers()
        self._build_sos()
        self._build_tickets()
        self._build_withdrawals()
        self._build_alerts()
        self._report(time.time() - t0)

    # ---- cities ---------------------------------------------------------

    def _tz_for(self, cc, state):
        if cc == 'AU':
            return AU_TZ[state]
        if cc == 'GB':
            return 'Europe/London'
        if cc == 'IN':
            return 'Asia/Kolkata'
        return D.STATE_TZ.get(state, D.COUNTRIES[cc][3])

    def _build_cities(self):
        from servers.admin_access.models import CITY, COUNTRY, GLOBAL, STATE, GeoNode
        from servers.pricing.models import ServiceZone

        self._phase('geo tree + zones')
        root = GeoNode.objects.filter(kind=GLOBAL).first()
        if root is None:
            root = GeoNode(kind=GLOBAL, code='GLOBAL', name='Global', status='active')
            root.save()

        def get_or_make(kind, code, name, parent, status='active'):
            node = GeoNode.objects.filter(code=code).first()
            if node is not None:
                return node, False
            node = GeoNode(kind=kind, code=code, name=name, parent=parent, status=status)
            node.save()
            return node, True

        cities = []
        used_codes = set()
        for cc, (cname, table, mult, _tz, calling) in D.COUNTRIES.items():
            cnode, _ = get_or_make(COUNTRY, cc, cname, root)
            states = {}
            for rank, (name, st, st_name, lat, lng) in enumerate(table):
                if st not in states:
                    states[st], _ = get_or_make(STATE, f'{cc}-{st}', st_name, cnode)
                flat = name.replace(' ', '').upper()
                n = 3
                while f'{cc}-{st}-{flat[:n]}' in used_codes and n < len(flat):
                    n += 1
                code = f'{cc}-{st}-{flat[:n]}'
                used_codes.add(code)
                paused = D.PAUSED.get(cc) == name
                node, made = get_or_make(CITY, code, name, states[st],
                                         'paused' if paused else 'active')
                tz = self._tz_for(cc, st)
                weight = mult / (rank + 2.0) ** 1.0
                if paused:
                    weight *= 0.2
                cities.append({
                    'idx': len(cities), 'name': name, 'cc': cc, 'state': st, 'lat': lat,
                    'lng': lng, 'node': node, 'geo_created': made, 'code': code,
                    'tz': tz, 'weight': weight, 'paused': paused, 'rank': rank,
                    'currency': CURRENCY[cc], 'calling': calling,
                })
        self.cities = cities
        self.city_cum = []
        acc = 0.0
        for c in cities:
            acc += c['weight']
            self.city_cum.append(acc)

        zones = []
        for c in cities:
            dlat = 0.18
            dlng = 0.18 / max(0.2, math.cos(math.radians(c['lat'])))
            poly = {'type': 'Polygon', 'coordinates': [[
                [round(c['lng'] - dlng, 5), round(c['lat'] - dlat, 5)],
                [round(c['lng'] + dlng, 5), round(c['lat'] - dlat, 5)],
                [round(c['lng'] + dlng, 5), round(c['lat'] + dlat, 5)],
                [round(c['lng'] - dlng, 5), round(c['lat'] + dlat, 5)],
                [round(c['lng'] - dlng, 5), round(c['lat'] - dlat, 5)],
            ]]}
            zones.append(ServiceZone(
                code=c['code'] + ZONE_SUFFIX, name=f"{c['name']} (synthetic)",
                country=c['cc'], state_code=c['state'], city=c['name'], zone_type='city',
                polygon_geojson=poly,
                # Below the load harness's own Hyderabad zone (priority 10), so a
                # harness quote never resolves to a synthetic zone.
                priority=1, currency=c['currency'], timezone_name=c['tz'],
                distance_unit=DISTANCE_UNIT[c['cc']], tax_mode=TAX_MODE[c['cc']],
                geo_node=c['node'], is_active=not c['paused'],
                metadata={'synthetic': True, 'geo_created': c['geo_created'],
                          'money_note': ('money stamped USD: CAD/GBP/AUD are not in '
                                         'base.money.CURRENCIES' if c['cc'] in ('CA', 'GB', 'AU')
                                         else '')},
            ))
        created = ServiceZone.objects.bulk_create(zones)
        for c, z in zip(cities, created):
            c['zone_id'] = z.pk
        self.zone_city = {c['zone_id']: c for c in cities}

        # Places (pickup/drop vocabulary) per city, and per-city day/hour tables.
        for c in cities:
            if c['name'] in D.LOCALITIES:
                c['places'] = list(D.LOCALITIES[c['name']])
            else:
                places = []
                for k in range(1, 9):
                    ang = k * 0.785
                    rad = 0.012 + 0.006 * (k % 4)
                    places.append((f'Area {k}', c['lat'] + rad * math.sin(ang),
                                   c['lng'] + rad * math.cos(ang)))
                c['places'] = places
        self._build_time_tables()
        self._end(f'({len(cities)} cities)')

    def _build_time_tables(self):
        """Per city: local-midnight epochs for the last 60 days and the day weights.

        Today's weight is scaled by how much of the local day's traffic has
        already happened, so a city that is still asleep has had few trips today.
        """
        hw = D.HOUR_WEIGHTS
        self.hour_cum = []
        acc = 0.0
        for w in hw:
            acc += w
            self.hour_cum.append(acc)
        self.hour_total = acc
        cutoff = self.now - timedelta(minutes=75)
        self.cutoff_epoch = cutoff.timestamp()
        for c in self.cities:
            tzinfo = ZoneInfo(c['tz'])
            local_now = cutoff.astimezone(tzinfo)
            today = local_now.date()
            mids = []
            for d in range(HISTORY_DAYS):
                day = today - timedelta(days=d)
                mids.append(datetime(day.year, day.month, day.day, tzinfo=tzinfo)
                            .astimezone(UTC).timestamp())
            elapsed_h = local_now.hour + local_now.minute / 60.0
            full = int(elapsed_h)
            frac_mass = (self.hour_cum[full - 1] if full > 0 else 0.0) \
                + hw[full] * (elapsed_h - full)
            today_frac = frac_mass / self.hour_total
            weights = []
            for d in range(HISTORY_DAYS):
                w = 3.0 if d < 7 else 1.0
                if d == 0:
                    w *= today_frac
                weights.append(w)
            cum, a = [], 0.0
            for w in weights:
                a += w
                cum.append(a)
            c['mids'] = mids
            c['day_cum'] = cum
            c['today_frac'] = today_frac

    # ---- reference rows ----------------------------------------------------

    def _build_reference(self):
        from servers.driver.models import VehicleType
        from servers.ride.models import TripStatus
        self.status_id = {}
        for code in ('requested', 'accepted', 'reached', 'in_progress', 'completed', 'cancelled'):
            self.status_id[code] = TripStatus.objects.get_or_create(status_code=code)[0].pk
        self.vtype_id = {}
        for code in ('sedan', 'hatchback', 'suv', 'auto'):
            self.vtype_id[code] = VehicleType.objects.get_or_create(type=code)[0].pk

    # ---- users ----------------------------------------------------------

    def _make_users(self, role, per_city, label, email_tag, phone_base):
        """Insert users city by city. Returns {city_idx: [user_id, ...]}."""
        ids = {c['idx']: [] for c in self.cities}
        rng = self.rng
        n = 0
        buf, buf_city = [], []
        now = self.now

        def flush():
            created = User.objects.bulk_create(buf, batch_size=USER_BATCH)
            for u, ci in zip(created, buf_city):
                ids[ci].append(u.pk)
            buf.clear()
            buf_city.clear()

        for c, count in zip(self.cities, per_city):
            for _ in range(count):
                n += 1
                phone = f"+{c['calling']}0{phone_base + n:09d}"
                joined = now - timedelta(days=rng.randint(HISTORY_DAYS + 1, 540),
                                         seconds=rng.randint(0, 86399))
                buf.append(User(
                    username=phone, phone_number=phone, password='!', role=role,
                    full_name=f'Synthetic {label} {n}',
                    email=f'{email_tag}{n}@{SYN_DOMAIN}', city=c['name'],
                    ops_role='', is_active=True, date_joined=joined,
                    created_at=joined, updated_at=joined,
                ))
                buf_city.append(c['idx'])
                if len(buf) >= USER_BATCH:
                    flush()
        if buf:
            flush()
        return ids

    def _build_riders(self):
        from servers.rider.models import Rider
        self._phase('riders')
        weights = [c['weight'] for c in self.cities]
        per_city = _apportion(self.n_riders, weights, 1)
        with _real_timestamps(User, Rider):
            ids = self._make_users('rider', per_city, 'Rider', 'r', 0)
            rng = self.rng
            buf = []
            for c in self.cities:
                for uid in ids[c['idx']]:
                    cnt = rng.randint(0, 40)
                    buf.append(Rider(
                        user_id_id=uid, rating=round(rng.uniform(4.2, 5.0), 2),
                        rating_count=cnt, home_city_id=c['node'].pk,
                        created_at=self.now - timedelta(days=rng.randint(HISTORY_DAYS + 1, 540)),
                    ))
                    if len(buf) >= USER_BATCH:
                        Rider.objects.bulk_create(buf)
                        buf = []
            if buf:
                Rider.objects.bulk_create(buf)
        self.riders_by_city = ids
        self._end(f'({sum(per_city)} riders)')

    def _build_drivers(self):
        from servers.driver.models import Driver, DriverSuspension, Vehicle
        self._phase('drivers + vehicles')
        rng = self.rng
        now = self.now
        weights = [c['weight'] for c in self.cities]
        per_city = _apportion(self.n_drivers, weights, 2)
        with _real_timestamps(User, Driver, Vehicle, DriverSuspension):
            ids = self._make_users('driver', per_city, 'Driver', 'd', 100_000_000)
            self.approved_by_city = {c['idx']: [] for c in self.cities}  # (driver_pk, user_id, vehicle_pk, vtype)
            self.driver_user = {}
            self.suspended = []
            self.synthetic_driver_pks = []
            seq = 0
            for c in self.cities:
                uids = ids[c['idx']]
                drivers = []
                meta = []
                for k, uid in enumerate(uids):
                    seq += 1
                    r = rng.random()
                    if k == 0:
                        kind = 'off'          # every city keeps at least one approved driver
                    elif r < 0.04:
                        kind = 'pending'
                    elif r < 0.055:
                        kind = 'rejected'
                    elif r < 0.065:
                        kind = 'suspended'
                    else:
                        kind = 'online' if rng.random() < 0.1925 else 'off'
                    approved = kind in ('online', 'off')
                    drivers.append(Driver(
                        user_id_id=uid,
                        status='blocked' if kind == 'suspended' else (kind if approved else 'off'),
                        doc_status={'pending': 'pending', 'rejected': 'rejected'}.get(kind, 'approved'),
                        doc_rejection_reason='Synthetic: document unreadable' if kind == 'rejected' else None,
                        approved=approved,
                        ratings=round(rng.uniform(3.8, 5.0), 2) if kind in ('online', 'off', 'suspended') else 0,
                        total_trips=0, home_city_id=c['node'].pk,
                        license_expiry=(now + timedelta(days=rng.randint(60, 1800))).date(),
                        uploaded_timestamp=now - timedelta(days=rng.randint(30, 500)),
                        doc_status_updated_at=now - timedelta(days=rng.randint(1, 400)),
                    ))
                    meta.append(kind)
                created = Driver.objects.bulk_create(drivers, batch_size=USER_BATCH)
                vehicles = []
                for d, kind, uid in zip(created, meta, uids):
                    self.driver_user[d.pk] = uid
                    self.synthetic_driver_pks.append(d.pk)
                    brand, model, vt = rng.choice(
                        D.VEHICLES if c['cc'] == 'IN' else D.VEHICLES[:9])
                    vehicles.append(Vehicle(
                        driver_id_id=d.pk, vehicle_type_id_id=self.vtype_id[vt],
                        brand=brand, model=model, color=rng.choice(D.COLORS),
                        year=rng.randint(2014, 2025),
                        vehicle_number=f'SYN-{d.pk:07d}', capacity=3 if vt == 'auto' else 4,
                        status='active',
                        insurance_expiry=(now + timedelta(days=rng.randint(30, 700))).date(),
                        permit_expiry=(now + timedelta(days=rng.randint(30, 700))).date(),
                        fitness_expiry=(now + timedelta(days=rng.randint(30, 700))).date(),
                        puc_expiry=(now + timedelta(days=rng.randint(30, 300))).date(),
                    ))
                vcreated = Vehicle.objects.bulk_create(vehicles, batch_size=USER_BATCH)
                susp = []
                for d, v, kind, vehicle in zip(created, vcreated, meta, vehicles):
                    if kind in ('online', 'off'):
                        self.approved_by_city[c['idx']].append(
                            (d.pk, d.user_id_id, v.pk, vehicle.vehicle_type_id_id))
                    if kind == 'suspended':
                        susp.append(DriverSuspension(
                            driver_id=d.pk,
                            reason_code=rng.choice(['conduct', 'rider_complaints', 'documents',
                                                    'safety', 'fraud', 'other']),
                            note='Synthetic suspension', previous_approved=True,
                            created_at=now - timedelta(days=rng.randint(1, 50))))
                if susp:
                    DriverSuspension.objects.bulk_create(susp)
            # One statement points every driver at their vehicle.
            Driver.objects.filter(user_id__email__endswith='@' + SYN_DOMAIN).update(
                active_vehicle=Subquery(
                    Vehicle.objects.filter(driver_id=OuterRef('pk')).values('pk')[:1]))
        self._end(f'({len(self.synthetic_driver_pks)} drivers)')

    # ---- trips ----------------------------------------------------------

    def _fare(self, cc, dist_km, dur_min, surge):
        if cc == 'IN':
            f = max(70.0, 40.0 + 11.0 * dist_km + 1.6 * dur_min)
        else:
            f = max(7.0, 3.0 + 1.15 * dist_km + 0.32 * dur_min)
        return int(round(f * surge * 100))

    def _pick_places(self, c):
        rng = self.rng
        places = c['places']
        p1 = rng.choice(places)
        p2 = rng.choice(places)
        j = 0.0035
        return (
            (p1, round(p1[1] + rng.uniform(-j, j), 7), round(p1[2] + rng.uniform(-j, j), 7)),
            (p2, round(p2[1] + rng.uniform(-j, j), 7), round(p2[2] + rng.uniform(-j, j), 7)),
        )

    def _address(self, place, c):
        return f'Synthetic Rd {self.rng.randint(1, 300)}, {place[0]}, {c["name"]}'

    def _trip_time(self, c):
        """A request time (epoch seconds) for city `c`, weighted to recent days."""
        rng = self.rng
        cum = c['day_cum']
        for _ in range(8):
            d = bisect.bisect_left(cum, rng.random() * cum[-1])
            h = bisect.bisect_left(self.hour_cum, rng.random() * self.hour_total)
            ts = c['mids'][d] + h * 3600 + rng.randint(0, 3599)
            if ts <= self.cutoff_epoch:
                return ts
        return c['mids'][1] + rng.randint(0, 86399)

    def _build_trip(self, c, status, ts, riders, ci_drivers, active_kind=None, used_drivers=None):
        """One Trip row (unsaved). `ts` is epoch seconds of the request."""
        from servers.ride.models import Trip
        rng = self.rng
        cc = c['cc']
        (pk1, lat1, lng1), (pk2, lat2, lng2) = self._pick_places(c)
        dist = min(35.0, max(1.2, rng.lognormvariate(1.7, 0.6)))
        est_dur = dist * 2.8 + rng.uniform(3, 8)
        peak = 7 <= int((ts % 86400) // 3600) <= 9 or 17 <= int((ts % 86400) // 3600) <= 20
        surge = round(rng.uniform(1.2, 1.8), 2) if rng.random() < (0.16 if peak else 0.04) else 1.0
        est_fare = self._fare(cc, dist, est_dur, surge)
        t0 = datetime.fromtimestamp(ts, UTC)
        rider = rng.choice(riders)
        driver = None
        if ci_drivers and status not in ('requested',) and not (
                status == 'cancelled' and active_kind == 'nodriver'):
            for _ in range(6):
                driver = rng.choice(ci_drivers)
                if used_drivers is None or driver[0] not in used_drivers:
                    break
            else:
                driver = None
        kw = dict(
            user_id_id=rider, status_id_id=self.status_id[status if status != 'nodriver' else 'cancelled'],
            requested_at=t0, zone_id=c['zone_id'], currency=c['currency'],
            pickup_address=self._address(pk1, c), pickup_lat=lat1, pickup_long=lng1,
            destination_address=self._address(pk2, c), destination_lat=lat2, destination_long=lng2,
            estimated_distance_km=round(dist, 2), estimated_duration_min=round(est_dur, 2),
            estimated_fare_minor=est_fare, surge_multiplier=surge,
            client_request_id=f'{TRIP_PREFIX}{self._trip_seq}',
            requested_vehicle_type_id=(driver[3] if driver else self.vtype_id['sedan']),
            payment_method='cash' if rng.random() < (0.55 if cc == 'IN' else 0.08) else 'online',
        )
        self._trip_seq += 1
        if driver is not None:
            kw['driver_id_id'] = driver[0]
            kw['vehicle_id_id'] = driver[2]
        accepted = t0 + timedelta(seconds=rng.randint(15, 110))
        reached = accepted + timedelta(seconds=rng.randint(120, 600))
        started = reached + timedelta(seconds=rng.randint(40, 240))

        if status == 'completed':
            a_dist = dist * rng.uniform(0.97, 1.12)
            a_dur = est_dur * rng.uniform(0.85, 1.25)
            done = started + timedelta(minutes=a_dur)
            fare = int(round(est_fare * rng.uniform(0.95, 1.08)))
            kw.update(accepted_at=accepted, reached_at=reached, started_at=started,
                      completed_at=done, actual_distance_km=round(a_dist, 2),
                      actual_duration_min=round(a_dur, 2), final_fare_minor=fare,
                      payment_status='completed', commission_percent_snapshot=15,
                      last_driver_activity_at=done)
        elif status == 'cancelled':
            cancel_at = t0 + timedelta(seconds=rng.randint(30, 420))
            if active_kind == 'nodriver':
                kw.update(cancelled_at=t0 + timedelta(seconds=rng.randint(90, 240)),
                          cancelled_by='system', cancellation_reason='No driver found',
                          payment_status=None)
            elif active_kind == 'driver':
                kw.update(accepted_at=accepted, cancelled_at=accepted + timedelta(seconds=rng.randint(60, 600)),
                          cancelled_by='driver',
                          cancellation_reason=rng.choice(D.CANCEL_REASONS_DRIVER))
            elif active_kind == 'admin':
                kw.update(accepted_at=accepted, cancelled_at=accepted + timedelta(minutes=rng.randint(2, 30)),
                          cancelled_by='admin', cancellation_reason='Synthetic: cancelled by operator')
            else:
                after_accept = driver is not None and rng.random() < 0.5
                if after_accept:
                    kw.update(accepted_at=accepted, cancelled_at=max(cancel_at, accepted + timedelta(seconds=20)),
                              cancellation_fee_minor=((3000 if cc == "IN" else 250)
                                                    if rng.random() < 0.2 else 0))
                else:
                    kw.update(cancelled_at=cancel_at)
                    kw.pop('driver_id_id', None)
                    kw.pop('vehicle_id_id', None)
                kw.update(cancelled_by='rider', cancellation_reason=rng.choice(D.CANCEL_REASONS_RIDER))
        return Trip(**kw), driver

    def _flush_trips(self, buf):
        from servers.ride.models import Trip
        Trip.objects.bulk_create(buf, batch_size=TRIP_BATCH)
        buf.clear()

    def _build_trips(self):
        from servers.ride.models import Trip
        self._phase('trips')
        rng = self.rng
        self._trip_seq = 1
        n_total = self.n_trips
        n_active = max(8, int(n_total * 0.0002))
        n_active = min(n_active, n_total // 4)
        live = [c for c in self.cities if not c['paused'] and self.approved_by_city[c['idx']]]
        live_w = [c['weight'] ** 1.5 for c in live]
        busy = set()
        buf = []
        now = self.now
        self.stuck_trips = 0

        with _real_timestamps(Trip):
            # --- currently active trips (some stuck) ------------------------
            kinds = (['requested'] * 20 + ['accepted'] * 25 + ['reached'] * 10 + ['in_progress'] * 45)
            for i in range(n_active):
                c = rng.choices(live, weights=live_w)[0]
                kind = rng.choice(kinds)
                stuck = rng.random() < 0.2 and kind in ('accepted', 'in_progress')
                riders = self.riders_by_city[c['idx']]
                drivers = self.approved_by_city[c['idx']]
                status = kind
                trip, driver = self._build_trip(
                    c, 'requested' if kind == 'requested' else 'accepted', now.timestamp() - 600,
                    riders, drivers, used_drivers=busy)
                if kind != 'requested' and driver is None:
                    kind = 'requested'
                    trip.driver_id_id = None
                    trip.vehicle_id_id = None
                if kind != 'requested':
                    busy.add(driver[0])
                est = float(trip.estimated_duration_min)
                trip.status_id_id = self.status_id[kind]
                trip.payment_method = 'deferred'
                trip.payment_status = 'pending'
                trip.otp = f'{rng.randint(0, 9999):04d}'
                if kind == 'requested':
                    trip.requested_at = now - timedelta(seconds=rng.randint(20, 240))
                elif kind == 'accepted':
                    ago = rng.randint(35 * 60, 120 * 60) if stuck else rng.randint(60, 12 * 60)
                    trip.requested_at = now - timedelta(seconds=ago + 40)
                    trip.accepted_at = now - timedelta(seconds=ago)
                    trip.last_driver_activity_at = (now - timedelta(seconds=ago - 30) if stuck
                                                    else now - timedelta(seconds=rng.randint(3, 60)))
                elif kind == 'reached':
                    ago = rng.randint(60, 8 * 60)
                    trip.requested_at = now - timedelta(seconds=ago + 700)
                    trip.accepted_at = now - timedelta(seconds=ago + 600)
                    trip.reached_at = now - timedelta(seconds=ago)
                    trip.last_driver_activity_at = now - timedelta(seconds=rng.randint(3, 60))
                else:
                    if stuck:
                        ago = rng.randint(int((est + 90) * 60), int((est + 300) * 60))
                    else:
                        ago = rng.randint(60, max(120, int(est * 60 * 0.9)))
                    trip.requested_at = now - timedelta(seconds=ago + 900)
                    trip.accepted_at = now - timedelta(seconds=ago + 800)
                    trip.reached_at = now - timedelta(seconds=ago + 100)
                    trip.started_at = now - timedelta(seconds=ago)
                    trip.last_driver_activity_at = (now - timedelta(seconds=ago - 600) if stuck
                                                    else now - timedelta(seconds=rng.randint(3, 40)))
                if stuck:
                    self.stuck_trips += 1
                buf.append(trip)
            self._flush_trips(buf)
            self.busy_drivers = busy

            # --- history -----------------------------------------------------
            n_hist = n_total - n_active
            cities_idx = list(range(len(self.cities)))
            done = 0
            report_every = 250_000
            next_report = report_every
            while done < n_hist:
                size = min(TRIP_BATCH, n_hist - done)
                picks = rng.choices(cities_idx, cum_weights=self.city_cum, k=size)
                for ci in picks:
                    c = self.cities[ci]
                    r = rng.random()
                    if r < 0.85:
                        status, kind = 'completed', None
                    elif r < 0.93:
                        status, kind = 'cancelled', None
                    elif r < 0.97:
                        status, kind = 'cancelled', 'nodriver'
                    elif r < 0.99:
                        status, kind = 'cancelled', 'driver'
                    else:
                        status, kind = 'cancelled', 'admin'
                    ts = self._trip_time(c)
                    trip, _ = self._build_trip(
                        c, status, ts, self.riders_by_city[ci], self.approved_by_city[ci],
                        active_kind=kind)
                    buf.append(trip)
                self._flush_trips(buf)
                done += size
                if done >= next_report:
                    self._p(f'  trips {done + n_active:,}/{n_total:,} ({time.time() - self._t:.0f}s)')
                    next_report += report_every
        self._end(f'({n_total} trips, {n_active} active, {self.stuck_trips} stuck)')

    def _finish_drivers(self):
        """total_trips from the trips, 'on ride' for drivers with an active trip,
        last_withdrawal_at is set with the withdrawals."""
        from servers.driver.models import Driver
        from servers.ride.models import Trip
        self._phase('driver counters')
        completed = Trip.objects.filter(
            driver_id=OuterRef('pk'), status_id_id=self.status_id['completed'],
            client_request_id__startswith=TRIP_PREFIX,
        ).order_by().values('driver_id').annotate(n=Count('pk')).values('n')
        Driver.objects.filter(user_id__email__endswith='@' + SYN_DOMAIN).update(
            total_trips=Coalesce(Subquery(completed), 0))
        if self.busy_drivers:
            Driver.objects.filter(pk__in=list(self.busy_drivers)).update(status='on ride')
        self._end()

    # ---- SOS ------------------------------------------------------------

    def _sample_trips(self, count, status_code, extra=None):
        """Deterministic random sample of synthetic trips with `status_code`."""
        from servers.ride.models import Trip
        agg = Trip.objects.filter(client_request_id__startswith=TRIP_PREFIX).aggregate(
            lo=Min("pk"), hi=Max("pk"))
        hi = agg["hi"] or 0
        lo = agg["lo"] or 0
        rng = self.rng
        found, seen = [], set()
        attempts = 0
        while len(found) < count and attempts < 20 and hi:
            attempts += 1
            cand = [p for p in (rng.randint(max(lo, 1), hi) for _ in range(int(count * 1.4) + 20))
                    if p not in seen]
            seen.update(cand)
            qs = Trip.objects.filter(pk__in=cand, client_request_id__startswith=TRIP_PREFIX,
                                     status_id_id=self.status_id[status_code])
            found.extend(qs.select_related('user_id', 'driver_id__user_id'))
        return found[:count]

    def _build_sos(self):
        from servers.ride.models import Trip
        from servers.safety.models import (
            CLOSED, U_CLOSED, SafetyIncident, SafetyIncidentUpdate)
        from servers.safety.services import add_update, note_on_sos_transition
        from servers.sos.models import SOSEvent, SOSEventUpdate

        self._phase('SOS events + safety incidents')
        rng = self.rng
        now = self.now
        total = max(4, round(self.n_trips * 0.0002))
        n_open = min(6, max(1, total // 10))
        n_ack = min(8, max(1, total // 8))
        n_rest = max(0, total - n_open - n_ack)

        active_ip = list(Trip.objects.filter(
            client_request_id__startswith=TRIP_PREFIX,
            status_id_id=self.status_id['in_progress']).select_related('user_id', 'driver_id__user_id'))
        rng.shuffle(active_ip)
        recent = self._sample_trips(n_rest, 'completed')
        plan = []
        for i in range(n_open + n_ack):
            trip = active_ip.pop() if active_ip else (recent.pop() if recent else None)
            if trip is None:
                break
            plan.append((trip, 'open' if i < n_open else 'acknowledged'))
        for trip in recent:
            plan.append((trip, rng.choices(['resolved', 'false_alarm', 'acknowledged'],
                                           weights=[62, 30, 8])[0]))

        types = ['panic'] * 55 + ['harassment'] * 10 + ['accident'] * 12 + ['medical'] * 6 \
            + ['vehicle_breakdown'] * 12 + ['other'] * 5
        created = 0
        with _real_timestamps(SOSEventUpdate):
            for trip, status in plan:
                by_driver = trip.driver_id_id is not None and rng.random() < 0.3
                user = trip.driver_id.user_id if by_driver else trip.user_id
                label = user.full_name or user.phone_number
                if status == 'open':
                    ts = now - timedelta(seconds=rng.randint(60, 12 * 60))
                elif trip.status_id_id == self.status_id['in_progress']:
                    ts = now - timedelta(minutes=rng.randint(15, 25))
                    if trip.started_at and ts < trip.started_at:
                        ts = trip.started_at + timedelta(seconds=30)
                else:
                    base = trip.started_at or trip.requested_at
                    ts = min(base + timedelta(minutes=rng.randint(3, 20)), now - timedelta(minutes=5))
                ev = SOSEvent(
                    user=user, user_label=label[:255],
                    initiated_by='driver' if by_driver else 'rider', trip=trip,
                    event_type=rng.choice(types), status='open',
                    latitude=trip.pickup_lat, longitude=trip.pickup_long,
                    location_accuracy_m=rng.choice([8.0, 12.0, 25.0, 60.0]),
                    note='Synthetic SOS event', ip_address='192.0.2.1',
                    user_agent='synthetic-seed')
                ev.save()          # anchors, opens the incident, links the SOS
                created += 1
                if status != 'open':
                    ack_at = ts + timedelta(minutes=rng.randint(1, 6))
                    SOSEventUpdate.objects.create(
                        event=ev, actor=None, actor_label='Synthetic operator',
                        new_status='acknowledged', note='Synthetic: acknowledged',
                        created_at=ack_at)
                    note_on_sos_transition(ev, 'acknowledged', None, 'Synthetic: acknowledged')
                    new_status = 'acknowledged'
                    end_at = ack_at
                    if status in ('resolved', 'false_alarm'):
                        end_at = ack_at + timedelta(minutes=rng.randint(4, 45))
                        SOSEventUpdate.objects.create(
                            event=ev, actor=None, actor_label='Synthetic operator',
                            new_status=status, note='Synthetic: closed', created_at=end_at)
                        note_on_sos_transition(ev, status, None, 'Synthetic: closed')
                        new_status = status
                    SOSEvent.objects.filter(pk=ev.pk).update(status=new_status)
                    if status in ('resolved', 'false_alarm') and ev.incident_id:
                        inc = SafetyIncident.objects.get(pk=ev.incident_id)
                        before = inc.status
                        SafetyIncident.objects.filter(pk=inc.pk).update(
                            status=CLOSED, closed_at=end_at,
                            outcome='no_action',
                            closing_summary='Synthetic: ' + ('false alarm' if status == 'false_alarm'
                                                             else 'resolved on contact'))
                        add_update(inc, U_CLOSED, label='Synthetic operator', from_status=before,
                                   to_status=CLOSED, note='Synthetic: closed')
                # Backdate: the row was written now; its history belongs in the past.
                SOSEvent.objects.filter(pk=ev.pk).update(created_at=ts)
                if ev.incident_id:
                    SafetyIncident.objects.filter(pk=ev.incident_id).update(opened_at=ts)
                    SafetyIncidentUpdate.objects.filter(incident_id=ev.incident_id).update(
                        created_at=ts + timedelta(seconds=30))
        self.sos_created = created
        self._end(f'({created} SOS: {n_open} open, rest acknowledged/resolved/false alarm)')

    # ---- support tickets ---------------------------------------------------

    def _build_tickets(self):
        from servers.support.models import SupportMessage, SupportTicket
        self._phase('support tickets')
        rng = self.rng
        now = self.now
        want = max(5, round(self.n_trips * 0.01))
        trips = self._sample_trips(want, 'completed')
        issues = [i for i, _ in D.TICKET_ISSUES]
        iw = [w for _, w in D.TICKET_ISSUES]
        sla = {'urgent': (15, 240), 'high': (60, 1440), 'normal': (240, 4320), 'low': (1440, 10080)}
        tickets = []
        with _real_timestamps(SupportTicket, SupportMessage):
            for trip in trips:
                c = self.zone_city[trip.zone_id]
                issue = rng.choices(issues, weights=iw)[0]
                by_driver = trip.driver_id_id is not None and rng.random() < 0.2
                uid = trip.driver_id.user_id_id if by_driver else trip.user_id_id
                created_at = min(trip.requested_at + timedelta(minutes=rng.randint(20, 1800)),
                                 now - timedelta(minutes=2))
                age_days = (now - created_at).days
                if issue == 'safety':
                    priority = rng.choice(['urgent', 'high'])
                else:
                    priority = rng.choices(['urgent', 'high', 'normal', 'low'],
                                           weights=[4, 14, 72, 10])[0]
                if age_days >= 4:
                    status = 'CLOSED' if rng.random() < 0.85 else rng.choice(['OPEN', 'IN_PROGRESS'])
                else:
                    status = rng.choices(['OPEN', 'IN_PROGRESS', 'WAITING_USER', 'CLOSED'],
                                         weights=[35, 22, 10, 33])[0]
                fr, rs = sla[priority]
                responded = None if status == 'OPEN' else created_at + timedelta(minutes=rng.randint(5, fr * 2))
                if responded and responded > now:
                    responded = now
                resolved = None
                if status == 'CLOSED':
                    resolved = min(created_at + timedelta(minutes=rng.randint(30, rs)), now)
                tickets.append(SupportTicket(
                    user_id_id=uid, trip_id_id=trip.pk if rng.random() < 0.9 else None,
                    issue_type=issue, status=status, priority=priority, country=c['cc'],
                    description=f'Synthetic ticket about {issue.replace("_", " ")} (trip {trip.pk})',
                    created_at=created_at, updated_at=resolved or responded or created_at,
                    resolved_at=resolved, first_responded_at=responded,
                    first_response_due_at=created_at + timedelta(minutes=fr),
                    resolution_due_at=created_at + timedelta(minutes=rs),
                    safety_flag=(issue == 'safety'),
                ))
            made = SupportTicket.objects.bulk_create(tickets, batch_size=TRIP_BATCH)
            msgs = [SupportMessage(ticket_id=t.pk, author_id=t.user_id_id, author_role='user',
                                   body=t.description, created_at=t.created_at)
                    for t in made]
            SupportMessage.everything.bulk_create(msgs, batch_size=TRIP_BATCH)
        self.tickets_created = len(made)
        self._end(f'({len(made)} tickets)')

    # ---- withdrawals -------------------------------------------------------

    def _build_withdrawals(self):
        from servers.driver.models import Driver, DriverBankAccount, WithdrawalRequest
        self._phase('withdrawals')
        rng = self.rng
        now = self.now
        eligible = [pk for ci in range(len(self.cities))
                    for pk, _u, _v, _t in self.approved_by_city[ci]]
        n_drivers = int(len(eligible) * 0.20)
        chosen = rng.sample(eligible, n_drivers)
        driver_city = {}
        for ci in range(len(self.cities)):
            for pk, _u, _v, _t in self.approved_by_city[ci]:
                driver_city[pk] = self.cities[ci]
        rows, banks, bank_done = [], [], set()
        statuses = (['completed'] * 52 + ['processed'] * 10 + ['pending'] * 12 + ['approved'] * 8
                    + ['rejected'] * 5 + ['failed'] * 5 + ['unresolved'] * 2 + ['reversed'] * 1
                    + ['conflict'] * 1)
        with _real_timestamps(WithdrawalRequest):
            for pk in chosen:
                c = driver_city[pk]
                for _ in range(rng.choices([1, 2, 3, 4], weights=[45, 30, 17, 8])[0]):
                    status = rng.choice(statuses)
                    if status in ('pending', 'approved', 'unresolved'):
                        age = rng.randint(0, 5 * 86400)
                    else:
                        age = rng.randint(0, 30 * 86400) if rng.random() < 0.5 else rng.randint(0, 10 * 86400)
                    if status == 'failed' and rng.random() < 0.35:
                        age = rng.randint(60, 14 * 3600)   # failed TODAY: the finance queue
                    requested = now - timedelta(seconds=age)
                    amount = (rng.randint(5, 80) * 10000 if c['cc'] == 'IN'
                              else rng.randint(2, 40) * 1000)
                    method = 'upi' if (c['cc'] == 'IN' and rng.random() < 0.7) else 'bank'
                    processed = None
                    if status in ('completed', 'processed', 'rejected', 'failed', 'reversed'):
                        processed = min(requested + timedelta(minutes=rng.randint(5, 600)), now)
                    rows.append(WithdrawalRequest(
                        driver_id=pk, amount_minor=amount, currency=c['currency'],
                        payout_method=method, status=status, requested_at=requested,
                        processed_at=processed, fee_minor=0 if method == 'upi' else
                        (1000 if c['cc'] == 'IN' else 100),
                        admin_notes={'failed': 'Synthetic: provider declined',
                                     'rejected': 'Synthetic: rejected by finance'}.get(status),
                        origin='driver'))
                    if method == 'bank' and pk not in bank_done:
                        bank_done.add(pk)
                        banks.append(DriverBankAccount(
                            driver_id=pk, bank_name='Synthetic Bank',
                            account_number=f'SYN{pk:012d}', ifsc_code='SYNT0000001'))
            WithdrawalRequest.objects.bulk_create(rows, batch_size=TRIP_BATCH)
            DriverBankAccount.objects.bulk_create(banks, batch_size=TRIP_BATCH)
        last = WithdrawalRequest.objects.filter(
            driver_id=OuterRef('pk'), status__in=('completed', 'processed')
        ).order_by().values('driver_id').annotate(m=Max('requested_at')).values('m')
        Driver.objects.filter(pk__in=chosen).update(last_withdrawal_at=Subquery(last))
        self.withdrawals_created = len(rows)
        self._end(f'({len(rows)} requests, {len(banks)} bank accounts)')

    # ---- operator alerts ---------------------------------------------------

    def _build_alerts(self):
        from servers.command_centre import models as cc_models
        from servers.command_centre.models import OperatorAlert
        self._phase('operator alerts')
        rng = self.rng
        now = self.now
        kinds = [
            (cc_models.SAFETY_SOS_ESCALATED, 'critical', 'SOS not acknowledged in time'),
            (cc_models.SAFETY_INCIDENT_UNASSIGNED, 'warning', 'Safety incident has no owner'),
            (cc_models.SAFETY_COVERAGE_GAP, 'warning', 'No safety officer covers this city'),
            (cc_models.FINANCE_PAYOUT_REVERSED, 'warning', 'Payout reversed by the bank'),
            (cc_models.FINANCE_PAYOUT_CONFLICT, 'critical', 'Payout paid after refund'),
            (cc_models.FRAUD_FLAG_HIGH, 'warning', 'High-risk fraud flag raised'),
        ]
        live = [c for c in self.cities if not c['paused']]
        total = 36
        rows = []
        with _real_timestamps(OperatorAlert):
            for i in range(total):
                kind, sev, title = rng.choice(kinds)
                c = rng.choices(live, weights=[x['weight'] for x in live])[0]
                is_open = i < 14
                created = now - timedelta(minutes=rng.randint(2, 180) if is_open
                                          else rng.randint(240, 6 * 24 * 60))
                ack = None if is_open else created + timedelta(minutes=rng.randint(3, 120))
                rows.append(OperatorAlert(
                    kind=kind, severity=sev, title=f'{title} ({c["name"]})',
                    payload={'synthetic': True}, source_type=ALERT_SOURCE, source_id=str(i + 1),
                    city_id=c['node'].pk, created_at=created, acknowledged_at=ack,
                    acknowledged_note='' if is_open else 'Synthetic: handled'))
            OperatorAlert.objects.bulk_create(rows)
        self._end(f'({len(rows)} alerts, 14 open)')

    # ---- report ------------------------------------------------------------

    def _report(self, seconds):
        from servers.admin_access.models import CITY, COUNTRY
        from servers.command_centre.models import OperatorAlert
        from servers.driver.models import Driver, DriverBankAccount, Vehicle, WithdrawalRequest
        from servers.pricing.models import ServiceZone
        from servers.rider.models import Rider
        from servers.ride.models import Trip
        from servers.safety.models import SafetyIncident
        from servers.sos.models import SOSEvent
        from servers.support.models import SupportTicket

        syn = '@' + SYN_DOMAIN
        counts = {
            'zones (synthetic)': ServiceZone.objects.filter(code__endswith=ZONE_SUFFIX).count(),
            'drivers': Driver.objects.filter(user_id__email__endswith=syn).count(),
            'vehicles': Vehicle.objects.filter(driver_id__user_id__email__endswith=syn).count(),
            'riders': Rider.objects.filter(user_id__email__endswith=syn).count(),
            'trips': Trip.objects.filter(client_request_id__startswith=TRIP_PREFIX).count(),
            'sos_events': SOSEvent.objects.filter(trip__client_request_id__startswith=TRIP_PREFIX).count(),
            'safety_incidents': SafetyIncident.objects.filter(
                trip__client_request_id__startswith=TRIP_PREFIX).count(),
            'support_tickets': SupportTicket.objects.filter(user_id__email__endswith=syn).count(),
            'withdrawals': WithdrawalRequest.objects.filter(
                driver__user_id__email__endswith=syn).count(),
            'bank_accounts': DriverBankAccount.objects.filter(
                driver__user_id__email__endswith=syn).count(),
            'operator_alerts': OperatorAlert.objects.filter(source_type=ALERT_SOURCE).count(),
        }
        self._p(f'row counts after {seconds:.0f}s of seeding:')
        for k, v in counts.items():
            self._p(f'  {k:22s} {v:>10,}')
        self.counts = counts
