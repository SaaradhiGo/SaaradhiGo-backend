"""Open a safety incident for every open safety-concern ticket that has none (24 Nov, owner).

New tickets do this on save; this catches the ones raised before, and any whose hook failed.
"""
from django.core.management.base import BaseCommand

from servers.support.models import SupportTicket, open_safety_incident


class Command(BaseCommand):
    help = 'Open safety incidents for open safety-concern tickets that have none.'

    def handle(self, *args, **opts):
        rows = SupportTicket.objects.exclude(status='CLOSED').filter(safety_flag=True)
        opened = sum(1 for t in rows.only('id') if open_safety_incident(t.id) is not None)
        self.stdout.write(f'Opened {opened} safety incident(s) from {rows.count()} open safety ticket(s).')
