"""Trip labels that still read "Current location" (14 Nov, owner): a short place name from the
trip's stored coordinates. One reverse-geocode per point at most, through the shared Maps cache
(7 days), so a rerun costs nothing. Only the labels change; the stored address is left as it was.

    python manage.py backfill_place_labels [--dry-run] [--limit N]
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from servers.ride.models import Trip
from servers.ride.places import short_place
from servers.ride.rider_booking import GENERIC_PICKUP_LABELS, needs_street_address, street_address


def _generic(q_field):
    q = Q(**{f'{q_field}__isnull': True})
    for word in GENERIC_PICKUP_LABELS:
        q |= Q(**{f'{q_field}__iexact': word})
    return q


class Command(BaseCommand):
    help = 'Give trips whose label reads "Current location" a short place name from their coordinates.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')
        parser.add_argument('--limit', type=int, default=0)

    def handle(self, *args, dry_run=False, limit=0, **opts):
        # 14 Nov: also labels that start with a house number ("10, HITEC City"), from before the rule
        # skipped it.
        numbered = Q(pickup_label__regex=r'^[0-9#]') | Q(destination_label__regex=r'^[0-9#]')
        todo = Trip.objects.filter(_generic('pickup_label') | _generic('destination_label') | numbered).order_by('id')
        if limit:
            todo = todo[:limit]
        fixed = calls = unresolved = 0
        for trip in todo:
            changed = []
            for side, lat, lng in (('pickup', trip.pickup_lat, trip.pickup_long),
                                   ('destination', trip.destination_lat, trip.destination_long)):
                current = getattr(trip, f'{side}_label') or ''
                numbered = bool(current) and current[0] in '0123456789#'
                if not (needs_street_address(current) or numbered) or lat is None or lng is None:
                    continue
                address = getattr(trip, f'{side}_address') or ''
                if needs_street_address(address):
                    calls += 1
                    address = street_address(lat, lng)  # cached for 7 days: a rerun costs nothing
                label = short_place(address)
                if label == current:
                    continue
                if label and not needs_street_address(label):
                    setattr(trip, f'{side}_label', label)
                    changed.append(f'{side}_label')
                else:
                    unresolved += 1
            if changed:
                fixed += 1
                if not dry_run:
                    # update_fields without the addresses: save() leaves the labels as set here.
                    trip.save(update_fields=changed)
        verb = 'would fix' if dry_run else 'fixed'
        self.stdout.write(f'{verb} {fixed} trips; {calls} points looked up; {unresolved} without an address')
