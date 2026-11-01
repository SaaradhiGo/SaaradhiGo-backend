"""The payment and payout adapter registry: provider NAME -> adapter class.

Business code never imports an adapter module; it asks the factory
(`servers.payments.payment_gateways.factory`), which resolves a country to a provider name from
settings and asks this registry for the class. Adapters are imported only when asked for, so a
stub costs nothing and an SDK is loaded only by the provider actually in use.

STATUS per provider: `implemented` (talks to the vendor) or `stub` (answers "not available").
"""

import importlib

#: name -> (module, class, status)
PAYMENT_ADAPTERS = {
    'cashfree': ('integrations.payments.cashfree', 'CashfreeGateway', 'implemented'),
    'razorpay': ('integrations.payments.razorpay', 'RazorpayGateway', 'stub'),
    'stripe': ('integrations.payments.stripe', 'StripeGateway', 'stub'),
}
PAYOUT_ADAPTERS = {
    'cashfree': ('integrations.payouts.cashfree', 'CashfreeGateway', 'implemented'),
    'razorpay': ('integrations.payouts.razorpay', 'RazorpayPayoutGateway', 'stub'),
    'stripe_connect': ('integrations.payouts.stripe_connect', 'StripeConnectGateway', 'stub'),
}
#: Names accepted as spellings of a registered provider.
ALIASES = {'stripe-connect': 'stripe_connect'}

#: Currencies each provider can settle, declared here so the status page can read them without
#: constructing an adapter. Cashfree is INR only.
CURRENCIES = {'cashfree': ('INR',), 'razorpay': ('INR',), 'stripe': ('USD',),
              'stripe_connect': ('USD',)}

KINDS = {'payments': PAYMENT_ADAPTERS, 'payouts': PAYOUT_ADAPTERS}


def canonical(name):
    text = str(name or '').strip().lower()
    return ALIASES.get(text, text)


def known(kind, name):
    return canonical(name) in KINDS[kind]


def status(kind, name):
    """`implemented`, `stub` or None (unknown name)."""
    entry = KINDS[kind].get(canonical(name))
    return entry[2] if entry else None


def adapter_class(kind, name):
    try:
        module, cls, _ = KINDS[kind][canonical(name)]
    except KeyError:
        raise ValueError(f'Unknown {kind} provider: {name!r}') from None
    return getattr(importlib.import_module(module), cls)
