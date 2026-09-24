"""Authorization for pricing data.

`IsPlatformAdmin` was a FIFTH independent answer to "is this an operator", and like
the one on the admin password endpoint it was an "any of" gate: `role == 'admin'` OR
`is_staff`. Either alone was sufficient, so a rider carrying `is_staff`, or a
`role='admin'` account without it, could rewrite the rate card -- the number a rider
is charged.

It now delegates to the single definition, and mutating pricing requires the
`pricing.write` capability rather than merely being an operator. Support and driver
operations have no business changing a fare.

Reads stay operator-wide: an operator investigating a fare dispute needs to see the
card that produced the quote, and refusing that would push them to guess.
"""

from rest_framework.permissions import SAFE_METHODS, BasePermission

from base.permissions import CAP_PRICING_WRITE, has_capability, is_operator


class IsPlatformAdmin(BasePermission):
    """Any operator may read pricing; only `pricing.write` may change it.

    Name kept because it is referenced from several viewsets, and renaming it in the
    same change that fixes it would obscure the fix in the diff.
    """

    def has_permission(self, request, view):
        if not is_operator(request.user):
            return False
        if request.method in SAFE_METHODS:
            return True
        return has_capability(request.user, CAP_PRICING_WRITE)
