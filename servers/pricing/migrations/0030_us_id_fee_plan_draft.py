"""USA (14 Nov): an EMPTY US-ID driver fee plan, Fair Share structure, as a draft for the owner to
fill in the console (per-trip fee, daily cap, first-days waiver). No numbers are invented: a draft
never applies, and US drivers cannot go online until an active plan exists (MarketRegulation
`fee_plan_required`)."""
from django.db import migrations


def forward(apps, schema_editor):
    DriverFeePlan = apps.get_model('pricing', 'DriverFeePlan')
    if DriverFeePlan.objects.filter(country='US', scope_code='US-ID').exists():
        return
    DriverFeePlan.objects.create(country='US', scope_code='US-ID', currency='USD', plan_type='fair_share',
                                 amounts={}, status='draft', note='Idaho: empty until the owner sets it',
                                 created_by_label='system (14 Nov USA brief)')


class Migration(migrations.Migration):
    dependencies = [('pricing', '0029_idaho_zone_and_classes')]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
