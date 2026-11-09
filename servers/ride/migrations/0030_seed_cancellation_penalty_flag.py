"""The `cancellation_penalty` flag (MVAG 2025, 2026-11-09): off by default, ON for India.

Money-affecting, so any later change goes through a checker. India's override is written only
where the India geo node exists (QA); an environment without it keeps the flag off.
"""

from django.db import migrations

KEY = 'cancellation_penalty'
AUTHOR = 'MVAG 2025 seed (migration ride 0030)'


def seed(apps, schema_editor):
    FeatureFlag = apps.get_model('platform_ops', 'FeatureFlag')
    FeatureFlagOverride = apps.get_model('platform_ops', 'FeatureFlagOverride')
    FeatureFlagVersion = apps.get_model('platform_ops', 'FeatureFlagVersion')
    GeoNode = apps.get_model('admin_access', 'GeoNode')

    flag, created = FeatureFlag.objects.get_or_create(key=KEY, defaults={
        'description': 'MVAG 2025: an unjustified cancellation costs 10% of the fare, capped '
                       '(rider: due on the next ride; driver: a ledger debit).',
        'default_enabled': False, 'owner_area': 'pricing', 'money_affecting': True,
        'updated_by_label': AUTHOR})
    if not created:
        return
    india = GeoNode.objects.filter(kind='country', code='IN').first()
    if india is not None:
        FeatureFlagOverride.objects.create(flag=flag, node=india, enabled=True)
    FeatureFlagVersion.objects.create(
        flag=flag, version=1, changed_by_label=AUTHOR,
        reason='MVAG 2025 compliance: on in India with the central defaults',
        snapshot={'default_enabled': False,
                  'overrides': [{'node': 'IN', 'enabled': True}] if india else []})


class Migration(migrations.Migration):

    dependencies = [
        ('ride', '0029_cancellation_penalty'),
        ('platform_ops', '0001_initial'),
        ('admin_access', '0002_city_status_draft'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
