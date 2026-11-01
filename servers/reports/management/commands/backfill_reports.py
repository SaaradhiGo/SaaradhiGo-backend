"""Rebuild the report rollups for a date range.

    python manage.py backfill_reports --since 2026-09-01 [--until 2026-10-31] [--city IN-TG-HYD] \
        --actor-email <operator>

Recomputes every city (or one) for each local day from `--since` to `--until` (default: today in
each city's clock), plus the weekly rider cohorts. Read-only over business data: it deletes and
re-inserts only the derived rollup tables, so it is idempotent and safe to repeat. Audited: one
row per run naming the operator, the range, the cities and the number of days rebuilt.
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.reports import compute

COMMAND = 'backfill_reports'
AUDIT_ACTION = 'reports_backfilled'
MAX_DAYS = 800


class Command(BaseCommand):
    help = 'Rebuild the report rollups from the source data for a date range (idempotent).'

    def add_arguments(self, parser):
        parser.add_argument('--since', required=True, help='first local date, YYYY-MM-DD')
        parser.add_argument('--until', help='last local date, YYYY-MM-DD (default: today per city)')
        parser.add_argument('--city', help='one city code (default: every city)')
        add_actor_argument(parser)

    def handle(self, *args, **options):
        try:
            since = date.fromisoformat(options['since'])
            until = date.fromisoformat(options['until']) if options.get('until') else None
        except ValueError as exc:
            raise CommandError(f'dates must be YYYY-MM-DD: {exc}') from exc
        if until and until < since:
            raise CommandError('--until must not be before --since')
        cities = compute.active_cities()
        if options.get('city'):
            cities = [c for c in cities if c.code == options['city']]
            if not cities:
                raise CommandError(f'No city {options["city"]!r}.')
        actor = require_actor(options, COMMAND)
        reason = f'Backfill report rollups from {since}' + (f' to {until}' if until else '')
        days = 0
        with attributed(actor, COMMAND, reason=reason):
            for city in cities:
                end = until or compute.local_today(city)
                if (end - since).days + 1 > MAX_DAYS:
                    raise CommandError(f'The range is limited to {MAX_DAYS} days per city.')
                days += compute.rebuild_range([city], since, end, trigger='backfill')
            record_admin_action(
                None, action=AUDIT_ACTION, target_type='report_rollups', target_id='backfill',
                after={'since': since.isoformat(), 'until': until.isoformat() if until else None,
                       'cities': [c.code for c in cities], 'days_rebuilt': days},
                reason=reason)
        self.stdout.write(self.style.SUCCESS(
            f'{days} city-days rebuilt across {len(cities)} cities.'))
