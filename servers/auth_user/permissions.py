# Re-export the canonical IsAdmin so existing imports keep working.
# Single source of truth lives in base/permissions.py.
from base.permissions import IsAdmin

__all__ = ['IsAdmin']
