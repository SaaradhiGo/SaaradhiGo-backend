"""
Base Payment Gateway Interface

Defines the common interface that all payment gateway implementations must follow.
"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class PayoutDispatchUnknown(Exception):
    """The payout request may or may not have reached the provider.

    Raised only for failures that happen *after* the HTTP request was issued: a
    network timeout, a dropped connection, or a 5xx. In those cases the provider may
    have created the transfer and we simply did not learn the outcome.

    This is deliberately NOT the same as a rejection. A validation error, a
    missing token, or a 4xx is a definite "no money moved", and the caller may
    safely refund the driver's wallet. An unknown outcome must not be refunded,
    because the driver may already have been paid -- refunding on top of that is
    a double credit, and there is no provider status call proven to tell us
    which happened.

    Whether the provider honours transferId idempotency on a retry is unverified
    (see the provider sandbox verification requirement), so a retry is not a
    safe substitute for asking a human to check.
    """


class NotConfigured(Exception):
    """A provider was asked to do something it cannot: no adapter, or only a stub.

    Raised by `UnavailableGateway.require()`. The gateway methods themselves answer
    None / False instead (the shape every call site already handles), so selecting an
    unavailable provider degrades to the existing "not configured" behaviour and never
    crashes a request.
    """


class BasePaymentGateway(ABC):
    """Abstract base class for payment gateway implementations."""

    #: False on a stub: nothing can be created, charged, refunded or paid out through it.
    available = True
    
    @abstractmethod
    def get_name(self) -> str:
        """Return the name of the payment gateway (e.g., 'razorpay', 'cashfree')."""
        pass
    
    def supported_currencies(self):
        """Currency codes this gateway can actually settle in.

        Declared, not assumed. Every call site passed `currency='INR'` as a
        literal, which was correct while there was one market and silently
        wrong the moment there were two: a dollar fare would have been sent to
        a rupee gateway as a rupee amount, charging the rider roughly a
        ninetieth of what they owed and recording the payment in the wrong
        currency.

        Defaults to INR-only because that is what the one implemented gateway
        supports. A gateway that supports more overrides this.
        """
        return ('INR',)

    # MONEY CROSSES THIS INTERFACE AS INTEGER MINOR UNITS, with its currency:
    # `amount_minor=13568, currency='INR'` is Rs 135.68. Razorpay and Stripe
    # take minor units and pass the integer straight through. An adapter whose
    # provider wants major units (Cashfree) converts at its own boundary with
    # `base.money.to_gateway_major` / `to_major_string`, and nowhere else.

    @abstractmethod
    def create_order(self, amount_minor: int, trip_id: int, currency: str = 'INR') -> Optional[Dict[str, Any]]:
        """
        Create a payment order.

        Args:
            amount_minor: Amount in integer minor units of `currency` (paise, cents)
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
    def create_refund(self, payment_id: str, amount_minor: Optional[int] = None,
                      currency: str = 'INR') -> Optional[Dict[str, Any]]:
        """
        Create a refund for a payment.

        Args:
            payment_id: Gateway payment ID
            amount_minor: Integer minor units to refund (None = full refund)
            currency: Currency code of `amount_minor`
            
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
    
    @abstractmethod
    def create_upi_payout(
        self,
        upi_id: str,
        amount_minor: int,
        purpose: str = "payout",
        currency: str = "INR",
        reference_id: Optional[str] = None,
        name: Optional[str] = None,
        **extra
    ) -> Optional[Dict[str, Any]]:
        """
        Create a payout to a UPI ID.

        `extra` carries provider-specific reuse hints (`contact_id`, `fund_account_id`,
        `contact_reference`); an adapter that has no use for them ignores them.
        """
        pass

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


class UnavailableGateway(BasePaymentGateway):
    """A provider that cannot act: a country with none configured, or a stub adapter.

    FALSY on purpose (`bool(gateway)` is False), so the existing `if not gateway:` checks at
    every call site answer "payment gateway not configured" (HTTP 503 / awaiting_rail), and a
    site with no check gets None / False back instead of an AttributeError. It never falls back
    to another country's provider: that is the whole point of selecting by country.

    TO IMPLEMENT A REAL ADAPTER (Razorpay, Stripe, Stripe Connect), subclass BasePaymentGateway
    and fill exactly these, in minor units with the currency (see the money note above):
      get_name()                       provider name
      supported_currencies()           currencies it can settle (declared, never assumed)
      create_order(amount_minor, trip_id, currency, customer_id=, customer_phone=,
                   customer_email=)    -> {'gateway','gateway_order_id','order_id',
                                           'payment_session_id' or client secret, 'order_amount'}
      verify_payment_signature(order_id)                  -> bool
      verify_webhook_signature(body, signature, timestamp) -> bool (fail closed, with replay window)
      create_refund(payment_id, amount_minor, currency)    -> dict or None
      get_order_status(order_id)       -> {'order_id','order_status','order_amount',
                                           'order_currency','payment_status'} or None
      create_upi_payout(...)           payouts rail (UPI is India only; US payouts are Stripe
                                       Connect transfers, a different method added alongside)
    Raise PayoutDispatchUnknown for a payout whose outcome is unknown after the request was sent.
    """

    available = False
    provider = 'none'

    def __init__(self, provider='none', reason=''):
        self.provider = provider
        self.reason = reason or f'{provider} is not available'

    def __bool__(self):
        return False

    def require(self):
        raise NotConfigured(self.reason)

    def get_name(self) -> str:
        return self.provider

    def supported_currencies(self):
        return ()

    def create_order(self, amount_minor, trip_id=None, currency='INR', **_):
        logger.warning('create_order refused: %s', self.reason)
        return None

    def verify_payment_signature(self, order_id) -> bool:
        return False

    def verify_webhook_signature(self, body, signature, timestamp=None) -> bool:
        return False

    def create_refund(self, payment_id, amount_minor=None, currency='INR'):
        logger.warning('create_refund refused: %s', self.reason)
        return None

    def create_upi_payout(self, upi_id, amount_minor, purpose='payout', currency='INR',
                          reference_id=None, name=None, **_):
        # None is a definite rejection (no money moved), so the caller may refund safely.
        logger.warning('create_upi_payout refused: %s', self.reason)
        return None

    def get_order_status(self, order_id):
        return None
