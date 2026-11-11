"""The daily Withdraw now limits (owner brief, 10 Nov), as a new version of each country's settings.

India: 3 instant withdrawals and 50,000.00 INR a day. USA: 3 and 1,000.00 USD a day. Written as a
seed because only the owner holds `approval.check` on QA (AUDIT §35, decision 1); each row names
this migration as its author, and the next change goes through Payouts > Settings and the checker.
A country with no active row, or one that already has these values, is left alone.
"""

from django.db import migrations
from django.utils import timezone

AUTHOR = 'Owner brief 2026-11-10: daily instant limit (migration payments 0031)'
REASON = ('Daily Withdraw now limit (owner, 10 Nov): counted over the driver\'s local day; failed '
          'or reversed ones do not count; scheduled payouts are never limited.')
LIMITS = {'IN': (3, 5_000_000), 'US': (3, 100_000)}
FIELDS = ('fee_percent', 'minimum_withdrawal_minor', 'stuck_payout_hours', 'payout_schedule',
          'payout_weekday', 'reward_hold_days', 'recovery_max_percent', 'payout_hold_max_days',
          'payout_interval_days', 'payout_anchor_date', 'payout_time_local',
          'instant_cashout_enabled', 'instant_cashout_fee_flat_minor', 'instant_fee_percent',
          'instant_fee_min_minor', 'instant_fee_max_minor', 'instant_fee_provider', 'instant_fee_tiers')


def apply(apps, schema_editor):
    Config = apps.get_model('payments', 'PayoutCountryConfig')
    for country, (count, amount) in LIMITS.items():
        current = Config.objects.filter(country=country, is_active=True).first()
        if current is None or (current.instant_max_per_day == count
                               and current.instant_max_amount_per_day_minor == amount):
            continue
        values = {k: getattr(current, k) for k in FIELDS}
        version = (Config.objects.filter(country=country).order_by('-version')
                   .values_list('version', flat=True).first() or 0) + 1
        current.is_active = False
        current.valid_to = timezone.now()
        current.save(update_fields=['is_active', 'valid_to'])
        Config.objects.create(country=country, currency=current.currency, version=version,
                              reason=REASON, created_by_label=AUTHOR, instant_max_per_day=count,
                              instant_max_amount_per_day_minor=amount, **values)


class Migration(migrations.Migration):

    dependencies = [('payments', '0030_instant_daily_limit')]

    operations = [migrations.RunPython(apply, migrations.RunPython.noop)]
