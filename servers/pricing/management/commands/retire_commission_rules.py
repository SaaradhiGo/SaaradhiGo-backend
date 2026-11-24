"""Close every active commission rule (owner, 24 Nov: no commission fallback).

Drivers are charged only through approved fee plans; commission survives only as a fee-plan type.
Each rule's window ends now and it is archived, so trips already booked keep the rate they were
booked at. Audited to the actor, with the reason. Dry run unless --apply.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.services import record_admin_action
from servers.pricing.commission_models import ACTIVE, ARCHIVED, CommissionRule

COMMAND = 'retire_commission_rules'


class Command(BaseCommand):
    help = 'Archive every active commission rule (no fallback: fee plans only).'

    def add_arguments(self, parser):
        parser.add_argument('--reason', required=True)
        parser.add_argument('--apply', action='store_true')
        add_actor_argument(parser)

    def handle(self, *args, reason, apply, **options):
        now = timezone.now()
        rules = list(CommissionRule.objects.filter(status=ACTIVE))
        for r in rules:
            self.stdout.write(f'rule {r.id}: {r.country} {getattr(r, "percent", "")}% v{r.version}')
        if not apply:
            self.stdout.write(f'Dry run: {len(rules)} active rule(s). Add --apply.')
            return
        actor = require_actor(options, COMMAND)
        with attributed(actor, COMMAND, reason=reason), transaction.atomic():
            for r in rules:
                before = {'status': r.status, 'effective_to': r.effective_to.isoformat() if r.effective_to else None}
                r.status = ARCHIVED
                if r.effective_to is None or r.effective_to > now:
                    r.effective_to = now
                r.save(update_fields=['status', 'effective_to'])
                record_admin_action(None, action='commission_rule_retired', target_type='commission_rule',
                                    target_id=r.id, before=before,
                                    after={'status': ARCHIVED, 'effective_to': r.effective_to.isoformat()},
                                    reason=reason)
        self.stdout.write(f'Retired {len(rules)} commission rule(s).')
