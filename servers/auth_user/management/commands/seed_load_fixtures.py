"""Create riders, drivers and pricing for a load run, and emit their tokens.

    python manage.py seed_load_fixtures --riders 50 --drivers 50 --json

REFUSES OUTSIDE A LOAD STACK
----------------------------
It creates approved drivers and hands out access tokens, so it must never run
anywhere real. It refuses unless `ENVIRONMENT` is exactly `load`, which the load
compose file sets and no other environment does. That is a stricter test than "not
production": QA is also somewhere this should never run.

WHY IT MINTS TOKENS INSTEAD OF LETTING THE HARNESS LOG IN
--------------------------------------------------------
Two reasons, and both are about keeping the measurement honest.

Logging in fifty riders and fifty drivers through the SMS bypass would need a
hundred `TEST_PHONE_NUMBERS` entries baked into the container, and it would put a
hundred OTP round-trips into a measurement that is supposed to be about the ride
lifecycle. Auth latency is not what §13 asks for, and it has its own suites.

So the load run measures booking, dispatch, commands and GPS -- not sign-in. The
harness says so in its own output rather than leaving a reader to assume it covered
everything.

Tokens come from `AccessToken.for_user`, the same machinery the login view uses, so
they are real tokens rather than a hand-rolled approximation of one.
"""

import json
import os
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from rest_framework_simplejwt.tokens import AccessToken

User = get_user_model()

# Hyderabad, matching the polygon the other fixtures use so quotes resolve.
P_LAT, P_LNG = 17.4450000, 78.3800000
POLYGON = {
    'type': 'Polygon',
    'coordinates': [[
        [78.30, 17.40], [78.50, 17.40], [78.50, 17.50], [78.30, 17.50],
        [78.30, 17.40],
    ]],
}


class Command(BaseCommand):
    help = 'Seed a load-test stack with riders, drivers, a zone and a rate card.'

    def add_arguments(self, parser):
        parser.add_argument('--riders', type=int, default=50)
        parser.add_argument('--drivers', type=int, default=50)
        parser.add_argument('--json', action='store_true',
                            help='Emit the token map as JSON on stdout.')

    def handle(self, *args, **options):
        env = (os.environ.get('ENVIRONMENT') or '').strip().lower()
        if env != 'load':
            raise CommandError(
                f'refusing to run: ENVIRONMENT is {env!r}, not "load". This '
                f'command creates approved drivers and issues access tokens, so '
                f'it must only ever run against a disposable load stack.'
            )

        from servers.driver.models import Driver, Vehicle, VehicleType
        from servers.pricing.models import RateCard, ServiceZone
        from servers.rider.models import Rider
        from servers.ride.models import TripStatus

        n_riders = options['riders']
        n_drivers = options['drivers']

        with transaction.atomic():
            for code in ('requested', 'accepted', 'reached', 'in_progress',
                         'completed', 'cancelled'):
                TripStatus.objects.get_or_create(status_code=code)

            vt, _ = VehicleType.objects.get_or_create(type='sedan')

            zone, _ = ServiceZone.objects.get_or_create(
                code='IN-TG-HYD-LOAD',
                defaults={
                    'name': 'Hyderabad load', 'zone_type': 'city',
                    'city': 'Hyderabad', 'polygon_geojson': POLYGON,
                    'priority': 10, 'is_active': True,
                },
            )
            RateCard.objects.get_or_create(
                zone=zone, vehicle_type=vt,
                defaults={
                    'base_fare': Decimal('30'), 'per_km_fare': Decimal('12'),
                    'per_min_fare': Decimal('2'), 'min_fare': Decimal('50'),
                    'commission_percent': Decimal('20.00'),
                },
            )

            riders = []
            for i in range(n_riders):
                phone = f'+9190000{i:05d}'
                u, created = User.objects.get_or_create(
                    phone_number=phone,
                    defaults={'role': 'rider', 'username': phone},
                )
                if created or not Rider.objects.filter(user_id=u).exists():
                    Rider.objects.get_or_create(user_id=u)
                riders.append({'phone': phone, 'id': u.id,
                               'token': str(AccessToken.for_user(u))})

            drivers = []
            for i in range(n_drivers):
                phone = f'+9195000{i:05d}'
                u, created = User.objects.get_or_create(
                    phone_number=phone,
                    defaults={'role': 'driver', 'username': phone},
                )
                d, _ = Driver.objects.get_or_create(
                    user_id=u,
                    defaults={'approved': True, 'status': 'online'},
                )
                if not d.approved:
                    d.approved = True
                    d.save(update_fields=['approved'])
                if d.active_vehicle_id is None:
                    v = Vehicle.objects.create(
                        driver_id=d, vehicle_type_id=vt,
                        vehicle_number=f'TS09LT{i:04d}')
                    d.active_vehicle = v
                    d.save(update_fields=['active_vehicle'])
                drivers.append({'phone': phone, 'id': u.id, 'driver_id': d.id,
                                'token': str(AccessToken.for_user(u))})

        payload = {'riders': riders, 'drivers': drivers,
                   'pickup': [P_LAT, P_LNG], 'vehicle_type': 'sedan'}

        if options['json']:
            # Only the JSON, so the harness can parse stdout directly.
            self.stdout.write(json.dumps(payload))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'seeded {len(riders)} riders, {len(drivers)} approved drivers, '
                f'1 zone, 1 rate card'))
