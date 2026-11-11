"""Cashfree payouts: the same adapter object as payments (one class speaks both Cashfree
products), exposed here so the payouts capability has its own entry per vendor."""

from integrations.payments.cashfree import CashfreeGateway  # noqa: F401
