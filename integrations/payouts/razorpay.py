"""Razorpay payouts (RazorpayX): STUB. See integrations/payments/razorpay.py."""

from servers.payments.payment_gateways.base_gateway import UnavailableGateway


class RazorpayPayoutGateway(UnavailableGateway):
    CURRENCIES = ('INR',)

    def __init__(self):
        super().__init__('razorpay', 'razorpay payouts adapter is a stub: not implemented yet')
