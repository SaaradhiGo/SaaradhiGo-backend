from decimal import Decimal, InvalidOperation
from django import template

register = template.Library()


@register.filter
def percentage_of(value, percent):
    """Return (value * percent) / 100 as a float. Gracefully handles bad input by returning 0."""
    try:
        v = Decimal(value)
        p = Decimal(percent)
        result = (v * p) / Decimal(100)
        return float(result)
    except (InvalidOperation, TypeError, ValueError):
        try:
            return float(value) * float(percent) / 100
        except Exception:
            return 0
