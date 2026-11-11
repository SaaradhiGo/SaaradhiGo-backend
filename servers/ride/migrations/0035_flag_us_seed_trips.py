"""Flag the US display city's seed trips (`seed_us_test_city`) as seed data (11 Nov)."""
from django.db import migrations


def flag(apps, schema_editor):
    Trip = apps.get_model('ride', 'Trip')
    Trip.objects.filter(client_request_id__startswith='qa-us-seed-trip-').update(is_seed=True)


class Migration(migrations.Migration):
    dependencies = [('ride', '0034_seed_flag')]
    operations = [migrations.RunPython(flag, migrations.RunPython.noop)]
