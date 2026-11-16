"""A fresh database starts with the owner's payout ruling of 9 Nov (owner, 14 Nov).

`seed_payout_config` on an empty database must leave exactly what payments migrations 0026 (the
2026-11-09 ruling) and 0031 (the 2026-11-10 daily limits) leave on QA: USA weekly with a 10.00 USD
minimum, India every 5 days with a 100.00 INR minimum. Compared against the migrations' own tables,
so the seed and the ruling cannot drift apart again.
"""

import importlib
from io import StringIO

import pytest
from django.core.management import call_command

from servers.payments.models import PayoutCountryConfig
from conftest import seed_actor

pytestmark = pytest.mark.django_db

RULING = importlib.import_module('servers.payments.migrations.0026_payout_ruling_2026_11_09').RULING
LIMITS = importlib.import_module('servers.payments.migrations.0031_seed_instant_daily_limits').LIMITS


def test_fresh_database_matches_the_9_nov_ruling():
    PayoutCountryConfig.objects.all().delete()
    call_command('seed_payout_config', actor_email=seed_actor().email, stdout=StringIO())
    for country, wanted in RULING.items():
        row = PayoutCountryConfig.objects.get(country=country, is_active=True)
        for field, value in wanted.items():
            assert getattr(row, field) == value, (country, field, getattr(row, field), value)
        count, amount = LIMITS[country]
        assert (row.instant_max_per_day, row.instant_max_amount_per_day_minor) == (count, amount)
    india = PayoutCountryConfig.objects.get(country='IN', is_active=True)
    us = PayoutCountryConfig.objects.get(country='US', is_active=True)
    assert (india.payout_schedule, india.payout_interval_days, india.minimum_withdrawal_minor,
            india.currency) == ('every_n_days', 5, 10000, 'INR')
    assert (us.payout_schedule, us.payout_weekday, us.minimum_withdrawal_minor,
            us.currency) == ('weekly', 0, 1000, 'USD')
