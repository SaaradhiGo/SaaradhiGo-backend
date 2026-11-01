"""Razorpay: PAYMENTS STUB. No SDK, no network, no package, no credentials read.

Selectable by configuration (PAYMENT_PROVIDERS={"IN": "razorpay"}) so the swap needs no code
change in business code, but every method answers "provider not available" (None / False, and
the gateway is falsy) until an implementer fills the interface. The methods to fill are listed on
`servers.payments.payment_gateways.base_gateway.UnavailableGateway`. The owner expects to use
Razorpay for India payments, so this adapter is the one to build first.
"""

from servers.payments.payment_gateways.base_gateway import UnavailableGateway


class RazorpayGateway(UnavailableGateway):
    CURRENCIES = ('INR',)  # what a real adapter would declare; nothing settles until it exists

    def __init__(self):
        super().__init__('razorpay', 'razorpay adapter is a stub: not implemented yet')
