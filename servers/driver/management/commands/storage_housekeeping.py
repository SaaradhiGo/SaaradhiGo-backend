"""Storage housekeeping by hand.

    manage.py storage_housekeeping --missing           # report records whose file is gone
    manage.py storage_housekeeping --missing --apply   # ...and clear those fields
    manage.py storage_housekeeping --sweep [--apply]   # unattached uploads older than 24 h

Dry run unless --apply. Prints counts and record ids, never a key or a URL.
"""
from collections import Counter

from django.core.management.base import BaseCommand

from servers.driver.upload_sweep import (
    clear_missing,
    files_missing_from_storage,
    sweep_unattached_uploads,
)


class Command(BaseCommand):
    help = 'Report or clear records whose file is missing; sweep unattached uploads.'

    def add_arguments(self, parser):
        parser.add_argument('--missing', action='store_true')
        parser.add_argument('--sweep', action='store_true')
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **opts):
        if opts['missing']:
            missing = files_missing_from_storage()
            by_field = Counter(f'{m._meta.label}.{f.name}' for m, f, _ in missing)
            for name, n in sorted(by_field.items()):
                ids = sorted(pk for m, f, pk in missing if f'{m._meta.label}.{f.name}' == name)
                self.stdout.write(f'  {name}: {n} missing (ids {ids[:20]}{"..." if n > 20 else ""})')
            if opts['apply']:
                self.stdout.write(f'Cleared {clear_missing(missing)} field(s).')
            else:
                self.stdout.write(f'{len(missing)} missing. Dry run: nothing changed.')
        if opts['sweep']:
            result = sweep_unattached_uploads(dry_run=not opts['apply'])
            self.stdout.write(f'Sweep {"applied" if opts["apply"] else "dry run"}: {result}')
