"""US Withdraw now stays OFF until Stripe's instant-payout price is entered (owner brief, 9 Nov).

A new US version: Withdraw now off, the provider named (Stripe), and no fee value, with the reason
"Stripe instant fee: set from Stripe pricing". The driver never sees a $0 fee that is not real
(`payout_scope.instant_ready` also requires a fee rule before offering it).
"""

from django.db import migrations
from django.utils import timezone

AUTHOR = 'US instant fee placeholder (migration payments 0027)'
REASON = ('Stripe instant fee: set from Stripe pricing (placeholder). Withdraw now stays off in the '
          'US until the fee rule is entered in Payouts > Settings.')
FIELDS = ('fee_percent', 'minimum_withdrawal_minor', 'stuck_payout_hours', 'payout_schedule',
          'payout_weekday', 'reward_hold_days', 'recovery_max_percent', 'payout_hold_max_days',
          'payout_interval_days', 'payout_anchor_date', 'payout_time_local',
          'instant_cashout_enabled', 'instant_cashout_fee_flat_minor', 'instant_fee_percent',
          'instant_fee_min_minor', 'instant_fee_max_minor', 'instant_fee_provider', 'instant_fee_tiers')


def apply(apps, schema_editor):
    Config = apps.get_model('payments', 'PayoutCountryConfig')
    current = Config.objects.filter(country='US', is_active=True).first()
    if current is None or (not current.instant_cashout_enabled and current.instant_fee_provider == 'Stripe'):
        return
    values = {k: getattr(current, k) for k in FIELDS}
    values.update({'instant_cashout_enabled': False, 'instant_fee_provider': 'Stripe',
                   'instant_fee_percent': None, 'instant_cashout_fee_flat_minor': None,
                   'instant_fee_tiers': None})
    version = (Config.objects.filter(country='US').order_by('-version')
               .values_list('version', flat=True).first() or 0) + 1
    current.is_active = False
    current.valid_to = timezone.now()
    current.save(update_fields=['is_active', 'valid_to'])
    Config.objects.create(country='US', currency=current.currency, version=version, reason=REASON,
                          created_by_label=AUTHOR, **values)


class Migration(migrations.Migration):

    dependencies = [('payments', '0026_payout_ruling_2026_11_09')]

    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
