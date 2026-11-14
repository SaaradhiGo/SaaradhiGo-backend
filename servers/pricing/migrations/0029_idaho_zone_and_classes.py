"""USA from day 1 (14 Nov): the Standard and XL classes, India's own class list, and the Treasure
Valley zone (Boise, Meridian, Nampa, Kuna) -- INACTIVE, with no rate card.

ADDITIVE. India's country regulation row is pinned to the four classes it has always offered, so
adding classes for the US never asks an Indian city for new fare cards. The Idaho zone is created
inactive and priced in miles, in America/Boise and USD; it opens only through the launch gate,
after the console sets its rate card, fee plan and platform insurance.
"""
from django.db import migrations

INDIA_CLASSES = ['auto', 'hatchback', 'sedan', 'suv']
US_CLASSES = {'standard': (4, 'A 4-door car for up to 4 riders.'),
              'xl': (6, 'A larger vehicle for 6 or more riders.')}
# Boise, Meridian, Nampa and Kuna, with the roads between them (lng, lat). Approximate: the
# console's zone editor refines it before launch.
TREASURE_VALLEY = [
    [-116.660, 43.520], [-116.660, 43.650], [-116.400, 43.700], [-116.100, 43.720],
    [-116.050, 43.600], [-116.150, 43.520], [-116.330, 43.440], [-116.500, 43.450],
    [-116.660, 43.520],
]


def forward(apps, schema_editor):
    VehicleType = apps.get_model('driver', 'VehicleType')
    MarketRegulation = apps.get_model('pricing', 'MarketRegulation')
    ServiceZone = apps.get_model('pricing', 'ServiceZone')
    for name, (seats, text) in US_CLASSES.items():
        VehicleType.objects.get_or_create(type=name, defaults={'seats': seats, 'description': text})
    india = MarketRegulation.objects.filter(country='IN', state_code='').first()
    if india is None:
        MarketRegulation.objects.create(country='IN', state_code='', currency='INR', version=1,
                                        vehicle_classes=INDIA_CLASSES)
    elif india.vehicle_classes is None:
        india.vehicle_classes = INDIA_CLASSES
        india.save(update_fields=['vehicle_classes'])
    ServiceZone.objects.get_or_create(code='US-ID-TV', defaults={
        'name': 'Treasure Valley', 'country': 'US', 'state_code': 'ID', 'city': 'Boise',
        'zone_type': 'city', 'parent': None,
        'polygon_geojson': {'type': 'Polygon', 'coordinates': [TREASURE_VALLEY]},
        'priority': 10, 'currency': 'USD', 'timezone_name': 'America/Boise', 'distance_unit': 'mi',
        'tax_mode': 'additive', 'is_active': False,
        'metadata': {'cities': ['Boise', 'Meridian', 'Nampa', 'Kuna'],
                     'operator': 'DeepTrics LLC', 'airport': 'BOI rules not set (owner to give)'},
    })


class Migration(migrations.Migration):
    dependencies = [('pricing', '0028_market_rules_usa'), ('driver', '0052_suspension_reasons_usa')]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
