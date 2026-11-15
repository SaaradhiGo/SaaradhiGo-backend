"""Open a maker-checker request to change a threshold, as a system maker recording an owner ruling.

Nothing is written until a second person (a platform.settings holder) approves it in Approvals.
Example (US round 6, Srini's ruling 15 Nov): one Finance person refunds up to $50 per trip and
$250 a day alone:

    manage.py propose_approval_policy --country US --action finance.refund --limit 5000 \\
        --daily-limit 25000 --per-target --ruling "Srini's ruling, 15 Nov: ..."
"""
from django.core.management.base import BaseCommand, CommandError

from servers.approvals import service


class Command(BaseCommand):
    help = 'Propose a threshold change (system maker); a checker must approve it.'

    def add_arguments(self, parser):
        parser.add_argument('--country', required=True)
        parser.add_argument('--action', required=True)
        parser.add_argument('--limit', type=int, required=True, help='maker_limit, minor units')
        parser.add_argument('--daily-limit', type=int, default=None, help='per maker per local day, minor units')
        parser.add_argument('--per-target', action='store_true', help='the limit is per trip in total')
        parser.add_argument('--ruling', required=True, help='whose ruling and why (kept as the reason)')

    def handle(self, *args, **o):
        from base.countries import get_country

        country = o['country'].upper()
        try:
            outcome = service.propose_policy_change(
                None, country=country, action_key=o['action'], limit_kind='money',
                currency=get_country(country).currency, maker_limit=o['limit'],
                maker_daily_limit=o['daily_limit'], cumulative_per_target=o['per_target'],
                reason=o['ruling'], system_label='system:owner_ruling')
        except service.ApprovalError as exc:
            raise CommandError(str(exc)) from None
        row = outcome.request
        self.stdout.write(f'approval request #{row.id} {row.status}'
                          f'{" (already open)" if outcome.duplicate else ""}: {row.action_key} {country}')
