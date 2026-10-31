"""Seed the maker-checker thresholds for India, and nothing for the US.

    python manage.py seed_approval_policies --actor-email <operator>

WHAT IT SEEDS (owner rulings, AUDIT.md §10 / §22, INR in paise)
-------------------------------------------------------------
    support.refund      maker alone up to  50,000  (₹500 per trip)
    finance.refund      maker alone up to 500,000  (₹5,000)
    finance.adjust      maker alone up to 200,000  (₹2,000)
    finance.payout      always a checker
    pricing.change      always a checker
    driver.deactivate   always a checker
    bulk.action         maker alone up to 50 records
    support.sla_change  always a checker
    finance.payout_config_change  always a checker
    finance.wallet_credit         always a checker
    zone.change         always a checker (module 9)
    notification.template_change, notification.campaign_send  always a checker (module 11)
    promotion.launch, promotion.budget_increase  always a checker, India and US (module 12)

Nothing is written for the United States. That is the ruling, not an
omission: a US action that needs a threshold is refused with
THRESHOLD_NOT_CONFIGURED until a named person configures one through
`approvals.policy_change`, which is itself a maker-checker action. A seed
that quietly gave the US India's numbers, or no limits, would be a money
decision taken by a script.

IDEMPOTENT, AND IT NEVER OVERWRITES
-----------------------------------
An action that already has an active policy in India is left exactly as it
is, whatever its numbers. Thresholds change through maker-checker, and a
redeploy that reset them to the seed would undo a two-person decision with
nobody's name on it. So this creates version 1 where none exists and
touches nothing else; a second run writes nothing, including to the audit
log.

One summary audit row per run that created something, naming the operator
(`--actor-email`, required before the first write) and every policy
written, in the shape every other seed uses.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from servers.admin_audit.actor_option import (
    add_actor_argument,
    attributed,
    require_actor,
)
from servers.admin_audit.services import record_admin_action
from servers.approvals.actions import (
    BULK_ACTION, DRIVER_DEACTIVATE, FINANCE_ADJUST, FINANCE_PAYOUT,
    FINANCE_PAYOUT_CONFIG, FINANCE_REFUND, FINANCE_WALLET_CREDIT, PRICING_CHANGE,
    NOTIFICATION_CAMPAIGN_SEND, NOTIFICATION_TEMPLATE_CHANGE, PROMOTION_BUDGET_INCREASE,
    PROMOTION_LAUNCH, SUPPORT_REFUND, SUPPORT_SLA_CHANGE, ZONE_CHANGE,
)
from servers.approvals.models import ALWAYS, COUNT, MONEY
from servers.approvals.service import active_policy, write_policy_version

COMMAND = 'seed_approval_policies'
AUDIT_ACTION = 'approval_policies_seeded'

#: (country, action, limit_kind, currency, maker_limit). Integer minor units.
INDIA_DEFAULTS = [
    ('IN', SUPPORT_REFUND, MONEY, 'INR', 50_000),
    ('IN', FINANCE_REFUND, MONEY, 'INR', 500_000),
    ('IN', FINANCE_ADJUST, MONEY, 'INR', 200_000),
    ('IN', FINANCE_PAYOUT, ALWAYS, '', None),
    ('IN', PRICING_CHANGE, ALWAYS, '', None),
    ('IN', DRIVER_DEACTIVATE, ALWAYS, '', None),
    ('IN', BULK_ACTION, COUNT, '', 50),
    ('IN', SUPPORT_SLA_CHANGE, ALWAYS, '', None),
    # Part B (owner, AUDIT 27.43): payout settings and rider wallet credits
    # always need a checker. Seeded for the countries that already have
    # policies (India); none for the US.
    ('IN', FINANCE_PAYOUT_CONFIG, ALWAYS, '', None),
    ('IN', FINANCE_WALLET_CREDIT, ALWAYS, '', None),
    # Module 9: a zone or geofence change always needs a checker.
    ('IN', ZONE_CHANGE, ALWAYS, '', None),
    # Module 11: a template going live and a campaign send always need a checker.
    ('IN', NOTIFICATION_TEMPLATE_CHANGE, ALWAYS, '', None),
    ('IN', NOTIFICATION_CAMPAIGN_SEND, ALWAYS, '', None),
    # Module 12: a campaign going live, and a budget being raised, always need a checker.
    ('IN', PROMOTION_LAUNCH, ALWAYS, '', None),
    ('IN', PROMOTION_BUDGET_INCREASE, ALWAYS, '', None),
]

#: The United States (owner rulings, AUDIT 27.44), USD minor units.
#: A payout RUN (finance.payout carrying withdrawal_ids) always needs a checker
#: whatever maker_limit says: that is the framework's multi_target_key rule.
US_DEFAULTS = [
    ('US', SUPPORT_REFUND, MONEY, 'USD', 1_500),
    ('US', FINANCE_REFUND, MONEY, 'USD', 15_000),
    ('US', FINANCE_ADJUST, ALWAYS, '', None),
    ('US', FINANCE_WALLET_CREDIT, ALWAYS, '', None),
    ('US', FINANCE_PAYOUT, MONEY, 'USD', 100_000),
    ('US', BULK_ACTION, ALWAYS, '', None),
    ('US', PRICING_CHANGE, ALWAYS, '', None),
    ('US', DRIVER_DEACTIVATE, ALWAYS, '', None),
    ('US', SUPPORT_SLA_CHANGE, ALWAYS, '', None),
    ('US', FINANCE_PAYOUT_CONFIG, ALWAYS, '', None),
    ('US', ZONE_CHANGE, ALWAYS, '', None),
    ('US', NOTIFICATION_TEMPLATE_CHANGE, ALWAYS, '', None),
    ('US', NOTIFICATION_CAMPAIGN_SEND, ALWAYS, '', None),
    ('US', PROMOTION_LAUNCH, ALWAYS, '', None),
    ('US', PROMOTION_BUDGET_INCREASE, ALWAYS, '', None),
]

SEED_REASON = 'Seed: maker-checker defaults (AUDIT.md §10/§22 India; §27.44 United States).'


class Command(BaseCommand):
    help = 'Seed the India maker-checker thresholds. Writes nothing for the US.'

    def add_arguments(self, parser):
        add_actor_argument(parser)

    def handle(self, *args, **options):
        actor = require_actor(options, COMMAND)

        created = []
        skipped = []
        with attributed(actor, COMMAND, reason=SEED_REASON):
            with transaction.atomic():
                for country, action_key, kind, currency, limit in INDIA_DEFAULTS + US_DEFAULTS:
                    if active_policy(country, action_key) is not None:
                        skipped.append(f'{country}:{action_key}')
                        continue
                    policy, _superseded = write_policy_version(
                        country=country, action_key=action_key, limit_kind=kind,
                        currency=currency, maker_limit=limit, created_by=actor,
                        reason=SEED_REASON,
                    )
                    created.append(policy)

                if created:
                    record_admin_action(
                        None,
                        action=AUDIT_ACTION,
                        target_type='approval_policy',
                        target_id='seed',
                        after={
                            'created': [p.as_dict() for p in created],
                            'skipped_existing': skipped,
                            'us_policies_written': sum(1 for p in created if p.country == 'US'),
                        },
                        reason=SEED_REASON,
                    )

        for policy in created:
            self.stdout.write(
                f'  {policy.country} {policy.action_key}: {policy.limit_kind}'
                + (f' {policy.currency}' if policy.currency else '')
                + (f' maker_limit={policy.maker_limit}' if policy.maker_limit is not None
                   else ' always a checker')
            )
        if skipped:
            self.stdout.write(f'  left as configured: {", ".join(skipped)}')
        self.stdout.write(self.style.SUCCESS(
            f'{len(created)} policies created, {len(skipped)} already configured. '
            f'Countries without a policy for an action stay blocked.'
        ))
