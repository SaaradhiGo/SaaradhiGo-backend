"""Give every existing record its readable reference (24 Nov, owner). Safe to run again."""
from django.apps import apps
from django.core.management.base import BaseCommand

from base import public_ref

RELATED = {
    public_ref.TRIP: ('zone__geo_node',), public_ref.DRIVER: ('home_city',), public_ref.RIDER: ('home_city',),
    public_ref.TICKET: ('trip_id__zone__geo_node', 'user_id'), public_ref.PAYOUT: ('driver__home_city',),
    public_ref.APPROVAL: ('scope_node',), public_ref.FRAUD: ('anchor_node',),
}


class Command(BaseCommand):
    help = 'Assign public references to every record that has none.'

    def handle(self, *args, **opts):
        for (app, name), kind in public_ref.MODELS.items():
            model = apps.get_model(app, name)
            rows = model.objects.filter(public_ref__isnull=True).select_related(*RELATED[kind]).order_by('pk')
            done = sum(1 for obj in rows.iterator(chunk_size=500) if public_ref.give(kind, obj))
            self.stdout.write(f'{name}: {done} reference(s) assigned')
