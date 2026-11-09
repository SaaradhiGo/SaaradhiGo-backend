"""MVAG 2025 values (owner brief, 2026-11-09): the India fare band and cancellation penalty, an
unset Telangana row and the India commission cap of 20%.

Written as a seed rather than a console request because only the owner holds `approval.check`
on QA (AUDIT §33). Every row names this migration as its author and carries the reason, and
the commission row's version moves on, so the console history shows the change. Nothing is
overwritten: an existing regulation row is left as it is.
"""

from decimal import Decimal

from django.db import migrations

AUTHOR = 'MVAG 2025 seed (migration pricing 0020)'
CAP_REASON = 'MVAG 2025: own-vehicle driver keeps at least 80%'


def seed(apps, schema_editor):
    MarketRegulation = apps.get_model('pricing', 'MarketRegulation')
    CommissionCountryConfig = apps.get_model('pricing', 'CommissionCountryConfig')

    MarketRegulation.objects.get_or_create(
        country='IN', state_code='',
        defaults={'base_fare_source': 'state_notified',
                  'max_dynamic_multiplier': Decimal('2.00'),
                  'min_dynamic_multiplier': Decimal('0.50'),
                  # 10% of the fare, at most 100.00 INR; free for 5 minutes after acceptance
                  # (the window is a platform choice, AUDIT §33).
                  'cancel_penalty_percent': Decimal('10.00'), 'cancel_penalty_cap_minor': 10000,
                  'cancel_free_seconds': 300,
                  # Health 5,00,000 INR, term 10,00,000 INR, 40 hours induction. The gate is OFF
                  # until operations is ready; the owner turns it on in the console.
                  'driver_records_gate': False, 'health_insurance_min_minor': 50_000_000,
                  'term_insurance_min_minor': 100_000_000, 'induction_min_hours': Decimal('40.0'),
                  'note': 'MVAG 2025 central defaults', 'updated_by_label': AUTHOR,
                  'reason': 'MVAG 2025: dynamic fare between 0.5x and 2x of the base fare'})
    # Hyderabad keeps its 1.5x rate-card cap (stricter, legal under 2020 and 2025). The state
    # row exists so the console shows it, with every value unset until the owner confirms them.
    MarketRegulation.objects.get_or_create(
        country='IN', state_code='TG',
        defaults={'note': 'state notification not confirmed', 'updated_by_label': AUTHOR,
                  'reason': 'Telangana values wait for the state notification'})

    row = CommissionCountryConfig.objects.filter(country='IN').first()
    if row is None:
        CommissionCountryConfig.objects.create(
            country='IN', notice_days=7, max_percent=Decimal('20.00'), version=1,
            updated_by_label=AUTHOR, reason=CAP_REASON)
    elif row.max_percent is None or row.max_percent > Decimal('20.00'):
        row.max_percent = Decimal('20.00')
        row.version += 1
        row.updated_by_label = AUTHOR
        row.reason = CAP_REASON
        row.save()


class Migration(migrations.Migration):

    dependencies = [
        ('pricing', '0019_mvag_2025_regulation'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
