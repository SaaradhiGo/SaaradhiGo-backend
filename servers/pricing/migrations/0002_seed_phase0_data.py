"""Seed Phase-0 zones + rate cards.

What this does:

1. Create the Hyderabad city zone with the same polygon currently
   hardcoded in base/service_area.py. The polygon stays a literal here
   (not imported) so that re-running the migration on a fresh DB works
   even after the legacy module is deleted in a future phase.

2. For each existing VehicleFarePricing row, create an equivalent
   RateCard scoped to the Hyderabad zone. This keeps the live fare
   schedule identical the moment the new lookup path is wired in.

3. If VehicleFarePricing is empty (fresh DB / CI), seed sensible
   Phase-0 defaults so /estimate-fare/ still produces a number.
"""

from decimal import Decimal

from django.db import migrations


# Same vertices as base/service_area.py:_HYDERABAD_POLYGON_LONLAT.
HYDERABAD_POLYGON = [
    [78.205, 17.190],
    [78.155, 17.380],
    [78.230, 17.560],
    [78.420, 17.620],
    [78.580, 17.580],
    [78.700, 17.480],
    [78.730, 17.310],
    [78.680, 17.180],
    [78.470, 17.140],
    [78.330, 17.155],
    [78.205, 17.190],  # close the ring
]


PHASE0_DEFAULTS = [
    # (vehicle_type, base, per_km, per_min, min_fare, commission%)
    ('auto', Decimal('30.00'), Decimal('12.00'), Decimal('1.50'), Decimal('40.00'), Decimal('18.00')),
    ('hatchback', Decimal('45.00'), Decimal('14.00'), Decimal('2.00'), Decimal('70.00'), Decimal('18.00')),
    ('sedan', Decimal('60.00'), Decimal('17.00'), Decimal('2.50'), Decimal('100.00'), Decimal('20.00')),
    ('suv', Decimal('100.00'), Decimal('23.00'), Decimal('3.00'), Decimal('150.00'), Decimal('20.00')),
]


def seed_forward(apps, schema_editor):
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    RateCard = apps.get_model('pricing', 'RateCard')
    VehicleType = apps.get_model('driver', 'VehicleType')
    try:
        VehicleFarePricing = apps.get_model('ride', 'VehicleFarePricing')
    except LookupError:
        VehicleFarePricing = None

    zone, _ = ServiceZone.objects.update_or_create(
        code='IN-TG-HYD',
        defaults={
            'name': 'Hyderabad',
            'country': 'IN',
            'state_code': 'TG',
            'city': 'Hyderabad',
            'zone_type': 'city',
            'parent': None,
            'polygon_geojson': {
                'type': 'Polygon',
                'coordinates': [HYDERABAD_POLYGON],
            },
            'priority': 10,
            'currency': 'INR',
            'timezone_name': 'Asia/Kolkata',
            'is_active': True,
            'metadata': {
                'launch_phase': 'phase-0',
                'mva_2020_compliance': True,
            },
        },
    )

    seeded_from_existing = False
    if VehicleFarePricing is not None:
        for vfp in VehicleFarePricing.objects.all():
            vt = vfp.vehicle_type_id
            RateCard.objects.update_or_create(
                zone=zone,
                vehicle_type=vt,
                version=1,
                defaults={
                    'base_fare': vfp.base_fare,
                    'per_km_fare': vfp.per_km_fare,
                    'per_min_fare': vfp.per_min_fare,
                    'min_fare': vfp.min_fare,
                    'night_surge_multiplier': vfp.night_surge_multiplier,
                    'night_surge_start_hour': 23,
                    'night_surge_end_hour': 5,
                    'surge_cap_multiplier': Decimal('1.50'),
                    'commission_percent': Decimal('18.00'),
                    'gst_percent': Decimal('5.00'),
                    'is_active': True,
                    'notes': 'Imported from legacy VehicleFarePricing during Phase-0 migration.',
                },
            )
            seeded_from_existing = True

    if not seeded_from_existing:
        for vt_name, base, per_km, per_min, min_fare, commission in PHASE0_DEFAULTS:
            vt, _ = VehicleType.objects.get_or_create(type=vt_name)
            RateCard.objects.update_or_create(
                zone=zone,
                vehicle_type=vt,
                version=1,
                defaults={
                    'base_fare': base,
                    'per_km_fare': per_km,
                    'per_min_fare': per_min,
                    'min_fare': min_fare,
                    'night_surge_multiplier': Decimal('1.25'),
                    'night_surge_start_hour': 23,
                    'night_surge_end_hour': 5,
                    'surge_cap_multiplier': Decimal('1.50'),
                    'commission_percent': commission,
                    'gst_percent': Decimal('5.00'),
                    'is_active': True,
                    'notes': 'Phase-0 default seed (no legacy VehicleFarePricing rows found).',
                },
            )


def seed_reverse(apps, schema_editor):
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    RateCard = apps.get_model('pricing', 'RateCard')
    RateCard.objects.filter(zone__code='IN-TG-HYD').delete()
    ServiceZone.objects.filter(code='IN-TG-HYD').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0001_initial'),
        ('driver', '0014_vehicle_expiries'),
        ('ride', '0006_alter_trip_status_id'),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_reverse),
    ]
