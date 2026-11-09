"""Writing a payout config version: one function for the seed, the ruling and the console edit.

A change is a NEW row (the old one closes and stays in the history), like an
`ApprovalPolicy`. The caller owns the transaction.
"""

from decimal import Decimal

from django.utils import timezone

from servers.payments.models import PayoutCountryConfig

CONFIG_FIELDS = (
    'fee_percent', 'minimum_withdrawal_minor', 'stuck_payout_hours',
    'payout_schedule', 'payout_weekday',
    # The cycle and the instant fee rule (owner ruling 2026-11-09).
    'payout_interval_days', 'payout_anchor_date', 'payout_time_local',
    'instant_cashout_enabled', 'instant_cashout_fee_flat_minor', 'instant_fee_percent',
    'instant_fee_min_minor', 'instant_fee_max_minor', 'instant_fee_provider',
    'instant_fee_tiers',
    # Fraud money actions (AUDIT 27.56): changed through the same finance.payout_config_change
    # checker as every other payout setting.
    'reward_hold_days', 'recovery_max_percent', 'payout_hold_max_days',
)

#: Defaults when a value is not sent (an older pending request, a seed that predates the fields).
FRAUD_MONEY_DEFAULTS = {'reward_hold_days': 7, 'recovery_max_percent': 50,
                        'payout_hold_max_days': 14}
#: The same for the cycle and the instant fee rule: a request raised before they existed leaves
#: them as they were.
CYCLE_DEFAULTS = {'payout_interval_days': None, 'payout_anchor_date': None,
                  'payout_time_local': None, 'instant_cashout_enabled': False,
                  'instant_cashout_fee_flat_minor': None, 'instant_fee_percent': None,
                  'instant_fee_min_minor': None, 'instant_fee_max_minor': None,
                  'instant_fee_provider': '', 'instant_fee_tiers': None}
CARRIED = {**FRAUD_MONEY_DEFAULTS, **CYCLE_DEFAULTS}


def values_of(config):
    """The comparable values of a config row, as plain Python."""
    return {
        'fee_percent': Decimal(str(config.fee_percent)),
        'minimum_withdrawal_minor': config.minimum_withdrawal_minor,
        'stuck_payout_hours': config.stuck_payout_hours,
        'payout_schedule': config.payout_schedule,
        'payout_weekday': config.payout_weekday,
        'reward_hold_days': config.reward_hold_days,
        'recovery_max_percent': config.recovery_max_percent,
        'payout_hold_max_days': config.payout_hold_max_days,
        **{k: getattr(config, k) for k in CYCLE_DEFAULTS},
    }


def write_config_version(country, values, *, reason, snapshot):
    """Close the country's active row (if any) and write the next version.

    `values` holds the CONFIG_FIELDS; `snapshot` is `actor_snapshot(...)` of
    whoever put this version in place. Returns `(new_row, superseded_or_None)`.
    """
    country = str(country).upper()
    previous = (PayoutCountryConfig.objects.select_for_update()
                .filter(country=country, is_active=True).first())
    version = 1
    if previous is not None:
        version = (PayoutCountryConfig.objects.filter(country=country)
                   .order_by('-version').values_list('version', flat=True).first() or 0) + 1
        previous.is_active = False
        previous.valid_to = timezone.now()
        previous.save(update_fields=['is_active', 'valid_to'])
        values = {**{k: getattr(previous, k) for k in CARRIED}, **values}
    else:
        values = {**CARRIED, **values}
    row = PayoutCountryConfig.objects.create(
        country=country, version=version, reason=(reason or '')[:10000],
        created_by_id=snapshot.get('actor_id'), created_by_label=snapshot.get('actor_label', ''),
        created_by_email=snapshot.get('actor_email', ''), created_by_role=snapshot.get('actor_role', ''),
        **{k: values[k] for k in CONFIG_FIELDS},
    )
    return row, previous
