"""manage.py integrity_scan [--fix]: the data integrity checks (servers.platform_ops.integrity).

--fix creates missing Driver/Rider rows and missing driver wallets; money rows are never written.
Prints counts and record ids only, never names or numbers.
"""
import json

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Check roles, wallets, trip and payout ledger rows; --fix repairs profiles and wallets.'

    def add_arguments(self, parser):
        parser.add_argument('--fix', action='store_true')

    def handle(self, *args, **options):
        from servers.platform_ops.integrity import scan

        self.stdout.write(json.dumps(scan(fix=options['fix']), indent=1, default=str))
