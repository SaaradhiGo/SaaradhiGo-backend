"""
Payment Gateway Factory

Factory for creating payment gateway instances.
"""

import logging
from django.conf import settings
from typing import Optional

from .base_gateway import BasePaymentGateway
from .cashfree_gateway import CashfreeGateway

logger = logging.getLogger(__name__)


def get_payment_gateway(gateway_name: Optional[str] = None) -> BasePaymentGateway:
    """
    Get a payment gateway instance.
    
    Args:
        gateway_name: Name of the gateway (only 'cashfree' is supported).
                     If None, uses settings.PAYMENT_GATEWAY.
    
    Returns:
        BasePaymentGateway instance
    
    Raises:
        ValueError: If gateway_name is unknown
        ImportError: If required SDK is not installed
    """
    if not gateway_name:
        gateway_name = getattr(settings, 'PAYMENT_GATEWAY', 'cashfree')
    
    gateway_name = gateway_name.lower()
    
    if gateway_name == 'cashfree':
        return CashfreeGateway()
    else:
        raise ValueError(f"Unknown payment gateway: {gateway_name}. Only 'cashfree' is supported.")


def get_payment_gateway_for_payments() -> BasePaymentGateway:
    """
    Get payment gateway for payment processing.
    
    Uses PAYMENT_GATEWAY setting to determine which gateway to use.
    """
    gateway_name = getattr(settings, 'PAYMENT_GATEWAY', 'cashfree')
    return get_payment_gateway(gateway_name)


def get_payment_gateway_for_payouts() -> BasePaymentGateway:
    """
    Get payment gateway for payout processing.
    
    Uses PAYOUT_GATEWAY setting to determine which gateway to use.
    """
    gateway_name = getattr(settings, 'PAYOUT_GATEWAY', 'cashfree')
    return get_payment_gateway(gateway_name)


def get_available_gateways() -> list:
    """Get list of available payment gateways."""
    gateways = []
    
    # Check if Cashfree is available
    try:
        import cashfree_pg
        if getattr(settings, 'CASHFREE_APP_ID', None) and getattr(settings, 'CASHFREE_SECRET_KEY', None):
            gateways.append('cashfree')
    except ImportError:
        pass
    
    return gateways


def is_gateway_available(gateway_name: str) -> bool:
    """Check if a payment gateway is available."""
    gateways = get_available_gateways()
    return gateway_name.lower() in gateways