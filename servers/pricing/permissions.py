from rest_framework.permissions import BasePermission


class IsPlatformAdmin(BasePermission):
    """Only authenticated users whose role is 'admin' may mutate
    pricing data. Read access is also admin-only for now (we don't
    expose rate cards to riders/drivers as a list -- they see a
    quote, not the underlying card)."""

    def has_permission(self, request, view):
        user = getattr(request, 'user', None)
        if not user or not user.is_authenticated:
            return False
        role = getattr(user, 'role', None)
        if role and str(role).lower() == 'admin':
            return True
        # Django staff also counts (for ops who log in via admin auth).
        return bool(getattr(user, 'is_staff', False))
