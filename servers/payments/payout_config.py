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
`OWNER_RULING` is what the owner ruled in AUDIT 27.43 (India: fee 0, minimum
500.00 INR, stuck after 24 hours, on request. United States: fee 0, minimum
1.00 USD, stuck after 72 hours, weekly). `seed_payout_config` writes them for a
fresh environment (create-if-missing); the one-off `apply_payout_ruling`
command writes them as a new version on an environment that already has the
old India row. Nothing here reads them as a fallback.
"""

from decimal import ROUND_HALF_UP, Decimal

from base.countries import COUNTRIES, get_country
from base.money import from_minor

#: The owner's payout values (AUDIT 27.43), in the units the model stores.
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
}
OWNER_RULING = (INDIA_SEED, US_SEED)
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
    """`(country_code_or_None, config_or_None)`."""
    country = driver_payout_country(driver)
    return country, active_config(country)


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
