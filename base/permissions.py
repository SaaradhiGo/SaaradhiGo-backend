"""Project-level DRF permission classes.

Single source of truth so we don't end up with two divergent `IsAdmin`
implementations (one strict, one lax) that get silently imported from
different apps and produce different access decisions.
"""

from rest_framework.permissions import BasePermission


class IsAdmin(BasePermission):
    """Allow access only to authenticated admin users.

    Three conditions, all required:
    - authenticated (JWT or session)
    - `is_staff=True` (Django-standard signal for back-office access)
    - `role == 'admin'` (application-level intent flag on our CustomUser)

    We deliberately do NOT require `is_superuser`. Superuser bestows full
    admin-site privileges and should be reserved for break-glass; ops admins
    operating the platform are staff but not necessarily superusers.
    """

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and getattr(user, 'role', None) == 'admin'
        )
