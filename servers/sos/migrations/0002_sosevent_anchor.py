"""Anchor every SOS event to an admin-scope node (owner's ruling, 2026-10-28).

Adds `anchor_node` / `anchor_source` and backfills existing rows with the
same order new rows use, minus the GPS step: the trip's zone node, else a
driver's home city, else `unanchored`. Historical coordinates are NOT run
through the polygons here -- a migration that depends on shapely and the
live zone set is a migration that can fail a deploy -- so a historical
rider SOS with a fix but no trip lands as `unanchored` and stays visible
to every Safety Officer, which is the safe direction. No alerts are raised
for historical rows: they are closed incidents, not open ones.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_anchor(apps, schema_editor):
    SOSEvent = apps.get_model('sos', 'SOSEvent')
    Trip = apps.get_model('ride', 'Trip')
    Driver = apps.get_model('driver', 'Driver')

    events = list(SOSEvent.objects.filter(anchor_source='').values_list('id', 'trip_id', 'user_id'))
    if not events:
        return

    trip_ids = {t for _, t, _ in events if t}
    user_ids = {u for _, _, u in events if u}
    trip_node = dict(
        Trip.objects.filter(id__in=trip_ids).values_list('id', 'zone__geo_node_id')
    ) if trip_ids else {}
    home_city = dict(
        Driver.objects.filter(user_id__in=user_ids).values_list('user_id', 'home_city_id')
    ) if user_ids else {}

    for event_id, trip_id, user_id in events:
        node = trip_node.get(trip_id) if trip_id else None
        if node:
            source = 'trip'
        else:
            node = home_city.get(user_id) if user_id else None
            source = 'home_city' if node else 'unanchored'
        SOSEvent.objects.filter(id=event_id).update(
            anchor_node_id=node, anchor_source=source,
        )


def noop(apps, schema_editor):
    """Reversing drops the columns; nothing to undo in data."""


class Migration(migrations.Migration):

    dependencies = [
        ('admin_access', '0001_initial'),
        ('sos', '0001_initial'),
        ('ride', '0020_money_minor_units_drop'),
        ('pricing', '0009_servicezone_geo_node'),
        ('driver', '0030_money_minor_units_drop'),
    ]

    operations = [
        migrations.AddField(
            model_name='sosevent',
            name='anchor_node',
            field=models.ForeignKey(blank=True, help_text='The admin-scope node this event is routed to. NULL means unanchored.', null=True, on_delete=django.db.models.deletion.PROTECT, related_name='sos_events', to='admin_access.geonode'),
        ),
        migrations.AddField(
            model_name='sosevent',
            name='anchor_source',
            field=models.CharField(blank=True, choices=[('trip', "The trip's zone"), ('gps', "The raiser's GPS position, resolved to a zone"), ('home_city', "The driver's home city"), ('unanchored', 'No anchor: routed to every safety responder')], db_index=True, default='', help_text='How anchor_node was chosen. Set on creation; blank is resolved on save.', max_length=12),
        ),
        migrations.RunPython(backfill_anchor, noop),
    ]
