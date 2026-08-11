"""Seed three additional Phase-0 launch cities.

Adds ServiceZone + RateCard rows for:

  IN-AP-VJA  Vijayawada (Krishna district, AP) -- polygon covers
             Vijayawada city + Tadepalle + Mangalagiri up to Penamaluru
             on the east bank of the Krishna.

  IN-TG-WGL  Warangal-Hanamkonda metro (Warangal Urban district, TG) --
             polygon covers Warangal + Hanamkonda + Kazipet up to
             Mamnoor and Bheempalli.

  IN-AP-VTZ  Visakhapatnam metro (AP) -- polygon covers the urban
             corridor from Anandapuram through MVP Colony, Dwaraka
             Nagar, RK Beach, down through Gajuwaka to Duvvada.

Polygons are intentionally generous convex hulls so legitimate metro
suburbs are inside; tighten via the admin API later if we see fares
quoted to clearly out-of-zone destinations.

Rate cards mirror the Hyderabad seed schedule with minor regional
adjustments (lower demand cities -> lower base fares for auto +
hatchback to be competitive vs. local operators; sedan + SUV
unchanged because they target a more uniform market segment).
"""
from decimal import Decimal

from django.db import migrations


VJA_POLYGON = [
    [80.560, 16.470],
    [80.530, 16.520],
    [80.580, 16.580],
    [80.665, 16.605],
    [80.730, 16.575],
    [80.760, 16.500],
    [80.730, 16.430],
    [80.660, 16.405],
    [80.595, 16.430],
    [80.560, 16.470],  # close ring
]

WGL_POLYGON = [
    [79.510, 17.940],
    [79.485, 18.000],
    [79.535, 18.060],
    [79.620, 18.080],
    [79.690, 18.050],
    [79.720, 17.980],
    [79.695, 17.910],
    [79.625, 17.880],
    [79.555, 17.895],
    [79.510, 17.940],
]

VTZ_POLYGON = [
    [83.150, 17.620],
    [83.115, 17.700],
    [83.165, 17.770],
    [83.235, 17.800],
    [83.305, 17.770],
    [83.345, 17.700],
    [83.330, 17.620],
    [83.280, 17.560],
    [83.215, 17.555],
    [83.165, 17.580],
    [83.150, 17.620],
]


PHASE0_VARIANT_DEFAULTS = {
    'IN-AP-VJA': {
        'name': 'Vijayawada', 'state_code': 'AP', 'city': 'Vijayawada',
        'polygon': VJA_POLYGON,
        # Tier-2 city; slightly lower auto/hatchback base.
        'rate_overrides': {
            'auto':       (Decimal('25.00'), Decimal('11.00'), Decimal('1.50'), Decimal('35.00')),
            'hatchback':  (Decimal('40.00'), Decimal('13.00'), Decimal('1.50'), Decimal('60.00')),
            'sedan':      (Decimal('60.00'), Decimal('17.00'), Decimal('2.50'), Decimal('100.00')),
            'suv':        (Decimal('100.00'), Decimal('23.00'), Decimal('3.00'), Decimal('150.00')),
        },
    },
    'IN-TG-WGL': {
        'name': 'Warangal', 'state_code': 'TG', 'city': 'Warangal',
        'polygon': WGL_POLYGON,
        'rate_overrides': {
            'auto':       (Decimal('25.00'), Decimal('11.00'), Decimal('1.50'), Decimal('35.00')),
            'hatchback':  (Decimal('40.00'), Decimal('13.00'), Decimal('1.50'), Decimal('60.00')),
            'sedan':      (Decimal('55.00'), Decimal('16.00'), Decimal('2.50'), Decimal('95.00')),
            'suv':        (Decimal('95.00'), Decimal('22.00'), Decimal('3.00'), Decimal('140.00')),
        },
    },
    'IN-AP-VTZ': {
        'name': 'Visakhapatnam', 'state_code': 'AP', 'city': 'Visakhapatnam',
        'polygon': VTZ_POLYGON,
        # Coastal Tier-1; pricing matches Hyderabad apart from SUV +5%
        # to reflect airport-corridor distance norms.
        'rate_overrides': {
            'auto':       (Decimal('30.00'), Decimal('12.00'), Decimal('1.50'), Decimal('40.00')),
            'hatchback':  (Decimal('45.00'), Decimal('14.00'), Decimal('2.00'), Decimal('70.00')),
            'sedan':      (Decimal('60.00'), Decimal('17.00'), Decimal('2.50'), Decimal('100.00')),
            'suv':        (Decimal('105.00'), Decimal('24.00'), Decimal('3.00'), Decimal('155.00')),
        },
    },
}


def seed_forward(apps, schema_editor):
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    RateCard = apps.get_model('pricing', 'RateCard')
    VehicleType = apps.get_model('driver', 'VehicleType')

    for code, conf in PHASE0_VARIANT_DEFAULTS.items():
        zone, _ = ServiceZone.objects.update_or_create(
            code=code,
            defaults={
                'name': conf['name'],
                'country': 'IN',
                'state_code': conf['state_code'],
                'city': conf['city'],
                'zone_type': 'city',
                'parent': None,
                'polygon_geojson': {
                    'type': 'Polygon',
                    'coordinates': [conf['polygon']],
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
        for vt_name, (base, per_km, per_min, min_fare) in conf['rate_overrides'].items():
            vt, _ = VehicleType.objects.get_or_create(type=vt_name)
            RateCard.objects.update_or_create(
                zone=zone, vehicle_type=vt, version=1,
                defaults={
                    'base_fare': base,
                    'per_km_fare': per_km,
                    'per_min_fare': per_min,
                    'min_fare': min_fare,
                    'night_surge_multiplier': Decimal('1.25'),
                    'night_surge_start_hour': 23,
                    'night_surge_end_hour': 5,
                    'surge_cap_multiplier': Decimal('1.50'),
                    'commission_percent': Decimal('20.00') if vt_name in ('sedan', 'suv') else Decimal('18.00'),
                    'gst_percent': Decimal('5.00'),
                    'is_active': True,
                    'notes': f'Phase-0 seed for {conf["name"]}.',
                },
            )


def seed_reverse(apps, schema_editor):
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    RateCard = apps.get_model('pricing', 'RateCard')
    codes = ['IN-AP-VJA', 'IN-TG-WGL', 'IN-AP-VTZ']
    RateCard.objects.filter(zone__code__in=codes).delete()
    ServiceZone.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0003_alter_ratecard_id_alter_servicezone_code_and_more'),
        ('driver', '0015_driver_fatigue_lockout_until_drivercancellation_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_forward, seed_reverse),
    ]
