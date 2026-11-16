"""Payout rules per country: one read path, no fallbacks.

WHY NOTHING DEFAULTS
--------------------
The platform fee and the minimum withdrawal used to be literals in
`driver/views.py` (2.0 and 500.0) plus a currency table in
`driver/services.py`. A literal is a decision someone made for India that every
other market silently inherited. Here a country either has an active
`PayoutCountryConfig` row, or it has no payout rules at all: the driver balance
endpoint says withdrawals are not available yet and a withdrawal request is
refused with `WITHDRAWALS_NOT_AVAILABLE`. What a new country's fee and minimum
should be is the owner's call, not a default.

WHICH COUNTRY A DRIVER IS IN
----------------------------
The driver's home city decides when there is one; else the country of their phone
number; else the currency of the wallet they are paid from (see
`driver_payout_country`). A driver who can be placed in no country has no rules.

THE FEE RULE (owner, AUDIT 27.43)
---------------------------------
Any fee shown to a driver or a rider must be the fee actually charged, read
from the same config value. `withdrawal_fee` is the ONLY source for both the
number the balance endpoint shows (`fee_percent`, `fee_display`) and the
amount taken when a withdrawal is created. `tests/test_finance_actions.py`
holds display and charge to the same value.

THE OWNER'S VALUES
------------------
`FRESH_SEED` is what `seed_payout_config` writes on a fresh database: the owner's
ruling of 2026-11-09 (India every 5 days from 5 Nov at 10:00, minimum 100.00 INR;
United States weekly on Monday at 10:00, minimum 10.00 USD; both free, instant
withdrawal at the provider fee) with the 10 Nov daily Withdraw now limits, the
same values payments migrations 0026 and 0031 put on QA. `OWNER_RULING` keeps the
superseded AUDIT 27.43 values for the one-off `apply_payout_ruling` command only.
Nothing here reads them as a fallback.
"""

from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal

from base.countries import COUNTRIES, get_country
from base.money import from_minor

#: The cycle and instant-fee fields (2026-11-09) as the 27.43 seeds leave them: not set.
_NO_CYCLE = {'payout_interval_days': None, 'payout_anchor_date': None, 'payout_time_local': None,
             'instant_cashout_enabled': False, 'instant_cashout_fee_flat_minor': None,
             'instant_fee_percent': None, 'instant_fee_min_minor': None,
             'instant_fee_max_minor': None, 'instant_fee_provider': '', 'instant_fee_tiers': None,
             'instant_max_per_day': None, 'instant_max_amount_per_day_minor': None}

#: The owner's payout values (AUDIT 27.43), in the units the model stores. Superseded on QA by
#: the 2026-11-09 ruling (payments migration 0026); these remain what a fresh seed writes.
#: Weekday 0 is Monday: weekend earnings are paid out at the start of the week.
INDIA_SEED = {
    'country': 'IN',
    'fee_percent': Decimal('0.00'),
    'minimum_withdrawal_minor': 50000,  # 500.00 INR
    'stuck_payout_hours': 24,
    'payout_schedule': 'on_request',
    'payout_weekday': None,
    # Fraud money actions (AUDIT 27.56): reward hold days, recovery cap %, payout hold max days.
    'reward_hold_days': 7,
    'recovery_max_percent': 50,
    'payout_hold_max_days': 14,
    **_NO_CYCLE,
}
US_SEED = {
    'country': 'US',
    'fee_percent': Decimal('0.00'),
    'minimum_withdrawal_minor': 100,  # 1.00 USD
    'stuck_payout_hours': 72,
    'payout_schedule': 'weekly',
    'payout_weekday': 0,
    'reward_hold_days': 7,
    'recovery_max_percent': 50,
    'payout_hold_max_days': 14,
    **_NO_CYCLE,
}
OWNER_RULING = (INDIA_SEED, US_SEED)

#: A fresh database (owner, 14 Nov): the 2026-11-09 ruling plus the 2026-11-10 daily limits,
#: field for field what payments migrations 0026 and 0031 leave on QA.
INDIA_FRESH = {
    **INDIA_SEED,
    'minimum_withdrawal_minor': 10000,  # 100.00 INR
    'payout_schedule': 'every_n_days', 'payout_weekday': None,
    'payout_interval_days': 5, 'payout_anchor_date': date(2026, 11, 5),
    'payout_time_local': time(10, 0), 'instant_cashout_enabled': True,
    'instant_fee_provider': 'RazorpayX',
    'instant_fee_tiers': [[99999, 472], [2500000, 708], [None, 1062]],
    'instant_max_per_day': 3, 'instant_max_amount_per_day_minor': 5_000_000,
}
US_FRESH = {
    **US_SEED,
    'minimum_withdrawal_minor': 1000,  # 10.00 USD
    'payout_schedule': 'weekly', 'payout_weekday': 0,
    'payout_time_local': time(10, 0), 'instant_cashout_enabled': True,
    'instant_fee_provider': 'Stripe',
    'instant_max_per_day': 3, 'instant_max_amount_per_day_minor': 100_000,
}
FRESH_SEED = (INDIA_FRESH, US_FRESH)
FRESH_REASON = ("Seed: the owner's payout ruling of 2026-11-09 with the 2026-11-10 daily limits "
                '(India every 5 days, minimum 100.00 INR; United States weekly, minimum 10.00 USD).')
RULING_REASON = 'Owner ruling, AUDIT 27.43'

_COUNTRY_OF_CURRENCY = {c.currency: c.code for c in COUNTRIES.values()}


def active_config(country):
    """The live `PayoutCountryConfig` for a country code, or None."""
    from servers.payments.models import PayoutCountryConfig

    if not country:
        return None
    return PayoutCountryConfig.objects.filter(country=str(country).upper(), is_active=True).first()


def driver_payout_country(driver):
    """The country code a driver's payout rules are read from, or None.

    In order of how much each says about WHERE the driver is paid:

      1. the home city's country (an operator placed them);
      2. the country of their phone number (the identity they signed up with:
         +91 India, +1 United States);
      3. the currency of the wallet they are paid from.

    The wallet comes last because a wallet written without a currency was
    labelled with the platform default, which says nothing about the driver. A
    driver who can be placed by none of these has no payout rules.
    """
    home = getattr(driver, 'home_city', None)
    if home is not None:
        country = home.country
        if country is not None:
            return country.code
    from base.countries import country_of_phone

    phone_country = country_of_phone(getattr(getattr(driver, 'user_id', None), 'phone_number', None))
    if phone_country is not None:
        return phone_country.code
    from servers.rider.models import Wallet

    currency = (Wallet.objects.filter(user_id=getattr(driver, 'user_id_id', None))
                .order_by('scope')  # 'driver' sorts before 'rider'
                .values_list('currency', flat=True).first())
    return _COUNTRY_OF_CURRENCY.get(str(currency or '').upper())


def config_for_driver(driver):
    """`(country_code_or_None, config_or_None)`: the settings for the driver's home city,
    resolved city -> state -> country (`payout_scope.EffectivePayoutConfig`, 2026-11-09). It reads
    like the country row, so every caller is unchanged."""
    from .payout_scope import effective_for_driver

    return effective_for_driver(driver)


def minimum_withdrawal(config):
    """The minimum as a Decimal in the country's currency."""
    return from_minor(config.minimum_withdrawal_minor, get_country(config.country).currency)


NOT_AVAILABLE_MESSAGE = 'Withdrawals are not available in your country yet.'


# ---------------------------------------------------------------------------
# The fee: one source for what is shown and what is charged
# ---------------------------------------------------------------------------

def fee_percent(config):
    """The configured transfer fee as a Decimal percentage; 0 with no config."""
    if config is None:
        return Decimal('0')
    return Decimal(str(config.fee_percent))


def withdrawal_fee(config, amount_minor):
    """The transfer fee, in minor units, taken from a withdrawal of `amount_minor`.

    THE ONLY SOURCE of a withdrawal fee. The driver balance endpoint shows
    `fee_percent(config)`; the withdrawal path charges this; both read the
    same `config.fee_percent`. Rounded half up to a whole minor unit.
    """
    pct = fee_percent(config)
    if pct <= 0 or not amount_minor:
        return 0
    return int((Decimal(int(amount_minor)) * pct / Decimal('100'))
               .quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def fee_display(config):
    """What a driver is shown for the fee, or None when there is no fee to show.

    The text is derived from the same percentage `withdrawal_fee` charges, so
    the two cannot drift apart. None (fee 0, or no config) tells the app to
    hide the line.
    """
    pct = fee_percent(config)
    if pct <= 0:
        return None
    text = format(pct.normalize(), 'f')
    return f'{text}%'
