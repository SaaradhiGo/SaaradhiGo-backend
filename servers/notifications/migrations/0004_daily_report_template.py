"""25 Nov (owner ruling): the daily report email template. ADDITIVE, create-if-missing."""
from django.db import migrations
from django.utils import timezone

REASON = 'Seed (migration): the daily report email, owner ruling 25 Nov.'


def forward(apps, schema_editor):
    from servers.notifications import seed_data

    Template = apps.get_model('notifications', 'NotificationTemplate')
    GeoNode = apps.get_model('admin_access', 'GeoNode')
    for key, channel, country, copy, active in seed_data.seed_rows():
        if key != seed_data.REPORT_DAILY:
            continue
        if Template.objects.filter(key=key, channel=channel, country=country, locale='en').exists():
            continue
        node = GeoNode.objects.filter(kind='country', code=country).first()
        if node is None:
            continue
        Template.objects.create(
            key=key, channel=channel, country=country, geo_node=node, locale='en', category='transactional',
            subject=copy.get('subject', ''), title=copy.get('title', ''), body=copy['body'],
            variables=copy['variables'], personal_variables=[], sample_values=copy['sample'], version=1,
            status='active' if active else 'draft', reason=REASON,
            activated_at=timezone.now() if active else None, created_by_label='system:migration')


class Migration(migrations.Migration):

    dependencies = [('notifications', '0003_payout_held_templates')]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
