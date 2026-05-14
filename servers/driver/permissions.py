from rest_framework.permissions import BasePermission

# Re-export the canonical IsAdmin from base/permissions.py so existing
# imports keep working without two divergent admin checks coexisting.
from base.permissions import IsAdmin  # noqa: F401


class IsDriver(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and hasattr(user, 'driver')
        )


__all__ = ['IsAdmin', 'IsDriver']
