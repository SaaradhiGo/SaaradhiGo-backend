"""Google Maps Platform list prices per SKU (USD per 1,000 calls, first tier) and free monthly calls,
as published for the pricing that took effect on 1 March 2025. Editable in the console
(Configure > Integrations > Maps); verify against the Google Cloud billing account. Route cache
answers are not billed and carry no price row.
"""

from decimal import Decimal

from django.db import migrations

NOTE = 'Google list price from 1 Mar 2025 (first tier); verify in billing'
PRICES = [
    # sku, USD per 1,000, free calls a month
    ('places_autocomplete_session', '2.83', 10000),
    ('places_autocomplete', '2.83', 10000),
    ('places_details', '5.00', 10000),      # Place Details Essentials
    ('geocoding', '5.00', 10000),
    ('routes_essentials', '5.00', 10000),
    ('routes_pro', '10.00', 5000),
    ('directions_legacy', '5.00', 10000),
    ('dynamic_maps', '7.00', 10000),        # Maps JavaScript map loads (web)
]


def seed(apps, schema_editor):
    MapsSkuPrice = apps.get_model('ride', 'MapsSkuPrice')
    for sku, usd, free in PRICES:
        MapsSkuPrice.objects.get_or_create(sku=sku, defaults={
            'usd_per_1000': Decimal(usd), 'free_monthly_calls': free, 'note': NOTE,
            'updated_by_label': 'seed (migration ride 0032)'})


class Migration(migrations.Migration):

    dependencies = [('ride', '0031_maps_cost_control')]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
