"""Seed the paused US city with labelled test data, so cross-country scope
can be checked on QA.

    python manage.py seed_us_test_city --actor-email you@saaradhigo.com
    python manage.py seed_us_test_city --remove --actor-email you@saaradhigo.com

THE RULING (owner, 2026-10-28)
------------------------------
    Cross-country check: QA has no US data, so seed the paused US city
    (clearly labelled seed) with a couple of test drivers and trips, then
    confirm a Hyderabad-scoped role can't see them (404, never 403) and
    Super Admin can.

WHAT IT CREATES, AND WHY EACH ROW IS WHERE SCOPE LOOKS
------------------------------------------------------
Scope is decided by `servers.admin_access.scoping.SCOPE_PATHS`:

  * a TRIP is scoped by `zone__geo_node__path`, so each seeded trip carries
    `zone=<the Austin ServiceZone>`, and that zone carries
    `geo_node=<GeoNode US-TX-AUS>`;
  * a DRIVER is scoped by `home_city__path`, so each seeded driver carries
    `home_city=<GeoNode US-TX-AUS>`.

A rider has no anchor by design (`RIDERS_ARE_GLOBAL`), so the seeded rider
is reachable only through the trips.

Rows, all found again by fixed keys so a second run creates nothing:

  * one `ServiceZone` `US-TX-AUS` named "Austin (QA seed)", `is_active`
    False -- the city is paused, and an inactive zone is never offered by
    `find_zone_for_point`, so no real rider can be quoted in it by accident;
  * two drivers and one rider, `+1 512 555 01xx` numbers (the NANP range
    reserved for fiction), `@saaradhigo.test` addresses, "(QA US seed)" in
    the name, no usable password;
  * one sedan per driver, so the driver list and the trip's vehicle card
    have something to show;
  * two trips in the Austin zone, one completed and one cancelled, with
    Austin coordinates and USD fares in integer cents, plus a fare breakdown
    on the completed one so the trip detail's fare card renders.

No rate card: nothing in the trip list or detail needs one, and a rate card
in an inactive zone is one more row to explain.

THE GEO NODE MUST ALREADY EXIST
-------------------------------
`seed_admin_access` owns the geo tree. This command links to the node it
finds and refuses if `US-TX-AUS` is missing, rather than growing a second
place that writes geo nodes.

WHO RAN IT
----------
`--actor-email` is required (`admin_audit.actor_option`): the run writes
driver status (`approved=True`), which is the owner's rule for the flag.
There is no unattended fallback -- this is a one-off a person runs for a
QA check, not a deploy step. One summary audit row per run lists what was
created, by id; the rows themselves are not access rows, so the model-layer
receivers write nothing for them.

--remove
--------
Deletes only what this seed created, found by the same fixed keys, and
refuses -- before deleting anything -- if any row this seed did not create
points at one of them: a trip somebody booked against the Austin zone, a
withdrawal raised on a seed driver, a vehicle added to one. Every reverse
relation is checked, whatever its `on_delete`, because `DO_NOTHING` on
`Trip.driver_id` means the database would refuse mid-delete otherwise, and
`SET_NULL` on `Trip.zone` means a stranger's trip would silently lose its
zone. One summary audit row per run lists what was removed.
"""

import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from base.seeding import require_seedable_environment
from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action

COMMAND = 'seed_us_test_city'
SEED_ACTION = 'us_test_city_seeded'
REMOVE_ACTION = 'us_test_city_seed_removed'
TARGET_TYPE = 'us_test_city_seed'

#: The label on every human-readable field, so a reader of the console or
#: of a database dump cannot mistake a seed row for a real one.
LABEL = 'QA US seed'

ZONE_CODE = 'US-TX-AUS'
ZONE_NAME = f'Austin ({LABEL})'
#: A loose box around Austin, TX: [lon, lat] vertices as GeoJSON wants.
AUSTIN_POLYGON = {
    'type': 'Polygon',
    'coordinates': [[
        [-97.95, 30.12], [-97.55, 30.12], [-97.55, 30.52], [-97.95, 30.52],
        [-97.95, 30.12],
    ]],
}

#: `+1 512 555 01xx`: 512 is Austin's area code, and 555-01xx is the block
#: the North American Numbering Plan reserves for fiction. These numbers
#: cannot belong to anybody.
DRIVERS = [
    {'phone': '+15125550101', 'email': 'qa.us.driver1@saaradhigo.test',
     'name': f'Alex Driver One ({LABEL})', 'plate': 'QAUS0001',
     'brand': 'Toyota', 'model': 'Camry', 'color': 'White', 'year': 2021},
    {'phone': '+15125550102', 'email': 'qa.us.driver2@saaradhigo.test',
     'name': f'Blair Driver Two ({LABEL})', 'plate': 'QAUS0002',
     'brand': 'Honda', 'model': 'Accord', 'color': 'Silver', 'year': 2020},
]
RIDER = {'phone': '+15125550199', 'email': 'qa.us.rider1@saaradhigo.test',
         'name': f'Casey Rider ({LABEL})'}

#: Trips are found again by `client_request_id`, the booking idempotency
#: key, which is unique per rider. Fares are integer USD cents.
TRIPS = [
    {
        'key': 'qa-us-seed-trip-completed',
        'status': 'completed',
        'driver': 0,
        'pickup': ('Texas State Capitol, 1100 Congress Ave, Austin, TX 78701',
                   Decimal('30.2747000'), Decimal('-97.7404000')),
        'drop': ('Austin-Bergstrom International Airport, Austin, TX 78719',
                 Decimal('30.1975000'), Decimal('-97.6664000')),
        'estimated_fare_minor': 2450,          # $24.50
        'final_fare_minor': 2450,
        'estimated_distance_km': Decimal('13.20'),
        'actual_distance_km': Decimal('13.50'),
        'estimated_duration_min': Decimal('22.00'),
        'actual_duration_min': Decimal('24.00'),
        'payment_method': 'cash',
        'payment_status': 'completed',
        'breakdown': {'base_fare_minor': 250, 'distance_fare_minor': 1640,
                      'time_fare_minor': 560, 'total_fare_minor': 2450},
    },
    {
        'key': 'qa-us-seed-trip-cancelled',
        'status': 'cancelled',
        'driver': 1,
        'pickup': ('University of Texas at Austin, 110 Inner Campus Dr, Austin, TX 78712',
                   Decimal('30.2849000'), Decimal('-97.7341000')),
        'drop': ('Zilker Park, 2100 Barton Springs Rd, Austin, TX 78746',
                 Decimal('30.2669000'), Decimal('-97.7729000')),
        'estimated_fare_minor': 1875,          # $18.75
        'final_fare_minor': None,
        'estimated_distance_km': Decimal('4.60'),
        'actual_distance_km': None,
        'estimated_duration_min': Decimal('12.00'),
        'actual_duration_min': None,
        'payment_method': 'cash',
        'payment_status': 'cancelled',
        'breakdown': None,
    },
]
VEHICLE_TYPE = 'sedan'

#: The kinds of row this seed owns, in the order `--remove` deletes them:
#: trips first, because `Trip.driver_id` and `Trip.vehicle_id` are
#: `DO_NOTHING` and the database would refuse the driver's delete while a
#: trip still points at it; users last but one, because a user's delete
#: cascades to the rider, the driver and the driver's vehicles; the zone
#: last, because trips point at it.
KINDS = ('fare_pricing', 'trips', 'vehicles', 'drivers', 'rider', 'users', 'zone')


class Command(BaseCommand):
    help = (
        'Seed the paused US city (Austin) with labelled test drivers and '
        'trips for the cross-country scope check. QA/dev only. --remove '
        'deletes exactly what this created.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--remove', action='store_true',
            help='Delete the rows this seed created, and nothing else. '
                 'Refuses if any other row references them.',
        )
        add_actor_argument(
            parser,
            help='Email of the operator running this. Required: the seed '
                 'creates approved drivers, and the summary audit row names '
                 'whoever did it.',
        )

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        what = 'the US test city seed'
        # The shared gate first, then the same production refusal as
        # `seed_admin_access --test-admins`: both before any query.
        require_seedable_environment(what)
        self._refuse_in_production()

        # Before the first write.
        actor = require_actor(options, COMMAND)

        if options['remove']:
            self._remove(actor)
        else:
            self._seed(actor)

    def _refuse_in_production(self):
        if getattr(settings, 'IS_PRODUCTION', False):
            raise CommandError(
                'Refusing to seed the US test city in production. These are '
                'labelled test drivers and trips; they exist so cross-country '
                'scope can be checked on QA.'
            )
        env = os.environ.get('ENVIRONMENT', '').strip().lower()
        if env in {'prod', 'production'}:
            raise CommandError(
                'Refusing to seed the US test city: ENVIRONMENT is production.'
            )

    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------

    def _seed(self, actor):
        from servers.admin_access.models import GeoNode

        node = GeoNode.objects.filter(code=ZONE_CODE).first()
        if node is None:
            raise CommandError(
                f'GeoNode {ZONE_CODE!r} does not exist. The geo tree is owned '
                f'by seed_admin_access; run that first. Nothing was changed.'
            )

        reason = (f'Seed: {LABEL}. Labelled test drivers and trips in the '
                  f'paused US city, for the cross-country scope check.')
        with attributed(actor, COMMAND, reason=reason):
            with transaction.atomic():
                created, present = self._write_rows(node)
                self._record(SEED_ACTION, created=created, already_present=present)

        self._report('created', created, present)

    def _write_rows(self, node):
        """Find-or-create every row. Returns `(created, already_present)`,
        each `{kind: [ids]}`."""
        from django.contrib.auth import get_user_model

        from servers.driver.models import Driver, Vehicle, VehicleType
        from servers.pricing.models import ServiceZone
        from servers.ride.models import FarePricing, Trip, TripStatus
        from servers.rider.models import Rider

        user_model = get_user_model()
        created = {kind: [] for kind in KINDS}
        present = {kind: [] for kind in KINDS}

        def note(kind, obj, was_created):
            (created if was_created else present)[kind].append(obj.pk)
            return obj

        zone = ServiceZone.objects.filter(code=ZONE_CODE).first()
        if zone is None:
            zone = ServiceZone.objects.create(
                code=ZONE_CODE, name=ZONE_NAME, country='US', state_code='TX',
                city='Austin', zone_type='city', polygon_geojson=AUSTIN_POLYGON,
                priority=10,
                # Texas is Central; the country default is one of six.
                timezone_name='America/Chicago',
                # Paused: never offered to a real quote.
                is_active=False, geo_node=node,
                metadata={'seed': COMMAND, 'label': LABEL},
            )
            note('zone', zone, True)
        else:
            note('zone', zone, False)

        def user(spec, role):
            account, was_created = user_model.objects.get_or_create(
                phone_number=spec['phone'],
                defaults={
                    'username': spec['phone'], 'email': spec['email'],
                    'full_name': spec['name'], 'role': role, 'city': 'Austin',
                },
            )
            # Never signed into: these exist to be LOOKED AT by scoped
            # operators, not to be used.
            if account.has_usable_password():
                account.set_unusable_password()
                account.save(update_fields=['password'])
            return note('users', account, was_created)

        vehicle_type, _ = VehicleType.objects.get_or_create(type=VEHICLE_TYPE)

        drivers, vehicles = [], []
        for spec in DRIVERS:
            account = user(spec, 'driver')
            driver, was_created = Driver.objects.get_or_create(
                user_id=account,
                defaults={
                    'home_city': node, 'approved': True,
                    'doc_status': 'approved', 'status': 'off',
                    'ratings': Decimal('4.80'),
                },
            )
            note('drivers', driver, was_created)
            vehicle = Vehicle.objects.filter(
                driver_id=driver, vehicle_number=spec['plate']).first()
            if vehicle is None:
                vehicle = Vehicle.objects.create(
                    driver_id=driver, vehicle_type_id=vehicle_type,
                    vehicle_number=spec['plate'], brand=spec['brand'],
                    model=spec['model'], color=spec['color'],
                    year=spec['year'], capacity=4,
                )
                note('vehicles', vehicle, True)
            else:
                note('vehicles', vehicle, False)
            if driver.active_vehicle_id is None:
                driver.active_vehicle = vehicle
                driver.save(update_fields=['active_vehicle'])
            drivers.append(driver)
            vehicles.append(vehicle)

        rider_account = user(RIDER, 'rider')
        rider, was_created = Rider.objects.get_or_create(user_id=rider_account)
        note('rider', rider, was_created)

        now = timezone.now()
        for index, spec in enumerate(TRIPS):
            trip = Trip.objects.filter(
                user_id=rider_account, client_request_id=spec['key']).first()
            if trip is not None:
                note('trips', trip, False)
                breakdown = FarePricing.objects.filter(trip_id=trip).first()
                if breakdown is not None:
                    note('fare_pricing', breakdown, False)
                continue

            status, _ = TripStatus.objects.get_or_create(status_code=spec['status'])
            driver = drivers[spec['driver']]
            # Yesterday-ish, one hour apart, so the list orders them stably.
            requested_at = now - timedelta(days=1, hours=index)
            pickup_address, pickup_lat, pickup_lng = spec['pickup']
            drop_address, drop_lat, drop_lng = spec['drop']
            fields = {
                'user_id': rider_account, 'driver_id': driver,
                'vehicle_id': vehicles[spec['driver']],
                'requested_vehicle_type': vehicle_type, 'status_id': status,
                'zone': zone, 'currency': zone.currency,
                'client_request_id': spec['key'],
                'pickup_address': pickup_address,
                'pickup_lat': pickup_lat, 'pickup_long': pickup_lng,
                'destination_address': drop_address,
                'destination_lat': drop_lat, 'destination_long': drop_lng,
                'estimated_fare_minor': spec['estimated_fare_minor'],
                'final_fare_minor': spec['final_fare_minor'],
                'estimated_distance_km': spec['estimated_distance_km'],
                'actual_distance_km': spec['actual_distance_km'],
                'estimated_duration_min': spec['estimated_duration_min'],
                'actual_duration_min': spec['actual_duration_min'],
                'payment_method': spec['payment_method'],
                'payment_status': spec['payment_status'],
                'accepted_at': requested_at + timedelta(minutes=1),
            }
            if spec['status'] == 'completed':
                fields.update({
                    'reached_at': requested_at + timedelta(minutes=6),
                    'started_at': requested_at + timedelta(minutes=8),
                    'completed_at': requested_at + timedelta(minutes=32),
                })
            else:
                fields.update({
                    'cancelled_at': requested_at + timedelta(minutes=3),
                    'cancelled_by': 'rider',
                    'cancellation_reason': f'{LABEL}: rider cancelled after accept',
                })
            trip = Trip.objects.create(**fields)
            # `requested_at` is auto_now_add, so it is backdated afterwards.
            Trip.objects.filter(pk=trip.pk).update(requested_at=requested_at)
            note('trips', trip, True)

            if spec['breakdown']:
                breakdown = FarePricing.objects.create(
                    trip_id=trip, currency=zone.currency,
                    surge_multiplier=Decimal('1.00'),
                    snapshot_type=FarePricing.SNAPSHOT_FINAL,
                    finalized_at=fields['completed_at'],
                    quoted_distance_km=spec['estimated_distance_km'],
                    quoted_duration_min=spec['estimated_duration_min'],
                    actual_distance_km=spec['actual_distance_km'],
                    actual_duration_min=spec['actual_duration_min'],
                    zone_code=ZONE_CODE, vehicle_type=VEHICLE_TYPE,
                    pricing_source=LABEL[:16],
                    **spec['breakdown'],
                )
                note('fare_pricing', breakdown, True)

        # The counter the console shows beside the driver.
        for driver in drivers:
            completed = Trip.objects.filter(
                driver_id=driver, status_id__status_code='completed').count()
            if driver.total_trips != completed:
                driver.total_trips = completed
                driver.save(update_fields=['total_trips'])

        return created, present

    # ------------------------------------------------------------------
    # Remove
    # ------------------------------------------------------------------

    def _remove(self, actor):
        rows = find_seed_rows()
        found = {kind: [obj.pk for obj in objs] for kind, objs in rows.items()}

        reason = f'Removed: {LABEL}. The labelled test rows in the paused US city.'
        with attributed(actor, COMMAND, reason=reason):
            with transaction.atomic():
                if any(found.values()):
                    strangers = foreign_references(rows)
                    if strangers:
                        raise CommandError(
                            f'Refusing to remove the {LABEL} rows: '
                            f'{len(strangers)} row(s) this seed did not create '
                            f'point at them. Nothing was deleted.\n  '
                            + '\n  '.join(strangers[:20])
                            + ('\n  ...' if len(strangers) > 20 else '')
                        )
                    for kind in KINDS:
                        for obj in rows[kind]:
                            obj.delete()
                self._record(REMOVE_ACTION, removed=found)

        self._report('removed', found, {})

    # ------------------------------------------------------------------
    # The row, and the summary
    # ------------------------------------------------------------------

    def _record(self, action, **after):
        after['environment'] = getattr(settings, 'ENVIRONMENT', '')
        after['label'] = LABEL
        row = record_admin_action(
            None, action=action, target_type=TARGET_TYPE, target_id=ZONE_CODE,
            before={}, after=after,
            reason=f'{COMMAND}: one summary row per run; ids by kind in `after`.',
        )
        if row is None:
            self.stderr.write(self.style.WARNING(
                'the summary audit row could not be written; see the log'))

    def _report(self, verb, changed, present):
        total = sum(len(ids) for ids in changed.values())
        if total == 0:
            if verb == 'created':
                self.stdout.write(self.style.SUCCESS(
                    f'nothing created: the {LABEL} rows are already present '
                    f'({_describe(present)})'))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f'nothing removed: no {LABEL} rows found'))
            return
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {total} {LABEL} row(s): {_describe(changed)}'))
        if verb == 'created' and any(present.values()):
            self.stdout.write(f'  already present: {_describe(present)}')


def _describe(ids_by_kind):
    parts = [f'{kind} {ids}' for kind, ids in ids_by_kind.items() if ids]
    return ', '.join(parts) if parts else 'nothing'


# ----------------------------------------------------------------------
# Finding the seed again
# ----------------------------------------------------------------------

def find_seed_rows():
    """Every row this seed owns, by its fixed keys: `{kind: [instances]}`.

    Module-level so a test can ask the same question the command does.
    """
    from django.contrib.auth import get_user_model

    from servers.driver.models import Driver, Vehicle
    from servers.pricing.models import ServiceZone
    from servers.ride.models import FarePricing, Trip
    from servers.rider.models import Rider

    user_model = get_user_model()
    phones = [spec['phone'] for spec in DRIVERS] + [RIDER['phone']]
    users = list(user_model.objects.filter(phone_number__in=phones).order_by('pk'))
    drivers = list(Driver.objects.filter(user_id__in=users).order_by('pk'))
    plates = [spec['plate'] for spec in DRIVERS]
    vehicles = list(Vehicle.objects.filter(
        driver_id__in=drivers, vehicle_number__in=plates).order_by('pk'))
    riders = list(Rider.objects.filter(user_id__in=users).order_by('pk'))
    trips = list(Trip.objects.filter(
        user_id__in=users,
        client_request_id__in=[spec['key'] for spec in TRIPS],
    ).order_by('pk'))
    fare_pricing = list(FarePricing.objects.filter(trip_id__in=trips).order_by('pk'))
    zone = list(ServiceZone.objects.filter(code=ZONE_CODE))
    return {
        'fare_pricing': fare_pricing, 'trips': trips, 'vehicles': vehicles,
        'drivers': drivers, 'rider': riders, 'users': users, 'zone': zone,
    }


def foreign_references(rows):
    """Rows NOT in `rows` that point at a row in `rows`, as descriptions.

    Walks every reverse relation of every seed row, whatever its
    `on_delete`: a CASCADE would silently take the stranger with it, a
    SET_NULL would silently detach it, a PROTECT would raise mid-delete and
    a DO_NOTHING would fail at the database. All four are the same answer
    here -- refuse, and say what is in the way.
    """
    own = {(type(obj)._meta.label, obj.pk) for objs in rows.values() for obj in objs}
    strangers = []
    for objs in rows.values():
        for obj in objs:
            for rel in obj._meta.related_objects:
                model = rel.related_model
                for other in model._default_manager.filter(**{rel.field.name: obj}):
                    if (model._meta.label, other.pk) in own:
                        continue
                    strangers.append(
                        f'{model._meta.label} #{other.pk} ({rel.field.name}) -> '
                        f'{type(obj)._meta.label} #{obj.pk}'
                    )
    return strangers
