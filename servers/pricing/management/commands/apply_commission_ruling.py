"""Apply the owner's commission ruling (AUDIT 27.36 / 27.44).

    python manage.py apply_commission_ruling --actor-email <operator>

A ONE-OFF RULING COMMAND, NOT A SEED
------------------------------------
The owner ruled a 15% default commission and, because the platform is pre-launch
with no live drivers or riders (27.44), that it is applied directly rather than
through a driver-notice cycle. For India and the United States this creates an
ACTIVE country-level 15% rule (kind percentage, effective now, no driver notice,
reason "Owner ruling, AUDIT 27.44: 15% default, pre-launch, no live drivers"),
plus the per-country config row (notice 7 days, cap not confirmed) if missing.

* EFFECTIVE FROM THE EARLIEST TRIP ON RECORD (or now, with no trips), not from "now".
  This is the first commission rule the environment has ever had, so there is no
  earlier rate for it to be retroactive to. A trip booked before this module has no
  snapshot and resolves "the rule effective at its requested_at"; a trip still
  unsettled when the ruling runs (in progress, or completed and awaiting settlement)
  would otherwise predate the rule and fail to settle with COMMISSION_NOT_CONFIGURED.
  Settled trips are untouched (their settlement rows already hold their rate).
* It does NOT touch a country that already has an applicable country-level rule
  (a person set it since); it reports it.
* It creates no city or vehicle-class override.
* Audited: one row per rule written, and one summary row naming the operator
  (--actor-email is mandatory, like every command that writes money).
* Idempotent: a second run finds both rules in place and writes nothing, not even
  an audit row.

It changes what drivers are charged from the old rate cards' 18/20 to 15, by the
owner's ruling. It moves no money by itself.
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from servers.admin_access.models import GeoNode
from servers.admin_audit.actor_option import add_actor_argument, attributed, require_actor
from servers.admin_audit.models import actor_snapshot
from servers.admin_audit.services import record_admin_action
from servers.pricing.commission_models import (
    ACTIVE, APPLICABLE_STATUSES, PERCENTAGE, CommissionCountryConfig, CommissionRule,
)
from servers.pricing.commission_workflow import rule_dict
from servers.ride.models import Trip

COMMAND = 'apply_commission_ruling'
COUNTRIES = ('IN', 'US')
PERCENT = Decimal('15.00')
REASON = 'Owner ruling, AUDIT 27.44: 15% default, pre-launch, no live drivers'


class Command(BaseCommand):
    help = 'Apply the owner commission ruling: an active country-level 15% rule for IN and US.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)
        written, skipped = [], []
        with attributed(actor, COMMAND, reason=REASON):
            with transaction.atomic():
                snap = actor_snapshot(actor)
                now = timezone.now()
                earliest = (Trip.objects.order_by('requested_at')
                            .values_list('requested_at', flat=True).first())
                start = min(now, earliest) if earliest is not None else now
                for country in COUNTRIES:
                    node = GeoNode.objects.filter(kind='country', code=country).first()
                    existing = CommissionRule.objects.filter(
                        country=country, geo_node=node, vehicle_type__isnull=True,
                        kind=PERCENTAGE, status__in=APPLICABLE_STATUSES,
                        effective_to__isnull=True).first()
                    if existing is not None:
                        skipped.append((country, existing))
                    else:
                        version = (CommissionRule.objects.filter(
                            country=country, geo_node=node, vehicle_type__isnull=True)
                            .count()) + 1
                        rule = CommissionRule.objects.create(
                            country=country, geo_node=node, kind=PERCENTAGE, percent=PERCENT,
                            status=ACTIVE, version=version, effective_from=start,
                            activated_at=now, reason=REASON, created_by_id=snap['actor_id'],
                            created_by_label=snap['actor_label'],
                            created_by_email=snap['actor_email'],
                            created_by_role=snap['actor_role'])
                        written.append(rule)
                        record_admin_action(
                            None, action='commission_rule_activated',
                            target_type='commission_rule', target_id=rule.id,
                            after=rule_dict(rule, now), reason=REASON)
                    CommissionCountryConfig.objects.get_or_create(
                        country=country, defaults={'reason': REASON,
                                                   'updated_by_label': snap['actor_label']})
                if written:
                    record_admin_action(
                        None, action='commission_ruling_applied', target_type='commission_rule',
                        target_id='ruling-27.44',
                        after={'countries': [r.country for r in written],
                               'percent': str(PERCENT)}, reason=REASON)
        for rule in written:
            self.stdout.write(f'  {rule.country}: active {rule.percent}% rule written (id {rule.id})')
        for country, rule in skipped:
            self.stdout.write(f'  {country}: left alone, a country rule is already in place '
                              f'({rule.percent}%, id {rule.id})')
        self.stdout.write(self.style.SUCCESS(
            f'{len(written)} rule(s) written.' if written
            else 'Nothing to change: the ruling is already in place.'))
