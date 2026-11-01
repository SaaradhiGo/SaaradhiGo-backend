"""Stripe Connect payouts (US driver transfers): STUB. No SDK, no network, no package.

Fill the interface listed on `UnavailableGateway`; US payouts are Connect transfers, not UPI, so
the implementer adds a transfer method alongside (the UPI method stays India-only). Blocker #17.
"""

from servers.payments.payment_gateways.base_gateway import UnavailableGateway


class StripeConnectGateway(UnavailableGateway):
    CURRENCIES = ('USD',)

    def __init__(self):
        super().__init__('stripe_connect',
                         'stripe connect adapter is a stub: not implemented yet')
