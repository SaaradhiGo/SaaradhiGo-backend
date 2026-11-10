from rest_framework.permissions import BasePermission

# Re-export the canonical IsAdmin from base/permissions.py so existing
# imports keep working without two divergent admin checks coexisting.
from base.permissions import IsAdmin  # noqa: F401


class IsDriver(BasePermission):
    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if hasattr(user, 'driver'):
            return True
        # A driver account made before 25 Oct had no Driver row and was refused on every
        # driver endpoint (AUDIT §36). Make the unapproved row its sign-up should have made.
        if getattr(user, 'role', None) == 'driver':
            from servers.auth_user.profiles import ensure_role_profile
            profile = ensure_role_profile(user)
            if profile is not None:
                user.driver = profile
                return True
        return False


__all__ = ['IsAdmin', 'IsDriver']
