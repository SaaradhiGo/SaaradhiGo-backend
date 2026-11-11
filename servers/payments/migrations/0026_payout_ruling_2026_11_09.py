"""The owner's payout ruling of 2026-11-09, as a new version of each country's settings.

Supersedes the AUDIT 27.43 values (India: on request, minimum 500.00 INR) by Srini's 9 Nov
ruling (AUDIT §33). Written as a seed because only the owner holds `approval.check` on QA; each
row names this migration as its author. A country with no active row is left alone.

India: every 5 days from 2026-11-05 at 10:00 IST, minimum 100.00 INR, instant on. The instant
fee is RazorpayX's published per-payout price for IMPS/UPI: 4 / 6 / 9 INR by amount band (below
1,000; 1,000 to 25,000; above), plus 18% GST, so 4.72 / 7.08 / 10.62 INR passed through.
United States: weekly on Monday at 10:00 local, minimum 10.00 USD, instant on. No provider fee
is entered for the US: Stripe's instant price is for the owner to confirm (the report says so).
"""

from datetime import date, time
from decimal import Decimal

from django.db import migrations
from django.utils import timezone

AUTHOR = 'Owner ruling 2026-11-09 (migration payments 0026)'
REASON = ('Owner ruling 2026-11-09: paid automatically on a cycle, free to the driver; instant '
          'withdrawal optional, at the provider fee only. Supersedes AUDIT 27.43.')
FIELDS = ('fee_percent', 'minimum_withdrawal_minor', 'stuck_payout_hours', 'payout_schedule',
          'payout_weekday', 'reward_hold_days', 'recovery_max_percent', 'payout_hold_max_days',
          'payout_interval_days', 'payout_anchor_date', 'payout_time_local',
          'instant_cashout_enabled', 'instant_cashout_fee_flat_minor', 'instant_fee_percent',
          'instant_fee_min_minor', 'instant_fee_max_minor', 'instant_fee_provider',
          'instant_fee_tiers')

RULING = {
    'IN': {'fee_percent': Decimal('0.00'), 'minimum_withdrawal_minor': 10000,
           'payout_schedule': 'every_n_days', 'payout_weekday': None,
           'payout_interval_days': 5, 'payout_anchor_date': date(2026, 11, 5),
           'payout_time_local': time(10, 0), 'instant_cashout_enabled': True,
           'instant_cashout_fee_flat_minor': None, 'instant_fee_percent': None,
           'instant_fee_min_minor': None, 'instant_fee_max_minor': None,
           'instant_fee_provider': 'RazorpayX',
           'instant_fee_tiers': [[99999, 472], [2500000, 708], [None, 1062]]},
    'US': {'fee_percent': Decimal('0.00'), 'minimum_withdrawal_minor': 1000,
           'payout_schedule': 'weekly', 'payout_weekday': 0,
           'payout_interval_days': None, 'payout_anchor_date': None,
           'payout_time_local': time(10, 0), 'instant_cashout_enabled': True,
           'instant_fee_provider': 'Stripe'},
}


def apply(apps, schema_editor):
    Config = apps.get_model('payments', 'PayoutCountryConfig')
    for country, wanted in RULING.items():
        current = Config.objects.filter(country=country, is_active=True).first()
        if current is None:
            continue
        values = {k: getattr(current, k) for k in FIELDS}
        values.update(wanted)
        if all(getattr(current, k) == v for k, v in values.items()):
            continue
        version = (Config.objects.filter(country=country).order_by('-version')
                   .values_list('version', flat=True).first() or 0) + 1
        current.is_active = False
        current.valid_to = timezone.now()
        current.save(update_fields=['is_active', 'valid_to'])
        Config.objects.create(country=country, currency=current.currency, version=version,
                              reason=REASON, created_by_label=AUTHOR, **values)


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0025_payout_settings_extension'),
    ]

    operations = [
        migrations.RunPython(apply, migrations.RunPython.noop),
    ]
