"""
Payment gateway factory: which provider serves WHICH COUNTRY, by configuration only.

    settings.PAYMENT_PROVIDERS   country -> provider name (env PAYMENT_PROVIDERS_JSON)
    settings.PAYOUT_PROVIDERS    country -> provider name (env PAYOUT_PROVIDERS_JSON)

e.g. {"IN": "razorpay", "US": null}, which is the DEFAULT (owner decision 2026-11-01: India uses
Razorpay and RazorpayX). Cashfree stays registered but is configured nowhere unless a table says so. A null / missing country has NO provider: the factory then
returns an `UnavailableGateway` (falsy), which every call site already treats as "not configured"
(HTTP 503, or the withdrawal stays awaiting a rail). It NEVER falls back to another country's
provider. The one legacy fallback: with no entry for India in the table, the old global
PAYMENT_GATEWAY / PAYOUT_GATEWAY setting is India's provider, but ONLY when it is set explicitly
(it has no default, so India never silently resolves to Cashfree).

Business code reaches a provider only through this module (the vendor boundary is enforced by
tests/test_vendor_boundary.py); the adapters themselves live in the top-level `integrations/`.
"""

import logging
from typing import Optional

from django.conf import settings

from base.countries import COUNTRIES
from integrations.payments import registry

from .base_gateway import BasePaymentGateway, UnavailableGateway

logger = logging.getLogger(__name__)

#: kind -> (table setting, legacy IN fallback setting)
_SETTINGS = {
    'payments': ('PAYMENT_PROVIDERS', 'PAYMENT_GATEWAY'),
    'payouts': ('PAYOUT_PROVIDERS', 'PAYOUT_GATEWAY'),
}


def _country_code(country):
    return str(country or '').strip().upper()


def provider_name_for(kind, country) -> Optional[str]:
    """The configured provider NAME for a country, or None. Pure configuration, no I/O."""
    code = _country_code(country)
    if not code:
        return None
    table_setting, legacy = _SETTINGS[kind]
    table = getattr(settings, table_setting, None) or {}
    if code in table:
        name = table[code]
    elif code == 'IN':
        name = getattr(settings, legacy, '')
    else:
        name = None
    name = str(name or '').strip().lower()
    return registry.canonical(name) if name else None


def configured_providers(kind):
    """`{country code: provider name or None}` for every known country (status pages)."""
    return {code: provider_name_for(kind, code) for code in sorted(COUNTRIES)}


def _build(kind, name) -> BasePaymentGateway:
    gateway = registry.adapter_class(kind, name)()
    # An adapter that knows it has no credentials (or a refused key) answers falsy, so every call
    # site's `if not gateway:` path says "not configured" without a vendor call.
    reason = getattr(gateway, 'not_configured_reason', None)
    reason = reason(kind) if callable(reason) else None
    if reason:
        return UnavailableGateway(registry.canonical(name), reason)
    return gateway


def _resolve(kind, gateway_name, country) -> BasePaymentGateway:
    if gateway_name:
        # By name: a payment row already recorded its provider, or a webhook route belongs to
        # one provider. Unknown names are a programming error, as they always were.
        return _build(kind, gateway_name)
    name = provider_name_for(kind, country)
    if not name:
        code = _country_code(country) or 'an unknown country'
        return UnavailableGateway(
            'none', f'no {kind[:-1]} provider is configured for {code}')
    if not registry.known(kind, name):
        logger.error('%s provider %r for %s is not a registered adapter', kind, name, country)
        return UnavailableGateway(name, f'{name} is not a registered {kind[:-1]} provider')
    return _build(kind, name)


def get_payment_gateway(gateway_name: Optional[str] = None, country=None) -> BasePaymentGateway:
    """The payment gateway for a provider NAME, or else for a COUNTRY.

    A country with no provider yields a falsy `UnavailableGateway`, never another country's.
    Raises ValueError for an unknown explicit name.
    """
    return _resolve('payments', gateway_name, country)


def get_webhook_verifier(kind, gateway_name) -> BasePaymentGateway:
    """The named provider's adapter WITHOUT the "credentials present" gate.

    A webhook's signature needs only the webhook secret, so a deployment whose API keys are not
    set yet still answers an unsigned / forged delivery with 400 (and no side effect) rather than
    a 503. Never use it to make an outbound call.
    """
    return registry.adapter_class(kind, gateway_name)()


def get_payment_gateway_for_payments(country=None) -> BasePaymentGateway:
    """The gateway that takes rider payments in `country`."""
    return _resolve('payments', None, country)


def get_payment_gateway_for_payouts(country=None, gateway_name=None) -> BasePaymentGateway:
    """The gateway that pays drivers out in `country` (or the named one, for a webhook)."""
    return _resolve('payouts', gateway_name, country)


def any_payout_provider_configured() -> bool:
    """True when at least one country has an implemented payout provider."""
    return any(name and registry.status('payouts', name) == 'implemented'
               for name in configured_providers('payouts').values())


# ---------------------------------------------------------------------------
# Resolving a country from what the caller has
# ---------------------------------------------------------------------------

def country_of_currency(currency):
    code = str(currency or '').strip().upper()
    for country in COUNTRIES.values():
        if country.currency == code:
            return country.code
    return None


def country_for_trip(trip):
    """A trip's country: its zone's country when it has one, else the country of its currency."""
    zone = getattr(trip, 'zone', None)
    zone_country = getattr(zone, 'country', None)
    if zone_country:
        return _country_code(getattr(zone_country, 'code', zone_country))
    from base.money import currency_for_trip

    return country_of_currency(currency_for_trip(trip))


def country_for_driver(driver):
    """A driver's payout country (home city, phone, wallet currency), or None."""
    from servers.payments.payout_config import driver_payout_country

    return driver_payout_country(driver)


def get_available_gateways() -> list:
    """Names of implemented payment providers that have credentials set (diagnostics)."""
    names = []
    for name, (_m, _c, state) in registry.PAYMENT_ADAPTERS.items():
        if state != 'implemented':
            continue
        if name == 'cashfree' and getattr(settings, 'CASHFREE_APP_ID', None) \
                and getattr(settings, 'CASHFREE_SECRET_KEY', None):
            names.append(name)
        elif name == 'razorpay' and not registry.adapter_class('payments', name)().not_configured_reason('payments'):
            names.append(name)
    return names


def is_gateway_available(gateway_name: str) -> bool:
    return registry.canonical(gateway_name) in get_available_gateways()
