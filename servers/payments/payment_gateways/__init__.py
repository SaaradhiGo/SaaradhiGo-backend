"""
Payment Gateway Abstraction Layer

The interface (`BasePaymentGateway`) and the factory that picks a provider per country by
configuration. The vendor adapters (Cashfree, and the Razorpay / Stripe stubs) live in the
top-level `integrations/` package; nothing here imports a vendor SDK.
"""

from .base_gateway import BasePaymentGateway, UnavailableGateway
from .factory import get_payment_gateway

__all__ = [
    'BasePaymentGateway',
    'UnavailableGateway',
    'get_payment_gateway',
]
