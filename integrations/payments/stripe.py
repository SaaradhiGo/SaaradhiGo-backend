"""Stripe: PAYMENTS STUB (US card payments). No SDK, no network, no package, no credentials read.

Selectable by configuration (PAYMENT_PROVIDERS={"US": "stripe"}); every method answers "provider
not available" until an implementer fills the interface listed on
`servers.payments.payment_gateways.base_gateway.UnavailableGateway`. Go-live blocker #17.
"""

from servers.payments.payment_gateways.base_gateway import UnavailableGateway


class StripeGateway(UnavailableGateway):
    CURRENCIES = ('USD',)

    def __init__(self):
        super().__init__('stripe', 'stripe adapter is a stub: not implemented yet')
