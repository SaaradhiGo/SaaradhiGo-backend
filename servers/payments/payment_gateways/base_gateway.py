"""
Base Payment Gateway Interface

Defines the common interface that all payment gateway implementations must follow.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class BasePaymentGateway(ABC):
    """Abstract base class for payment gateway implementations."""
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the name of the payment gateway (e.g., 'razorpay', 'cashfree')."""
        pass
    
    @abstractmethod
    def create_order(self, amount: float, trip_id: int, currency: str = 'INR') -> Optional[Dict[str, Any]]:
        """
        Create a payment order.
        
        Args:
            amount: Amount in currency units (e.g., INR)
            trip_id: Trip ID for reference
            currency: Currency code (default: 'INR')
            
        Returns:
            Order object or None on failure
        """
        pass
    
    @abstractmethod
    def verify_payment_signature(
        self, 
        order_id: str
    ) -> bool:
        """
        Verify payment signature (for frontend callback).
        
        Args:
            order_id: Gateway order ID
            payment_id: Gateway payment ID
            signature: Payment signature
            
        Returns:
            True if signature is valid
        """
        pass
    
    @abstractmethod
    def verify_webhook_signature(self, body: bytes, signature: str, timestamp: Optional[str] = None) -> bool:
        """
        Verify webhook signature.
        
        Args:
            body: Raw request body
            signature: Webhook signature header
            timestamp: Webhook timestamp header (optional)
            
        Returns:
            True if signature is valid
        """
        pass
    
    @abstractmethod
    def create_refund(self, payment_id: str, amount: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Create a refund for a payment.
        
        Args:
            payment_id: Gateway payment ID
            amount: Amount to refund (None = full refund)
            
        Returns:
            Refund object or None on failure
        """
        pass
    
    # @abstractmethod
    # def create_payout(
    #     self,
    #     contact_id: str,
    #     account_number: str,
    #     ifsc_code: str,
    #     amount: float,
    #     purpose: str = "payout",
    #     currency: str = "INR",
    #     reference_id: Optional[str] = None,
    #     name: Optional[str] = None
    # ) -> Optional[Dict[str, Any]]:
    #     """
    #     Create a payout to a bank account.
    #     """
    #     pass
    
    # @abstractmethod
    # def create_upi_payout(
    #     self,
    #     upi_id: str,
    #     amount: float,
    #     purpose: str = "payout",
    #     currency: str = "INR",
    #     reference_id: Optional[str] = None,
    #     name: Optional[str] = None
    # ) -> Optional[Dict[str, Any]]:
    #     """
    #     Create a payout to a UPI ID.
    #     """
    #     pass

    # @abstractmethod
    # def create_beneficiary(
    #     self,
    #     beneficiary_id: str,
    #     name: str,
    #     email: str,
    #     phone: str,
    #     bank_account: Optional[Dict] = None,
    #     upi_id: Optional[str] = None
    # ) -> Optional[Dict[str, Any]]:
    #     """
    #     Create a beneficiary/contact for payouts.
    #     """
    #     pass
    
    @abstractmethod
    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the status of an order.
        
        Args:
            order_id: Gateway order ID
            
        Returns:
            Order status object or None on failure
        """
        pass
    
    # @abstractmethod
    # def get_payout_status(self, payout_id: str) -> Optional[Dict[str, Any]]:
    #     """
    #     Get the status of a payout.
        
    #     Args:
    #         payout_id: Gateway payout ID
            
    #     Returns:
    #         Payout status object or None on failure
    #     """
    #     pass