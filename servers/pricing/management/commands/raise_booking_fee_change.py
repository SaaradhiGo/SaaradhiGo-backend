"""Raise a rider booking fee change for a second person to approve (never applies it).

    python manage.py raise_booking_fee_change --country IN --scope IN-TG-HYD \
        --amounts auto=0,hatchback=0,sedan=0,suv=0 --reason "Owner ruling 26 Nov ..."

Rerunnable: a change already waiting for the place is reported, not raised again.
"""

from django.core.management.base import BaseCommand, CommandError

from servers.approvals.service import ApprovalError


class Command(BaseCommand):
    help = 'Raise a rider booking fee change (system maker); a checker approves it in the console.'

    def add_arguments(self, parser):
        parser.add_argument('--country', required=True)
        parser.add_argument('--scope', default='')
        parser.add_argument('--amounts', required=True, help='class=minor,... e.g. auto=0,suv=0')
        parser.add_argument('--reason', required=True)
        parser.add_argument('--effective-from', default='')

    def handle(self, *args, **o):
        from servers.pricing import booking_fees

        try:
            amounts = {k.strip(): int(v) for k, v in (x.split('=') for x in o['amounts'].split(','))}
        except ValueError as exc:
            raise CommandError('--amounts is class=minor,...') from exc
        data = {'amounts': amounts, 'reason': o['reason'], 'effective_from': o['effective_from'] or None}
        try:
            outcome = booking_fees.propose(None, o['country'], o['scope'], data,
                                           system_label='system: ops task (owner ruling, 26 Nov)')
        except ApprovalError as exc:
            self.stdout.write(f'not raised: {exc}')
            return
        r = outcome.request
        self.stdout.write(f'raised {r.public_ref} (id {r.id}) status={r.status} '
                          f'starts {r.payload.get("requested_from")}')
