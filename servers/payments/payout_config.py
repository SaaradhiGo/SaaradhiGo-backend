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

INDIA'S VALUES
--------------
`INDIA_SEED` is exactly what the code did before this module existed (2 percent,
500.00 rupees, a payout stuck after 24 hours). It is only ever written by
`seed_payout_config`, create-if-missing; nothing here reads it as a fallback.
"""

from decimal import Decimal

from base.countries import COUNTRIES, get_country
from base.money import from_minor

#: Today's India behaviour, in the units the model stores.
INDIA_SEED = {
    'country': 'IN',
    'fee_percent': Decimal('2.00'),
    'minimum_withdrawal_minor': 50000,  # 500.00 INR
    'stuck_payout_hours': 24,
}

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
