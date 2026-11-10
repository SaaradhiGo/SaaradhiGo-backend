"""India's driver fee plan on QA (owner brief, 9 Nov), and its two flags.

* v1, ACTIVE from 10 Nov: `fair_share` for cars (hatchback, sedan, SUV): Rs 10 a trip, at most
  Rs 149 a day, nothing on a day under Rs 1,000; no fee in a driver's first 60 days from their first
  trip. Rider platform fee Rs 5 set, behind the `rider_platform_fee` flag, which stays OFF.
  Autos (and bikes, not a class on QA yet) are not in the plan, so they keep today's commission.
* v2, DRAFT: the same with autos listed and their values left blank, "to be decided by Srini".
* Flags: `rider_platform_fee` (off, money-affecting, until the CA confirms the tax treatment) and
  `driver_rebate` (off; nothing reads it yet: a rebate from the open-books surplus is a later prompt).

Written as a seed (only the owner holds approval.check on QA; AUDIT §35). The USA keeps the
percentage commission: no plan row.
"""

from datetime import date

from django.db import migrations

AUTHOR = 'Fair Share seed (migration pricing 0022)'
CARS = {'per_trip_fee_minor': 1000, 'rider_platform_fee_minor': 500}


def seed(apps, schema_editor):
    Plan = apps.get_model('pricing', 'DriverFeePlan')
    if Plan.objects.filter(country='IN', scope_code='').exists():
        return
    common = dict(country='IN', scope_code='', currency='INR', plan_type='fair_share',
                  earnings_floor_minor=100000, daily_cap_minor=14900, waiver_first_days=60,
                  created_by_label=AUTHOR)
    Plan.objects.create(version=1, status='active', effective_from=date(2026, 11, 10),
                        amounts={'hatchback': CARS, 'sedan': CARS, 'suv': CARS},
                        note='India cars: Rs 10 a trip, cap Rs 149 a day, floor Rs 1,000; 60-day waiver',
                        reason='Owner brief 9 Nov: Fair Share for India cars', **common)
    Plan.objects.create(version=2, status='draft',
                        amounts={'hatchback': CARS, 'sedan': CARS, 'suv': CARS, 'auto': {}, 'bike': {}},
                        note='Autos and bikes: to be decided by Srini',
                        reason='Draft: autos and bikes to be decided', **common)

    Flag = apps.get_model('platform_ops', 'FeatureFlag')
    Version = apps.get_model('platform_ops', 'FeatureFlagVersion')
    for key, text in (
            ('rider_platform_fee', 'A fixed platform fee per trip on the rider fare (9 Nov). OFF until '
                                   'the CA confirms its tax treatment.'),
            ('driver_rebate', 'A rebate to drivers from the open-books surplus. Not built: kept off '
                              'until 6 months of data (later prompt).')):
        flag, created = Flag.objects.get_or_create(key=key, defaults={
            'description': text, 'default_enabled': False, 'owner_area': 'pricing',
            'money_affecting': True, 'updated_by_label': AUTHOR})
        if created:
            Version.objects.create(flag=flag, version=1, changed_by_label=AUTHOR, reason=text,
                                   snapshot={'default_enabled': False, 'overrides': []})


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0021_driver_fee_plans'),
        ('platform_ops', '0001_initial'),
    ]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
